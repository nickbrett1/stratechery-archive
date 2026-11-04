"""The mirror loop, end to end, against a synthesised archive."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import FAKE_TOKEN, build_vault, make_eml
from stratechery_archive import images, mirror


@pytest.fixture
def vault(tmp_path):
    db_path = tmp_path / "msgvault.db"
    conn = build_vault(db_path)
    conn.close()
    return db_path


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "archive"
    path.mkdir()
    return path


def latest_run(archive: Path) -> dict:
    """The newest run record by mtime (filenames sort on the -N suffix wrongly)."""
    path = max((archive / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
    return json.loads(path.read_text())


def read_index(archive: Path) -> list[dict]:
    lines = (archive / "index.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_writes_verbatim_bytes_and_index_lines(vault, archive):
    assert mirror.run(vault, archive) == 0

    for message_id in (1, 2, 3):
        written = (archive / "raw" / f"{message_id}.eml").read_bytes()
        # Nothing is interpreted: bytes out are bytes in.
        assert written == make_eml(message_id)

    entries = read_index(archive)
    assert [entry["message_id"] for entry in entries] == [1, 2, 3]
    first = entries[0]
    assert first["bytes"] == len(make_eml(1))
    assert first["sha256"] == hashlib.sha256(make_eml(1)).hexdigest()
    assert first["from"] == "email@stratechery.com"
    assert first["sent_at"] == "2026-11-03T11:01:05Z"
    assert first["rfc822_message_id"] == "<message-1@mail.stratechery.com>"
    assert first["subject"] == "Weekly Briefing 1"


def test_index_is_sorted_by_message_id(vault, archive):
    mirror.run(vault, archive)

    ids = [entry["message_id"] for entry in read_index(archive)]
    assert ids == sorted(ids)


def test_image_urls_are_recorded_without_their_query(vault, archive):
    mirror.run(vault, archive)

    images_recorded = read_index(archive)[0]["images"]
    paths = [image["url_path"] for image in images_recorded]
    # The per-recipient token is in the query string and must never reach the
    # index; duplicates within a message collapse.
    assert paths == [
        "https://track.stratechery.com/pixel.png",
        "https://cdn.stratechery.com/logo.png",
    ]
    for entry in read_index(archive):
        for image in entry["images"]:
            assert "?" not in image["url_path"]
            assert FAKE_TOKEN not in json.dumps(entry)


def test_manifest_covers_every_eml_and_verifies(vault, archive):
    mirror.run(vault, archive)

    manifest = (archive / "raw" / "SHA256SUMS").read_text().splitlines()
    assert len(manifest) == 3
    for line in manifest:
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((archive / "raw" / name).read_bytes()).hexdigest() == digest
    # No index.jsonl or runs/ entries: a log that grows inside the attested
    # directory invalidates its own attestation.
    assert not any("SHA256SUMS" in line or "jsonl" in line for line in manifest)


def test_size_estimate_mismatch_fails_without_writing(tmp_path, archive):
    db_path = tmp_path / "msgvault.db"
    conn = build_vault(db_path, [1])
    conn.execute("UPDATE messages SET size_estimate = size_estimate + 1 WHERE id = 1")
    conn.commit()
    conn.close()

    assert mirror.run(db_path, archive) == 1

    assert not (archive / "raw" / "1.eml").exists()
    run_file = next((archive / "runs").glob("*.json"))
    payload = json.loads(run_file.read_text())
    assert payload["totals"]["written"] == 0
    assert payload["totals"]["failed"] == 1
    assert "size_estimate" in payload["failures"][0]["reason"]


def test_second_run_rewrites_nothing_and_is_byte_identical(vault, archive):
    mirror.run(vault, archive)
    index_before = (archive / "index.jsonl").read_bytes()
    manifest_before = (archive / "raw" / "SHA256SUMS").read_bytes()
    mtimes = {p.name: p.stat().st_mtime_ns for p in (archive / "raw").glob("*.eml")}

    assert mirror.run(vault, archive) == 0

    payload = latest_run(archive)
    assert payload["totals"]["written"] == 0
    assert payload["totals"]["skipped_unchanged"] == 3
    assert (archive / "index.jsonl").read_bytes() == index_before
    assert (archive / "raw" / "SHA256SUMS").read_bytes() == manifest_before
    assert {p.name: p.stat().st_mtime_ns for p in (archive / "raw").glob("*.eml")} == mtimes


def test_a_tampered_raw_file_is_reported_never_overwritten(vault, archive):
    mirror.run(vault, archive)
    target = archive / "raw" / "1.eml"
    target.write_bytes(b"tampered")
    before = target.read_bytes()

    assert mirror.run(vault, archive) == 1

    assert target.read_bytes() == before
    payload = latest_run(archive)
    assert payload["totals"]["failed"] == 1
    assert "immutable" in payload["failures"][0]["reason"]


def test_unattested_raw_file_is_not_adopted_or_overwritten(vault, archive):
    (archive / "raw").mkdir()
    stray = archive / "raw" / "99.eml"
    stray.write_bytes(b"somebody else's file")

    assert mirror.run(vault, archive) == 1

    assert stray.read_bytes() == b"somebody else's file"
    payload = latest_run(archive)
    assert any("no index line" in failure["reason"] for failure in payload["failures"])


def test_missing_message_raw_is_a_failure_not_a_silent_skip(tmp_path, archive):
    db_path = tmp_path / "msgvault.db"
    conn = build_vault(db_path, [1, 2])
    conn.execute("DELETE FROM message_raw WHERE message_id = 2")
    conn.commit()
    conn.close()

    assert mirror.run(db_path, archive) == 1

    payload = latest_run(archive)
    assert payload["totals"]["in_db"] == 2
    assert payload["totals"]["written"] == 1
    assert payload["totals"]["failed"] == 1


def test_non_zlib_blob_is_a_failure(tmp_path, archive):
    db_path = tmp_path / "msgvault.db"
    conn = build_vault(db_path, [1])
    conn.execute("UPDATE message_raw SET compression = 'none' WHERE message_id = 1")
    conn.commit()
    conn.close()

    assert mirror.run(db_path, archive) == 1
    assert not (archive / "raw" / "1.eml").exists()


def test_undecodable_blob_is_a_failure(tmp_path, archive):
    db_path = tmp_path / "msgvault.db"
    conn = build_vault(db_path, [1])
    conn.execute("UPDATE message_raw SET raw_data = ? WHERE message_id = 1", (b"not zlib",))
    conn.commit()
    conn.close()

    assert mirror.run(db_path, archive) == 1
    payload = latest_run(archive)
    assert "zlib" in payload["failures"][0]["reason"]


def test_run_file_records_the_query_and_the_code(vault, archive):
    mirror.run(vault, archive)

    payload = latest_run(archive)
    assert "stratechery.com" in payload["population"]["query"]
    assert len(payload["population"]["query_sha"]) == 64
    assert len(payload["extractor_sha"]) == 64
    assert payload["bytes"]["raw_total"] == 3 * len(make_eml(1))
    assert payload["totals"] == {
        "in_db": 3,
        "written": 3,
        "skipped_unchanged": 0,
        "failed": 0,
    }
    assert payload["assets"] == {"referenced": 6, "fetched": 0, "failed": 0}


def test_no_tmp_files_survive(vault, archive):
    mirror.run(vault, archive)

    assert list(archive.rglob("*.tmp")) == []


def test_sender_id_fallback_still_finds_the_message(tmp_path, archive):
    # The v_messages fallback fired 0 times on this corpus, but it is part of
    # the view's definition and must not be dropped.
    db_path = tmp_path / "msgvault.db"
    conn = build_vault(db_path, [1], domain_via_sender_id=True)
    conn.close()

    assert mirror.run(db_path, archive) == 0
    assert (archive / "raw" / "1.eml").exists()


def test_other_domains_are_not_in_the_population(tmp_path, archive):
    db_path = tmp_path / "msgvault.db"
    conn = build_vault(db_path, [1], domain="example.com")
    conn.close()

    assert mirror.run(db_path, archive) == 0
    assert list((archive / "raw").glob("*.eml")) == []


def test_population_sql_is_a_single_statement(vault):
    import sqlite3

    from stratechery_archive import population

    conn = sqlite3.connect(vault)
    sql = population.population_sql(conn)
    conn.close()

    assert sql.strip().upper().startswith("WITH")
    assert "stratechery.com" in sql
    assert "raw_data" not in sql  # blobs are fetched per message, not selected


def test_image_urls_scan_is_not_a_parser():
    html = (
        '<IMG alt="x" SRC="https://a.example/x.png?tok=1">'
        "<img src='https://b.example/y%20z.png?x=2&y=3'>"
        '<img src="https://a.example/x.png?tok=2">'
        "<img src=c.example/unquoted.png>"
        "<div data-src=\"https://not-an-img.example/x.png\"></div>"
        '<img src="">'
    )

    assert images.image_urls(html) == [
        "https://a.example/x.png",
        "https://b.example/y%20z.png",
    ]
    assert images.image_urls(None) == []


def test_sent_at_is_rendered_without_changing_the_instant():
    assert mirror.normalise_sent_at("2016-12-21 11:27:42+00:00") == "2016-12-21T11:27:42Z"
    assert mirror.normalise_sent_at("2026-11-03 11:01:05") == "2026-11-03T11:01:05Z"
    assert mirror.normalise_sent_at(None) is None
    # Unparseable values pass through rather than being dropped.
    assert mirror.normalise_sent_at("sometime in 2017") == "sometime in 2017"
