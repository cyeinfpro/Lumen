from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from ..domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    PublishedArtifact,
    StagedArtifact,
    UploadTicket,
)


_CHUNK_SIZE = 256 * 1024
_LINK_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EXDEV,
        errno.EPERM,
        errno.EACCES,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
)


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactIdentityMismatch(ArtifactStoreError):
    pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while staging artifact")
        view = view[written:]


def _identity(path: Path) -> ArtifactIdentity:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ArtifactStoreError("artifact path is not a regular file")
    return ArtifactIdentity(
        sha256=_hash_file(path),
        size_bytes=info.st_size,
        device=info.st_dev,
        inode=info.st_ino,
    )


class FileSystemArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _ensure_directory(self, path: Path) -> None:
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
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

    def _stage_dir(self, ticket: UploadTicket) -> Path:
        temp_root = self.root / ".upload-tmp"
        self._ensure_directory(temp_root)
        ticket_dir = temp_root / ticket.value
        try:
            ticket_dir.mkdir(mode=0o700)
        except FileExistsError:
            info = ticket_dir.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ArtifactStoreError("upload ticket directory is unsafe") from None
        return ticket_dir

    async def stage(
        self,
        ticket: UploadTicket,
        source: AsyncIterator[bytes],
        *,
        max_bytes: int,
    ) -> StagedArtifact:
        ticket_dir = await asyncio.to_thread(self._stage_dir, ticket)
        fd, raw_path = await asyncio.to_thread(
            tempfile.mkstemp,
            ".source",
            "artifact-",
            str(ticket_dir),
        )
        path = Path(raw_path)
        os.fchmod(fd, 0o600)
        digest = hashlib.sha256()
        size = 0
        try:
            async for chunk in source:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ArtifactStoreError("upload exceeds maximum bytes")
                digest.update(chunk)
                await asyncio.to_thread(_write_all, fd, chunk)
            if size <= 0:
                raise ArtifactStoreError("empty upload")
            await asyncio.to_thread(os.fsync, fd)
        except BaseException:
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        os.close(fd)
        info = path.lstat()
        return StagedArtifact(
            ticket=ticket,
            path=str(path),
            identity=ArtifactIdentity(
                sha256=digest.hexdigest(),
                size_bytes=size,
                device=info.st_dev,
                inode=info.st_ino,
            ),
            modified_at=info.st_mtime,
        )

    @staticmethod
    def _copy_exclusive(source: Path, destination: Path) -> None:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst, length=_CHUNK_SIZE)
                dst.flush()
                os.fsync(dst.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def _publish_path_sync(
        self,
        source: Path,
        key: ArtifactKey,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        source_identity = _identity(source)
        if not expected.matches(source_identity):
            raise ArtifactIdentityMismatch("source artifact identity changed")
        destination = self._path(key, create_parent=True)
        try:
            existing = _identity(destination)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.sha256 == expected.sha256
                and existing.size_bytes == expected.size_bytes
            ):
                return PublishedArtifact(key=key, identity=existing, created=False)
            raise FileExistsError(destination)
        try:
            os.link(source, destination)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                return self._publish_path_sync(source, key, expected)
            if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
                raise
            self._copy_exclusive(source, destination)
        _fsync_directory(destination.parent)
        published_identity = _identity(destination)
        if (
            published_identity.sha256 != expected.sha256
            or published_identity.size_bytes != expected.size_bytes
        ):
            destination.unlink(missing_ok=True)
            raise ArtifactIdentityMismatch("published artifact verification failed")
        return PublishedArtifact(key=key, identity=published_identity, created=True)

    async def publish(
        self,
        staged: StagedArtifact,
        key: ArtifactKey,
    ) -> PublishedArtifact:
        return await self.publish_path(
            Path(staged.path),
            key,
            expected=staged.identity,
        )

    async def publish_path(
        self,
        source: Path,
        key: ArtifactKey,
        *,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        return await asyncio.to_thread(
            self._publish_path_sync,
            source,
            key,
            expected,
        )

    async def identity(self, key: ArtifactKey) -> ArtifactIdentity | None:
        try:
            return await asyncio.to_thread(_identity, self._path(key))
        except FileNotFoundError:
            return None

    async def exists(self, key: ArtifactKey) -> bool:
        return await self.identity(key) is not None

    async def open(self, key: ArtifactKey) -> AsyncIterator[bytes]:
        path = self._path(key)
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            while chunk := await asyncio.to_thread(handle.read, _CHUNK_SIZE):
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
            current = await asyncio.to_thread(_identity, path)
        except FileNotFoundError:
            return False
        if expected is not None and not expected.matches(current):
            raise ArtifactIdentityMismatch("refusing to delete changed artifact")
        await asyncio.to_thread(path.unlink)
        await asyncio.to_thread(_fsync_directory, path.parent)
        return True

    async def delete_staged(self, staged: StagedArtifact) -> bool:
        path = Path(staged.path)
        try:
            current = await asyncio.to_thread(_identity, path)
        except FileNotFoundError:
            return False
        if not staged.identity.matches(current):
            raise ArtifactIdentityMismatch("refusing to delete changed staged artifact")
        await asyncio.to_thread(path.unlink)
        ticket_dir = path.parent
        try:
            await asyncio.to_thread(ticket_dir.rmdir)
        except OSError:
            pass
        return True

    async def list_staged(self) -> list[StagedArtifact]:
        temp_root = self.root / ".upload-tmp"
        try:
            await asyncio.to_thread(self._validate_directory, temp_root)
        except FileNotFoundError:
            return []
        staged: list[StagedArtifact] = []
        for ticket_dir in await asyncio.to_thread(lambda: list(temp_root.iterdir())):
            info = await asyncio.to_thread(ticket_dir.lstat)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                continue
            try:
                ticket = UploadTicket(ticket_dir.name)
            except ValueError:
                continue
            for path in await asyncio.to_thread(lambda: list(ticket_dir.iterdir())):
                try:
                    identity = await asyncio.to_thread(_identity, path)
                except (FileNotFoundError, ArtifactStoreError):
                    continue
                path_info = await asyncio.to_thread(path.lstat)
                staged.append(
                    StagedArtifact(
                        ticket=ticket,
                        path=str(path),
                        identity=identity,
                        modified_at=path_info.st_mtime,
                    )
                )
        return staged

    def processing_path(self, key: ArtifactKey) -> Path:
        return self._path(key)


def publish_file_sync(
    source: Path,
    destination: Path,
) -> None:
    root = destination.parent
    while root.parent != root and not root.exists():
        root = root.parent
    store = FileSystemArtifactStore(root)
    relative = destination.relative_to(root).as_posix()
    store._publish_path_sync(source, ArtifactKey(relative), _identity(source))
