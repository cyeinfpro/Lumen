"""Admin 备份/恢复的文件系统助手(chmod 容忍 EPERM、私有追加打开)。

从 routes/admin_backups.py 拆出,保持路由文件在 route/controller 行数上限内。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple, TextIO


class ScriptResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


def chmod_tolerate_eperm(path: Path | str, mode: int) -> None:
    """chmod that swallows EPERM from squashing mounts (CIFS/NFS).

    Production /opt/lumendata is commonly mounted CIFS with
    ``forceuid,forcegid,uid=...,gid=...,file_mode=0664``. The mount option
    pins the on-wire mode and uid; every chmod from any caller — even the
    file's apparent local owner — returns EPERM because the CIFS server
    doesn't accept the mode change. The mount itself already enforces
    file_mode, so our redundant chmod is purely defensive on local fs.
    Any other OSError still propagates so genuine faults (ENOSPC, EBADF,
    EROFS, ...) keep failing fast.
    """
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass


def open_private_append(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except PermissionError:
            # Same EPERM-on-squashed-mount story as chmod_tolerate_eperm; see
            # there for the full rationale. Kept inline because os.fchmod takes
            # a fd, not a path, so the helper signature doesn't fit.
            pass
        return os.fdopen(fd, "a", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
