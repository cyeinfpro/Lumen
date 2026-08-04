"""Filesystem identity guards shared by retention helpers."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class DirectoryIdentity(Protocol):
    path: Path
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class DirectoryPathGuard:
    path: Path
    device: int | None
    inode: int | None
    mode: int | None

    @classmethod
    def from_handle(
        cls,
        directory: DirectoryIdentity,
    ) -> DirectoryPathGuard:
        return cls(
            path=directory.path,
            device=directory.device,
            inode=directory.inode,
            mode=directory.mode,
        )

    @classmethod
    def absent(cls, path: Path) -> DirectoryPathGuard:
        return cls(path=path, device=None, inode=None, mode=None)

    def matches(self, info: os.stat_result) -> bool:
        return (
            self.device is not None
            and self.inode is not None
            and self.mode is not None
            and self.device == info.st_dev
            and self.inode == info.st_ino
            and stat.S_IFMT(self.mode) == stat.S_IFMT(info.st_mode)
        )
