"""Crash-consistent helpers for file publication and removal."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path


_RENAME_NOREPLACE_LINUX = 1
_RENAME_EXCL_DARWIN = 0x00000004


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
        return renameat2, _RENAME_NOREPLACE_LINUX
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
        return renameatx_np, _RENAME_EXCL_DARWIN
    return None


_RENAME_NOREPLACE_API = _load_rename_noreplace()


def rename_noreplace_available() -> bool:
    return _RENAME_NOREPLACE_API is not None


def rename_entry_noreplace(
    source: str,
    target: str,
    source_fd: int,
    target_fd: int,
) -> None:
    api = _RENAME_NOREPLACE_API
    if api is None:
        raise OSError(
            errno.ENOTSUP,
            "descriptor-relative rename no-replace is unavailable",
        )
    function, flag = api
    ctypes.set_errno(0)
    result = function(
        source_fd,
        os.fsencode(source),
        target_fd,
        os.fsencode(target),
        flag,
    )
    if result != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error), target)


def fsync_directory(directory: Path) -> None:
    """Persist directory entry changes made before this call."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os,
        "O_CLOEXEC",
        0,
    )
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def fsync_directory_fd(directory_fd: int) -> None:
    """Persist changes through an already verified directory descriptor."""
    os.fsync(directory_fd)


def durable_mkdir(directory: Path, *, mode: int = 0o777) -> None:
    """Create a directory chain and persist every newly linked component."""
    missing: list[Path] = []
    current = directory
    while True:
        try:
            info = current.stat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISDIR(info.st_mode):
                break
            raise NotADirectoryError(current)
        parent = current.parent
        if parent == current:
            raise FileNotFoundError(directory)
        missing.append(current)
        current = parent

    for child in reversed(missing):
        try:
            child.mkdir(mode=mode)
        except FileExistsError:
            if not child.is_dir():
                raise
        fsync_directory(child.parent)


def write_bytes_and_fsync(path: Path, data: bytes) -> None:
    """Create a regular file and persist its contents before publication."""
    with path.open("xb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def durable_unlink(path: Path, *, missing_ok: bool = False) -> bool:
    """Remove a filesystem entry and persist the parent directory change."""
    try:
        path.unlink()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise
    fsync_directory(path.parent)
    return True


def durable_rmdir(path: Path, *, missing_ok: bool = False) -> bool:
    """Remove an empty directory and persist its parent entry change."""
    try:
        path.rmdir()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise
    fsync_directory(path.parent)
    return True


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    temporary_path: Path,
) -> None:
    """Publish bytes only after file and directory durability barriers."""
    if temporary_path.parent != path.parent:
        raise ValueError("temporary file must share the target parent directory")
    try:
        write_bytes_and_fsync(temporary_path, data)
        os.replace(temporary_path, path)
        fsync_directory(path.parent)
    except BaseException:
        durable_unlink(temporary_path, missing_ok=True)
        raise
