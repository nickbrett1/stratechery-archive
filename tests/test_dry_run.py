"""Tests for the --dry-run mount contract (BRIEF §3.2).

The interesting cases are the two that must not be confused: a bare CI container
with no volumes at all (exit 0, smoke gate) and a deployed job whose mount source
was wrong (exit 1, naming the path).
"""

import sqlite3
from pathlib import Path

import pytest

from stratechery_archive.__main__ import main, run_dry_run


@pytest.fixture
def vault_db(tmp_path: Path) -> Path:
    path = tmp_path / "msgvault.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    path = tmp_path / "archive"
    path.mkdir()
    return path


def test_nothing_mounted_is_the_smoke_case_and_passes(tmp_path, capsys):
    # CI runs the image with no volumes; this must exit 0 or the gate is red.
    code = run_dry_run(tmp_path / "db" / "msgvault.db", tmp_path / "archive")

    assert code == 0
    assert "no mounts attached" in capsys.readouterr().out


def test_missing_db_in_deployed_job_fails_and_names_the_path(
    vault_db, archive_dir, capsys
):
    # The archive mount exists, so this is a real job with a broken db mount.
    missing = vault_db.parent / "not-there.db"

    code = run_dry_run(missing, archive_dir)

    captured = capsys.readouterr()
    assert code == 1
    assert str(missing) in captured.err
    assert "missing" in captured.err


def test_missing_archive_in_deployed_job_fails_and_names_the_path(
    vault_db, tmp_path, capsys
):
    missing = tmp_path / "archive"

    code = run_dry_run(vault_db, missing)

    captured = capsys.readouterr()
    assert code == 1
    assert str(missing) in captured.err


def test_docker_created_empty_db_dir_is_caught(vault_db, archive_dir):
    # Docker creates a missing bind source as an empty directory. The mount
    # "exists" but the file is absent, which is exactly the host failure mode.
    empty_db_dir = vault_db.parent / "empty"
    empty_db_dir.mkdir()

    code = run_dry_run(empty_db_dir / "msgvault.db", archive_dir)

    assert code == 1


def test_both_mounts_usable_passes(vault_db, archive_dir, capsys):
    code = run_dry_run(vault_db, archive_dir)

    out = capsys.readouterr().out
    assert code == 0
    assert "read-only ok" in out
    assert "writable" in out


def test_db_is_opened_read_only(vault_db, archive_dir):
    # The vault is live and shared with its writer. A dry run must not be able
    # to change the file, so leave no -wal/-shm and no writes behind.
    before = sorted(p.name for p in vault_db.parent.iterdir())

    assert run_dry_run(vault_db, archive_dir) == 0

    assert sorted(p.name for p in vault_db.parent.iterdir()) == before


def test_not_a_sqlite_database_fails(vault_db, archive_dir):
    imposter = vault_db.parent / "imposter.db"
    imposter.write_bytes(b"not sqlite at all")

    code = run_dry_run(imposter, archive_dir)

    assert code == 1


def test_archive_that_is_a_file_fails(vault_db, tmp_path):
    not_a_dir = tmp_path / "archive"
    not_a_dir.write_text("")

    assert run_dry_run(vault_db, not_a_dir) == 1


def test_dry_run_writes_nothing(vault_db, archive_dir):
    run_dry_run(vault_db, archive_dir)

    assert list(archive_dir.iterdir()) == []


def test_main_without_dry_run_is_not_a_silent_success(vault_db, archive_dir, capsys):
    # A run that did nothing must not exit 0: that reads as an archived corpus.
    code = main(["--db", str(vault_db), "--archive", str(archive_dir)])

    assert code == 1
    assert "not implemented yet" in capsys.readouterr().err
