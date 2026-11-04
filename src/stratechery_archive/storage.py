"""File primitives shared by the mirror and the asset capture.

Split into its own module so ``assets`` and ``mirror`` can both use them
without importing each other.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    # The rename is only durable once the directory entry is. Best effort:
    # some filesystems (and tmpfs under test) refuse to fsync a directory.
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes) -> None:
    """`.tmp` -> fsync -> rename. A reader never sees a partial file."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    # Explicit mode: an archive should not inherit whatever umask the run
    # happened to have (0644 either way is what a consumer expects to read).
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    _fsync_dir(path.parent)
