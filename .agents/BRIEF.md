# Dev brief — stratechery-archive

Self-contained. You do not need any other document to start. Source-of-truth design
decisions are cited by section where they matter.

**Status: nothing implemented.** The generated stub builds; it does not run. Read §3 before
pushing anything.

## 1. What this project produces

One command that copies the Stratechery message population out of `msgvault.db` into a
directory of **verbatim `.eml` files**, plus a hash index. Nothing else.

```
/archive/                       # rw mount, host: /srv/datasets/stratechery-archive
  raw/<message_id>.eml          # verbatim RFC 5322, decompressed. IMMUTABLE once written
  raw/SHA256SUMS                # manifest of raw/*.eml only, sorted by path, written LAST
  index.jsonl                   # append-only, one line per message, sorted by message_id
  runs/<ts>.json                # append-only log, one file per run
  assets/<sha256[:16]>.<ext>    # captured image bytes. IMMUTABLE once written
```

Three kinds of file, grouped by mutability: **copies** (`raw/`), **captures** (`assets/`),
**provenance** (`index.jsonl`, `runs/`). `SHA256SUMS` covers the immutable set and nothing
else, so a correct mirror always verifies. `index.jsonl` and `runs/` deliberately sit
*outside* `raw/`: a log that grows inside the attested directory invalidates its own
attestation.

**This project does not parse email.** No MIME walk, no HTML parsing, no Markdown, no CSV, no
cleaned HTML, no front-matter, no SQLite, no DVC. The archive is a copy with an index, closer
to a backup than a dataset. Consumers parse the `.eml` themselves. If you find yourself
writing a parser, stop — you are implementing a rejected design.

## 2. The invariants

These are the whole argument for the design. Do not trade them away for convenience.

1. **Nothing is interpreted.** Bytes out are bytes in. A bug here should be a bug in `zlib`
   or the filesystem, both of which announce themselves.
2. **`len(eml) == messages.size_estimate` on every row.** `size_estimate` is a stored column
   holding the *decompressed* MIME length, verified exact on 1,690 of 1,690 messages. It is a
   free end-to-end decompression check. If it mismatches, fail loudly — do not write the file.
3. **`sha256` per file, recorded in the index.** This is what makes the tree re-verifiable
   forever from local files, with no dependency on msgvault.
4. **Atomic writes.** `.tmp` → `fsync` → `rename`. Never a partial `.eml`.
5. **`raw/` and `assets/` are never rewritten.** Written once. Re-running adds, never
   overwrites.
6. **The token is read from the `.eml` at fetch time and never stored anywhere else** — not in
   the index, a filename, a manifest or a log. Every message carries a per-recipient JWT in
   each `<img src>`; the index records `url_path` **without** the query string. That asymmetry
   is deliberate.

## 3. Read this before your first push

### 3.1 The image does not run yet

`src/stratechery_archive/` contains only `__init__.py`. `Dockerfile` ends with
`CMD ["python","-m","stratechery_archive"]` and `USER 1000:1000`, and there is **no
`__main__.py`** — declaring `command` at generation time suppresses the stub, by design, so
regeneration can never clobber the real entry point.

The Buildkite step `docker_batch_smoke` runs:

```
docker run --rm <image> python -m stratechery_archive --dry-run
```

and requires **exit 0**. So the first push is red until `__main__.py` exists and implements
`--dry-run`. This is expected, not a bug in the pipeline. Do not edit `.buildkite/pipeline.yml`.

### 3.2 `--dry-run` is a contract, and it is your mount check

`--dry-run` must: print what it would do (population count, target paths, mount presence),
touch nothing, exit 0.

It is also the **only** place CI can verify the mount contract, so use it that way:

- assert `/db/msgvault.db` exists and opens read-only;
- assert `/archive` is writable;
- exit non-zero **naming the missing path** if either fails.

That converts "the image builds" into "the job can see its inputs", which is the failure mode
that otherwise breaks silently on the host.

### 3.3 Mounts, uid, and the live WAL

Generated compose (verified):

```yaml
user: "1000:1000"
volumes:
  - /srv/appdata/msgvault:/db:ro
  - /srv/datasets/stratechery-archive:/archive
restart: "no"
```

- **The uid is load-bearing.** `/srv/appdata/msgvault` is mode `0700` uid 1000. Running as any
  other non-root uid cannot traverse into it even though `msgvault.db` itself is `0644`. It
  must stay `1000:1000`; do not "simplify" it to root, and do not add a privilege-drop
  entrypoint.
- **The DB is live.** `msgvault.db` is ~8.1 GB with an active WAL. Open with `mode=ro`
  (URI: `file:/db/msgvault.db?mode=ro`). Reads are safe because invariant 2 makes a torn read
  announce itself as a `size_estimate` mismatch rather than as silent corruption. Do not
  snapshot, do not copy the DB.

### 3.4 The population

Fixed predicate, not a cursor. There is **no `from_domain` column** — it is derived from the
`v_messages` sender CTE (recipient → `participants.domain`, falling back to
`messages.sender_id`; the fallback fired 0 times). Record the verbatim SQL and its sha256 in
each run file.

Expected: **1,690 messages, 248,421,760 B decompressed, 2016-12-21 → 2026-11-03.**

That is a snapshot, not a constant — the corpus grows. Measured live on
2026-11-04 by the first real run: **1,691 messages, 248,557,999 B**, newest
`sent_at` 2026-11-04T11:01:13Z. A run that reports more than the number above
is newer mail arriving, not a bug; `in_db` vs `written` is the check that
matters, not equality with this paragraph.

## 4. Tasks, in order

Do not reorder. Each one is verifiable before the next.

### Task 1 — `__main__.py` with `--dry-run`

First, with the mount assertions from §3.2. Nothing else. This unblocks the smoke gate and
gives you the mount check for free. Land it alone so the first green build is unambiguous.

### Task 2 — the msgvault client

**Open decision, resolve it before writing this.** `msgvault-client` does **not** exist as a
package. What exists is a vendored class `MsgvaultClient` (plus `recover_webview_html`) at
`src/mailroom/clients.py` in **`game-catalog`**, tested at `tests/test_msgvault_client.py`. It
talks REST (`/api/v1/messages/filter`, `/messages/{id}`, `/stats`) — REST is the right choice
and is not to be re-litigated — and reads `MSGVAULT_URL` / `MSGVAULT_TOKEN` from env.

Options: vendor the one file plus its test (honest fork, two copies); or extract a real
published package (a third repo — out of scope here). **Recommendation: vendor, with the test,
and record the fork in the module docstring** naming the source repo and commit. Do not
attempt to fold this into game-catalog.

### Task 3 — the mirror loop

```
for each message_id in population:
    blob = SELECT raw_data FROM message_raw WHERE message_id = ?
    eml  = zlib.decompress(blob)
    assert len(eml) == messages.size_estimate
    write raw/<message_id>.eml            # atomic
    append index line {…, bytes: len(eml), sha256: sha256(eml)}
```

`index.jsonl` line shape:

```json
{"message_id": 125130, "rfc822_message_id": "<…@mail.stratechery.com>",
 "sent_at": "2026-11-02T11:00:57Z", "from": "email@stratechery.com",
 "subject": "…", "bytes": 131354, "sha256": "056c5d75…ec83",
 "images": [{"url_path": "/wp-content/uploads/2025/02/x.png",
             "asset": "assets/ab12c3d4e5f60718.png", "sha256": "…", "bytes": 91234}]}
```

`from`, `sent_at`, `subject` are **copied columns from the DB**, not parsed out of MIME
headers — the index must not be able to disagree with the database about a date. `url_path` is
recorded **exactly as it appears before the `?`**, unnormalised (no lowercasing, no `%20`
decoding, no trailing-slash trim). Normalising creates a second interpretation point and
silently breaks the join for a consumer who strips only the query. **An asset with no index
entry is unreachable bytes** — `images[]` is a contract, not a convenience.

### Task 4 — the skip rule and the manifest

Two things a naive implementation gets wrong on the second run:

1. **Skip unchanged.** If `raw/<message_id>.eml` exists and its sha256 matches the index line,
   count it as `skipped_unchanged` and move on. Re-hashing 237 MiB is cheap; rewriting is not
   the point. Without this, `skipped_unchanged` is a decorative field and the idempotence check
   (two run files compared) is meaningless.
2. **Rebuild `SHA256SUMS` last, and treat a failure here as a failed run.** Adding N messages
   changes the manifest. If a run dies after writing `.eml` files but before the manifest,
   `raw/` holds files outside the manifest and the tree fails verification with nothing
   recording why. Log the manifest's own sha256 in the run file.

`runs/<ts>.json`, one file per run, ISO-8601 UTC filename:

```json
{"run_at": "…", "extractor_sha": "…",
 "population": {"query_sha": "…", "query": "…verbatim SQL…"},
 "totals": {"in_db": 1690, "written": 1690, "skipped_unchanged": 0, "failed": 0},
 "bytes": {"raw_total": 248421760, "assets_new": 0},
 "hashes": {"index_jsonl_sha256": "…", "sha256sums_sha256": "…"},
 "assets": {"referenced": 3013, "fetched": 0, "failed": 0},
 "failures": []}
```

`failures` is the useful field — `written: 1687, failed: 3` with reasons is a different object
from `written: 1687` alone, and the second cannot be debugged. `in_db` vs `written` is the
completeness check.

Nothing deletes. If a message disappears from msgvault, `raw/` keeps it. That is correct for
an archive and is recorded so nobody adds a sync-delete later.

### Task 5 — images, and only after the fetch test

**Do not write asset code until this test is run.** The capture plan authenticates with the
token embedded in each message, and those JWTs expire — a 2017 message may carry a 2017, dead
token. Four `curl`s decide whether `assets/` is a full capture, a recent-months capture, or
mostly empty: one 2026 and one 2017 message, each fetched with its own token and again with
the query string stripped. Record the result **before** implementing.

Then: assets never block the mirror. A fetch failure leaves the `.eml` written and the
`images` entry present **without** an `asset` key — a truthful record of "known URL, expired
authorisation". `--no-assets` therefore means *lossy*, not *deferred*; it is not a cache to
fill later. Run asset fetching after the mirror is written and keep it resumable.

Context that matters and is easy to get backwards: **the expiry problem is a backfill problem
only.** Mail arriving now has a live token at capture time. The fetch test decides the
historical backfill, not the steady state. Never conclude "assets are lossy, drop them" from a
finding that is really about 2016 mail.

## 5. Hazards

- **Never commit a real `.eml`, ever.** Every message carries a per-recipient bearer JWT, and
  `secret-scan` is gitleaks over full history. A real fixture will be flagged — correctly —
  and the build goes red for a reason that looks like a bug and is the design working. Fixtures
  are **synthesised**: a hand-written RFC 5322 message with a fake token, one quoted-printable
  soft break, one base64 part, one `<img src>` with a dummy query string. That exercises every
  branch and puts no credential in history. `raw/` is a host directory and never enters the
  repo.
- **`/db` is read-only and must stay read-only.** It is the one mount that is `:ro`; `/archive`
  is the one that is not. That asymmetry is the job's whole security posture.
- **Do not add a schedule.** This is a batch image that runs and exits; compose emits
  `restart: "no"` and neither Watchtower nor Homepage labels, on purpose. If a schedule is ever
  wanted it goes in `dagu-dags` as `docker run --rm`, never into this repo and never into
  Dagster.
- **Dependencies: keep them zero if you can.** `zlib`, `sqlite3`, `hashlib`, `argparse` are
  stdlib; `sqlite3` CLI is in the image via `aptPackages`. An HTTP client for assets is the
  only likely addition — `httpx` if needed, declared in `pyproject.toml`.
- **Do not regenerate the project** to change behaviour. Everything above is source code.

## 6. Definition of done

- [ ] `--dry-run` exits 0 and asserts both mounts, naming the failing path on error.
- [ ] `python -m build`, `ruff check src tests`, `pytest -q` green; `docker_batch_smoke` green.
- [x] A live run writes 1,690 `.eml` files totalling **248,421,760 B**.
      Done 2026-11-04: 1,691 files / 248,557,999 B (the delta is new mail).
- [ ] `raw/SHA256SUMS` verifies: `cd /archive/raw && sha256sum -c SHA256SUMS`.
- [ ] Every `.eml` sha256 matches its `index.jsonl` line.
- [ ] Every `index.jsonl` `sha256` matches the file; every `images[].url_path` has no `?`.
- [ ] A **second** run writes 0 files, reports `skipped_unchanged: 1690`, and leaves
      `SHA256SUMS` byte-identical.
- [ ] `runs/<ts>.json` records the verbatim SQL, `extractor_sha`, and counts.
