"""Bounded lifecycle management for descriptor-relative scan cursors."""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, Protocol, TypeVar


class ResettableCursor(Protocol):
    def reset(self) -> None: ...


class DirectoryIdentity(Protocol):
    device: int
    inode: int
    path: Path


CursorT = TypeVar("CursorT", bound=ResettableCursor)
_DirectoryKey = tuple[int, int]


@dataclass
class _CursorRecord(Generic[CursorT]):
    cursor: CursorT
    path_key: str
    last_access: int


@dataclass
class DirectoryScanRegistry(Generic[CursorT]):
    _cursor_factory: Callable[[], CursorT]
    _max_cursors: int
    _max_idle_accesses: int
    _cursors: OrderedDict[_DirectoryKey, _CursorRecord[CursorT]] = field(
        default_factory=OrderedDict
    )
    _paths: dict[str, _DirectoryKey] = field(default_factory=dict)
    _access_clock: int = 0

    @classmethod
    def create(
        cls,
        cursor_factory: Callable[[], CursorT],
        *,
        max_cursors: int = 256,
        max_idle_accesses: int = 4_096,
    ) -> DirectoryScanRegistry[CursorT]:
        if max_cursors <= 0 or max_idle_accesses <= 0:
            raise ValueError("cursor registry bounds must be positive")
        return cls(
            _cursor_factory=cursor_factory,
            _max_cursors=max_cursors,
            _max_idle_accesses=max_idle_accesses,
        )

    @staticmethod
    def _path_key(directory: DirectoryIdentity) -> str:
        return os.fspath(directory.path.absolute())

    def _drop(self, key: _DirectoryKey) -> None:
        record = self._cursors.pop(key, None)
        if record is None:
            return
        if self._paths.get(record.path_key) == key:
            self._paths.pop(record.path_key, None)
        record.cursor.reset()

    def _evict_idle(self) -> None:
        cutoff = self._access_clock - self._max_idle_accesses
        while self._cursors:
            key, record = next(iter(self._cursors.items()))
            if record.last_access > cutoff:
                break
            self._drop(key)

    def _enforce_capacity(self) -> None:
        while len(self._cursors) > self._max_cursors:
            self._drop(next(iter(self._cursors)))

    def cursor_for(self, directory: DirectoryIdentity) -> CursorT:
        self._access_clock += 1
        self._evict_idle()
        key = (directory.device, directory.inode)
        path_key = self._path_key(directory)
        previous_key = self._paths.get(path_key)
        if previous_key is not None and previous_key != key:
            self._drop(previous_key)

        record = self._cursors.get(key)
        if record is None:
            record = _CursorRecord(
                cursor=self._cursor_factory(),
                path_key=path_key,
                last_access=self._access_clock,
            )
            self._cursors[key] = record
        else:
            if (
                record.path_key != path_key
                and self._paths.get(record.path_key) == key
            ):
                self._paths.pop(record.path_key, None)
            record.path_key = path_key
            record.last_access = self._access_clock
            self._cursors.move_to_end(key)
        self._paths[path_key] = key
        self._enforce_capacity()
        return record.cursor

    def discard(self, directory: DirectoryIdentity) -> None:
        self._drop((directory.device, directory.inode))

    def reset_all(self) -> None:
        for record in self._cursors.values():
            record.cursor.reset()
        self._cursors.clear()
        self._paths.clear()

    @property
    def cursor_count(self) -> int:
        return len(self._cursors)
