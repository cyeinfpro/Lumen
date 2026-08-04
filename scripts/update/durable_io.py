"""Fail-closed durable file and directory operations for the updater."""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
from pathlib import Path
import stat
import tempfile


_UNSUPPORTED_DIRECTORY_FSYNC = frozenset(
    {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", -1),
        getattr(errno, "EOPNOTSUPP", -1),
    }
)


def _sync_filesystem(fd: int) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        syncfs = libc.syncfs
    except (AttributeError, OSError) as exc:
        raise OSError(
            errno.ENOTSUP,
            "syncfs is unavailable for directory durability fallback",
        ) from exc
    syncfs.argtypes = [ctypes.c_int]
    syncfs.restype = ctypes.c_int
    if syncfs(fd) != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code))


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(path, flags)
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC:
                raise
            _sync_filesystem(directory_fd)
    finally:
        os.close(directory_fd)


def _reject_unsafe_destination(target: Path) -> None:
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"destination symlink is not allowed: {target}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"destination is not a regular file: {target}")


def copy_file_durable(source: Path, target: Path) -> None:
    _reject_unsafe_destination(target)
    fd, temporary_raw = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_raw)
    try:
        with source.open("rb") as source_handle, os.fdopen(fd, "wb") as target_handle:
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                target_handle.write(chunk)
            os.fchmod(target_handle.fileno(), 0o600)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        _reject_unsafe_destination(target)
        os.replace(temporary, target)
        fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    copy_parser = subparsers.add_parser("copy-file")
    copy_parser.add_argument("source", type=Path)
    copy_parser.add_argument("target", type=Path)
    fsync_parser = subparsers.add_parser("fsync-directory")
    fsync_parser.add_argument("path", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "copy-file":
        copy_file_durable(args.source, args.target)
    else:
        fsync_directory(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
