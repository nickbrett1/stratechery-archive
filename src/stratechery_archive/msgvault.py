"""msgvault REST client - vendored from game-catalog.

FORK PROVENANCE (BRIEF §4 Task 2)
---------------------------------
Source repo:  nickbrett1/game-catalog (private)
Source path:  src/mailroom/clients.py
Source commit: d2d1aab7cba2e64f4683b0757a99fe4758b7804 (2026-10-05)
Source blob:  14395cfb5c46bc449f7ed397a2fbb867ff7f4fb4
Vendored:     2026-11-04

This is an honest fork, not an import. The alternative was extracting a
published package into a third repo, which is out of scope here. When the
upstream client changes, this copy does not follow it: re-vendor deliberately
and update the hashes above.

WHAT WAS DROPPED, AND WHY
-------------------------
Only ``MsgvaultClient`` and ``recover_webview_html`` are msgvault. The same
upstream module also carries ``IgdbClient``, ``PsnApiClient`` and Dagster
``@resource`` wrappers; none of them belong in this image, and the Dagster
import alone would drag dagster into a batch job that should stay at zero
dependencies beyond an HTTP client. The vendored ``MsgvaultClient`` is
otherwise unmodified - same retry rule, same paging, same date-watermark
cursor, same methods - so the upstream tests still apply.

HOW THIS PROJECT USES IT (read this before assuming the mirror uses REST)
-----------------------------------------------------------------------
The mirror does NOT copy bytes over this client. ``raw/<message_id>.eml`` is
the decompressed BLOB from ``message_raw`` read straight out of msgvault's
SQLite database (BRIEF §4 Task 3), because the REST API returns *parsed*
``body`` / ``body_html`` and this archive stores verbatim bytes. Anything that
parsed the mail would be the rejected design (BRIEF §1).

What the client is available for here: typed access to archive metadata that
the DB read path should not reimplement - ``/api/v1/stats`` for a population
cross-check, and ``/messages/filter`` for a per-sender sweep when diagnosing a
count mismatch. Nothing in the mirror loop depends on it yet. ``MSGVAULT_URL`` / ``MSGVAULT_TOKEN`` come
from the environment, never from a file or a flag.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx


class MsgvaultClient:
    """Client over the real msgvault REST API (verified upstream 2026-08-16).

    msgvault serves its archive over REST at `<base_url>`. The MCP server is
    NOT used: its daemon-client adapter drops body_html, while the REST API
    returns body + body_html correctly.

    API (see GET /openapi.json):
      - GET /api/v1/messages/filter?sender=..&offset=..&limit=500&sort=date&direction=desc
          -> {count, has_more, offset, limit, messages: [summaries]}
      - GET /api/v1/messages/{id} -> full message with `body` + `body_html`
      - GET /api/v1/stats -> archive totals
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
        retries: int = 3,
    ):
        # msgvault is flaky under load (observed upstream 2026-08-16) - retry
        # transient 5xx / transport errors with backoff.
        self.retries = retries
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout
        )

    # --- transport ---

    def _get(self, path: str, **params: Any) -> Any:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self._client.get(path, params=params or None)
                if resp.status_code in (429, *range(500, 600)) and attempt < self.retries:
                    time.sleep(2.0 * attempt)  # rate limit / transient - back off
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as exc:
                status = (
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None
                )
                if attempt == self.retries or (
                    status is not None and status < 500 and status != 429
                ):
                    raise
                last = exc
                time.sleep(2.0 * attempt)
        raise RuntimeError(f"msgvault request failed: {last}")

    # --- public API ---

    def search_messages(
        self,
        sender: str | None = None,
        subject: str | None = None,
        after: str | None = None,  # date bound (RFC3339 / YYYY-MM-DD)
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Newest-first messages from `sender` with an optional client-side
        subject filter, paging until `limit` matches or the archive is exhausted.

        `after` is a DATE/TIME watermark (RFC3339 or YYYY-MM-DD) forwarded to
        msgvault's native `after` filter, so only messages SENT at/after it are
        returned. This is the correct incremental cursor: msgvault's numeric
        message id is NOT monotonic with send date - a receipt archived later
        can carry a low id but a recent date, so an id-based cursor silently
        skipped such messages. A date watermark catches a message by when it
        was sent regardless of its id.
        """
        out: list[dict[str, Any]] = []
        page_offset = offset
        while True:
            params: dict[str, Any] = {
                "sort": "date",
                "direction": "desc",
                "offset": page_offset,
                "limit": 500,
            }
            if sender:
                params["sender"] = sender
            if after:
                params["after"] = str(after)
            page = self._get("/api/v1/messages/filter", **params)
            msgs = page.get("messages") or []
            for m in msgs:
                if subject and subject.lower() not in (m.get("subject") or "").lower():
                    continue
                out.append(m)
                if len(out) >= limit:
                    return out
            if not page.get("has_more") or not msgs:
                return out
            page_offset += len(msgs)

    def get_message(self, message_id: int) -> dict[str, Any]:
        """Fetch one message with its full body + body_html (single GET)."""
        d = self._get(f"/api/v1/messages/{message_id}")
        return {
            "id": d.get("id"),
            "subject": d.get("subject"),
            "from_email": d.get("from_email"),
            "sent_at": d.get("sent_at"),
            "has_attachments": d.get("has_attachments"),
            "body_text": d.get("body") or "",
            "body_html": d.get("body_html") or "",
        }

    def get_stats(self) -> dict[str, Any]:
        return self._get("/api/v1/stats")

    # --- internals ---

    def fetch_webview(self, url: str) -> str:
        """Fetch a 'View as a Web page' URL with a browser UA.

        click.emailinfo2.bestbuy.com 302-redirects to the real content on
        view.emailinfo2.bestbuy.com, so redirects are followed.
        """
        resp = self._client.get(
            url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True
        )
        resp.raise_for_status()
        return resp.text


_WEBVIEW_CLICK_RE = re.compile(
    r"https?://click\.emailinfo2?\.bestbuy\.com/[^\s\r\n]+"
)
_WEBVIEW_VIEW_RE = re.compile(
    r"https?://view\.emailinfo2?\.bestbuy\.com/[^\s\r\n]+"
)


def recover_webview_html(
    body_text: str, client: MsgvaultClient | None = None
) -> str | None:
    """Recover real content from a 'View as a Web page' stub body.

    Some senders archive only a stub (a 'View as a Web page' plus a click
    link). Fetch the web-view URL to recover the real HTML. Returns the fetched
    HTML or None when there is no recoverable URL.

    Not used by the mirror - the archive stores bytes, it does not recover
    parsed bodies - but vendored with the client so the fork stays whole.
    """
    # Prefer 'click.' links (they 302 to the real content); bare 'view.' links
    # require a token and return a maintenance page.
    m = _WEBVIEW_CLICK_RE.search(body_text) or _WEBVIEW_VIEW_RE.search(body_text)
    if not m:
        return None
    client = client or MsgvaultClient("http://msgvault:8080")
    try:
        return client.fetch_webview(m.group(0))
    except httpx.HTTPError:
        return None
