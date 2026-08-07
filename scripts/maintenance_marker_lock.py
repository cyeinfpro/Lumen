"""Cross-process lock shared by API and host maintenance marker writers."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Iterator


_MAINTENANCE_MARKER_MODE = 0o660


@contextmanager
def marker_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        root / ".maintenance-markers.lock",
        os.O_RDWR | os.O_CREAT,
        _MAINTENANCE_MARKER_MODE,
    )
    try:
        try:
            os.fchmod(descriptor, _MAINTENANCE_MARKER_MODE)
        except PermissionError:
            pass
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
