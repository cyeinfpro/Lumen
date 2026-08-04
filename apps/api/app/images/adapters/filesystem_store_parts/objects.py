from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import os
import stat
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from ...domain.artifact import ArtifactIdentity, ArtifactKey
from ..filesystem_staging import ArtifactIdentityMismatch, ArtifactStoreError


CHUNK_SIZE = 256 * 1024
_DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
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
    """Persist prior directory entry changes or fail the owning operation.

    Unsupported directory sync is not a successful compatibility mode: without
    an equivalent platform guarantee, publication must remain retryable.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in _DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS:
                raise
            _sync_filesystem(fd)
    finally:
        os.close(fd)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_identity(path: Path) -> ArtifactIdentity:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ArtifactStoreError("artifact path is not a regular file")
    return ArtifactIdentity(
        sha256=hash_file(path),
        size_bytes=info.st_size,
        device=info.st_dev,
        inode=info.st_ino,
    )


class FileSystemObjectsMixin:
    root: Path
    _monotonic: Callable[[], float]

    def __init__(
        self,
        root: str | Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._monotonic = monotonic

    def _ensure_directory(self, path: Path) -> None:
        relative = path.relative_to(self.root)
        current = self.root
        root_info = current.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ArtifactStoreError("artifact storage root is unsafe")
        for part in relative.parts:
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                fsync_directory(current.parent)
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ArtifactStoreError("artifact parent is not a safe directory")

    def _validate_directory(self, path: Path) -> None:
        relative = path.relative_to(self.root)
        current = self.root
        root_info = current.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ArtifactStoreError("artifact storage root is unsafe")
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ArtifactStoreError("artifact parent is not a safe directory")

    def _path(self, key: ArtifactKey, *, create_parent: bool = False) -> Path:
        path = self.root.joinpath(*key.value.split("/"))
        parent = path.parent
        if create_parent:
            self._ensure_directory(parent)
        else:
            try:
                self._validate_directory(parent)
            except ValueError as exc:
                raise ArtifactStoreError("artifact path escapes storage root") from exc
        return path

    async def identity(self, key: ArtifactKey) -> ArtifactIdentity | None:
        try:
            return await asyncio.to_thread(artifact_identity, self._path(key))
        except FileNotFoundError:
            return None

    async def exists(self, key: ArtifactKey) -> bool:
        return await self.identity(key) is not None

    async def open(self, key: ArtifactKey) -> AsyncIterator[bytes]:
        path = self._path(key)
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            while chunk := await asyncio.to_thread(handle.read, CHUNK_SIZE):
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def delete(
        self,
        key: ArtifactKey,
        expected: ArtifactIdentity | None = None,
    ) -> bool:
        path = self._path(key)
        try:
            current = await asyncio.to_thread(artifact_identity, path)
        except FileNotFoundError:
            return False
        if expected is not None and not expected.matches(current):
            raise ArtifactIdentityMismatch("refusing to delete changed artifact")
        await asyncio.to_thread(path.unlink)
        await asyncio.to_thread(fsync_directory, path.parent)
        return True
