"""Asset capture: content-addressed files, resumable, never blocking the mirror."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from conftest import build_vault
from stratechery_archive import assets, mirror

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PIXEL = b"GIF89a" + b"\x01" * 20

URL_PIXEL = "https://track.stratechery.com/pixel.png"
URL_LOGO = "https://cdn.stratechery.com/logo.png"


def serving(routes: dict[str, tuple[int, dict[str, str], bytes]], calls: list | None = None):
    """A MockTransport client that answers by exact URL path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        status, headers, body = routes.get(
            str(request.url).split("?")[0], (404, {}, b"not found")
        )
        return httpx.Response(status, headers=headers, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def vault(tmp_path) -> Path:
    path = tmp_path / "msgvault.db"
    conn = build_vault(path, [1])
    conn.close()
    return path


@pytest.fixture
def archive(tmp_path) -> Path:
    path = tmp_path / "archive"
    path.mkdir()
    return path


def entry(archive: Path, message_id: int = 1) -> dict:
    lines = (archive / "index.jsonl").read_text().splitlines()
    return next(e for e in map(json.loads, lines) if e["message_id"] == message_id)


def test_captures_bytes_into_a_content_addressed_file(vault, archive):
    routes = {
        URL_PIXEL: (200, {"content-type": "image/png"}, PNG),
        URL_LOGO: (200, {"content-type": "image/gif"}, PIXEL),
    }
    assert mirror.run(vault, archive, asset_client=serving(routes)) == 0

    digest = hashlib.sha256(PNG).hexdigest()
    asset = archive / "assets" / f"{digest[:16]}.png"
    assert asset.read_bytes() == PNG

    image = entry(archive)["images"][0]
    assert image["url_path"] == URL_PIXEL
    assert image["asset"] == f"assets/{digest[:16]}.png"
    assert image["sha256"] == digest
    assert image["bytes"] == len(PNG)


def test_extension_falls_back_to_magic_bytes_when_type_is_missing(vault, archive):
    routes = {URL_PIXEL: (200, {}, PNG), URL_LOGO: (200, {}, PIXEL)}
    mirror.run(vault, archive, asset_client=serving(routes))

    names = sorted(p.name for p in (archive / "assets").iterdir())
    assert names == sorted(
        [
            f"{hashlib.sha256(PIXEL).hexdigest()[:16]}.gif",
            f"{hashlib.sha256(PNG).hexdigest()[:16]}.png",
        ]
    )


def test_identical_bytes_under_two_urls_are_one_file(vault, archive):
    routes = {
        URL_PIXEL: (200, {"content-type": "image/png"}, PNG),
        URL_LOGO: (200, {"content-type": "image/png"}, PNG),
    }
    mirror.run(vault, archive, asset_client=serving(routes))

    assert len(list((archive / "assets").iterdir())) == 1
    recorded = {image["asset"] for image in entry(archive)["images"]}
    assert len(recorded) == 1


def test_a_fetch_failure_keeps_the_url_and_leaves_no_asset_key(vault, archive):
    routes = {URL_LOGO: (200, {"content-type": "image/png"}, PNG)}  # pixel 404s

    # Assets never block the mirror: exit 0, .eml written.
    assert mirror.run(vault, archive, asset_client=serving(routes)) == 0
    assert (archive / "raw" / "1.eml").exists()

    failed_image = entry(archive)["images"][0]
    assert failed_image["url_path"] == URL_PIXEL
    assert "asset" not in failed_image

    run = json.loads(max((archive / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime_ns).read_text())
    assert run["assets"]["failed"] == 1
    assert run["assets"]["fetched"] == 1
    assert run["assets"]["failures"][0]["url_path"] == URL_PIXEL
    # A dead CDN is not a mirror failure.
    assert run["totals"]["failed"] == 0


def test_an_html_error_page_with_200_is_not_an_image(vault, archive):
    routes = {
        URL_PIXEL: (200, {"content-type": "text/html"}, b"<html>blocked</html>"),
        URL_LOGO: (200, {"content-type": "image/png"}, PNG),
    }
    mirror.run(vault, archive, asset_client=serving(routes))

    image = entry(archive)["images"][0]
    assert "asset" not in image
    assert not any(p.suffix == ".bin" for p in (archive / "assets").iterdir())


def test_rerun_is_resumable_and_makes_no_requests(vault, archive):
    routes = {
        URL_PIXEL: (200, {"content-type": "image/png"}, PNG),
        URL_LOGO: (200, {"content-type": "image/gif"}, PIXEL),
    }
    mirror.run(vault, archive, asset_client=serving(routes))
    before = (archive / "index.jsonl").read_bytes()

    calls: list = []
    mirror.run(vault, archive, asset_client=serving({}, calls))

    assert calls == []
    assert (archive / "index.jsonl").read_bytes() == before


def test_no_assets_is_lossy_and_does_not_defer(vault, archive):
    calls: list = []
    mirror.run(vault, archive, no_assets=True, asset_client=serving({}, calls))

    assert calls == []
    assert not (archive / "assets").exists()
    for image in entry(archive)["images"]:
        assert "asset" not in image

    run = json.loads(max((archive / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime_ns).read_text())
    assert run["assets"]["referenced"] == 2
    assert run["assets"]["disabled"] is True


def test_existing_asset_file_is_never_rewritten(vault, archive):
    digest = hashlib.sha256(PNG).hexdigest()
    assets_dir = archive / "assets"
    assets_dir.mkdir()
    existing = assets_dir / f"{digest[:16]}.png"
    existing.write_bytes(PNG)
    mtime = existing.stat().st_mtime_ns

    mirror.run(
        vault,
        archive,
        asset_client=serving({URL_PIXEL: (200, {"content-type": "image/png"}, PNG)}),
    )

    assert existing.stat().st_mtime_ns == mtime


def test_oversized_response_is_refused(vault, archive, monkeypatch):
    monkeypatch.setattr(assets, "MAX_BYTES", 8)
    routes = {URL_PIXEL: (200, {"content-type": "image/png"}, PNG)}

    mirror.run(vault, archive, asset_client=serving(routes))

    assert "asset" not in entry(archive)["images"][0]


def test_webp_magic_is_recognised():
    webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
    assert assets.detect_extension(None, webp) == ".webp"
    assert assets.looks_like_image(None, webp)


def test_unknown_bytes_are_not_an_image():
    assert not assets.looks_like_image("application/octet-stream", b"%PDF-1.7")
