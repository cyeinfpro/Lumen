"""Safe video artifact accounting and deletion lifecycle."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .video_storage_accounting import (
    VIDEO_STORAGE_CLEANUP_METADATA_KEY,
    VIDEO_STORAGE_MAX_ISSUES,
    VIDEO_VARIANT_METADATA_KEYS,
    VideoArtifactCleanupResult,
    VideoArtifactInspection,
    clear_video_storage_cleanup_state,
    record_video_storage_cleanup,
    storage_key_parts,
    storage_key_starts_with,
    video_reference_declared_quota_contribution,
    video_reference_derived_variant_bytes,
    video_reference_quota_contribution,
    video_reference_variant_quota_bytes,
)

VIDEO_UPLOAD_ADOPTION_MARKER_DIRECTORY = ".lumen-video-upload-reconciliation"
VIDEO_UPLOAD_ADOPTION_MIN_AGE_SECONDS = 3600.0
_MAX_ADOPTION_MARKER_BYTES = 4096
_MAX_ADOPTION_MARKER_SCAN_MULTIPLIER = 8

__all__ = (
    "VIDEO_STORAGE_CLEANUP_METADATA_KEY",
    "VIDEO_UPLOAD_ADOPTION_MARKER_DIRECTORY",
    "VIDEO_UPLOAD_ADOPTION_MIN_AGE_SECONDS",
    "VideoArtifactCleanupResult",
    "VideoArtifactInspection",
    "VideoStorageLifecycle",
    "VideoUploadAdoptionMarker",
    "clear_video_storage_cleanup_state",
    "record_video_storage_cleanup",
    "video_reference_declared_quota_contribution",
    "video_reference_derived_variant_bytes",
    "video_reference_quota_contribution",
    "video_reference_variant_quota_bytes",
)


@dataclass(frozen=True)
class VideoUploadAdoptionMarker:
    marker_path: Path
    video_id: str
    user_id: str
    storage_key: str
    sha256: str
    size_bytes: int
    device: int
    inode: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _VideoDescriptor:
    video_id: str
    user_id: str
    owner_generation_id: str | None
    storage_key: str
    poster_storage_key: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_video(cls, video: Any) -> _VideoDescriptor:
        metadata = (
            dict(video.metadata_jsonb)
            if isinstance(getattr(video, "metadata_jsonb", None), dict)
            else {}
        )
        owner_generation_id = getattr(video, "owner_generation_id", None)
        return cls(
            video_id=str(video.id),
            user_id=str(video.user_id),
            owner_generation_id=(
                str(owner_generation_id) if owner_generation_id else None
            ),
            storage_key=str(getattr(video, "storage_key", "") or ""),
            poster_storage_key=(
                str(getattr(video, "poster_storage_key", "") or "") or None
            ),
            metadata=metadata,
        )


@dataclass(frozen=True)
class _ArtifactPlan:
    root_parts: tuple[str, ...]
    primary_relative_parts: tuple[str, ...]
    issues: tuple[str, ...]


def _append_issue(issues: list[str], value: str) -> None:
    if len(issues) >= VIDEO_STORAGE_MAX_ISSUES or value in issues:
        return
    issues.append(value)


def _artifact_plan(descriptor: _VideoDescriptor) -> _ArtifactPlan:
    issues: list[str] = []
    primary_parts = storage_key_parts(descriptor.storage_key)
    if primary_parts is None:
        return _ArtifactPlan((), (), ("invalid_primary_storage_key",))

    reference_root = (
        "u",
        descriptor.user_id,
        "vref",
        descriptor.video_id,
    )
    generation_root = (
        "u",
        descriptor.user_id,
        "v",
        descriptor.owner_generation_id or "",
    )
    workflow_run_id = descriptor.metadata.get("workflow_run_id")
    storyboard_prefix = (
        "u",
        descriptor.user_id,
        "storyboards",
        str(workflow_run_id or ""),
        "assembly",
    )

    if storage_key_starts_with(primary_parts, reference_root):
        root_parts = reference_root
    elif descriptor.owner_generation_id and storage_key_starts_with(
        primary_parts,
        generation_root,
    ):
        root_parts = generation_root
    elif (
        descriptor.metadata.get("workflow_type") == "storyboard"
        and workflow_run_id
        and storage_key_starts_with(primary_parts, storyboard_prefix)
        and len(primary_parts) >= len(storyboard_prefix) + 2
    ):
        root_parts = primary_parts[:-1]
    else:
        return _ArtifactPlan((), (), ("unowned_primary_storage_key",))

    candidate_keys: list[tuple[str, str]] = [
        ("primary", descriptor.storage_key),
    ]
    if descriptor.poster_storage_key:
        candidate_keys.append(("poster", descriptor.poster_storage_key))
    for metadata_key in VIDEO_VARIANT_METADATA_KEYS:
        raw = descriptor.metadata.get(metadata_key)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            _append_issue(issues, f"invalid_variant_metadata:{metadata_key}")
            continue
        storage_key = raw.get("storage_key")
        if not isinstance(storage_key, str) or not storage_key:
            _append_issue(issues, f"invalid_variant_storage_key:{metadata_key}")
            continue
        candidate_keys.append((metadata_key, storage_key))

    for label, storage_key in candidate_keys:
        parts = storage_key_parts(storage_key)
        if parts is None or not storage_key_starts_with(parts, root_parts):
            _append_issue(issues, f"artifact_outside_owned_root:{label}")

    return _ArtifactPlan(
        root_parts=root_parts,
        primary_relative_parts=primary_parts[len(root_parts) :],
        issues=tuple(issues),
    )


class VideoStorageLifecycle:
    def __init__(self, storage_root: str | Path) -> None:
        self.storage_root = Path(storage_root).resolve()
        self._directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )

    async def inspect(self, video: Any) -> VideoArtifactInspection:
        descriptor = _VideoDescriptor.from_video(video)
        return await asyncio.to_thread(self._inspect_sync, descriptor)

    async def inspect_many(
        self,
        videos: Iterable[Any],
    ) -> dict[str, VideoArtifactInspection]:
        descriptors = [_VideoDescriptor.from_video(video) for video in videos]

        def inspect_all() -> dict[str, VideoArtifactInspection]:
            return {
                descriptor.video_id: self._inspect_sync(descriptor)
                for descriptor in descriptors
            }

        return await asyncio.to_thread(inspect_all)

    async def cleanup(self, video: Any) -> VideoArtifactCleanupResult:
        descriptor = _VideoDescriptor.from_video(video)
        return await asyncio.to_thread(self._cleanup_sync, descriptor)

    async def record_upload_adoption_pending(
        self,
        *,
        video_id: str,
        user_id: str,
        storage_key: str,
        sha256: str,
    ) -> VideoUploadAdoptionMarker:
        return await asyncio.to_thread(
            self._record_upload_adoption_pending_sync,
            video_id=video_id,
            user_id=user_id,
            storage_key=storage_key,
            sha256=sha256,
        )

    async def clear_upload_adoption_marker(
        self,
        marker: VideoUploadAdoptionMarker,
    ) -> None:
        await asyncio.to_thread(
            self._clear_upload_adoption_marker_sync,
            marker,
        )

    async def aged_upload_adoption_markers(
        self,
        *,
        user_id: str,
        minimum_age_seconds: float = VIDEO_UPLOAD_ADOPTION_MIN_AGE_SECONDS,
        limit: int = 16,
    ) -> tuple[VideoUploadAdoptionMarker, ...]:
        return await asyncio.to_thread(
            self._aged_upload_adoption_markers_sync,
            user_id=user_id,
            minimum_age_seconds=minimum_age_seconds,
            limit=limit,
        )

    async def discard_unadopted_upload(
        self,
        marker: VideoUploadAdoptionMarker,
    ) -> bool:
        return await asyncio.to_thread(
            self._discard_unadopted_upload_sync,
            marker,
        )

    async def upload_artifact_matches(
        self,
        *,
        video_id: str,
        user_id: str,
        storage_key: str,
        sha256: str,
        size_bytes: int,
    ) -> bool:
        return await asyncio.to_thread(
            self._upload_artifact_matches_sync,
            video_id=video_id,
            user_id=user_id,
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=size_bytes,
        )

    @staticmethod
    def _safe_identifier(value: str) -> bool:
        return (
            bool(value)
            and value not in {".", ".."}
            and "/" not in value
            and "\\" not in value
            and "\x00" not in value
        )

    def _adoption_marker_path(
        self,
        *,
        video_id: str,
        user_id: str,
        token: str,
    ) -> Path:
        if not self._safe_identifier(video_id) or not self._safe_identifier(user_id):
            raise ValueError("invalid video upload adoption marker identity")
        if not self._safe_identifier(token):
            raise ValueError("invalid video upload adoption marker token")
        return (
            self.storage_root
            / VIDEO_UPLOAD_ADOPTION_MARKER_DIRECTORY
            / user_id
            / f"{video_id}.{token}.json"
        )

    def _record_upload_adoption_pending_sync(
        self,
        *,
        video_id: str,
        user_id: str,
        storage_key: str,
        sha256: str,
    ) -> VideoUploadAdoptionMarker:
        parts = storage_key_parts(storage_key)
        expected_prefix = ("u", user_id, "vref", video_id)
        if parts is None or not storage_key_starts_with(parts, expected_prefix):
            raise ValueError("video upload adoption storage key is outside its owner")
        path = self.storage_root.joinpath(*parts)
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("video upload adoption artifact is not a regular file")
        marker_path = self._adoption_marker_path(
            video_id=video_id,
            user_id=user_id,
            token=secrets.token_hex(12),
        )
        self._mkdir_parents_durable(marker_path.parent)
        payload = {
            "version": 1,
            "video_id": video_id,
            "user_id": user_id,
            "storage_key": storage_key,
            "sha256": sha256,
            "size_bytes": int(info.st_size),
            "device": int(info.st_dev),
            "inode": int(info.st_ino),
            "modified_ns": int(info.st_mtime_ns),
            "changed_ns": int(info.st_ctime_ns),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_ADOPTION_MARKER_BYTES:
            raise ValueError("video upload adoption marker is too large")
        temporary = marker_path.with_name(
            f".{marker_path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, marker_path)
            self._fsync_directory(marker_path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return VideoUploadAdoptionMarker(
            marker_path=marker_path,
            video_id=video_id,
            user_id=user_id,
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=max(0, int(info.st_size)),
            device=int(info.st_dev),
            inode=int(info.st_ino),
            modified_ns=int(info.st_mtime_ns),
            changed_ns=int(info.st_ctime_ns),
        )

    def _clear_upload_adoption_marker_sync(
        self,
        marker: VideoUploadAdoptionMarker,
    ) -> None:
        expected_parent = (
            self.storage_root / VIDEO_UPLOAD_ADOPTION_MARKER_DIRECTORY / marker.user_id
        )
        if marker.marker_path.parent != expected_parent:
            raise ValueError("video upload adoption marker path is invalid")
        marker.marker_path.unlink(missing_ok=True)
        try:
            marker.marker_path.parent.rmdir()
        except OSError:
            pass

    def _parse_adoption_marker(
        self,
        marker_path: Path,
        *,
        user_id: str,
    ) -> VideoUploadAdoptionMarker | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(marker_path, flags)
        except OSError:
            return None
        try:
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size > _MAX_ADOPTION_MARKER_BYTES
                ):
                    return None
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    raw = source.read(_MAX_ADOPTION_MARKER_BYTES + 1)
                if len(raw) > _MAX_ADOPTION_MARKER_BYTES:
                    return None
            except OSError:
                return None
        finally:
            os.close(descriptor)
        try:
            payload = json.loads(raw.decode("utf-8"))
            video_id = str(payload["video_id"])
            marker_user_id = str(payload["user_id"])
            storage_key = str(payload["storage_key"])
            sha256 = str(payload["sha256"])
            size_bytes = int(payload["size_bytes"])
            device = int(payload["device"])
            inode = int(payload["inode"])
            modified_ns = int(payload["modified_ns"])
            changed_ns = int(payload["changed_ns"])
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return None
        if (
            marker_user_id != user_id
            or not self._safe_identifier(video_id)
            or len(sha256) != 64
        ):
            return None
        parts = storage_key_parts(storage_key)
        if parts is None or not storage_key_starts_with(
            parts,
            ("u", user_id, "vref", video_id),
        ):
            return None
        return VideoUploadAdoptionMarker(
            marker_path=marker_path,
            video_id=video_id,
            user_id=user_id,
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=max(0, size_bytes),
            device=device,
            inode=inode,
            modified_ns=modified_ns,
            changed_ns=changed_ns,
        )

    def _aged_upload_adoption_markers_sync(
        self,
        *,
        user_id: str,
        minimum_age_seconds: float,
        limit: int,
    ) -> tuple[VideoUploadAdoptionMarker, ...]:
        if not self._safe_identifier(user_id) or limit <= 0:
            return ()
        directory = self.storage_root / VIDEO_UPLOAD_ADOPTION_MARKER_DIRECTORY / user_id
        cutoff = time.time() - max(0.0, minimum_age_seconds)
        candidates: list[tuple[int, VideoUploadAdoptionMarker]] = []
        scanned = 0
        try:
            entries = os.scandir(directory)
        except OSError:
            return ()
        with entries:
            for entry in entries:
                scanned += 1
                if scanned > limit * _MAX_ADOPTION_MARKER_SCAN_MULTIPLIER:
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if (
                    stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_mtime > cutoff
                ):
                    continue
                marker = self._parse_adoption_marker(
                    Path(entry.path),
                    user_id=user_id,
                )
                if marker is not None:
                    candidates.append((int(info.st_mtime_ns), marker))
        candidates.sort(key=lambda item: item[0])
        return tuple(marker for _mtime, marker in candidates[:limit])

    def _discard_unadopted_upload_sync(
        self,
        marker: VideoUploadAdoptionMarker,
    ) -> bool:
        parts = storage_key_parts(marker.storage_key)
        if parts is None or not storage_key_starts_with(
            parts,
            ("u", marker.user_id, "vref", marker.video_id),
        ):
            return False
        path = self.storage_root.joinpath(*parts)
        removed = False
        try:
            info = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if (
            info is not None
            and stat.S_ISREG(info.st_mode)
            and int(info.st_size) == marker.size_bytes
            and int(info.st_dev) == marker.device
            and int(info.st_ino) == marker.inode
            and int(info.st_mtime_ns) == marker.modified_ns
            and int(info.st_ctime_ns) == marker.changed_ns
        ):
            path.unlink()
            removed = True
            try:
                path.parent.rmdir()
            except OSError:
                pass
        marker.marker_path.unlink(missing_ok=True)
        try:
            marker.marker_path.parent.rmdir()
        except OSError:
            pass
        return removed

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

    def _upload_artifact_matches_sync(
        self,
        *,
        video_id: str,
        user_id: str,
        storage_key: str,
        sha256: str,
        size_bytes: int,
    ) -> bool:
        parts = storage_key_parts(storage_key)
        if (
            not self._safe_identifier(video_id)
            or not self._safe_identifier(user_id)
            or parts is None
            or not storage_key_starts_with(
                parts,
                ("u", user_id, "vref", video_id),
            )
            or len(sha256) != 64
        ):
            return False
        path = self.storage_root.joinpath(*parts)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return False
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or int(info.st_size) != max(
                0, int(size_bytes)
            ):
                return False
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            return digest.hexdigest() == sha256.lower()
        except OSError:
            return False
        finally:
            os.close(descriptor)

    def _open_owned_root(
        self,
        plan: _ArtifactPlan,
        issues: list[str],
    ) -> int | None:
        try:
            current_fd = os.open(self.storage_root, self._directory_flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            _append_issue(
                issues,
                f"storage_root_unavailable:{exc.errno or errno.EIO}",
            )
            return None
        try:
            for component in plan.root_parts:
                try:
                    next_fd = os.open(
                        component,
                        self._directory_flags,
                        dir_fd=current_fd,
                    )
                except FileNotFoundError:
                    os.close(current_fd)
                    return None
                except OSError as exc:
                    _append_issue(
                        issues,
                        f"unsafe_owned_root:{component}:{exc.errno or errno.EIO}",
                    )
                    os.close(current_fd)
                    return None
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    def _scan_directory(
        self,
        directory_fd: int,
        *,
        relative_parts: tuple[str, ...],
        primary_relative_parts: tuple[str, ...],
        issues: list[str],
    ) -> tuple[int, int, bool, int]:
        artifact_count = 0
        bytes_on_disk = 0
        primary_present = False
        primary_size_bytes = 0
        try:
            entries = os.scandir(directory_fd)
        except OSError as exc:
            _append_issue(
                issues,
                f"artifact_scan_failed:{'/'.join(relative_parts)}:{exc.errno or errno.EIO}",
            )
            return 0, 0, False, 0
        with entries:
            for entry in entries:
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
                        issues,
                        f"artifact_stat_failed:{'/'.join(child_relative)}:{exc.errno or errno.EIO}",
                    )
                    continue
                if stat.S_ISDIR(info.st_mode):
                    try:
                        child_fd = os.open(
                            entry.name,
                            self._directory_flags,
                            dir_fd=directory_fd,
                        )
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        _append_issue(
                            issues,
                            f"unsafe_artifact_directory:{'/'.join(child_relative)}:{exc.errno or errno.EIO}",
                        )
                        continue
                    try:
                        child_count, child_bytes, child_primary, child_primary_size = (
                            self._scan_directory(
                                child_fd,
                                relative_parts=child_relative,
                                primary_relative_parts=primary_relative_parts,
                                issues=issues,
                            )
                        )
                    finally:
                        os.close(child_fd)
                    artifact_count += child_count
                    bytes_on_disk += child_bytes
                    primary_present = primary_present or child_primary
                    primary_size_bytes = max(
                        primary_size_bytes,
                        child_primary_size,
                    )
                    continue
                artifact_count += 1
                bytes_on_disk += max(0, int(info.st_size))
                if child_relative == primary_relative_parts and stat.S_ISREG(
                    info.st_mode
                ):
                    primary_present = True
                    primary_size_bytes = max(0, int(info.st_size))
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                    _append_issue(
                        issues,
                        f"unsafe_artifact_type:{'/'.join(child_relative)}",
                    )
        return (
            artifact_count,
            bytes_on_disk,
            primary_present,
            primary_size_bytes,
        )

    def _inspection_for_plan(
        self,
        plan: _ArtifactPlan,
    ) -> VideoArtifactInspection:
        issues = list(plan.issues)
        if not plan.root_parts:
            return VideoArtifactInspection(
                artifact_count=0,
                bytes_on_disk=0,
                primary_present=False,
                primary_size_bytes=0,
                issues=tuple(issues),
            )
        directory_fd = self._open_owned_root(plan, issues)
        if directory_fd is None:
            return VideoArtifactInspection(
                artifact_count=0,
                bytes_on_disk=0,
                primary_present=False,
                primary_size_bytes=0,
                issues=tuple(issues),
            )
        try:
            (
                artifact_count,
                bytes_on_disk,
                primary_present,
                primary_size_bytes,
            ) = self._scan_directory(
                directory_fd,
                relative_parts=(),
                primary_relative_parts=plan.primary_relative_parts,
                issues=issues,
            )
        finally:
            os.close(directory_fd)
        return VideoArtifactInspection(
            artifact_count=artifact_count,
            bytes_on_disk=bytes_on_disk,
            primary_present=primary_present,
            primary_size_bytes=primary_size_bytes,
            issues=tuple(issues),
        )

    def _inspect_sync(self, descriptor: _VideoDescriptor) -> VideoArtifactInspection:
        return self._inspection_for_plan(_artifact_plan(descriptor))

    def _unlink_entry(self, name: str, *, directory_fd: int) -> None:
        os.unlink(name, dir_fd=directory_fd)

    def _delete_directory_contents(
        self,
        directory_fd: int,
        *,
        relative_parts: tuple[str, ...],
        errors: list[str],
    ) -> None:
        try:
            entries = os.scandir(directory_fd)
        except OSError as exc:
            _append_issue(
                errors,
                f"artifact_scan_failed:{'/'.join(relative_parts)}:{exc.errno or errno.EIO}",
            )
            return
        with entries:
            for entry in entries:
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
                            self._directory_flags,
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
                        self._delete_directory_contents(
                            child_fd,
                            relative_parts=child_relative,
                            errors=errors,
                        )
                    finally:
                        os.close(child_fd)
                    continue
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                    _append_issue(
                        errors,
                        f"unsafe_artifact_type:{'/'.join(child_relative)}",
                    )
                    continue
                try:
                    self._unlink_entry(entry.name, directory_fd=directory_fd)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    _append_issue(
                        errors,
                        f"artifact_unlink_failed:{'/'.join(child_relative)}:{exc.errno or errno.EIO}",
                    )

    def _cleanup_sync(self, descriptor: _VideoDescriptor) -> VideoArtifactCleanupResult:
        plan = _artifact_plan(descriptor)
        before = self._inspection_for_plan(plan)
        errors: list[str] = []
        if plan.root_parts:
            open_issues: list[str] = []
            directory_fd = self._open_owned_root(plan, open_issues)
            errors.extend(open_issues)
            if directory_fd is not None:
                try:
                    self._delete_directory_contents(
                        directory_fd,
                        relative_parts=(),
                        errors=errors,
                    )
                finally:
                    os.close(directory_fd)
        remaining = self._inspection_for_plan(plan)
        all_errors = list(dict.fromkeys((*plan.issues, *errors, *remaining.issues)))
        complete = not remaining.retained and not all_errors
        return VideoArtifactCleanupResult(
            complete=complete,
            deleted_artifacts=max(
                0,
                before.artifact_count - remaining.artifact_count,
            ),
            remaining=remaining,
            errors=tuple(all_errors[:VIDEO_STORAGE_MAX_ISSUES]),
        )
