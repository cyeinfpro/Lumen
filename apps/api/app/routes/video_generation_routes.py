"""Video generation listing and lifecycle route orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
from lumen_core.models import OutboxEvent, Video, VideoGeneration
from lumen_core.schemas import (
    VideoCreateIn,
    VideoGenerationOut,
    VideoGenerationsOut,
    VideoReferenceMediaIn,
)


@dataclass(frozen=True)
class GenerationRouteDependencies:
    http_error: Callable[..., Exception]
    generation_out: Callable[..., Awaitable[VideoGenerationOut]]
    decode_cursor: Callable[[str | None], tuple[datetime, str] | None]
    encode_cursor: Callable[[VideoGeneration], str]
    terminal_statuses: frozenset[str]
    invalidate_balance_cache: Callable[[str], Awaitable[None]]
    reject_canvas_retry: Callable[[VideoGeneration], None]
    create_record: Callable[..., Awaitable[VideoGenerationOut]]
    ensure_not_canvas_referenced: Callable[..., Awaitable[None]]
    logger: Any
    list_limit_max: int


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
            row.status = VideoGenerationStatus.CANCELED.value
            row.progress_stage = VideoGenerationStage.FINISHED.value
            row.progress_pct = 100
            row.error_code = "canceled"
            row.error_message = "cancelled before upstream submission"
            row.finished_at = now
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
                },
            )
            if transaction is None:
                await db.rollback()
                raise deps.http_error(
                    "video_hold_release_missing",
                    "video hold release transaction was not created",
                    409,
                )
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
                    url=(
                        item.get("url")
                        if isinstance(item.get("url"), str)
                        else None
                    ),
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
    video = (
        await db.execute(
            select(Video).where(
                Video.id == video_id,
                Video.user_id == user.id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise deps.http_error("not_found", "video not found", 404)
    await deps.ensure_not_canvas_referenced(db, video_id=video.id)
    video.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return Response(status_code=204)
