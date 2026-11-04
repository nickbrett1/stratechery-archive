# Asset fetch test — result (BRIEF §4 Task 5 gate)

Run 2026-11-04 on `ryzen` (192.168.1.69) by `ryzen-goose`, read-only:
`sqlite3.connect("file:/srv/appdata/msgvault/msgvault.db?mode=ro", uri=True)`
and four HTTP requests with `curl`. No token value was recorded; every query
string below is `?<redacted>`. Population at time of test: **1,691** messages
(all with ≥1 `<img src="http…">`; 1,689 with a query string on at least one).

## What was asked

Whether the per-recipient JWT in `<img src>` is load-bearing, because asset
capture was gated on it: the plan authenticated with each message's own token,
and a 2016 message could carry a 2016 token that had died. Four `curl`s were to
decide whether `assets/` is a full capture, a recent-months capture, or mostly
empty.

## Results

| case | msg id | era | status | content-type | bytes | first 8 bytes | verdict |
|---|---|---|---|---|---|---|---|
| a. newest content img, full URL + token | 125200 | 2026 | 200 | image/png | 141,689 | `89504e470d0a1a0a` | real PNG |
| b. same URL, query removed | 125200 | 2026 | 200 | image/png | 142,281 | `89504e470d0a1a0a` | real PNG |
| c. oldest tokenised img, full URL | 97413 | 2016 | 200 (1 redirect) | image/gif | 43 | `4749463839610100` | 1×1 GIF — **open-tracking pixel** |
| d. same URL, query removed | 97413 | 2016 | 200 (1 redirect) | image/gif | 43 | `4749463839610100` | identical 43-byte GIF |

Follow-up probe (because the first pass landed on a tracker, not content):

| probe | result |
|---|---|
| 4 content PNGs in msg 97413 (`stratechery.com/wp-content/uploads/2016/…`) | 4/4 → 200, image/png, real bytes (159,520–561,273 B) |
| first content image in 5 messages across 2017 | 3/3 that had one → 200, real PNG/JPEG; 2 messages had no content-host image (MailChimp gallery only) |

DB-only attribution across all 1,691 messages:

| era | msgs | query on a **content** image | query on **tracker only** | any query | content img present |
|---|---|---|---|---|---|
| before 2018-01-01 | 52 | 0 | 52 | 52 | 25 |
| 2018-01-01+ | 1,639 | 394 | 562 | 956 | 1,317 |

Hosts carrying a query on a content image: `i0.wp.com` (903), `i1.wp.com` (35),
`i2.wp.com` (27). Nothing else. Plain `stratechery.com/wp-content/uploads/…`
never carries a query, in either era.

## Verdict

**`assets/` is a full capture, not a recent-months one.** 2016/2017 content
images are still fetchable today (7/7 tested, HTTP 200, real PNG/JPEG bytes),
and the token was never involved: it guarded only the open-tracking pixel
before 2018, and only the `iN.wp.com` Jetpack proxy after — a proxy that serves
identical bytes with or without the query.

So the expiry problem the brief worried about does not exist for content
images. What exists is the opposite: the token is noise, and the brief's rule
that `url_path` is recorded before the `?` is what keeps it out of the index.

## Consequences for Task 5

1. **No token is needed to fetch assets.** Fetch every `url_path` as recorded
   (query already stripped). Do not carry the token forward, do not retry with
   it, and do not persist it.
2. **`--no-assets` is therefore a lossy mode in principle but not in practice**
   for this corpus — but it stays opt-in and resumable, as briefed.
3. **Do not normalise `iN.wp.com` to `stratechery.com/wp-content/uploads/…`,
   even though the origin path serves the same image.** This was suggested by
   the probe and it is the rejected design here: `url_path` is recorded exactly
   as it appears (BRIEF §4 Task 3), because a second interpretation of the same
   URL silently breaks the join for a consumer who strips only the query. Asset
   identity is the content sha256, which collapses the duplicate bytes anyway.
4. **Tracker pixels are content too.** A 43-byte GIF is the bytes the message
   referenced; capture it. It is not worth special-casing, and `images[]` is a
   contract, not a curated list.
5. **One class remains untested:** `gallery.mailchimp.com` images (22 URLs over
   2 messages, Aug/Oct 2017). They are a CDN, not a tracker, and carry no query
   string. Treat as a normal fetch; if any fail, they land in `failures` with a
   reason, which is the designed behaviour. Do not pre-emptively skip them.

## Schema note (side finding, fixed)

`participants` has no `email` and no `address` column. The sender column is
**`email_address`**. `population.py` resolves the column by probing
`email_address` → `email` → `address` and names the schema in the error if none
matches; a regression test covers the real name.
