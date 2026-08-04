#!/usr/bin/env python3
"""Hold the scripts unit lock across an exec or verify an inherited lock fd."""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import stat
import sys
import time


def _open_lock(path: Path) -> int:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags, 0o600)
    opened = os.fstat(fd)
    current = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_uid != os.geteuid()
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        os.close(fd)
        raise SystemExit("scripts unit lock path is unsafe")
    os.fchmod(fd, 0o600)
    return fd


def _lock_with_timeout(fd: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise SystemExit(75)
            time.sleep(0.05)


def _same_open_file(fd: int, path: Path) -> bool:
    try:
        opened = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(current.st_mode)
        and opened.st_uid == os.geteuid()
        and opened.st_dev == current.st_dev
        and opened.st_ino == current.st_ino
    )


def _verify(fd_raw: str, path_raw: str) -> int:
    try:
        fd = int(fd_raw)
    except ValueError:
        return 1
    path = Path(path_raw)
    if not _same_open_file(fd, path):
        return 1
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 1
    return 0


def _exec_locked(path_raw: str, timeout_raw: str, command: list[str]) -> int:
    if not command:
        raise SystemExit("missing command for scripts unit lock")
    try:
        timeout = max(0.0, float(timeout_raw))
    except ValueError:
        timeout = 60.0
    raw_path = Path(path_raw)
    path = raw_path.parent.resolve(strict=True) / raw_path.name
    fd = _open_lock(path)
    try:
        _lock_with_timeout(fd, timeout)
        os.set_inheritable(fd, True)
        env = os.environ.copy()
        env["LUMEN_SCRIPT_UNIT_LOCK_PATH"] = str(path)
        env["LUMEN_SCRIPT_UNIT_LOCK_FD"] = str(fd)
        os.execvpe(command[0], command, env)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise SystemExit(f"cannot exec scripts unit command: {command[0]}")
        raise
    finally:
        os.close(fd)
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "verify":
        if len(sys.argv) != 4:
            return 2
        return _verify(sys.argv[2], sys.argv[3])
    if len(sys.argv) >= 2 and sys.argv[1] == "exec":
        if len(sys.argv) < 6 or sys.argv[4] != "--":
            return 2
        return _exec_locked(sys.argv[2], sys.argv[3], sys.argv[5:])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
