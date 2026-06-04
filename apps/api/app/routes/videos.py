"""Video generation API and media endpoints."""

from __future__ import annotations

import hashlib
import errno
import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    EV_VIDEO_CANCELED,
    EV_VIDEO_QUEUED,
    VideoGenerationStage,
    VideoGenerationStatus,
    task_channel,
)
from lumen_core.models import Image, OutboxEvent, PricingRule, Video, VideoGeneration
from lumen_core.models import new_uuid7
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    MoneyOut,
    VideoCreateIn,
    VideoGenerationOut,
    VideoGenerationsOut,
    VideoModelOptionOut,
    VideoOptionsOut,
    VideoOut,
    VideoPriceOptionOut,
)
from lumen_core.video_billing import (
    VIDEO_PRICING_SCOPE,
    VIDEO_PRICING_UNIT,
    VideoBillingError,
    estimate_video_cost,
)
from lumen_core.video_providers import (
    VIDEO_ACTIONS,
    parse_video_provider_config_json,
    select_video_provider,
)

from ..arq_pool import get_arq_pool
from ..billing_cache_state import invalidate_balance_cache
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..observability import task_publish_errors_total
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from ..sse_publish import publish_sse_event


router = APIRouter()
logger = logging.getLogger(__name__)

_DEFAULT_VIDEO_DURATIONS = [5, 10]
_DEFAULT_VIDEO_RESOLUTIONS = ["720p", "1080p"]
_DEFAULT_VIDEO_ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:5", "3:4", "4:3"]
_DEFAULT_VIDEO_FPS = [24, 30]
_VIDEO_DEADLINE = timedelta(minutes=10)
_VIDEO_LIST_LIMIT_MAX = 100
_VIDEO_TERMINAL_STATUSES = {
    VideoGenerationStatus.SUCCEEDED.value,
    VideoGenerationStatus.FAILED.value,
    VideoGenerationStatus.CANCELED.value,
    VideoGenerationStatus.EXPIRED.value,
}


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


def _money(amount_micro: int) -> MoneyOut:
    return MoneyOut(**billing_core.money_dict(int(amount_micro)))


def _video_binary_url(video_id: str) -> str:
    return f"/api/videos/{video_id}/binary"


def _video_poster_url(video_id: str, poster_storage_key: str | None) -> str | None:
    return f"/api/videos/{video_id}/poster" if poster_storage_key else None


def _video_out(video: Video) -> VideoOut:
    return VideoOut(
        id=video.id,
        url=_video_binary_url(video.id),
        poster_url=_video_poster_url(video.id, video.poster_storage_key),
        width=video.width,
        height=video.height,
        duration_ms=video.duration_ms,
        fps=video.fps,
        has_audio=video.has_audio,
        mime=video.mime,
        size_bytes=video.size_bytes,
        faststart=video.faststart,
        created_at=video.created_at,
    )


async def _video_for_generation(
    db: AsyncSession,
    video_generation_id: str,
) -> Video | None:
    return (
        await db.execute(
            select(Video).where(
                Video.owner_generation_id == video_generation_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _generation_out(
    db: AsyncSession, row: VideoGeneration, video: Video | None = None
) -> VideoGenerationOut:
    if video is None:
        video = await _video_for_generation(db, row.id)
    return VideoGenerationOut(
        id=row.id,
        action=row.action,
        model=row.model,
        prompt=row.prompt,
        input_image_id=row.input_image_id,
        duration_s=row.duration_s,
        resolution=row.resolution,
        aspect_ratio=row.aspect_ratio,
        fps=row.fps,
        generate_audio=row.generate_audio,
        seed=row.seed,
        status=row.status,
        progress_stage=row.progress_stage,
        progress_pct=row.progress_pct,
        provider_name=row.provider_name,
        provider_kind=row.provider_kind,
        est_token_upper=row.est_token_upper,
        est_cost=_money(row.est_cost_micro),
        billed_tokens=row.billed_tokens,
        billed_cost=_money(row.billed_cost_micro)
        if row.billed_cost_micro is not None
        else None,
        video=_video_out(video) if video is not None else None,
        error_code=row.error_code,
        error_message=row.error_message,
        diagnostics=row.diagnostics or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        submitted_at=row.submitted_at,
        finished_at=row.finished_at,
    )


async def _setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    return await get_setting(db, spec)


async def _video_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "video.enabled"), False
    )


async def _billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.enabled"), False
    )


async def _allow_negative_balance(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.allow_negative_balance"), False
    )


async def _video_provider_state(db: AsyncSession):
    raw_video = await _setting_raw(db, "video.providers")
    raw_shared = await _setting_raw(db, "providers")
    providers, _proxies, errors = parse_video_provider_config_json(
        raw_video,
        shared_provider_raw=raw_shared,
    )
    return providers, errors


async def _video_hold_estimates(db: AsyncSession) -> dict[str, Any]:
    raw = await _setting_raw(db, "video.token_hold_estimates")
    if not raw:
        raise _http(
            "video_estimates_missing",
            "video token hold estimates are not configured",
            503,
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _http(
            "video_estimates_invalid", "video token hold estimates are invalid", 503
        ) from exc
    if not isinstance(parsed, dict):
        raise _http(
            "video_estimates_invalid",
            "video token hold estimates must be an object",
            503,
        )
    return parsed


def _estimate_pairs(estimates: dict[str, Any]) -> tuple[list[int], list[str]]:
    durations: set[int] = set()
    resolutions: set[str] = set()
    for model_value in estimates.values():
        if not isinstance(model_value, dict):
            continue
        for action_value in model_value.values():
            if not isinstance(action_value, dict):
                continue
            for key in action_value:
                if not isinstance(key, str) or ":" not in key:
                    continue
                resolution, duration = key.rsplit(":", 1)
                try:
                    duration_s = int(duration)
                except ValueError:
                    continue
                if resolution:
                    resolutions.add(resolution)
                if duration_s > 0:
                    durations.add(duration_s)
    return (
        sorted(durations) or list(_DEFAULT_VIDEO_DURATIONS),
        sorted(resolutions) or list(_DEFAULT_VIDEO_RESOLUTIONS),
    )


def _request_fingerprint(body: VideoCreateIn) -> str:
    payload = body.model_dump(mode="json", exclude={"idempotency_key"})
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _video_price_options(db: AsyncSession) -> list[VideoPriceOptionOut]:
    rows = (
        (
            await db.execute(
                select(PricingRule).where(
                    PricingRule.scope == VIDEO_PRICING_SCOPE,
                    PricingRule.unit == VIDEO_PRICING_UNIT,
                    PricingRule.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    out: list[VideoPriceOptionOut] = []
    for row in rows:
        if row.variant not in VIDEO_ACTIONS:
            continue
        out.append(
            VideoPriceOptionOut(
                model=row.key,
                action=row.variant,  # type: ignore[arg-type]
                price=_money(row.price_micro),
                enabled=row.enabled,
                note=row.note,
            )
        )
    return out


@router.get("/options", response_model=VideoOptionsOut)
async def video_options(
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoOptionsOut:
    enabled = await _video_enabled(db)
    estimates: dict[str, Any] = {}
    unavailable_reason: str | None = None
    try:
        estimates = await _video_hold_estimates(db)
    except HTTPException as exc:
        unavailable_reason = (
            exc.detail.get("error", {}).get("code")
            if isinstance(exc.detail, dict)
            else "video_estimates_missing"
        )

    providers, provider_errors = await _video_provider_state(db)
    if provider_errors:
        unavailable_reason = "video_provider_config_invalid"
    prices = await _video_price_options(db)
    price_pairs = {(item.model, item.action) for item in prices}
    model_actions: dict[str, set[str]] = {}
    for provider in providers:
        mapping = provider.models or {}
        for key in mapping:
            if ":" not in key:
                for action in VIDEO_ACTIONS:
                    if provider.supports(key, action) and (key, action) in price_pairs:
                        model_actions.setdefault(key, set()).add(action)
                continue
            model, action = key.rsplit(":", 1)
            if (
                action in VIDEO_ACTIONS
                and provider.supports(model, action)
                and (model, action) in price_pairs
            ):
                model_actions.setdefault(model, set()).add(action)
    if enabled and not model_actions and unavailable_reason is None:
        unavailable_reason = "video_provider_or_pricing_missing"
    durations, resolutions = _estimate_pairs(estimates)
    return VideoOptionsOut(
        enabled=enabled and unavailable_reason is None,
        models=[
            VideoModelOptionOut(model=model, actions=sorted(actions))  # type: ignore[arg-type]
            for model, actions in sorted(model_actions.items())
        ],
        durations_s=durations,
        resolutions=resolutions,
        aspect_ratios=list(_DEFAULT_VIDEO_ASPECT_RATIOS),
        fps=list(_DEFAULT_VIDEO_FPS),
        generate_audio=True,
        pricing=prices,
        hold_estimates=estimates,
        unavailable_reason=None
        if enabled and unavailable_reason is None
        else unavailable_reason or "video_disabled",
    )


async def _require_video_create_ready(
    db: AsyncSession,
    body: VideoCreateIn,
) -> tuple[Any, dict[str, Any]]:
    if not await _video_enabled(db):
        raise _http("video_disabled", "video generation is disabled", 503)
    if not await _billing_enabled(db):
        raise _http("billing_disabled", "video generation requires wallet billing", 503)
    estimates = await _video_hold_estimates(db)
    durations, resolutions = _estimate_pairs(estimates)
    if body.duration_s not in durations:
        raise _http("invalid_duration", "duration_s is not available", 422)
    if body.resolution not in resolutions:
        raise _http("invalid_resolution", "resolution is not available", 422)
    if body.aspect_ratio not in _DEFAULT_VIDEO_ASPECT_RATIOS:
        raise _http("invalid_aspect_ratio", "aspect_ratio is not available", 422)
    if body.fps is not None and body.fps not in _DEFAULT_VIDEO_FPS:
        raise _http("invalid_fps", "fps is not available", 422)
    providers, provider_errors = await _video_provider_state(db)
    if provider_errors:
        raise _http("video_provider_config_invalid", "; ".join(provider_errors), 503)
    provider = select_video_provider(providers, model=body.model, action=body.action)
    if provider is None:
        raise _http(
            "video_provider_missing",
            "no enabled video provider supports this model/action",
            503,
        )
    return provider, estimates


async def _input_image_snapshot(
    db: AsyncSession,
    *,
    user_id: str,
    image_id: str | None,
    fallback_snapshot: tuple[str | None, str | None] | None = None,
) -> tuple[str | None, str | None]:
    if image_id is None:
        if fallback_snapshot is not None:
            return fallback_snapshot
        return None, None
    if fallback_snapshot is not None and fallback_snapshot[0]:
        return fallback_snapshot
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if img is None:
        if fallback_snapshot is not None:
            return fallback_snapshot
        raise _http("input_image_not_found", "input image not found", 404)
    return img.storage_key, img.sha256


async def _create_video_generation_record(
    db: AsyncSession,
    body: VideoCreateIn,
    user: CurrentUser,
    *,
    input_image_snapshot: tuple[str | None, str | None] | None = None,
) -> VideoGenerationOut:
    provider, estimates = await _require_video_create_ready(db, body)
    input_storage_key, input_sha256 = await _input_image_snapshot(
        db,
        user_id=user.id,
        image_id=body.input_image_id,
        fallback_snapshot=input_image_snapshot,
    )
    try:
        cost = await estimate_video_cost(
            db,
            model=body.model,
            action=body.action,
            resolution=body.resolution,
            duration_s=body.duration_s,
            fps=body.fps,
            generate_audio=body.generate_audio,
            estimates=estimates,
        )
    except VideoBillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    if cost.hold_micro <= 0:
        raise _http("video_hold_invalid", "video hold amount must be positive", 422)

    now = datetime.now(timezone.utc)
    request_fingerprint = _request_fingerprint(body)
    vg = VideoGeneration(
        id=new_uuid7(),
        user_id=user.id,
        action=body.action,
        model=body.model,
        provider_name=provider.name,
        provider_kind=provider.kind,
        prompt=body.prompt,
        input_image_id=body.input_image_id,
        input_image_storage_key=input_storage_key,
        input_image_sha256=input_sha256,
        duration_s=body.duration_s,
        resolution=body.resolution,
        aspect_ratio=body.aspect_ratio,
        fps=body.fps,
        generate_audio=body.generate_audio,
        seed=body.seed,
        watermark=body.watermark,
        upstream_request={
            "model": body.model,
            "provider_name": provider.name,
            "provider_kind": provider.kind,
            "upstream_model": provider.upstream_model_for(body.model, body.action),
        },
        diagnostics={"request_fingerprint": request_fingerprint},
        status=VideoGenerationStatus.QUEUED.value,
        progress_stage=VideoGenerationStage.QUEUED.value,
        progress_pct=0,
        deadline_at=now + _VIDEO_DEADLINE,
        idempotency_key=body.idempotency_key,
        request_fingerprint=request_fingerprint,
        est_token_upper=cost.estimated_tokens,
        est_cost_micro=cost.hold_micro,
    )
    db.add(vg)
    try:
        await billing_core.hold(
            db,
            user.id,
            cost.hold_micro,
            ref_type="video_generation",
            ref_id=vg.id,
            idempotency_key=f"video_generation:hold:{vg.id}",
            allow_negative=await _allow_negative_balance(db),
            meta={
                "model": body.model,
                "action": body.action,
                "resolution": body.resolution,
                "duration_s": body.duration_s,
                "estimated_tokens": cost.estimated_tokens,
                "unit_price_micro": cost.unit_price_micro,
                "provider_name": provider.name,
            },
        )
    except billing_core.BillingError as exc:
        await db.rollback()
        raise _http(exc.code, exc.message, exc.status_code) from exc
    payload = {
        "task_id": vg.id,
        "user_id": user.id,
        "kind": "video_generation",
    }
    outbox = OutboxEvent(kind="video_generation", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        winner = (
            await db.execute(
                select(VideoGeneration).where(
                    VideoGeneration.user_id == user.id,
                    VideoGeneration.idempotency_key == body.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if winner is not None:
            return await _generation_out(db, winner)
        raise _http("idempotency_conflict", "idempotency_key conflict", 409) from exc
    await db.refresh(vg)
    await invalidate_balance_cache(user.id)
    await _publish_video_queued(payload)
    return await _generation_out(db, vg)


async def _publish_video_queued(payload: dict[str, Any]) -> None:
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "run_video_generation",
            payload["task_id"],
            _job_id=arq_job_id(
                "video_generation",
                payload["task_id"],
                payload.get("outbox_id"),
            ),
        )
        redis = get_redis()
        await publish_sse_event(
            redis,
            user_id=payload["user_id"],
            channel=task_channel(payload["task_id"]),
            event_name=EV_VIDEO_QUEUED,
            data={
                "video_generation_id": payload["task_id"],
                "kind": "video_generation",
                "status": VideoGenerationStatus.QUEUED.value,
                "stage": VideoGenerationStage.QUEUED.value,
                "progress_pct": 0,
                "video_id": None,
                "error_code": None,
            },
        )
    except Exception:
        task_publish_errors_total.labels(kind="video_generation").inc()
        logger.warning(
            "best-effort video queued publish failed task_id=%s",
            payload.get("task_id"),
            exc_info=True,
        )


@router.post(
    "/generations",
    response_model=VideoGenerationOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_video_generation(
    body: VideoCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoGenerationOut:
    if getattr(user, "account_mode", "wallet") != "wallet":
        raise _http(
            "account_mode_forbidden", "video generation requires wallet mode", 403
        )
    existing = (
        await db.execute(
            select(VideoGeneration).where(
                VideoGeneration.user_id == user.id,
                VideoGeneration.idempotency_key == body.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return await _generation_out(db, existing)
    return await _create_video_generation_record(db, body, user)


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        ts, row_id = cursor.split("|", 1)
        return datetime.fromisoformat(ts), row_id
    except (ValueError, TypeError):
        raise _http("invalid_cursor", "cursor is invalid", 422)


def _encode_cursor(row: VideoGeneration) -> str:
    return f"{row.created_at.isoformat()}|{row.id}"


@router.get("/generations", response_model=VideoGenerationsOut)
async def list_video_generations(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=_VIDEO_LIST_LIMIT_MAX),
) -> VideoGenerationsOut:
    stmt = select(VideoGeneration).where(VideoGeneration.user_id == user.id)
    if status:
        stmt = stmt.where(VideoGeneration.status == status)
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        created_at, row_id = decoded
        stmt = stmt.where(
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
                stmt.order_by(
                    VideoGeneration.created_at.desc(), VideoGeneration.id.desc()
                ).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page = rows[:limit]
    next_cursor = _encode_cursor(rows[limit]) if len(rows) > limit else None
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
            video.owner_generation_id: video for video in videos
        }
    return VideoGenerationsOut(
        items=[
            await _generation_out(db, row, videos_by_generation_id.get(row.id))
            for row in page
        ],
        next_cursor=next_cursor,
    )


@router.get("/generations/{generation_id}", response_model=VideoGenerationOut)
async def get_video_generation(
    generation_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
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
        raise _http("not_found", "video generation not found", 404)
    return await _generation_out(db, row)


@router.post(
    "/generations/{generation_id}/cancel",
    response_model=VideoGenerationOut,
    dependencies=[Depends(verify_csrf)],
)
async def cancel_video_generation(
    generation_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoGenerationOut:
    row = (
        await db.execute(
            select(VideoGeneration)
            .where(
                VideoGeneration.id == generation_id, VideoGeneration.user_id == user.id
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("not_found", "video generation not found", 404)
    now = datetime.now(timezone.utc)
    if row.status not in _VIDEO_TERMINAL_STATUSES:
        row.cancel_requested_at = row.cancel_requested_at or now
        if row.status == VideoGenerationStatus.QUEUED.value and not row.provider_task_id:
            row.status = VideoGenerationStatus.CANCELED.value
            row.progress_stage = VideoGenerationStage.FINISHED.value
            row.progress_pct = 100
            row.error_code = "canceled"
            row.error_message = "cancelled before upstream submission"
            row.finished_at = now
            await billing_core.release(
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
    await db.commit()
    await db.refresh(row)
    await invalidate_balance_cache(user.id)
    try:
        await publish_sse_event(
            get_redis(),
            user_id=user.id,
            channel=task_channel(row.id),
            event_name=EV_VIDEO_CANCELED,
            data={
                "video_generation_id": row.id,
                "kind": "video_generation",
                "status": row.status,
                "stage": row.progress_stage,
                "progress_pct": row.progress_pct,
                "video_id": None,
                "error_code": row.error_code,
            },
        )
    except Exception:
        logger.warning("video cancel SSE publish failed id=%s", row.id, exc_info=True)
    return await _generation_out(db, row)


@router.post(
    "/generations/{generation_id}/retry",
    response_model=VideoGenerationOut,
    dependencies=[Depends(verify_csrf)],
)
async def retry_video_generation(
    generation_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
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
        raise _http("not_found", "video generation not found", 404)
    body = VideoCreateIn(
        action=row.action,  # type: ignore[arg-type]
        model=row.model,
        prompt=row.prompt,
        input_image_id=row.input_image_id,
        duration_s=row.duration_s,
        resolution=row.resolution,  # type: ignore[arg-type]
        aspect_ratio=row.aspect_ratio,
        fps=row.fps,
        generate_audio=row.generate_audio,
        seed=row.seed,
        watermark=row.watermark,
        idempotency_key=f"retry:{row.id}:{new_uuid7()}",
    )
    return await _create_video_generation_record(
        db,
        body,
        user,
        input_image_snapshot=(row.input_image_storage_key, row.input_image_sha256),
    )


@router.delete(
    "/{video_id}",
    status_code=204,
    dependencies=[Depends(verify_csrf)],
)
async def delete_video(
    video_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
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
        raise _http("not_found", "video not found", 404)
    video.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return Response(status_code=204)


def _fs_path(storage_key: str) -> Path:
    root = Path(settings.storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise _http("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise _http("invalid_path", "absolute storage paths are not allowed", 400)
    p = (root / key_path).resolve()
    try:
        p.relative_to(root)
    except ValueError as exc:
        raise _http("invalid_path", "storage path escapes root", 400) from exc
    return p


def _open_regular_file_no_symlink(path: Path) -> tuple[BinaryIO, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _http("not_found", "binary missing", 404) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _http(
                "invalid_path", "symlink storage paths are not allowed", 400
            ) from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _http("not_found", "binary missing", 404)
        return os.fdopen(fd, "rb"), int(st.st_size)
    except Exception:
        os.close(fd)
        raise


def _iter_file_and_close(
    f: BinaryIO,
    *,
    start: int = 0,
    length: int | None = None,
) -> Iterator[bytes]:
    try:
        if start:
            f.seek(start)
        remaining = length
        while remaining is None or remaining > 0:
            chunk_size = (
                1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            )
            data = f.read(chunk_size)
            if not data:
                break
            if remaining is not None:
                remaining -= len(data)
            yield data
    finally:
        f.close()


def _quote_etag(etag: str) -> str:
    value = etag.strip()
    if value.startswith('"') and value.endswith('"'):
        return value
    return f'"{value}"'


def _parse_range(range_header: str, size: int) -> tuple[int, int] | None:
    if not range_header.startswith("bytes=") or "," in range_header:
        return None
    spec = range_header.removeprefix("bytes=").strip()
    if "-" not in spec:
        return None
    start_raw, end_raw = spec.split("-", 1)
    try:
        if start_raw == "":
            suffix = int(end_raw)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= size:
        return None
    return start, min(end, size - 1)


def _media_response(
    request: Request,
    path: Path,
    *,
    media_type: str,
    etag: str,
    last_modified: datetime | None,
    immutable: bool,
) -> Response:
    quoted_etag = _quote_etag(etag)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": (
            "private, max-age=31536000, immutable"
            if immutable
            else "private, max-age=3600"
        ),
        "ETag": quoted_etag,
    }
    if last_modified is not None:
        headers["Last-Modified"] = format_datetime(last_modified, usegmt=True)
    if request.headers.get("if-none-match") == quoted_etag:
        return Response(status_code=304, headers=headers)
    f, size = _open_regular_file_no_symlink(path)
    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_range(range_header, size)
        if parsed is None:
            f.close()
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{size}"},
            )
        start, end = parsed
        length = end - start + 1
        return StreamingResponse(
            _iter_file_and_close(f, start=start, length=length),
            status_code=206,
            media_type=media_type,
            headers={
                **headers,
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
            },
        )
    return StreamingResponse(
        _iter_file_and_close(f),
        media_type=media_type,
        headers={**headers, "Content-Length": str(size)},
    )


async def _owned_video(db: AsyncSession, user_id: str, video_id: str) -> Video:
    video = (
        await db.execute(
            select(Video).where(
                Video.id == video_id,
                Video.user_id == user_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise _http("not_found", "video not found", 404)
    return video


@router.get("/{video_id}/binary")
async def video_binary(
    video_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    video = await _owned_video(db, user.id, video_id)
    return _media_response(
        request,
        _fs_path(video.storage_key),
        media_type=video.mime,
        etag=video.etag or video.sha256,
        last_modified=video.updated_at,
        immutable=True,
    )


@router.get("/{video_id}/poster")
async def video_poster(
    video_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    video = await _owned_video(db, user.id, video_id)
    if not video.poster_storage_key:
        raise _http("not_found", "poster not found", 404)
    return _media_response(
        request,
        _fs_path(video.poster_storage_key),
        media_type="image/jpeg",
        etag=f"{video.etag}:poster",
        last_modified=video.updated_at,
        immutable=True,
    )
