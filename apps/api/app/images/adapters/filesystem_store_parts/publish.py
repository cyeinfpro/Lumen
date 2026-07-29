from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import shutil
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ...domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    PublishedArtifact,
    StagedArtifact,
)
from ..filesystem_staging import ArtifactIdentityMismatch, ArtifactStoreError
from .objects import CHUNK_SIZE, artifact_identity, fsync_directory


class ArtifactConflict(FileExistsError):
    pass


PUBLISH_LOCK_FILE = ".artifact-publish.lock"
LINK_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EXDEV,
        errno.EPERM,
        errno.EACCES,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
)


@contextmanager
def publish_directory_lock(
    destination: Path,
    *,
    exclusive: bool,
) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_path = destination.parent / PUBLISH_LOCK_FILE
    fd = os.open(lock_path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactStoreError("artifact publish lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class FileSystemPublishMixin:
    @staticmethod
    def _copy_exclusive(source: Path, destination: Path) -> None:
        destination_created = False
        fd: int | None = None
        with publish_directory_lock(destination, exclusive=True):
            try:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                destination_created = True
                with os.fdopen(fd, "wb") as dst:
                    fd = None
                    with source.open("rb") as src:
                        shutil.copyfileobj(src, dst, length=CHUNK_SIZE)
                        dst.flush()
                        os.fsync(dst.fileno())
            except BaseException:
                if fd is not None:
                    os.close(fd)
                if destination_created:
                    destination.unlink(missing_ok=True)
                raise

    def _resolve_existing_destination(
        self,
        destination: Path,
        key: ArtifactKey,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact | None:
        with publish_directory_lock(destination, exclusive=False):
            try:
                existing = artifact_identity(destination)
            except FileNotFoundError:
                return None
        if existing.sha256 == expected.sha256 and (
            existing.size_bytes == expected.size_bytes
        ):
            self._record_publish_idempotent_winner("filesystem")
            return PublishedArtifact(key=key, identity=existing, created=False)
        self._record_publish_conflict("filesystem")
        raise ArtifactConflict(f"artifact destination conflict for key={key.value}")

    def _publish_path_sync(
        self,
        source: Path,
        key: ArtifactKey,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        source_identity = artifact_identity(source)
        if not expected.matches(source_identity):
            raise ArtifactIdentityMismatch("source artifact identity changed")
        destination = self._path(key, create_parent=True)
        while True:
            existing = self._resolve_existing_destination(destination, key, expected)
            if existing is not None:
                return existing
            try:
                os.link(source, destination)
            except FileExistsError:
                continue
            except OSError as exc:
                if exc.errno not in LINK_UNSUPPORTED_ERRNOS:
                    raise
                try:
                    self._copy_exclusive(source, destination)
                except FileExistsError:
                    continue
            break
        fsync_directory(destination.parent)
        published_identity = artifact_identity(destination)
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


def publish_file(
    source: Path,
    destination: Path,
    *,
    store_factory: Callable[[Path], Any],
) -> None:
    root = destination.parent
    while root.parent != root and not root.exists():
        root = root.parent
    store = store_factory(root)
    relative = destination.relative_to(root).as_posix()
    store._publish_path_sync(  # noqa: SLF001 - compatibility sync entry point
        source,
        ArtifactKey(relative),
        artifact_identity(source),
    )
