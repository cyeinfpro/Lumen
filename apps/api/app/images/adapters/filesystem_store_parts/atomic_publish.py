"""Atomic no-overwrite installation for prepared publish files."""

from __future__ import annotations

import ctypes
import errno
import os
from collections.abc import Callable
from pathlib import Path

from .objects import fsync_directory


RENAME_NOREPLACE_LINUX = 1
RENAME_EXCL_DARWIN = 0x00000004
RENAME_NOREPLACE_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
)


def _load_rename_noreplace() -> tuple[Callable[..., int], int] | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        return renameat2, RENAME_NOREPLACE_LINUX
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is not None:
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        return renameatx_np, RENAME_EXCL_DARWIN
    return None


RENAME_NOREPLACE_API = _load_rename_noreplace()


def _rename_noreplace(source: Path, destination: Path) -> bool:
    api = RENAME_NOREPLACE_API
    if api is None:
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_fd = os.open(source.parent, flags)
    try:
        function, rename_flag = api
        ctypes.set_errno(0)
        result = function(
            parent_fd,
            os.fsencode(source.name),
            parent_fd,
            os.fsencode(destination.name),
            rename_flag,
        )
        error = ctypes.get_errno() if result != 0 else 0
    finally:
        os.close(parent_fd)
    if result == 0:
        return True
    error = error or errno.EIO
    if error in RENAME_NOREPLACE_UNSUPPORTED_ERRNOS:
        return False
    raise OSError(error, os.strerror(error), destination)


def install_file_noreplace(source: Path, destination: Path) -> None:
    if source.parent != destination.parent:
        raise ValueError("publish temp must share the destination directory")
    if _rename_noreplace(source, destination):
        fsync_directory(destination.parent)
        return

    os.link(source, destination)
    fsync_directory(destination.parent)
    source.unlink()
    fsync_directory(destination.parent)
