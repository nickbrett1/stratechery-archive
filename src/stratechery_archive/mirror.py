"""The mirror loop: msgvault.db -> a directory of verbatim .eml files.

Tasks 3 and 4 of the dev brief, in one pass because the second is the first
run's correctness: a loop that cannot skip unchanged work and cannot rebuild
its manifest is not a mirror, it is a one-shot dump.

Nothing is interpreted. The bytes written are ``zlib.decompress(raw_data)`` and
nothing else happens to them. The only check is ``len(eml) == size_estimate``,
which is exact on 1,690/1,690 rows and turns a torn WAL read into a loud
failure instead of silent corruption.

Three kinds of file, grouped by mutability (BRIEF §1):

  raw/<message_id>.eml      copies - written once, never rewritten
  raw/SHA256SUMS            manifest over raw/ only, written LAST
  index.jsonl               provenance - one line per message
  runs/<ts>.json            provenance - one file per run

Never a partial .eml: every write is .tmp -> fsync -> rename -> fsync(dir).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stratechery_archive import assets, images, population
from stratechery_archive.storage import sha256_bytes, sha256_file, write_atomic

CHUNK = 1024 * 1024


class MirrorError(RuntimeError):
    """A precondition of the mirror is broken; the run cannot start or continue."""


@dataclass
class Failure:
    message_id: int | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"message_id": self.message_id, "reason": self.reason}


@dataclass
class Counters:
    in_db: int = 0
    written: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    failures: list[Failure] = field(default_factory=list)

    def fail(self, message_id: int | None, reason: str) -> None:
        self.failed += 1
        self.failures.append(Failure(message_id, reason))


# --- bytes and files -------------------------------------------------------


# --- index -----------------------------------------------------------------


def load_index(path: Path) -> dict[int, dict[str, Any]]:
    """Existing index lines, keyed by message_id. Absent file is an empty index."""
    entries: dict[int, dict[str, Any]] = {}
    if not path.exists():
        return entries
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MirrorError(f"{path}:{number}: index line is not JSON: {exc}") from exc
            entries[int(entry["message_id"])] = entry
    return entries


def dump_index(entries: dict[int, dict[str, Any]]) -> bytes:
    lines = [
        json.dumps(entries[message_id], separators=(",", ":"), ensure_ascii=False)
        for message_id in sorted(entries)
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def dump_manifest(entries: dict[int, dict[str, Any]]) -> bytes:
    """`sha256sum -c` input, paths relative to raw/, sorted by path."""
    lines = [
        f"{entries[message_id]['sha256']}  {message_id}.eml"
        for message_id in sorted(entries, key=lambda mid: f"{mid}.eml")
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


# --- metadata --------------------------------------------------------------


def normalise_sent_at(value: Any) -> str | None:
    """DB timestamp -> RFC 3339 UTC with a Z, without changing the instant.

    The index must not be able to disagree with the database about a date, so
    this only re-renders the stored value. An unparseable value is passed
    through verbatim rather than dropped.
    """
    if value is None:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def extractor_sha() -> str:
    """sha256 over this package's sources, so a run names the code that made it."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run_filename(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_run_file(runs_dir: Path, now: datetime, payload: dict[str, Any]) -> Path:
    stem = _run_filename(now)
    path = runs_dir / f"{stem}.json"
    suffix = 1
    while path.exists():
        path = runs_dir / f"{stem}-{suffix}.json"
        suffix += 1
    write_atomic(path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    return path


# --- the loop --------------------------------------------------------------


def _decode(row: sqlite3.Row, blob: bytes) -> bytes:
    """Decompress one stored blob, or raise MirrorError naming the obstacle."""
    compression = row["compression"]
    if compression != "zlib":
        raise MirrorError(f"compression={compression!r}, expected 'zlib'")
    if row["encryption_version"]:
        raise MirrorError(
            f"encryption_version={row['encryption_version']}, expected 0"
        )
    try:
        return zlib.decompress(blob)
    except zlib.error as exc:
        raise MirrorError(f"zlib could not decompress: {exc}") from exc


def run(
    db_path: Path,
    archive_dir: Path,
    *,
    no_assets: bool = False,
    asset_client: Any = None,
) -> int:
    """Mirror the population. Returns 0 when every message was handled.

    ``no_assets`` means *lossy*, not deferred: the run records the URLs and
    captures no bytes (BRIEF §4 Task 5). ``asset_client`` exists so tests can
    inject a transport; production uses a plain httpx client.
    """
    raw_dir = archive_dir / "raw"
    runs_dir = archive_dir / "runs"
    raw_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    index_path = archive_dir / "index.jsonl"

    started = datetime.now(UTC)
    counters = Counters()
    entries = load_index(index_path)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sql, rows = population.iter_population(conn)
        counters.in_db = len(rows)
        print(f"population: {counters.in_db} messages (query sha {population.query_sha256(sql)})")

        for row in rows:
            message_id = int(row["message_id"])
            target = raw_dir / f"{message_id}.eml"
            existing = entries.get(message_id)

            try:
                blob_row = conn.execute(
                    "SELECT raw_data FROM message_raw WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if blob_row is None or blob_row[0] is None:
                    raise MirrorError("no message_raw row")

                # Skip unchanged before doing any work: re-hashing is cheap and
                # rewriting is not (BRIEF §4 Task 4).
                if target.exists() and existing is not None:
                    if sha256_file(target) == existing.get("sha256"):
                        counters.skipped_unchanged += 1
                        continue
                    # raw/ is immutable. A file that differs from the index is
                    # a conflict to report, never something to overwrite.
                    raise MirrorError(
                        "raw file exists but does not match its index line; "
                        "raw/ is immutable"
                    )
                if target.exists() and existing is None:
                    raise MirrorError(
                        "raw file exists with no index line; refusing to adopt or "
                        "overwrite an unattested file"
                    )

                eml = _decode(row, blob_row[0])
                expected = row["size_estimate"]
                if expected is None:
                    raise MirrorError("size_estimate is NULL; cannot verify decompression")
                if len(eml) != expected:
                    # Invariant 2, and the reason a torn read is safe: it
                    # announces itself here instead of being written.
                    raise MirrorError(
                        f"decompressed {len(eml)} B but size_estimate is {expected} B"
                    )

                write_atomic(target, eml)
                entries[message_id] = {
                    "message_id": message_id,
                    "rfc822_message_id": row["rfc822_message_id"],
                    "sent_at": normalise_sent_at(row["sent_at"]),
                    # Copied from the DB, not parsed out of MIME headers, so the
                    # index cannot disagree with msgvault about a date or sender.
                    "from": row["from_email"],
                    "subject": row["subject"],
                    "bytes": len(eml),
                    "sha256": sha256_bytes(eml),
                    "images": [{"url_path": url} for url in images.image_urls(row["body_html"])],
                }
                counters.written += 1
            except MirrorError as exc:
                counters.fail(message_id, str(exc))
            except OSError as exc:
                counters.fail(message_id, f"filesystem error: {exc}")
    finally:
        conn.close()

    # A file in raw/ that no index line attests is unreachable bytes and a
    # manifest hole. Report it: this is the partial-run failure mode Task 4
    # exists to make visible.
    for path in sorted(raw_dir.glob("*.eml")):
        message_id = int(path.stem) if path.stem.isdigit() else None
        if message_id is not None and message_id in entries:
            continue
        if any(failure.message_id == message_id for failure in counters.failures):
            continue
        counters.fail(
            message_id,
            f"raw/{path.name} exists with no index line; refusing to adopt or "
            "overwrite an unattested file",
        )

    # Assets are fetched after the mirror is written, so a dead CDN can never
    # cost us an .eml. Failures here are counted in the assets block and do not
    # change the exit code.
    asset_totals = assets.fetch_all(
        entries, archive_dir, client=asset_client, disabled=no_assets
    )

    # --- index, then manifest, then the run record ---

    index_bytes = dump_index(entries)
    manifest_bytes = dump_manifest(entries)
    manifest_error: str | None = None

    if not index_path.exists() or index_path.read_bytes() != index_bytes:
        write_atomic(index_path, index_bytes)

    # The manifest covers raw/ and nothing else, so a correct mirror always
    # verifies. It is written LAST: a run that dies before this point leaves
    # files outside the manifest, which is visible, rather than a manifest that
    # claims files which were never written.
    try:
        manifest_path = raw_dir / "SHA256SUMS"
        if not manifest_path.exists() or manifest_path.read_bytes() != manifest_bytes:
            write_atomic(manifest_path, manifest_bytes)
    except OSError as exc:
        manifest_error = f"could not write raw/SHA256SUMS: {exc}"
        counters.fail(None, manifest_error)

    finished = datetime.now(UTC)
    payload = {
        "run_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "extractor_sha": extractor_sha(),
        "population": {"query_sha": population.query_sha256(sql), "query": sql},
        "totals": {
            "in_db": counters.in_db,
            "written": counters.written,
            "skipped_unchanged": counters.skipped_unchanged,
            "failed": counters.failed,
        },
        "bytes": {
            "raw_total": sum(int(entry["bytes"]) for entry in entries.values()),
            "assets_new": asset_totals.bytes_new,
        },
        "hashes": {
            "index_jsonl_sha256": sha256_bytes(index_bytes),
            "sha256sums_sha256": sha256_bytes(manifest_bytes),
        },
        # A missing `asset` key on an images[] entry means "known URL, no
        # captured bytes" - the truthful record of a failed fetch.
        "assets": {**asset_totals.as_dict(), "disabled": no_assets},
        "failures": [failure.as_dict() for failure in counters.failures],
    }
    run_path = _write_run_file(runs_dir, finished, payload)

    print(
        f"in_db={counters.in_db} written={counters.written} "
        f"skipped_unchanged={counters.skipped_unchanged} failed={counters.failed}"
    )
    print(f"raw_total={payload['bytes']['raw_total']} B")
    print(
        f"assets: referenced={asset_totals.referenced} fetched={asset_totals.fetched} "
        f"skipped={asset_totals.skipped} failed={asset_totals.failed} "
        f"({asset_totals.bytes_new} B new)"
    )
    print(f"run record: {run_path}")
    for failure in counters.failures[:10]:
        print(f"  failed {failure.message_id}: {failure.reason}", file=sys.stderr)

    if manifest_error:
        print(f"error: {manifest_error}", file=sys.stderr)
        return 1
    return 1 if counters.failed else 0
