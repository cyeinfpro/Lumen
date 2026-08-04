"""Reference-video upload orchestration."""

from __future__ import annotations

import asyncio
import errno
import secrets
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import storage_capacity as capacity
from lumen_core.capacity_leases import CapacityLeaseLost
from lumen_core.model_base import new_uuid7
from lumen_core.model_entities import Video
from lumen_core.schema_models import VideoUploadOut

from ..services.active_user import lock_db_authenticated_user_snapshot
from ..services.video_storage_capacity import VideoStorageCapacityManager
from ..services.video_storage_lifecycle import (
    VideoReferenceStorageLockTimeout,
    VideoUploadAdoptionMarker,
    VideoStorageLifecycle,
    clear_video_storage_cleanup_state,
    video_reference_quota_contribution,
)
from ..services.video_upload_adoption import (
    VideoUploadAdoption,
    VideoUploadAdoptionProbe,
    clear_adoption_marker_best_effort as _clear_adoption_marker_best_effort,
)
from .video_upload_inventory import (
    ReferenceInventorySnapshot,
    load_reference_inventory as _load_reference_inventory,
    lock_reference_inventory_for_adoption,
)
from .video_upload_planning import (
    ReferenceInventoryChanged as _ReferenceInventoryChanged,
    ReferenceUploadPlan as _ReferenceUploadPlan,
    verify_reusable_reference_upload as _verify_reusable_reference_upload,
)

_REFERENCE_INVENTORY_CLEANUP_PAGE_SIZE = 32
_REFERENCE_UPLOAD_FILENAME_MAX_CHARS = 255
_REFERENCE_UPLOAD_FILENAME_MAX_BYTES = 255
_REFERENCE_UPLOAD_CAS_ATTEMPTS = 3
_STARTED_TASK_CONFIRMATION_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class UploadDependencies:
    reference_upload_ext: Callable[[UploadFile], tuple[str, str]]
    normalize_filename: Callable[[str | None], str]
    inspect_upload: Callable[[UploadFile], Awaitable[tuple[int, str, bytes]]]
    looks_like_video: Callable[[bytes], bool]
    http_error: Callable[..., Exception]
    fs_path: Callable[[str], Path]
    write_new_file_atomic: Callable[[Path, BinaryIO], None]
    unlink_file_if_exists: Callable[[Path], None]
    upload_key: Callable[[str, str, str], str]
    ensure_access_token: Callable[[Video], Any]
    token_expiry: Callable[[], str]
    upload_out: Callable[..., VideoUploadOut]
    storage_capacity: VideoStorageCapacityManager
    storage_lifecycle: VideoStorageLifecycle
    probe_adoption: Callable[..., Awaitable[VideoUploadAdoptionProbe]]
    logger: Any
    max_count: int
    total_max_bytes: int


def _result_rows(result: Any) -> list[Any]:
    scalars = getattr(result, "scalars", None)
    if callable(scalars):
        return list(scalars().all())
    value = result.scalar_one_or_none()
    return [] if value is None else [value]


def _matching_video(rows: list[Any], sha256: str) -> Any | None:
    matches = [row for row in rows if getattr(row, "sha256", None) == sha256]
    return next(
        (row for row in matches if getattr(row, "deleted_at", None) is None),
        matches[0] if matches else None,
    )


def normalize_reference_filename(raw: str | None) -> str:
    value = unicodedata.normalize("NFC", str(raw or "")).strip()
    if not value:
        return "reference-video"
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in value
    ):
        raise ValueError("filename contains control characters")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("filename contains a path separator")
    if len(value) > _REFERENCE_UPLOAD_FILENAME_MAX_CHARS:
        raise ValueError("filename is too long")
    if len(value.encode("utf-8")) > _REFERENCE_UPLOAD_FILENAME_MAX_BYTES:
        raise ValueError("filename is too large")
    return value


def _update_reference_video(
    video: Any,
    *,
    filename: str,
    mime: str,
    size: int,
    sha256: str,
    storage_key: str | None,
    deps: UploadDependencies,
) -> None:
    if storage_key is not None:
        video.storage_key = storage_key
    video.deleted_at = None
    video.mime = mime
    video.size_bytes = size
    video.sha256 = sha256
    video.etag = sha256
    metadata = dict(getattr(video, "metadata_jsonb", None) or {})
    metadata["filename"] = filename
    metadata["source"] = "uploaded_reference"
    video.metadata_jsonb = metadata
    clear_video_storage_cleanup_state(video)
    deps.ensure_access_token(video)


def _ensure_projected_quota(
    *,
    current_count: int,
    current_bytes: int,
    projected_count: int,
    projected_bytes: int,
    deps: UploadDependencies,
) -> None:
    if projected_count > deps.max_count and projected_count > current_count:
        raise deps.http_error(
            "reference_video_quota_exceeded",
            f"reference video limit is {deps.max_count} files",
            429,
        )
    if projected_bytes > deps.total_max_bytes and projected_bytes > current_bytes:
        raise deps.http_error(
            "reference_video_quota_exceeded",
            "reference video storage quota exceeded",
            429,
        )


async def _wait_for_started_task(task: asyncio.Future[Any]) -> Any:
    deadline = time.monotonic() + _STARTED_TASK_CONFIRMATION_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("reference video operation confirmation timed out")
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _cleanup_written_path(
    path: Path,
    *,
    deps: UploadDependencies,
) -> None:
    cleanup = asyncio.ensure_future(asyncio.to_thread(deps.unlink_file_if_exists, path))
    try:
        await _wait_for_started_task(cleanup)
    except BaseException:
        deps.logger.warning(
            "reference video rollback cleanup failed path=%s",
            path,
            exc_info=True,
        )


async def _best_effort_rollback(db: AsyncSession, *, logger: Any) -> None:
    rollback_task = asyncio.ensure_future(db.rollback())
    try:
        await _wait_for_started_task(rollback_task)
    except BaseException:
        logger.warning("reference video rollback confirmation failed", exc_info=True)


async def _record_reference_video_adoption_pending(
    *,
    video_id: str,
    user_id: str,
    storage_key: str,
    written_path: Path | None,
    sha256: str,
    deps: UploadDependencies,
) -> VideoUploadAdoptionMarker | None:
    if written_path is None:
        return None
    try:
        return await deps.storage_lifecycle.record_upload_adoption_pending(
            video_id=video_id,
            user_id=user_id,
            storage_key=storage_key,
            sha256=sha256,
        )
    except BaseException as exc:
        await _cleanup_written_path(written_path, deps=deps)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise deps.http_error(
            "video_upload_reconciliation_unavailable",
            "reference video upload could not enter reconciliation safely",
            503,
        ) from exc


async def _commit_reference_video_transaction(
    db: AsyncSession,
    *,
    marker: VideoUploadAdoptionMarker | None,
    deps: UploadDependencies,
) -> BaseException | None:
    try:
        commit_task = asyncio.ensure_future(db.commit())
        await asyncio.shield(commit_task)
    except asyncio.CancelledError as cancellation:
        try:
            await _wait_for_started_task(commit_task)
        except BaseException as exc:
            return exc
        await _clear_adoption_marker_best_effort(marker=marker, deps=deps)
        raise cancellation
    except BaseException as exc:
        return exc
    return None


async def _probe_reference_video_adoption(
    *,
    video_id: str,
    user_id: str,
    storage_key: str,
    sha256: str,
    size_bytes: int,
    deps: UploadDependencies,
) -> tuple[VideoUploadAdoptionProbe, asyncio.CancelledError | None]:
    try:
        probe = await deps.probe_adoption(
            video_id=video_id,
            user_id=user_id,
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=size_bytes,
        )
        return probe, None
    except asyncio.CancelledError as exc:
        return VideoUploadAdoptionProbe(VideoUploadAdoption.UNKNOWN), exc
    except Exception:
        deps.logger.error(
            "reference video adoption probe failed video_id=%s",
            video_id,
            exc_info=True,
        )
        return VideoUploadAdoptionProbe(VideoUploadAdoption.UNKNOWN), None


async def _refresh_reference_video_best_effort(
    db: AsyncSession,
    *,
    video: Any,
    video_id: str,
    deps: UploadDependencies,
) -> None:
    try:
        await db.refresh(video)
    except Exception:
        deps.logger.warning(
            "reference video refresh failed after commit video_id=%s",
            video_id,
            exc_info=True,
        )


async def _commit_reference_video_with_probe(
    *,
    db: AsyncSession,
    video: Any,
    marker: VideoUploadAdoptionMarker | None,
    written_path: Path | None,
    sha256: str,
    deps: UploadDependencies,
) -> Any:
    video_id = str(video.id)
    user_id = str(video.user_id)
    storage_key = str(video.storage_key)
    size_bytes = int(getattr(video, "size_bytes", 0) or 0)
    commit_error = await _commit_reference_video_transaction(
        db,
        marker=marker,
        deps=deps,
    )

    if commit_error is None:
        await _clear_adoption_marker_best_effort(marker=marker, deps=deps)
        await _refresh_reference_video_best_effort(
            db,
            video=video,
            video_id=video_id,
            deps=deps,
        )
        return video

    await _best_effort_rollback(db, logger=deps.logger)
    probe, probe_cancellation = await _probe_reference_video_adoption(
        video_id=video_id,
        user_id=user_id,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=size_bytes,
        deps=deps,
    )
    if probe.outcome is VideoUploadAdoption.ADOPTED:
        await _clear_adoption_marker_best_effort(marker=marker, deps=deps)
        if isinstance(commit_error, asyncio.CancelledError):
            raise commit_error
        return probe.video or video
    if probe.outcome is VideoUploadAdoption.NOT_ADOPTED:
        if marker is not None:
            await deps.storage_lifecycle.discard_unadopted_upload(marker)
        elif written_path is not None:
            await _cleanup_written_path(written_path, deps=deps)
        raise commit_error

    if probe_cancellation is not None:
        raise probe_cancellation
    if isinstance(commit_error, asyncio.CancelledError):
        raise commit_error
    raise deps.http_error(
        "video_upload_commit_unknown",
        "reference video upload commit outcome is unknown; "
        "the file was retained for reconciliation",
        503,
    ) from commit_error


async def _write_reserved(
    *,
    path: Path,
    file: UploadFile,
    size: int,
    deps: UploadDependencies,
) -> None:
    write_completed = False
    try:
        async with deps.storage_capacity.reserve(size):
            write_task = asyncio.ensure_future(
                asyncio.to_thread(
                    deps.write_new_file_atomic,
                    path,
                    file.file,
                )
            )
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                try:
                    await _wait_for_started_task(write_task)
                    write_completed = True
                except BaseException:
                    pass
                if write_completed:
                    await _cleanup_written_path(path, deps=deps)
                raise
            write_completed = True
    except capacity.StorageCapacityExceeded as exc:
        raise deps.http_error(
            "storage_insufficient_space",
            "not enough free storage to accept this video",
            507,
        ) from exc
    except (capacity.StorageCapacityUnavailable, CapacityLeaseLost) as exc:
        if write_completed:
            await _cleanup_written_path(path, deps=deps)
        raise deps.http_error(
            "storage_capacity_unavailable",
            "video storage capacity is temporarily unavailable",
            503,
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise deps.http_error(
                "storage_insufficient_space",
                "not enough free storage to accept this video",
                507,
            ) from exc
        raise


async def _rollback_inherited_transaction(db: AsyncSession) -> None:
    in_transaction = getattr(db, "in_transaction", None)
    if callable(in_transaction) and in_transaction():
        await db.rollback()


def _ensure_inventory_projected_quota(
    *,
    inventory: ReferenceInventorySnapshot,
    upload_size: int,
    deps: UploadDependencies,
) -> None:
    existing = inventory.existing
    if existing is None:
        _ensure_projected_quota(
            current_count=inventory.quota_count,
            current_bytes=inventory.quota_bytes,
            projected_count=inventory.quota_count + 1,
            projected_bytes=inventory.quota_bytes + upload_size,
            deps=deps,
        )
        return
    if existing.deleted_at is None:
        return
    inspection = inventory.inspections[str(existing.id)]
    current_count, current_bytes = video_reference_quota_contribution(
        existing,
        inspection,
    )
    recovered_bytes = inspection.bytes_on_disk + max(
        0,
        upload_size - inspection.primary_size_bytes,
    )
    _ensure_projected_quota(
        current_count=inventory.quota_count,
        current_bytes=inventory.quota_bytes,
        projected_count=inventory.quota_count - current_count + 1,
        projected_bytes=inventory.quota_bytes - current_bytes + recovered_bytes,
        deps=deps,
    )


def _reference_upload_plan(
    *,
    user_id: str,
    ext: str,
    inventory: ReferenceInventorySnapshot,
    upload_size: int,
    deps: UploadDependencies,
) -> _ReferenceUploadPlan:
    _ensure_inventory_projected_quota(
        inventory=inventory,
        upload_size=upload_size,
        deps=deps,
    )
    existing = inventory.existing
    if existing is None:
        video_id = new_uuid7()
        storage_key = deps.upload_key(user_id, video_id, ext)
        return _ReferenceUploadPlan(
            created=True,
            video_id=video_id,
            storage_key=storage_key,
            path=deps.fs_path(storage_key),
        )

    inspection = inventory.inspections[str(existing.id)]
    if inspection.issues and not inspection.primary_present:
        raise deps.http_error(
            "video_storage_state_invalid",
            "reference video storage cannot be repaired safely",
            503,
        )
    if existing.deleted_at is not None and inspection.issues:
        raise deps.http_error(
            "video_storage_state_invalid",
            "deleted reference video storage cannot be recovered safely",
            503,
        )
    if inspection.primary_present:
        return _ReferenceUploadPlan(
            created=False,
            video_id=existing.id,
            storage_key=existing.storage_key,
            path=None,
        )
    storage_key = deps.upload_key(user_id, existing.id, ext)
    return _ReferenceUploadPlan(
        created=False,
        video_id=existing.id,
        storage_key=storage_key,
        path=deps.fs_path(storage_key),
    )


def _new_reference_video(
    *,
    plan: _ReferenceUploadPlan,
    user_id: str,
    filename: str,
    mime: str,
    size: int,
    sha256: str,
    deps: UploadDependencies,
) -> Video:
    video = Video(
        id=plan.video_id,
        user_id=user_id,
        owner_generation_id=None,
        storage_key=plan.storage_key,
        poster_storage_key=None,
        mime=mime,
        width=0,
        height=0,
        duration_ms=0,
        fps=None,
        size_bytes=size,
        sha256=sha256,
        etag=sha256,
        has_audio=False,
        faststart=False,
        visibility="private",
        metadata_jsonb={
            "source": "uploaded_reference",
            "filename": filename,
            "reference_access_token": secrets.token_urlsafe(32),
            "reference_access_token_expires_at": deps.token_expiry(),
        },
    )
    return video


async def _discard_prepared_upload(
    *,
    marker: VideoUploadAdoptionMarker | None,
    written_path: Path | None,
    deps: UploadDependencies,
) -> None:
    try:
        if marker is not None:
            discard = asyncio.ensure_future(
                deps.storage_lifecycle.discard_unadopted_upload(marker)
            )
            await _wait_for_started_task(discard)
        elif written_path is not None:
            await _cleanup_written_path(written_path, deps=deps)
    except BaseException:
        deps.logger.warning(
            "reference video prepared upload cleanup failed path=%s",
            written_path,
            exc_info=True,
        )


async def _adopt_reference_upload(
    *,
    user_id: str,
    filename: str,
    mime: str,
    size: int,
    sha256: str,
    inventory: ReferenceInventorySnapshot,
    plan: _ReferenceUploadPlan,
    marker: VideoUploadAdoptionMarker | None,
    user: Any,
    db: AsyncSession,
    deps: UploadDependencies,
) -> Any:
    commit_started = False
    try:
        await lock_db_authenticated_user_snapshot(db, user)
        locked = await lock_reference_inventory_for_adoption(
            user_id=user_id,
            sha256=sha256,
            db=db,
            deps=deps,
            cleanup_page_size=_REFERENCE_INVENTORY_CLEANUP_PAGE_SIZE,
            result_rows=_result_rows,
            matching_video=_matching_video,
        )
        if locked.signature != inventory.signature:
            raise _ReferenceInventoryChanged

        _ensure_inventory_projected_quota(
            inventory=inventory,
            upload_size=size,
            deps=deps,
        )
        if plan.created:
            if locked.existing is not None:
                raise _ReferenceInventoryChanged
            video = _new_reference_video(
                plan=plan,
                user_id=user_id,
                filename=filename,
                mime=mime,
                size=size,
                sha256=sha256,
                deps=deps,
            )
            db.add(video)
        else:
            snapshot = inventory.existing
            video = locked.existing
            if (
                snapshot is None
                or video is None
                or str(video.id) != plan.video_id
                or not snapshot.matches(video)
            ):
                raise _ReferenceInventoryChanged
            _update_reference_video(
                video,
                filename=filename,
                mime=mime,
                size=size,
                sha256=sha256,
                storage_key=plan.storage_key if plan.path is not None else None,
                deps=deps,
            )

        commit_started = True
        return await _commit_reference_video_with_probe(
            db=db,
            video=video,
            marker=marker,
            written_path=plan.path,
            sha256=sha256,
            deps=deps,
        )
    except _ReferenceInventoryChanged:
        await _best_effort_rollback(db, logger=deps.logger)
        raise
    except BaseException:
        if not commit_started:
            await _best_effort_rollback(db, logger=deps.logger)
            await _discard_prepared_upload(
                marker=marker,
                written_path=plan.path,
                deps=deps,
            )
        raise


async def _resolve_inventory_change(
    *,
    user_id: str,
    size: int,
    sha256: str,
    plan: _ReferenceUploadPlan,
    marker: VideoUploadAdoptionMarker | None,
    deps: UploadDependencies,
) -> Any | None:
    if marker is None:
        return None
    probe, probe_cancellation = await _probe_reference_video_adoption(
        video_id=plan.video_id,
        user_id=user_id,
        storage_key=plan.storage_key,
        sha256=sha256,
        size_bytes=size,
        deps=deps,
    )
    if probe.outcome is VideoUploadAdoption.ADOPTED:
        await _clear_adoption_marker_best_effort(marker=marker, deps=deps)
        return probe.video
    if probe.outcome is VideoUploadAdoption.NOT_ADOPTED:
        await _discard_prepared_upload(
            marker=marker,
            written_path=plan.path,
            deps=deps,
        )
        return None
    if probe_cancellation is not None:
        raise probe_cancellation
    raise deps.http_error(
        "video_upload_commit_unknown",
        "reference video upload ownership changed while adopting; "
        "the file was retained for reconciliation",
        503,
    )


async def upload_reference_video(
    *,
    user: Any,
    db: AsyncSession,
    file: UploadFile,
    deps: UploadDependencies,
) -> VideoUploadOut:
    user_id = str(user.id)
    try:
        filename = deps.normalize_filename(file.filename)
    except ValueError as exc:
        raise deps.http_error("invalid_filename", str(exc), 422) from exc
    file.filename = filename
    mime, ext = deps.reference_upload_ext(file)
    await _rollback_inherited_transaction(db)
    size, sha, header = await deps.inspect_upload(file)
    if not deps.looks_like_video(header):
        raise deps.http_error(
            "invalid_video_file",
            "reference video must be a valid mp4 or mov file",
            415,
        )

    for attempt in range(_REFERENCE_UPLOAD_CAS_ATTEMPTS):
        inventory = await _load_reference_inventory(
            user_id=user_id,
            sha256=sha,
            db=db,
            deps=deps,
            cleanup_page_size=_REFERENCE_INVENTORY_CLEANUP_PAGE_SIZE,
            result_rows=_result_rows,
            matching_video=_matching_video,
            clear_adoption_marker=_clear_adoption_marker_best_effort,
            adopted_outcome=VideoUploadAdoption.ADOPTED,
            not_adopted_outcome=VideoUploadAdoption.NOT_ADOPTED,
        )
        plan = _reference_upload_plan(
            user_id=user_id,
            ext=ext,
            inventory=inventory,
            upload_size=size,
            deps=deps,
        )
        try:
            async with deps.storage_lifecycle.reference_mutation_lock(
                user_id=user_id,
                video_id=plan.video_id,
            ):
                plan = await _verify_reusable_reference_upload(
                    plan=plan,
                    user_id=user_id,
                    size=size,
                    sha256=sha,
                    deps=deps,
                )
                marker: VideoUploadAdoptionMarker | None = None
                if plan.path is not None:
                    await file.seek(0)
                    await _write_reserved(
                        path=plan.path,
                        file=file,
                        size=size,
                        deps=deps,
                    )
                    marker = await _record_reference_video_adoption_pending(
                        video_id=plan.video_id,
                        user_id=user_id,
                        storage_key=plan.storage_key,
                        written_path=plan.path,
                        sha256=sha,
                        deps=deps,
                    )
                try:
                    adopted = await _adopt_reference_upload(
                        user_id=user_id,
                        filename=filename,
                        mime=mime,
                        size=size,
                        sha256=sha,
                        inventory=inventory,
                        plan=plan,
                        marker=marker,
                        user=user,
                        db=db,
                        deps=deps,
                    )
                except _ReferenceInventoryChanged:
                    adopted = await _resolve_inventory_change(
                        user_id=user_id,
                        size=size,
                        sha256=sha,
                        plan=plan,
                        marker=marker,
                        deps=deps,
                    )
        except VideoReferenceStorageLockTimeout as exc:
            raise deps.http_error(
                "video_storage_busy",
                "reference video storage is busy; retry shortly",
                503,
            ) from exc
        if adopted is None:
            if attempt + 1 < _REFERENCE_UPLOAD_CAS_ATTEMPTS:
                continue
            raise deps.http_error(
                "video_upload_inventory_changed",
                "reference video inventory changed repeatedly; retry the upload",
                409,
            )
        return deps.upload_out(adopted, created=plan.created)

    raise AssertionError("reference video upload CAS loop exhausted")
