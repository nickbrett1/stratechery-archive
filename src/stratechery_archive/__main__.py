"""Batch entry point for stratechery-archive.

Scope of this module today is Task 1 of the dev brief (.agents/BRIEF.md §4):
``--dry-run`` only. It is two things at once:

1. a preview that prints the target layout and touches nothing, and
2. the mount-contract check that CI can run, because the smoke gate is the only
   place that can prove the job can see its inputs.

Strictness rule (BRIEF §3.2 vs the smoke gate in .buildkite/pipeline.yml)

The smoke job runs the image with *no* volumes attached and requires exit 0.
The host runs it *with* volumes and a broken mount must fail loudly. Both are
satisfiable because Docker creates a missing bind-mount source as an empty
directory: a misconfigured host always leaves ``/db`` or ``/archive`` present
but unusable, while a bare CI container has neither.

So: if either mount point exists, checks are strict and a failure exits 1
naming the path. If neither exists, nothing is mounted to check, the report
says so, and the exit is 0.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# The one read-only mount and the one read-write mount. That asymmetry is the
# job's whole security posture (BRIEF §5); do not "simplify" it.
DEFAULT_DB_PATH = Path("/db/msgvault.db")
DEFAULT_ARCHIVE_DIR = Path("/archive")

EXIT_OK = 0
EXIT_MOUNT_FAILED = 1
EXIT_RUN_FAILED = 1


@dataclass(frozen=True)
class MountCheck:
    """Result of checking one mount, with a reason that names the failure."""

    path: Path
    ok: bool
    detail: str


def check_db(db_path: Path) -> MountCheck:
    """Assert the vault database exists and opens read-only.

    Opened with ``mode=ro`` on purpose: the DB is live, ~8.1 GB, and has an
    active WAL. A read-only connection cannot checkpoint or create -shm/-wal
    files, so the job cannot damage the writer it shares the file with.
    """
    if not db_path.exists():
        return MountCheck(db_path, False, "missing")
    if not db_path.is_file():
        return MountCheck(db_path, False, "not a regular file")

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return MountCheck(db_path, False, f"cannot open read-only: {exc}")

    try:
        # Cheapest possible proof that this is a readable SQLite database:
        # it parses the header and touches no user table or page.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as exc:
        return MountCheck(db_path, False, f"not a readable SQLite database: {exc}")
    finally:
        conn.close()

    size = db_path.stat().st_size
    return MountCheck(db_path, True, f"read-only ok, {size:,} B")


def check_archive(archive_dir: Path) -> MountCheck:
    """Assert the archive root exists, is a directory, and is writable."""
    if not archive_dir.exists():
        return MountCheck(archive_dir, False, "missing")
    if not archive_dir.is_dir():
        return MountCheck(archive_dir, False, "not a directory")
    if not os.access(archive_dir, os.W_OK | os.X_OK):
        # os.access is advisory for root, and the image runs as 1000:1000 for
        # exactly this reason (BRIEF §3.3). A root-owned, non-writable
        # /archive is the failure this catches.
        return MountCheck(archive_dir, False, "not writable by the current user")
    return MountCheck(archive_dir, True, "writable")


def describe_population(db_path: Path) -> tuple[int | None, str | None]:
    """(population count, error). Reads only; used by the dry-run preview."""
    import sqlite3

    from stratechery_archive import population

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return population.count(conn), None
        finally:
            conn.close()
    except (sqlite3.Error, population.SchemaError) as exc:
        return None, str(exc)


def run_dry_run(db_path: Path, archive_dir: Path) -> int:
    """Report what would happen, write nothing, return the exit code."""
    checks = (check_db(db_path), check_archive(archive_dir))

    print("stratechery-archive --dry-run")
    print(f"archive root: {archive_dir}")
    print("  raw/<message_id>.eml      verbatim bytes, written once")
    print("  raw/SHA256SUMS            manifest of raw/*.eml, written last")
    print("  index.jsonl               append-only, sorted by message_id")
    print("  runs/<ts>.json            one file per run")
    print("  assets/<sha256[:16]>.<ext> captured image bytes, written once")
    print("mounts:")
    for check in checks:
        status = "OK  " if check.ok else "FAIL"
        print(f"  {check.path}  {status}  {check.detail}")

    failures = [check for check in checks if not check.ok]
    if not failures:
        count, error = describe_population(db_path)
        if count is None:
            print(f"error: cannot read the population: {error}", file=sys.stderr)
            return EXIT_MOUNT_FAILED
        print(f"population: {count} messages would be mirrored")
        print("dry run complete: both mounts usable, no files written.")
        return EXIT_OK

    if not (db_path.exists() or archive_dir.exists()):
        print(
            "no mounts attached - nothing to check. "
            "This is the CI smoke case and exits 0 by contract.",
        )
        return EXIT_OK

    for failure in failures:
        print(f"error: {failure.path}: {failure.detail}", file=sys.stderr)
    print(
        "dry run failed: the job cannot see its inputs. Refusing to continue.",
        file=sys.stderr,
    )
    return EXIT_MOUNT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stratechery_archive",
        description="Mirror the Stratechery message population to verbatim .eml files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check the mounts and print the target layout; write nothing",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"msgvault database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--no-assets",
        action="store_true",
        help="record image URLs but capture no bytes (lossy, not deferred)",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=DEFAULT_ARCHIVE_DIR,
        help=f"archive root (default: {DEFAULT_ARCHIVE_DIR})",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        return run_dry_run(args.db, args.archive)

    # The mirror needs both inputs; the same checks the dry run makes, so a
    # broken mount fails here rather than halfway through the corpus.
    for check in (check_db(args.db), check_archive(args.archive)):
        if not check.ok:
            print(f"error: {check.path}: {check.detail}", file=sys.stderr)
            return EXIT_MOUNT_FAILED

    from stratechery_archive import mirror, population

    try:
        return mirror.run(args.db, args.archive, no_assets=args.no_assets)
    except (population.SchemaError, mirror.MirrorError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUN_FAILED


if __name__ == "__main__":
    sys.exit(main())
