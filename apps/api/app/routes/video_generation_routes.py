"""Video generation listing and lifecycle route orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Literal, cast

from fastapi import Request, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.constants import (
    EV_VIDEO_CANCELED,
    VideoGenerationStage,
    VideoGenerationStatus,
    task_channel,
)
from lumen_core.model_entities import (
    OutboxEvent,
    User,
    Video,
    VideoGeneration,
)
from lumen_core.schema_models import (
    VideoCreateIn,
    VideoGenerationOut,
    VideoGenerationsOut,
    VideoReferenceMediaIn,
)

from ..images.application.deleted_media_references import (
    active_video_generation_reference_id,
)
from ..services.video_storage_lifecycle import (
    VIDEO_STORAGE_CLEANUP_METADATA_KEY,
    VideoStorageLifecycle,
    record_video_storage_cleanup,
)


@dataclass(frozen=True)
class GenerationRouteDependencies:
    http_error: Callable[..., Exception]
    generation_out: Callable[..., Awaitable[VideoGenerationOut]]
    decode_cursor: Callable[[str | None], tuple[datetime, str] | None]
    encode_cursor: Callable[[VideoGeneration], str]
    terminal_statuses: frozenset[str]
    invalidate_balance_cache: Callable[[str], Awaitable[None]]
    allow_negative_balance: Callable[[AsyncSession], Awaitable[bool]]
    reject_canvas_retry: Callable[[VideoGeneration], None]
    create_record: Callable[..., Awaitable[VideoGenerationOut]]
    ensure_not_canvas_referenced: Callable[..., Awaitable[None]]
    storage_lifecycle: VideoStorageLifecycle
    logger: Any
    list_limit_max: int


_SUBMIT_DELIVERY_STATES = frozenset({"proven_absent", "unknown", "confirmed"})
SUBMIT_DELIVERY_PRECEDENCE = MappingProxyType(
    {
        "proven_absent": 0,
        "unknown": 1,
        "confirmed": 2,
    }
)
# Keep the old private name for callers that imported it before the contract
# was made immutable.
_SUBMIT_DELIVERY_PRECEDENCE = SUBMIT_DELIVERY_PRECEDENCE


def _video_submit_delivery_state(generation: VideoGeneration) -> str:
    if generation.provider_task_id:
        return "confirmed"
    raw_diagnostics = getattr(generation, "diagnostics", None)
    diagnostics = dict(raw_diagnostics) if isinstance(raw_diagnostics, dict) else {}
    states: list[str] = []
    aggregate = diagnostics.get("submit_delivery_state")
    if aggregate in _SUBMIT_DELIVERY_STATES:
        states.append(str(aggregate))
    history = diagnostics.get("submit_delivery_history")
    if isinstance(history, list):
        for item in history:
            state = item.get("state") if isinstance(item, dict) else None
            if state in _SUBMIT_DELIVERY_STATES:
                states.append(str(state))
    if isinstance(diagnostics.get("submit_receipt"), dict):
        states.append("confirmed")
    if states:
        return max(states, key=SUBMIT_DELIVERY_PRECEDENCE.__getitem__)
    if (
        int(getattr(generation, "attempt", 0) or 0) <= 0
        and int(getattr(generation, "submission_epoch", 0) or 0) <= 0
        and getattr(generation, "submit_started_at", None) is None
    ):
        return "proven_absent"
    return "unknown"


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


def _mark_video_storage_cleanup_pending(
    video: Video,
    *,
    deleted_at: datetime,
) -> None:
    metadata = dict(video.metadata_jsonb or {})
    previous = metadata.get(VIDEO_STORAGE_CLEANUP_METADATA_KEY)
    previous_cleanup = previous if isinstance(previous, dict) else {}
    remaining_artifacts = previous_cleanup.get("remaining_artifact_count")
    remaining_bytes = previous_cleanup.get("remaining_bytes")
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
    video.metadata_jsonb = metadata


def _clear_video_storage_cleanup_pending(video: Video) -> None:
    metadata = dict(video.metadata_jsonb or {})
    metadata.pop(VIDEO_STORAGE_CLEANUP_METADATA_KEY, None)
    video.metadata_jsonb = metadata


async def _reject_active_video_reference(
    db: AsyncSession,
    *,
    video: Video,
    deps: GenerationRouteDependencies,
    restore_soft_delete: bool,
) -> None:
    generation_id = await active_video_generation_reference_id(db, video=video)
    if generation_id is None:
        return
    if restore_soft_delete and video.deleted_at is not None:
        video.deleted_at = None
        _clear_video_storage_cleanup_pending(video)
        await db.commit()
    raise deps.http_error(
        "video_generation_reference_active",
        "video is retained by an active generation",
        409,
        video_generation_id=generation_id,
    )


async def list_video_generations(
    *,
    user: Any,
    db: AsyncSession,
    limit: int,
    cursor: str | None,
    status: str | None,
    deps: GenerationRouteDependencies,
) -> VideoGenerationsOut:
    limit = max(1, min(deps.list_limit_max, limit))
    decoded = deps.decode_cursor(cursor)
    statement = select(VideoGeneration).where(VideoGeneration.user_id == user.id)
    if status:
        statement = statement.where(VideoGeneration.status == status)
    if decoded is not None:
        created_at, row_id = decoded
        statement = statement.where(
            or_(
                VideoGeneration.created_at < created_at,
                and_(
                    VideoGeneration.created_at == created_at,
                    VideoGeneration.id < row_id,
                ),
            )
        )
    rows = (
        (
            await db.execute(
                statement.order_by(
                    VideoGeneration.created_at.desc(),
                    VideoGeneration.id.desc(),
                ).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page = rows[:limit]
    next_cursor = deps.encode_cursor(page[-1]) if len(rows) > limit and page else None
    generation_ids = [row.id for row in page]
    videos_by_generation_id: dict[str, Video] = {}
    if generation_ids:
        videos = (
            (
                await db.execute(
                    select(Video).where(
                        Video.owner_generation_id.in_(generation_ids),
                        Video.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        videos_by_generation_id = {
            video.owner_generation_id: video
            for video in videos
            if video.owner_generation_id is not None
        }
    return VideoGenerationsOut(
        items=[
            await deps.generation_out(
                db,
                row,
                videos_by_generation_id.get(row.id),
            )
            for row in page
        ],
        next_cursor=next_cursor,
    )


async def get_video_generation(
    *,
    generation_id: str,
    user: Any,
    db: AsyncSession,
    deps: GenerationRouteDependencies,
) -> VideoGenerationOut:
    row = (
        await db.execute(
            select(VideoGeneration).where(
                VideoGeneration.id == generation_id,
                VideoGeneration.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise deps.http_error("not_found", "video generation not found", 404)
    return await deps.generation_out(db, row)


async def cancel_video_generation(
    *,
    generation_id: str,
    user: Any,
    db: AsyncSession,
    deps: GenerationRouteDependencies,
) -> VideoGenerationOut:
    row = (
        await db.execute(
            select(VideoGeneration)
            .where(
                VideoGeneration.id == generation_id,
                VideoGeneration.user_id == user.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise deps.http_error("not_found", "video generation not found", 404)
    now = datetime.now(timezone.utc)
    balance_changed = False
    if row.status not in deps.terminal_statuses:
        row.cancel_requested_at = row.cancel_requested_at or now
        if (
            row.status == VideoGenerationStatus.QUEUED.value
            and not row.provider_task_id
        ):
            delivery_state = _video_submit_delivery_state(row)
            row.status = VideoGenerationStatus.CANCELED.value
            row.progress_stage = VideoGenerationStage.FINISHED.value
            row.progress_pct = 100
            row.error_code = "canceled"
            row.error_message = (
                "cancelled before upstream submission"
                if delivery_state == "proven_absent"
                else "cancelled while upstream submission outcome was unknown"
            )
            row.finished_at = now
            diagnostics = (
                dict(row.diagnostics) if isinstance(row.diagnostics, dict) else {}
            )
            if delivery_state == "proven_absent":
                transaction = await billing_core.release(
                    db,
                    user.id,
                    ref_type="video_generation",
                    ref_id=row.id,
                    idempotency_key=f"video_generation:release:{row.id}",
                    meta={
                        "model": row.model,
                        "action": row.action,
                        "provider_name": row.provider_name,
                        "billing_decision": "pre_submit_cancel_release",
                        "submit_delivery_state": delivery_state,
                    },
                )
                billing_decision = "pre_submit_cancel_release"
                actual_micro = 0
            else:
                held = await billing_core._held_amount_for_ref(  # noqa: SLF001
                    db,
                    user.id,
                    "video_generation",
                    row.id,
                )
                actual_micro = max(
                    int(held),
                    int(getattr(row, "est_cost_micro", 0) or 0),
                )
                billing_decision = "cancel_submit_delivery_unknown_default_charge"
                transaction = await billing_core.settle(
                    db,
                    user.id,
                    ref_type="video_generation",
                    ref_id=row.id,
                    actual_micro=actual_micro,
                    idempotency_key=f"video_generation:settle:{row.id}",
                    allow_negative=await deps.allow_negative_balance(db),
                    record_zero=actual_micro == 0,
                    meta={
                        "model": row.model,
                        "action": row.action,
                        "provider_name": row.provider_name,
                        "provider_task_id": row.provider_task_id,
                        "provider_idempotency_key": getattr(
                            row,
                            "provider_idempotency_key",
                            None,
                        ),
                        "billing_decision": billing_decision,
                        "submit_delivery_state": delivery_state,
                        "upstream_cost_knowledge": "unknown",
                    },
                )
            if transaction is None:
                await db.rollback()
                raise deps.http_error(
                    (
                        "video_hold_release_missing"
                        if delivery_state == "proven_absent"
                        else "video_hold_settle_missing"
                    ),
                    (
                        "video hold release transaction was not created"
                        if delivery_state == "proven_absent"
                        else "video hold settlement transaction was not created"
                    ),
                    409,
                )
            row.billed_cost_micro = actual_micro
            row.billed_tokens = None
            diagnostics["cancel_billing"] = {
                "at": now.isoformat(),
                "decision": billing_decision,
                "actual_micro": actual_micro,
                "submit_delivery_state": delivery_state,
            }
            row.diagnostics = diagnostics
            balance_changed = True
            db.add(
                OutboxEvent(
                    kind="sse",
                    payload={
                        "user_id": user.id,
                        "channel": task_channel(row.id),
                        "event_name": EV_VIDEO_CANCELED,
                        "data": {
                            "video_generation_id": row.id,
                            "kind": "video_generation",
                            "status": row.status,
                            "stage": row.progress_stage,
                            "progress_pct": row.progress_pct,
                            "submission_epoch": int(
                                getattr(row, "submission_epoch", 0) or 0
                            ),
                            "video_id": None,
                            "error_code": row.error_code,
                            "error_message": row.error_message,
                        },
                    },
                    published_at=None,
                )
            )
    await db.commit()
    await db.refresh(row)
    if balance_changed:
        await deps.invalidate_balance_cache(user.id)
    return await deps.generation_out(db, row)


async def retry_video_generation(
    *,
    generation_id: str,
    request: Request,
    user: Any,
    db: AsyncSession,
    deps: GenerationRouteDependencies,
) -> VideoGenerationOut:
    if getattr(user, "account_mode", "wallet") != "wallet":
        raise deps.http_error(
            "account_mode_forbidden",
            "video generation requires wallet mode",
            403,
        )
    row = (
        await db.execute(
            select(VideoGeneration)
            .where(
                VideoGeneration.id == generation_id,
                VideoGeneration.user_id == user.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise deps.http_error("not_found", "video generation not found", 404)
    deps.reject_canvas_retry(row)
    if row.status not in {
        VideoGenerationStatus.FAILED.value,
        VideoGenerationStatus.CANCELED.value,
        VideoGenerationStatus.EXPIRED.value,
    }:
        raise deps.http_error(
            "video_retry_not_terminal",
            "only failed, canceled, or expired video tasks can be retried",
            409,
            status=row.status,
        )
    raw_reference_media = (row.upstream_request or {}).get("reference_media")
    reference_snapshots = (
        [item for item in raw_reference_media if isinstance(item, dict)]
        if isinstance(raw_reference_media, list)
        else []
    )
    reference_inputs: list[VideoReferenceMediaIn] = []
    valid_reference_snapshots: list[dict[str, Any]] = []
    for item in reference_snapshots:
        raw_kind = item.get("kind")
        if raw_kind not in {"image", "video", "audio"}:
            continue
        kind = cast(Literal["image", "video", "audio"], raw_kind)
        try:
            reference_inputs.append(
                VideoReferenceMediaIn(
                    kind=kind,
                    image_id=(
                        item.get("image_id")
                        if isinstance(item.get("image_id"), str)
                        else None
                    ),
                    video_id=(
                        item.get("video_id")
                        if isinstance(item.get("video_id"), str)
                        else None
                    ),
                    url=(item.get("url") if isinstance(item.get("url"), str) else None),
                    label=(
                        item.get("label")
                        if isinstance(item.get("label"), str)
                        else None
                    ),
                    ref_id=(
                        item.get("ref_id")
                        if isinstance(item.get("ref_id"), str)
                        else None
                    ),
                )
            )
            valid_reference_snapshots.append(item)
        except ValueError:
            deps.logger.warning(
                "video retry skipped invalid reference snapshot id=%s snapshot=%r",
                row.id,
                item,
            )
    if row.action == "reference" and not reference_inputs:
        raise deps.http_error(
            "reference_media_missing",
            "original reference media snapshot is missing; create a new video task",
            409,
        )
    try:
        body = VideoCreateIn.model_validate(
            {
                "action": row.action,
                "model": row.model,
                "prompt": row.prompt,
                "input_image_id": row.input_image_id,
                "reference_media": reference_inputs,
                "duration_s": row.duration_s,
                "resolution": row.resolution,
                "aspect_ratio": row.aspect_ratio,
                "generate_audio": row.generate_audio,
                "seed": row.seed,
                "watermark": row.watermark,
                "idempotency_key": f"retry:{row.id}:{row.updated_at.isoformat()}",
            }
        )
    except ValueError as exc:
        raise deps.http_error(
            "invalid_retry_request",
            "original video generation request is no longer valid",
            422,
        ) from exc
    return await deps.create_record(
        db,
        body,
        user,
        request=request,
        input_image_snapshot=(
            row.input_image_storage_key,
            row.input_image_sha256,
            (
                (row.upstream_request or {}).get("input_image_url")
                if isinstance(
                    (row.upstream_request or {}).get("input_image_url"),
                    str,
                )
                else None
            ),
        ),
        reference_media_snapshot=valid_reference_snapshots,
    )


async def delete_video(
    *,
    video_id: str,
    user: Any,
    db: AsyncSession,
    deps: GenerationRouteDependencies,
) -> Response:
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    video = await _locked_owned_video(
        db,
        video_id=video_id,
        user_id=user.id,
    )
    if video is None:
        raise deps.http_error("not_found", "video not found", 404)
    if video.deleted_at is None:
        await deps.ensure_not_canvas_referenced(db, video_id=video.id)
        await _reject_active_video_reference(
            db,
            video=video,
            deps=deps,
            restore_soft_delete=False,
        )
        deleted_at = datetime.now(timezone.utc)
        video.deleted_at = deleted_at
        _mark_video_storage_cleanup_pending(video, deleted_at=deleted_at)
        await db.commit()

        await db.execute(select(User.id).where(User.id == user.id).with_for_update())
        video = await _locked_owned_video(
            db,
            video_id=video_id,
            user_id=user.id,
        )
        if video is None:
            raise deps.http_error("not_found", "video not found", 404)
        if video.deleted_at is None:
            deps.logger.info(
                "video cleanup skipped after concurrent restore video_id=%s",
                video.id,
            )
            return Response(status_code=204)

    await _reject_active_video_reference(
        db,
        video=video,
        deps=deps,
        restore_soft_delete=True,
    )
    cleanup = await deps.storage_lifecycle.cleanup(video)
    record_video_storage_cleanup(video, cleanup)
    await db.commit()
    if not cleanup.complete:
        deps.logger.warning(
            "video storage cleanup pending video_id=%s remaining=%s bytes=%s errors=%s",
            video.id,
            cleanup.remaining.artifact_count,
            cleanup.remaining.bytes_on_disk,
            cleanup.errors,
        )
        raise deps.http_error(
            "video_storage_cleanup_pending",
            "video cleanup is incomplete; retry deletion",
            503,
            remaining_artifacts=cleanup.remaining.artifact_count,
            remaining_bytes=cleanup.remaining.bytes_on_disk,
        )
    return Response(status_code=204)
