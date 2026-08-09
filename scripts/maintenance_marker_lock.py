"""Cross-process lock shared by API and host maintenance marker writers."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import tempfile
from typing import Iterator


_MAINTENANCE_MARKER_MODE = 0o660


def write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while persisting maintenance state")
        view = view[written:]


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                raise
    finally:
        os.close(descriptor)


def atomic_replace_bytes(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_raw)
    try:
        try:
            os.fchmod(descriptor, mode)
        except PermissionError:
            pass
        write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


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


__all__ = [
    "atomic_replace_bytes",
    "fsync_directory",
    "marker_lock",
    "write_all",
]
