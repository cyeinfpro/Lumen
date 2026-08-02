"""Short-transaction video deletion and detached storage cleanup."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import User, Video

from ..images.application.deleted_media_references import (
    active_video_generation_reference_id,
)
from ..services.video_storage_lifecycle import (
    VIDEO_STORAGE_CLEANUP_METADATA_KEY,
    VideoReferenceStorageLockTimeout,
    record_video_storage_cleanup,
)

_VIDEO_CLEANUP_CLAIM_KEY = "reference_inventory_cleanup_claim"
_VIDEO_CLEANUP_CLAIM_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class VideoCleanupSnapshot:
    id: str
    user_id: str
    owner_generation_id: str | None
    storage_key: str
    poster_storage_key: str | None
    size_bytes: int
    deleted_at: datetime
    metadata_jsonb: dict[str, Any]

    @classmethod
    def from_video(cls, video: Video) -> VideoCleanupSnapshot:
        owner_generation_id = getattr(video, "owner_generation_id", None)
        return cls(
            id=str(video.id),
            user_id=str(video.user_id),
            owner_generation_id=(
                str(owner_generation_id) if owner_generation_id else None
            ),
            storage_key=str(video.storage_key),
            poster_storage_key=(
                str(video.poster_storage_key) if video.poster_storage_key else None
            ),
            size_bytes=max(0, int(video.size_bytes or 0)),
            deleted_at=cast(datetime, video.deleted_at),
            metadata_jsonb=dict(video.metadata_jsonb or {}),
        )


@dataclass(frozen=True)
class VideoCleanupAttempt:
    token: str
    snapshot: VideoCleanupSnapshot


async def _locked_owned_video(
    db: AsyncSession,
    *,
    video_id: str,
    user_id: str,
) -> Video | None:
    return (
        await db.execute(
            select(Video)
            .where(
                Video.id == video_id,
                Video.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _mark_cleanup_pending(
    video: Video,
    *,
    deleted_at: datetime,
    token: str,
) -> None:
    metadata = dict(video.metadata_jsonb or {})
    previous = metadata.get(VIDEO_STORAGE_CLEANUP_METADATA_KEY)
    previous_cleanup = previous if isinstance(previous, dict) else {}
    remaining_artifacts = previous_cleanup.get("remaining_artifact_count")
    remaining_bytes = previous_cleanup.get("remaining_bytes")
    quarantine_token = previous_cleanup.get("quarantine_token")
    metadata[VIDEO_STORAGE_CLEANUP_METADATA_KEY] = {
        "state": "pending",
        "attempted_at": deleted_at.isoformat(),
        "remaining_artifact_count": (
            max(1, int(remaining_artifacts))
            if isinstance(remaining_artifacts, int)
            else 1
        ),
        "remaining_bytes": (
            max(0, int(remaining_bytes))
            if isinstance(remaining_bytes, int)
            else max(0, int(video.size_bytes or 0))
        ),
    }
    if isinstance(quarantine_token, str) and quarantine_token:
        metadata[VIDEO_STORAGE_CLEANUP_METADATA_KEY]["quarantine_token"] = (
            quarantine_token
        )
    metadata[_VIDEO_CLEANUP_CLAIM_KEY] = {
        "token": token,
        "claimed_at": deleted_at.isoformat(),
    }
    video.metadata_jsonb = metadata


def _clear_cleanup_pending(video: Video) -> None:
    metadata = dict(video.metadata_jsonb or {})
    metadata.pop(VIDEO_STORAGE_CLEANUP_METADATA_KEY, None)
    metadata.pop(_VIDEO_CLEANUP_CLAIM_KEY, None)
    video.metadata_jsonb = metadata


def _cleanup_claim_token(video: Video) -> str | None:
    metadata = video.metadata_jsonb if isinstance(video.metadata_jsonb, dict) else {}
    claim = metadata.get(_VIDEO_CLEANUP_CLAIM_KEY)
    token = claim.get("token") if isinstance(claim, dict) else None
    return token if isinstance(token, str) and token else None


def _cleanup_claim_is_live(video: Video, *, now: datetime) -> bool:
    metadata = video.metadata_jsonb if isinstance(video.metadata_jsonb, dict) else {}
    claim = metadata.get(_VIDEO_CLEANUP_CLAIM_KEY)
    claimed_at_raw = claim.get("claimed_at") if isinstance(claim, dict) else None
    if not isinstance(claimed_at_raw, str):
        return False
    try:
        claimed_at = datetime.fromisoformat(claimed_at_raw)
    except ValueError:
        return False
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    age = now - claimed_at.astimezone(timezone.utc)
    return age.total_seconds() < _VIDEO_CLEANUP_CLAIM_TTL_SECONDS


def _cleanup_is_complete(video: Video) -> bool:
    metadata = video.metadata_jsonb if isinstance(video.metadata_jsonb, dict) else {}
    cleanup = metadata.get(VIDEO_STORAGE_CLEANUP_METADATA_KEY)
    return isinstance(cleanup, dict) and cleanup.get("state") == "complete"


async def _reject_active_reference(
    db: AsyncSession,
    *,
    video: Video,
    deps: Any,
    restore_soft_delete: bool,
    active_reference: Callable[..., Awaitable[str | None]] = (
        active_video_generation_reference_id
    ),
) -> None:
    generation_id = await active_reference(db, video=video)
    if generation_id is None:
        return
    if restore_soft_delete and video.deleted_at is not None:
        video.deleted_at = None
        _clear_cleanup_pending(video)
        await db.commit()
    raise deps.http_error(
        "video_generation_reference_active",
        "video is retained by an active generation",
        409,
        video_generation_id=generation_id,
    )


async def _lock_owned_video(
    db: AsyncSession,
    *,
    video_id: str,
    user_id: str,
) -> Video | None:
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    return await _locked_owned_video(db, video_id=video_id, user_id=user_id)


async def prepare_video_cleanup(
    *,
    video_id: str,
    user_id: str,
    db: AsyncSession,
    deps: Any,
    reject_active_reference: Callable[..., Awaitable[None]] = (
        _reject_active_reference
    ),
) -> VideoCleanupAttempt | None:
    token = secrets.token_urlsafe(18)
    video = await _lock_owned_video(db, video_id=video_id, user_id=user_id)
    if video is None:
        raise deps.http_error("not_found", "video not found", 404)
    if video.deleted_at is None:
        await deps.ensure_not_canvas_referenced(db, video_id=video.id)
        await reject_active_reference(
            db,
            video=video,
            deps=deps,
            restore_soft_delete=False,
        )
        deleted_at = datetime.now(timezone.utc)
        video.deleted_at = deleted_at
        _mark_cleanup_pending(video, deleted_at=deleted_at, token=token)
        await db.commit()
        video = await _lock_owned_video(db, video_id=video_id, user_id=user_id)
        if video is None:
            raise deps.http_error("not_found", "video not found", 404)
        if video.deleted_at is None:
            await db.commit()
            deps.logger.info(
                "video cleanup skipped after concurrent restore video_id=%s",
                video.id,
            )
            return None

    await reject_active_reference(
        db,
        video=video,
        deps=deps,
        restore_soft_delete=True,
    )
    if _cleanup_is_complete(video):
        await db.commit()
        return None
    if (
        _cleanup_claim_token(video) != token
        and _cleanup_claim_is_live(video, now=datetime.now(timezone.utc))
    ):
        await db.commit()
        deps.logger.info("video cleanup already claimed video_id=%s", video.id)
        return None
    deleted_at = cast(datetime, video.deleted_at)
    _mark_cleanup_pending(video, deleted_at=deleted_at, token=token)
    snapshot = VideoCleanupSnapshot.from_video(video)
    await db.commit()
    return VideoCleanupAttempt(token=token, snapshot=snapshot)


async def record_video_cleanup_result(
    *,
    attempt: VideoCleanupAttempt,
    cleanup: Any,
    db: AsyncSession,
    deps: Any,
) -> bool:
    video = await _lock_owned_video(
        db,
        video_id=attempt.snapshot.id,
        user_id=attempt.snapshot.user_id,
    )
    if video is None:
        raise deps.http_error("not_found", "video not found", 404)
    if video.deleted_at is None:
        if _cleanup_claim_token(video) == attempt.token:
            _clear_cleanup_pending(video)
        await db.commit()
        deps.logger.info(
            "video cleanup result ignored after concurrent restore video_id=%s",
            video.id,
        )
        return False
    if _cleanup_claim_token(video) != attempt.token:
        await db.commit()
        deps.logger.info("video cleanup result lost CAS video_id=%s", video.id)
        return False
    metadata = dict(video.metadata_jsonb or {})
    metadata.pop(_VIDEO_CLEANUP_CLAIM_KEY, None)
    video.metadata_jsonb = metadata
    record_video_storage_cleanup(video, cleanup)
    if not cleanup.complete:
        metadata = dict(video.metadata_jsonb or {})
        cleanup_metadata = metadata.get(VIDEO_STORAGE_CLEANUP_METADATA_KEY)
        if isinstance(cleanup_metadata, dict):
            cleanup_metadata["quarantine_token"] = attempt.token
            metadata[VIDEO_STORAGE_CLEANUP_METADATA_KEY] = cleanup_metadata
            video.metadata_jsonb = metadata
    await db.commit()
    return True


async def detach_video_cleanup_if_owned(
    *,
    attempt: VideoCleanupAttempt,
    db: AsyncSession,
    deps: Any,
) -> Any | None:
    try:
        async with deps.storage_lifecycle.reference_mutation_lock(
            user_id=attempt.snapshot.user_id,
            video_id=attempt.snapshot.id,
        ):
            video = await _lock_owned_video(
                db,
                video_id=attempt.snapshot.id,
                user_id=attempt.snapshot.user_id,
            )
            if (
                video is None
                or video.deleted_at is None
                or _cleanup_claim_token(video) != attempt.token
            ):
                await db.commit()
                return None
            snapshot = VideoCleanupSnapshot.from_video(video)
            await db.commit()
            cleanup_metadata = snapshot.metadata_jsonb.get(
                VIDEO_STORAGE_CLEANUP_METADATA_KEY
            )
            prior_token = (
                cleanup_metadata.get("quarantine_token")
                if isinstance(cleanup_metadata, dict)
                else None
            )
            if isinstance(prior_token, str) and prior_token:
                prior = deps.storage_lifecycle.detached_cleanup(
                    user_id=snapshot.user_id,
                    video_id=snapshot.id,
                    token=prior_token,
                )
                if prior.path is not None:
                    return prior
            return deps.storage_lifecycle.detach_cleanup(
                snapshot,
                token=attempt.token,
            )
    except VideoReferenceStorageLockTimeout as exc:
        raise deps.http_error(
            "video_storage_busy",
            "video storage is busy; retry shortly",
            503,
        ) from exc


__all__ = (
    "VideoCleanupAttempt",
    "detach_video_cleanup_if_owned",
    "prepare_video_cleanup",
    "record_video_cleanup_result",
)
