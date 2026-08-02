"""Filesystem locking and quarantine cleanup for video artifacts."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import stat
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .video_storage_accounting import (
    VIDEO_STORAGE_MAX_ISSUES,
    VideoArtifactCleanupResult,
    VideoArtifactInspection,
)

VIDEO_CLEANUP_QUARANTINE_DIRECTORY = ".lumen-video-cleanup"
VIDEO_REFERENCE_LOCK_DIRECTORY = ".lumen-video-reference-locks"
VIDEO_REFERENCE_LOCK_TIMEOUT_SECONDS = 30.0
VIDEO_STORAGE_MAX_SCAN_ENTRIES = 2048
VIDEO_STORAGE_MAX_SCAN_DEPTH = 32


@dataclass(frozen=True)
class VideoDetachedCleanup:
    path: Path | None
    issues: tuple[str, ...] = ()


class VideoReferenceStorageLockTimeout(TimeoutError):
    pass


def _append_issue(issues: list[str], value: str) -> None:
    if len(issues) >= VIDEO_STORAGE_MAX_ISSUES or value in issues:
        return
    issues.append(value)


class VideoStorageCleanupManager:
    def __init__(self, storage_root: Path, directory_flags: int) -> None:
        self.storage_root = storage_root
        self.directory_flags = directory_flags

    @staticmethod
    def _safe_identifier(value: str) -> bool:
        return (
            bool(value)
            and value not in {".", ".."}
            and "/" not in value
            and "\\" not in value
            and "\x00" not in value
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _mkdir_parents_durable(cls, path: Path) -> None:
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent
        for directory in reversed(missing):
            try:
                directory.mkdir()
            except FileExistsError:
                if not directory.is_dir():
                    raise
            else:
                cls._fsync_directory(directory.parent)

    @asynccontextmanager
    async def reference_mutation_lock(
        self,
        *,
        user_id: str,
        video_id: str,
        timeout_seconds: float = VIDEO_REFERENCE_LOCK_TIMEOUT_SECONDS,
    ) -> AsyncIterator[None]:
        if not self._safe_identifier(user_id) or not self._safe_identifier(video_id):
            raise ValueError("invalid reference video lock identity")
        lock_dir = self.storage_root / VIDEO_REFERENCE_LOCK_DIRECTORY / user_id
        self._mkdir_parents_durable(lock_dir)
        lock_path = lock_dir / f"{video_id}.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(0.001, timeout_seconds)
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise VideoReferenceStorageLockTimeout(
                            "reference video storage lock timed out"
                        )
                    await asyncio.sleep(0.05)
            yield
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def detach_cleanup(
        self,
        *,
        user_id: str,
        video_id: str,
        root_parts: tuple[str, ...],
        token: str,
        issues: tuple[str, ...],
    ) -> VideoDetachedCleanup:
        if not self._safe_identifier(token):
            raise ValueError("invalid video cleanup token")
        if not root_parts:
            return VideoDetachedCleanup(path=None, issues=issues)
        source = self.storage_root.joinpath(*root_parts)
        quarantine_parent = (
            self.storage_root
            / VIDEO_CLEANUP_QUARANTINE_DIRECTORY
            / user_id
            / video_id
        )
        quarantine = quarantine_parent / token
        if quarantine.exists():
            return VideoDetachedCleanup(path=quarantine, issues=issues)
        try:
            info = os.stat(source, follow_symlinks=False)
        except FileNotFoundError:
            return VideoDetachedCleanup(path=None, issues=issues)
        if not stat.S_ISDIR(info.st_mode):
            return VideoDetachedCleanup(
                path=None,
                issues=tuple((*issues, "unsafe_cleanup_root")),
            )
        self._mkdir_parents_durable(quarantine_parent)
        os.rename(source, quarantine)
        self._fsync_directory(source.parent)
        self._fsync_directory(quarantine_parent)
        return VideoDetachedCleanup(path=quarantine, issues=issues)

    def detached_cleanup(
        self,
        *,
        user_id: str,
        video_id: str,
        token: str,
    ) -> VideoDetachedCleanup:
        if not all(
            self._safe_identifier(value) for value in (user_id, video_id, token)
        ):
            raise ValueError("invalid detached video cleanup identity")
        path = (
            self.storage_root
            / VIDEO_CLEANUP_QUARANTINE_DIRECTORY
            / user_id
            / video_id
            / token
        )
        return VideoDetachedCleanup(path=path if path.exists() else None)

    async def cleanup_detached(
        self,
        detached: VideoDetachedCleanup,
        *,
        unlink_entry: Callable[..., None],
    ) -> VideoArtifactCleanupResult:
        return await asyncio.to_thread(
            self._cleanup_detached_sync,
            detached,
            unlink_entry,
        )

    def _scan_directory(
        self,
        directory_fd: int,
        *,
        issues: list[str],
        scanned: list[int] | None = None,
        depth: int = 0,
    ) -> tuple[int, int]:
        if scanned is None:
            scanned = [0]
        if depth > VIDEO_STORAGE_MAX_SCAN_DEPTH:
            _append_issue(issues, "artifact_scan_depth_limit")
            return 0, 0
        artifact_count = 0
        bytes_on_disk = 0
        try:
            entries = os.scandir(directory_fd)
        except OSError as exc:
            _append_issue(
                issues,
                f"artifact_scan_failed:{exc.errno or errno.EIO}",
            )
            return 0, 0
        with entries:
            for entry in entries:
                if scanned[0] >= VIDEO_STORAGE_MAX_SCAN_ENTRIES:
                    _append_issue(issues, "artifact_scan_entry_limit")
                    break
                scanned[0] += 1
                try:
                    info = os.stat(
                        entry.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    _append_issue(
                        issues,
                        f"artifact_stat_failed:{entry.name}:{exc.errno or errno.EIO}",
                    )
                    continue
                if stat.S_ISDIR(info.st_mode):
                    try:
                        child_fd = os.open(
                            entry.name,
                            self.directory_flags,
                            dir_fd=directory_fd,
                        )
                    except OSError as exc:
                        _append_issue(
                            issues,
                            f"unsafe_artifact_directory:{entry.name}:{exc.errno or errno.EIO}",
                        )
                        continue
                    try:
                        child_count, child_bytes = self._scan_directory(
                            child_fd,
                            issues=issues,
                            scanned=scanned,
                            depth=depth + 1,
                        )
                    finally:
                        os.close(child_fd)
                    artifact_count += child_count
                    bytes_on_disk += child_bytes
                    continue
                artifact_count += 1
                bytes_on_disk += max(0, int(info.st_size))
        return artifact_count, bytes_on_disk

    def _delete_directory_contents(
        self,
        directory_fd: int,
        *,
        relative_parts: tuple[str, ...],
        errors: list[str],
        scanned: list[int],
        unlink_entry: Callable[..., None],
        depth: int = 0,
    ) -> int:
        if depth > VIDEO_STORAGE_MAX_SCAN_DEPTH:
            _append_issue(errors, "artifact_delete_depth_limit")
            return 0
        deleted = 0
        try:
            entries = os.scandir(directory_fd)
        except OSError as exc:
            _append_issue(
                errors,
                f"artifact_scan_failed:{'/'.join(relative_parts)}:{exc.errno or errno.EIO}",
            )
            return deleted
        with entries:
            for entry in entries:
                if scanned[0] >= VIDEO_STORAGE_MAX_SCAN_ENTRIES:
                    _append_issue(errors, "artifact_delete_entry_limit")
                    break
                scanned[0] += 1
                child_relative = (*relative_parts, entry.name)
                try:
                    info = os.stat(
                        entry.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    _append_issue(
                        errors,
                        f"artifact_stat_failed:{'/'.join(child_relative)}:{exc.errno or errno.EIO}",
                    )
                    continue
                if stat.S_ISDIR(info.st_mode):
                    try:
                        child_fd = os.open(
                            entry.name,
                            self.directory_flags,
                            dir_fd=directory_fd,
                        )
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        _append_issue(
                            errors,
                            f"unsafe_artifact_directory:{'/'.join(child_relative)}:{exc.errno or errno.EIO}",
                        )
                        continue
                    try:
                        deleted += self._delete_directory_contents(
                            child_fd,
                            relative_parts=child_relative,
                            errors=errors,
                            scanned=scanned,
                            unlink_entry=unlink_entry,
                            depth=depth + 1,
                        )
                    finally:
                        os.close(child_fd)
                    try:
                        os.rmdir(entry.name, dir_fd=directory_fd)
                    except OSError:
                        pass
                    continue
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                    _append_issue(
                        errors,
                        f"unsafe_artifact_type:{'/'.join(child_relative)}",
                    )
                    continue
                try:
                    unlink_entry(entry.name, directory_fd=directory_fd)
                    deleted += 1
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    _append_issue(
                        errors,
                        f"artifact_unlink_failed:{'/'.join(child_relative)}:{exc.errno or errno.EIO}",
                    )
        return deleted

    def _cleanup_detached_sync(
        self,
        detached: VideoDetachedCleanup,
        unlink_entry: Callable[..., None],
    ) -> VideoArtifactCleanupResult:
        if detached.path is None:
            remaining = VideoArtifactInspection(
                artifact_count=0,
                bytes_on_disk=0,
                primary_present=False,
                primary_size_bytes=0,
                issues=detached.issues,
            )
            return VideoArtifactCleanupResult(
                complete=not detached.issues,
                deleted_artifacts=0,
                remaining=remaining,
                errors=detached.issues,
            )
        expected_root = self.storage_root / VIDEO_CLEANUP_QUARANTINE_DIRECTORY
        try:
            detached.path.relative_to(expected_root)
        except ValueError as exc:
            raise ValueError("detached cleanup path is outside quarantine") from exc
        errors = list(detached.issues)
        deleted = 0
        try:
            directory_fd = os.open(detached.path, self.directory_flags)
        except FileNotFoundError:
            directory_fd = None
        except OSError as exc:
            directory_fd = None
            _append_issue(errors, f"cleanup_quarantine_open_failed:{exc.errno or errno.EIO}")
        if directory_fd is not None:
            try:
                deleted = self._delete_directory_contents(
                    directory_fd,
                    relative_parts=(),
                    errors=errors,
                    scanned=[0],
                    unlink_entry=unlink_entry,
                )
            finally:
                os.close(directory_fd)
        try:
            detached.path.rmdir()
        except OSError:
            pass
        remaining_count = 0
        remaining_bytes = 0
        if detached.path.exists():
            try:
                directory_fd = os.open(detached.path, self.directory_flags)
            except OSError as exc:
                _append_issue(
                    errors,
                    f"cleanup_quarantine_reopen_failed:{exc.errno or errno.EIO}",
                )
            else:
                try:
                    remaining_count, remaining_bytes = self._scan_directory(
                        directory_fd,
                        issues=errors,
                    )
                finally:
                    os.close(directory_fd)
        remaining = VideoArtifactInspection(
            artifact_count=remaining_count,
            bytes_on_disk=remaining_bytes,
            primary_present=False,
            primary_size_bytes=0,
            issues=tuple(errors[:VIDEO_STORAGE_MAX_ISSUES]),
        )
        return VideoArtifactCleanupResult(
            complete=not detached.path.exists() and not errors,
            deleted_artifacts=deleted,
            remaining=remaining,
            errors=tuple(errors[:VIDEO_STORAGE_MAX_ISSUES]),
        )


__all__ = (
    "VIDEO_CLEANUP_QUARANTINE_DIRECTORY",
    "VIDEO_REFERENCE_LOCK_DIRECTORY",
    "VIDEO_REFERENCE_LOCK_TIMEOUT_SECONDS",
    "VIDEO_STORAGE_MAX_SCAN_DEPTH",
    "VIDEO_STORAGE_MAX_SCAN_ENTRIES",
    "VideoDetachedCleanup",
    "VideoReferenceStorageLockTimeout",
    "VideoStorageCleanupManager",
)
