from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import shutil
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    contextmanager,
)
from pathlib import Path
from typing import Any

from ...domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    PublishedArtifact,
    StagedArtifact,
)
from ..filesystem_staging import ArtifactIdentityMismatch, ArtifactStoreError
from .atomic_publish import install_file_noreplace
from .objects import CHUNK_SIZE, artifact_identity, fsync_directory


class ArtifactConflict(FileExistsError):
    pass


PUBLISH_LOCK_FILE = ".artifact-publish.lock"
ARTIFACT_LIFECYCLE_LOCK_FILE = ".artifact-lifecycle.lock"
PUBLISH_TEMP_PREFIX = ".artifact-publish-"
PUBLISH_TEMP_SUFFIX = ".tmp"
LINK_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EXDEV,
        errno.EPERM,
        errno.EACCES,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
)


def _cleanup_publish_temps_locked(destination: Path) -> None:
    removed = False
    with os.scandir(destination.parent) as entries:
        for entry in entries:
            if not (
                entry.name.startswith(PUBLISH_TEMP_PREFIX)
                and entry.name.endswith(PUBLISH_TEMP_SUFFIX)
            ):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ArtifactStoreError("artifact publish temp entry is unsafe")
            try:
                Path(entry.path).unlink()
            except FileNotFoundError:
                continue
            removed = True
    if removed:
        fsync_directory(destination.parent)


def _open_lock_file(
    destination: Path,
    lock_file: str,
    *,
    unsafe_message: str,
) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(destination.parent / lock_file, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactStoreError(unsafe_message)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def publish_directory_lock(
    destination: Path,
    *,
    exclusive: bool,
) -> Iterator[None]:
    fd = _open_lock_file(
        destination,
        PUBLISH_LOCK_FILE,
        unsafe_message="artifact publish lock is unsafe",
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class _ArtifactLifecycleLock:
    def __init__(
        self,
        destination: Path,
        *,
        timeout_seconds: float | None,
    ) -> None:
        self.destination = destination
        self.timeout_seconds = timeout_seconds
        self.fd: int | None = None

    def acquire(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise TimeoutError("artifact lifecycle lock timed out")
        fd = _open_lock_file(
            self.destination,
            ARTIFACT_LIFECYCLE_LOCK_FILE,
            unsafe_message="artifact lifecycle lock is unsafe",
        )
        try:
            if self.timeout_seconds is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + self.timeout_seconds
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                "artifact lifecycle lock timed out"
                            ) from exc
                        time.sleep(min(0.01, remaining))
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def release(self) -> None:
        fd = self.fd
        if fd is None:
            return
        self.fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@asynccontextmanager
async def artifact_lifecycle_lock(
    destination: Path,
    *,
    timeout_seconds: float | None = None,
) -> AsyncIterator[None]:
    lock = _ArtifactLifecycleLock(
        destination,
        timeout_seconds=timeout_seconds,
    )
    acquire_task = asyncio.create_task(asyncio.to_thread(lock.acquire))
    try:
        await asyncio.shield(acquire_task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(acquire_task)
        except BaseException:  # noqa: BLE001
            pass
        else:
            lock.release()
        raise
    try:
        yield
    finally:
        lock.release()


class FileSystemPublishMixin:
    def artifact_lifecycle_fence(
        self,
        key: ArtifactKey,
        *,
        timeout_seconds: float | None = None,
    ) -> AbstractAsyncContextManager[None]:
        return artifact_lifecycle_lock(
            self._path(key),  # type: ignore[attr-defined]
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _copy_exclusive(source: Path, destination: Path) -> None:
        fd: int | None = None
        temp_path: Path | None = None
        with publish_directory_lock(destination, exclusive=True):
            try:
                _cleanup_publish_temps_locked(destination)
                fd, raw_temp_path = tempfile.mkstemp(
                    suffix=PUBLISH_TEMP_SUFFIX,
                    prefix=PUBLISH_TEMP_PREFIX,
                    dir=destination.parent,
                )
                temp_path = Path(raw_temp_path)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as dst:
                    fd = None
                    with source.open("rb") as src:
                        shutil.copyfileobj(src, dst, length=CHUNK_SIZE)
                        dst.flush()
                        os.fsync(dst.fileno())
                install_file_noreplace(temp_path, destination)
                temp_path = None
            except BaseException:
                if fd is not None:
                    os.close(fd)
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except FileNotFoundError:
                        pass
                    else:
                        fsync_directory(destination.parent)
                raise

    def _resolve_existing_destination(
        self,
        destination: Path,
        key: ArtifactKey,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact | None:
        with publish_directory_lock(destination, exclusive=True):
            _cleanup_publish_temps_locked(destination)
            try:
                existing = artifact_identity(destination)
            except FileNotFoundError:
                return None
            if existing.sha256 == expected.sha256 and (
                existing.size_bytes == expected.size_bytes
            ):
                fsync_directory(destination.parent)
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
            fsync_directory(destination.parent)
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
