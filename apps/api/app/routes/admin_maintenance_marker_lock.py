"""Cross-process serialization for maintenance marker claims."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


MAINTENANCE_MARKER_NAMES = (
    ".update.running",
    ".backup.running",
    ".restore.running",
)
_MAINTENANCE_MARKER_LOCK_NAME = ".maintenance-markers.lock"
_MAINTENANCE_MARKER_MODE = 0o660


@contextmanager
def maintenance_marker_lock(root: Path) -> Iterator[None]:
    """Serialize marker inspection, stale cleanup, and replacement."""

    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        root / _MAINTENANCE_MARKER_LOCK_NAME,
        os.O_RDWR | os.O_CREAT,
        _MAINTENANCE_MARKER_MODE,
    )
    try:
        try:
            os.fchmod(fd, _MAINTENANCE_MARKER_MODE)
        except PermissionError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


__all__ = ["MAINTENANCE_MARKER_NAMES", "maintenance_marker_lock"]
