"""Capture the image bytes a message references.

Assets never block the mirror (BRIEF §4 Task 5). A fetch failure leaves the
.eml written and the `images[]` entry present **without** an `asset` key -
a truthful record of "known URL, no captured bytes". That is why failures here
are counted separately from mirror failures and do not change the exit code:
one dead CDN must not turn a good mirror into a red run.

The gate was run before this module existed (see
.agents/findings/asset-fetch-test.md): content images are still fetchable and
the per-recipient token never guarded them, so assets are a full capture and
no token is sent. The token is not read, not passed, and not stored.

Assets are content-addressed, ``assets/<sha256[:16]>.<ext>``, so the same bytes
referenced by two URLs are one file. The extension comes from the response's
Content-Type, falling back to magic bytes - never from the URL, because URLs
lie about their type and a wrong extension is a wrong file.

Resumable by construction: an entry that already has an ``asset`` whose file
exists is skipped, so re-running finishes the gaps without re-downloading.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from stratechery_archive.storage import write_atomic

USER_AGENT = "Mozilla/5.0 (compatible; stratechery-archive/0.1)"
TIMEOUT = 30.0
MAX_BYTES = 25 * 1024 * 1024

CONTENT_TYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}

MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"\x00\x00\x01\x00", ".ico"),
)


class NotAnImage(Exception):
    """The response was a 200 but not image bytes (an error page, a firewall)."""


@dataclass
class AssetTotals:
    referenced: int = 0
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_new: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "referenced": self.referenced,
            "fetched": self.fetched,
            "skipped": self.skipped,
            "failed": self.failed,
            "failures": self.failures,
        }


def detect_extension(content_type: str | None, data: bytes) -> str:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type in CONTENT_TYPE_EXT:
        return CONTENT_TYPE_EXT[media_type]
    for magic, extension in MAGIC:
        if data.startswith(magic):
            return extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def looks_like_image(content_type: str | None, data: bytes) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type.startswith("image/"):
        return True
    return any(data.startswith(magic) for magic, _ in MAGIC) or (
        data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    )


def default_client() -> httpx.Client:
    """The production HTTP client. A seam for tests, not a configuration knob."""
    return httpx.Client(
        follow_redirects=True, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
    )


def _fetch(client: httpx.Client, url: str) -> tuple[bytes, str | None]:
    response = client.get(url)
    response.raise_for_status()
    if len(response.content) > MAX_BYTES:
        raise NotAnImage(f"response is {len(response.content)} B, over the {MAX_BYTES} B cap")
    content_type = response.headers.get("content-type")
    if not looks_like_image(content_type, response.content):
        raise NotAnImage(
            f"not an image: content-type={content_type!r}, "
            f"starts with {response.content[:16]!r}"
        )
    return response.content, content_type


def fetch_all(
    entries: dict[int, dict[str, Any]],
    archive_dir: Path,
    *,
    client: httpx.Client | None = None,
    disabled: bool = False,
) -> AssetTotals:
    """Fill in ``asset``/``sha256``/``bytes`` on every images[] entry.

    Mutates ``entries`` in place. Only assets written by this run are counted in
    ``fetched``; already-captured ones are ``skipped``, which is what makes a
    re-run cheap.
    """
    totals = AssetTotals()
    assets_dir = archive_dir / "assets"
    if not disabled:
        assets_dir.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    client = client or default_client()
    try:
        for message_id in sorted(entries):
            for image in entries[message_id].get("images") or []:
                totals.referenced += 1
                if disabled:
                    continue

                existing = image.get("asset")
                if existing and (archive_dir / existing).exists():
                    totals.skipped += 1
                    continue

                url = image["url_path"]
                try:
                    data, content_type = _fetch(client, url)
                except (httpx.HTTPError, NotAnImage, OSError) as exc:
                    # No `asset` key: the entry keeps saying "this message
                    # referenced this URL and we hold no bytes for it".
                    totals.failed += 1
                    totals.failures.append(
                        {"message_id": message_id, "url_path": url, "reason": str(exc)}
                    )
                    continue

                digest = hashlib.sha256(data).hexdigest()
                name = f"{digest[:16]}{detect_extension(content_type, data)}"
                relative = f"assets/{name}"
                try:
                    if not (archive_dir / relative).exists():
                        # Immutable once written: an existing file with this
                        # name already holds these bytes (the name is their sha).
                        write_atomic(archive_dir / relative, data)
                        totals.bytes_new += len(data)
                except OSError as exc:
                    totals.failed += 1
                    totals.failures.append(
                        {
                            "message_id": message_id,
                            "url_path": url,
                            "reason": f"could not write {relative}: {exc}",
                        }
                    )
                    continue
                totals.fetched += 1
                image["asset"] = relative
                image["sha256"] = digest
                image["bytes"] = len(data)
    finally:
        if owns_client:
            client.close()

    return totals
