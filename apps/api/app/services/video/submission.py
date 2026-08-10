"""Video generation submission orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.constants import VideoGenerationStage, VideoGenerationStatus
from lumen_core.models import OutboxEvent, VideoGeneration, new_uuid7
from lumen_core.schemas import VideoCreateIn, VideoGenerationOut
from lumen_core.video_billing import (
    VideoBillingError,
    VideoCostEstimate,
    estimate_video_cost,
    video_billing_model,
    video_pricing_variant,
)

from ...billing_cache_state import (
    invalidate_balance_cache as _invalidate_balance_cache,
)
from ...idempotency.advisory import lock_user_key
from ...images.domain.variants import (
    VIDEO_REFERENCE_VARIANT as VIDEO_REFERENCE_IMAGE_KIND,
)
from ...services.active_user import (
    ActiveUserFenceError,
    ActiveUserSnapshot,
    AccountMode,
    account_mode_from_user,
    active_user_fence_http_error,
    lock_active_user_snapshot,
)
from ...video_reference_videos import VIDEO_REFERENCE_VIDEO_KIND
from ..video_publish import publish_video_queued
from .errors import video_http_error
from .options import allow_negative_balance, require_video_create_ready
from .presentation import generation_out
from .reference_media import (
    HAPPYHORSE_ASPECT_RATIOS,
    OMNI_FLASH_ASPECT_RATIOS,
    input_image_snapshot as load_input_image_snapshot,
    provider_prefers_public_media_url,
    provider_requires_public_media,
    reference_media_snapshots,
    reference_public_base_url,
    validate_provider_reference_media,
)
from .reference_snapshots import (
    lock_user_reference_media,  # noqa: F401 - test-facing monkeypatch hook
)


AsyncCallback = Callable[..., Awaitable[Any]]
SyncCallback = Callable[..., Any]

VIDEO_DEADLINE = timedelta(minutes=10)


async def invalidate_video_balance_cache(user_id: str) -> None:
    """Invalidate the wallet balance cache after a durable video mutation."""
    await _invalidate_balance_cache(user_id)


def request_fingerprint(body: VideoCreateIn) -> str:
    payload = body.model_dump(mode="json", exclude={"idempotency_key"})
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generation_request_fingerprint(row: VideoGeneration) -> str | None:
    if isinstance(row.request_fingerprint, str) and row.request_fingerprint:
        return row.request_fingerprint
    diagnostics = row.diagnostics or {}
    value = diagnostics.get("request_fingerprint")
    return value if isinstance(value, str) and value else None


def ensure_idempotent_replay_matches(
    row: VideoGeneration,
    expected_fingerprint: str,
) -> None:
    existing_fingerprint = generation_request_fingerprint(row)
    if existing_fingerprint and existing_fingerprint != expected_fingerprint:
        raise video_http_error(
            "idempotency_request_mismatch",
            "idempotency_key was already used with a different video request",
            409,
        )


async def _find_idempotent_generation(
    db: AsyncSession,
    *,
    user_id: str,
    idempotency_key: str,
) -> VideoGeneration | None:
    return (
        await db.execute(
            select(VideoGeneration).where(
                VideoGeneration.user_id == user_id,
                VideoGeneration.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


async def _render_idempotent_replay(
    db: AsyncSession,
    row: VideoGeneration,
    *,
    expected_fingerprint: str,
    defer_commit: bool,
    generation_renderer: AsyncCallback,
) -> VideoGenerationOut:
    ensure_idempotent_replay_matches(row, expected_fingerprint)
    if defer_commit:
        raise video_http_error(
            "idempotency_conflict",
            "idempotency_key conflict",
            409,
        )
    return await generation_renderer(db, row)


@dataclass(frozen=True, slots=True)
class VideoSubmissionContext:
    request: Request | None = None
    session_id: str | None = None
    active_user_snapshot: ActiveUserSnapshot | None = None
    idempotency_serialized: bool = False
    input_image_snapshot: tuple[str | None, str | None, str | None] | None = None
    reference_media_snapshot: list[dict[str, Any]] | None = None
    workflow_metadata: dict[str, Any] | None = None
    defer_commit: bool = False
    deferred_publish_payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class VideoSubmissionServices:
    require_ready: AsyncCallback = require_video_create_ready
    public_base_loader: AsyncCallback = reference_public_base_url
    input_snapshot_loader: AsyncCallback = load_input_image_snapshot
    reference_snapshot_loader: AsyncCallback = reference_media_snapshots
    reference_validator: SyncCallback = validate_provider_reference_media
    allow_negative_loader: AsyncCallback = allow_negative_balance
    wallet_loader: AsyncCallback = billing_core.get_wallet
    generation_renderer: AsyncCallback = generation_out
    balance_invalidator: AsyncCallback = invalidate_video_balance_cache
    queued_publisher: AsyncCallback = publish_video_queued


async def _lock_video_submission_user(
    db: AsyncSession,
    *,
    user_id: str,
    expected_account_mode: AccountMode,
    context: VideoSubmissionContext,
) -> ActiveUserSnapshot:
    snapshot = context.active_user_snapshot
    if snapshot is None:
        try:
            snapshot = await lock_active_user_snapshot(
                db,
                user_id,
                expected_account_mode,
                session_id=context.session_id,
            )
        except ActiveUserFenceError as exc:
            raise active_user_fence_http_error(exc) from exc
    elif snapshot.user.id != user_id:
        raise RuntimeError("video submission active-user snapshot does not match user")
    if snapshot.account_mode != "wallet":
        raise video_http_error(
            "account_mode_forbidden",
            "video generation requires wallet mode",
            403,
        )
    return snapshot


async def _find_or_serialize_video_submission(
    db: AsyncSession,
    body: VideoCreateIn,
    *,
    user_id: str,
    request_fingerprint_value: str,
    context: VideoSubmissionContext,
    services: VideoSubmissionServices,
) -> VideoGenerationOut | None:
    winner = await _find_idempotent_generation(
        db,
        user_id=user_id,
        idempotency_key=body.idempotency_key,
    )
    if winner is not None:
        return await _render_idempotent_replay(
            db,
            winner,
            expected_fingerprint=request_fingerprint_value,
            defer_commit=context.defer_commit,
            generation_renderer=services.generation_renderer,
        )
    if context.idempotency_serialized:
        if context.active_user_snapshot is None:
            raise RuntimeError(
                "serialized video submission requires an active-user snapshot"
            )
        return None
    await lock_user_key(
        db,
        "video-generation",
        user_id,
        body.idempotency_key,
    )
    winner = await _find_idempotent_generation(
        db,
        user_id=user_id,
        idempotency_key=body.idempotency_key,
    )
    if winner is None:
        return None
    return await _render_idempotent_replay(
        db,
        winner,
        expected_fingerprint=request_fingerprint_value,
        defer_commit=context.defer_commit,
        generation_renderer=services.generation_renderer,
    )


@dataclass(frozen=True, slots=True)
class _VideoSubmissionPlan:
    provider: Any
    input_storage_key: str | None
    input_sha256: str | None
    input_image_url: str | None
    reference_snapshots: list[dict[str, Any]]
    reference_public_base: str | None
    requires_public_media: bool
    prefers_public_media_url: bool
    upstream_model: str
    billing_model: str
    pricing_variant: str
    cost: VideoCostEstimate
    variant_diagnostics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _VideoBillingAdmission:
    provider: Any
    upstream_model: str
    billing_model: str
    pricing_variant: str
    cost: VideoCostEstimate
    allow_negative: bool


def _validate_provider_aspect_ratio(provider_kind: str, body: VideoCreateIn) -> None:
    if (
        provider_kind == "dashscope"
        and body.action in {"t2v", "reference"}
        and body.aspect_ratio != "adaptive"
        and body.aspect_ratio not in HAPPYHORSE_ASPECT_RATIOS
    ):
        raise video_http_error(
            "invalid_aspect_ratio",
            "aspect_ratio is not available for HappyHorse",
            422,
            model=body.model,
            aspect_ratio=body.aspect_ratio,
            available_aspect_ratios=list(HAPPYHORSE_ASPECT_RATIOS),
        )
    if (
        provider_kind == "omni_flash"
        and body.aspect_ratio not in OMNI_FLASH_ASPECT_RATIOS
    ):
        raise video_http_error(
            "invalid_aspect_ratio",
            "aspect_ratio is not available for Omni Flash",
            422,
            model=body.model,
            aspect_ratio=body.aspect_ratio,
            available_aspect_ratios=list(OMNI_FLASH_ASPECT_RATIOS),
        )


def _reference_variant_diagnostics(
    input_image_url: str | None,
    reference_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    used_image_variant = (
        isinstance(input_image_url, str)
        and f"variant={VIDEO_REFERENCE_IMAGE_KIND}" in input_image_url
    ) or any(
        item.get("upstream_reference_variant") == VIDEO_REFERENCE_IMAGE_KIND
        or (
            isinstance(item.get("url"), str)
            and f"variant={VIDEO_REFERENCE_IMAGE_KIND}" in item["url"]
        )
        for item in reference_snapshots
        if item.get("kind") == "image"
    )
    used_video_variant = any(
        item.get("upstream_reference_variant") == VIDEO_REFERENCE_VIDEO_KIND
        or (
            isinstance(item.get("url"), str)
            and f"variant={VIDEO_REFERENCE_VIDEO_KIND}" in item["url"]
        )
        for item in reference_snapshots
        if item.get("kind") == "video"
    )
    return {
        "reference_image_public_variant": (
            VIDEO_REFERENCE_IMAGE_KIND if used_image_variant else None
        ),
        "reference_video_public_variant": (
            VIDEO_REFERENCE_VIDEO_KIND if used_video_variant else None
        ),
        "reference_image_public_variant_error_count": sum(
            1
            for item in reference_snapshots
            if item.get("kind") == "image"
            and isinstance(item.get("upstream_reference_variant_error"), dict)
        ),
        "reference_video_public_variant_error_count": sum(
            1
            for item in reference_snapshots
            if item.get("kind") == "video"
            and isinstance(item.get("upstream_reference_variant_error"), dict)
        ),
    }


def _reference_media_for_admission(
    body: VideoCreateIn,
    fallback_snapshots: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if fallback_snapshots is not None:
        return [dict(item) for item in fallback_snapshots if isinstance(item, dict)]
    return [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        for item in body.reference_media
    ]


async def _prepare_video_billing_admission(
    db: AsyncSession,
    body: VideoCreateIn,
    *,
    reference_media_snapshot: list[dict[str, Any]] | None,
    services: VideoSubmissionServices,
) -> _VideoBillingAdmission:
    provider, estimates = await services.require_ready(db, body)
    upstream_model = provider.upstream_model_for(body.model, body.action)
    reference_media = _reference_media_for_admission(
        body,
        reference_media_snapshot,
    )
    services.reference_validator(
        provider.kind,
        reference_media,
        model=body.model,
        upstream_model=upstream_model,
    )
    _validate_provider_aspect_ratio(provider.kind, body)
    billing_model = video_billing_model(body.model, upstream_model)
    pricing_variant = video_pricing_variant(
        body.action,
        reference_media,
        resolution=body.resolution,
    )
    try:
        cost = await estimate_video_cost(
            db,
            model=billing_model,
            action=body.action,
            resolution=body.resolution,
            duration_s=body.duration_s,
            generate_audio=body.generate_audio,
            estimates=estimates,
            pricing_variant=pricing_variant,
        )
    except VideoBillingError as exc:
        raise video_http_error(exc.code, exc.message, exc.status_code) from exc
    if cost.hold_micro <= 0:
        raise video_http_error(
            "video_hold_invalid",
            "video hold amount must be positive",
            422,
        )
    return _VideoBillingAdmission(
        provider=provider,
        upstream_model=upstream_model,
        billing_model=billing_model,
        pricing_variant=pricing_variant,
        cost=cost,
        allow_negative=await services.allow_negative_loader(db),
    )


async def _ensure_video_wallet_admission(
    db: AsyncSession,
    *,
    user_id: str,
    admission: _VideoBillingAdmission,
    services: VideoSubmissionServices,
) -> None:
    wallet = await services.wallet_loader(
        db,
        user_id,
        lock=False,
    )
    if wallet is None:
        raise video_http_error(
            "WALLET_UNAVAILABLE",
            "wallet could not be initialized",
            503,
        )
    balance_micro = int(getattr(wallet, "balance_micro", 0) or 0)
    if not admission.allow_negative and balance_micro < admission.cost.hold_micro:
        raise video_http_error(
            "INSUFFICIENT_BALANCE",
            "insufficient wallet balance",
            402,
            required_micro=admission.cost.hold_micro,
            balance_micro=balance_micro,
        )


async def _prepare_video_submission(
    db: AsyncSession,
    body: VideoCreateIn,
    *,
    user_id: str,
    request: Request | None,
    input_image_snapshot: tuple[str | None, str | None, str | None] | None,
    reference_media_snapshot: list[dict[str, Any]] | None,
    admission: _VideoBillingAdmission,
    services: VideoSubmissionServices,
) -> _VideoSubmissionPlan:
    requires_public_media = provider_requires_public_media(admission.provider)
    prefers_public_media_url = provider_prefers_public_media_url(admission.provider)
    reference_public_base = await services.public_base_loader(
        request,
        db,
        body,
        reference_media_snapshot,
        requires_public_media=requires_public_media,
        prefers_public_media_url=prefers_public_media_url,
    )
    (
        input_storage_key,
        input_sha256,
        input_image_url,
    ) = await services.input_snapshot_loader(
        db,
        user_id=user_id,
        image_id=body.input_image_id,
        fallback_snapshot=input_image_snapshot,
        reference_public_base_url=(
            reference_public_base if prefers_public_media_url else None
        ),
        required_public_media=requires_public_media,
    )
    reference_snapshots = await services.reference_snapshot_loader(
        db,
        user_id=user_id,
        items=body.reference_media,
        fallback_snapshots=reference_media_snapshot,
        reference_public_base_url=reference_public_base,
        required_public_media=requires_public_media,
    )
    services.reference_validator(
        admission.provider.kind,
        reference_snapshots,
        model=body.model,
        upstream_model=admission.upstream_model,
    )
    return _VideoSubmissionPlan(
        provider=admission.provider,
        input_storage_key=input_storage_key,
        input_sha256=input_sha256,
        input_image_url=input_image_url,
        reference_snapshots=reference_snapshots,
        reference_public_base=reference_public_base,
        requires_public_media=requires_public_media,
        prefers_public_media_url=prefers_public_media_url,
        upstream_model=admission.upstream_model,
        billing_model=admission.billing_model,
        pricing_variant=admission.pricing_variant,
        cost=admission.cost,
        variant_diagnostics=_reference_variant_diagnostics(
            input_image_url,
            reference_snapshots,
        ),
    )


def _build_video_generation(
    body: VideoCreateIn,
    *,
    generation_id: str,
    user_id: str,
    request_fingerprint_value: str,
    workflow_metadata: dict[str, Any] | None,
    plan: _VideoSubmissionPlan,
) -> VideoGeneration:
    upstream_request = {
        "model": body.model,
        "requested_model": body.model,
        "billing_model": plan.billing_model,
        "provider_name": plan.provider.name,
        "provider_kind": plan.provider.kind,
        "upstream_model": plan.upstream_model,
        "input_image_url": plan.input_image_url,
        "reference_media": plan.reference_snapshots,
        "pricing_variant": plan.pricing_variant,
    }
    if workflow_metadata:
        upstream_request.update(workflow_metadata)
    return VideoGeneration(
        id=generation_id,
        user_id=user_id,
        action=body.action,
        model=body.model,
        provider_name=plan.provider.name,
        provider_kind=plan.provider.kind,
        prompt=body.prompt,
        input_image_id=body.input_image_id,
        input_image_storage_key=plan.input_storage_key,
        input_image_sha256=plan.input_sha256,
        duration_s=body.duration_s,
        resolution=body.resolution,
        aspect_ratio=body.aspect_ratio,
        fps=None,
        generate_audio=body.generate_audio,
        seed=body.seed,
        watermark=body.watermark,
        upstream_request=upstream_request,
        diagnostics={
            "request_fingerprint": request_fingerprint_value,
            "reference_media_count": len(plan.reference_snapshots),
            "pricing_variant": plan.pricing_variant,
            "billing_model": plan.billing_model,
            "requested_model": body.model,
            "requires_public_media": plan.requires_public_media,
            "prefers_public_media_url": plan.prefers_public_media_url,
            "reference_public_media_url_enabled": (
                plan.reference_public_base is not None
            ),
            **plan.variant_diagnostics,
        },
        status=VideoGenerationStatus.QUEUED.value,
        progress_stage=VideoGenerationStage.QUEUED.value,
        progress_pct=0,
        deadline_at=datetime.now(UTC) + VIDEO_DEADLINE,
        idempotency_key=body.idempotency_key,
        request_fingerprint=request_fingerprint_value,
        est_token_upper=plan.cost.estimated_tokens,
        est_cost_micro=plan.cost.hold_micro,
    )


async def create_video_generation_record(
    db: AsyncSession,
    body: VideoCreateIn,
    user: Any,
    *,
    context: VideoSubmissionContext | None = None,
    services: VideoSubmissionServices | None = None,
) -> VideoGenerationOut:
    context = context or VideoSubmissionContext()
    services = services or VideoSubmissionServices()
    expected_account_mode = account_mode_from_user(user)
    request_fingerprint_value = request_fingerprint(body)
    replay = await _find_or_serialize_video_submission(
        db,
        body,
        user_id=user.id,
        request_fingerprint_value=request_fingerprint_value,
        context=context,
        services=services,
    )
    if replay is not None:
        return replay

    snapshot = await _lock_video_submission_user(
        db,
        user_id=user.id,
        expected_account_mode=expected_account_mode,
        context=context,
    )
    user = snapshot.user

    admission = await _prepare_video_billing_admission(
        db,
        body,
        reference_media_snapshot=context.reference_media_snapshot,
        services=services,
    )
    await _ensure_video_wallet_admission(
        db,
        user_id=user.id,
        admission=admission,
        services=services,
    )
    plan = await _prepare_video_submission(
        db,
        body,
        user_id=user.id,
        request=context.request,
        input_image_snapshot=context.input_image_snapshot,
        reference_media_snapshot=context.reference_media_snapshot,
        admission=admission,
        services=services,
    )
    generation_id = new_uuid7()
    generation = _build_video_generation(
        body,
        generation_id=generation_id,
        user_id=user.id,
        request_fingerprint_value=request_fingerprint_value,
        workflow_metadata=context.workflow_metadata,
        plan=plan,
    )
    try:
        db.add(generation)
        await billing_core.hold(
            db,
            user.id,
            admission.cost.hold_micro,
            ref_type="video_generation",
            ref_id=generation_id,
            idempotency_key=f"video_generation:hold:{generation_id}",
            allow_negative=admission.allow_negative,
            meta={
                "model": body.model,
                "billing_model": admission.billing_model,
                "requested_model": body.model,
                "action": body.action,
                "resolution": body.resolution,
                "duration_s": body.duration_s,
                "estimated_tokens": admission.cost.estimated_tokens,
                "unit_price_micro": admission.cost.unit_price_micro,
                "provider_name": admission.provider.name,
                "upstream_model": admission.upstream_model,
                "reference_media_count": len(plan.reference_snapshots),
                "pricing_variant": admission.pricing_variant,
            },
        )
        payload = {
            "task_id": generation.id,
            "user_id": user.id,
            "kind": "video_generation",
        }
        outbox = OutboxEvent(
            kind="video_generation",
            payload=payload,
            published_at=None,
        )
        db.add(outbox)
        await db.flush()
        payload["outbox_id"] = str(outbox.id)
        outbox.payload = dict(payload)
        if context.deferred_publish_payload is not None:
            context.deferred_publish_payload.update(payload)
        if not context.defer_commit:
            await db.commit()
    except billing_core.BillingError as exc:
        if not context.defer_commit:
            await db.rollback()
        raise video_http_error(exc.code, exc.message, exc.status_code) from exc
    except IntegrityError as exc:
        if context.defer_commit:
            raise video_http_error(
                "idempotency_conflict",
                "idempotency_key conflict",
                409,
            ) from exc
        await db.rollback()
        winner = await _find_idempotent_generation(
            db,
            user_id=user.id,
            idempotency_key=body.idempotency_key,
        )
        if winner is not None:
            return await _render_idempotent_replay(
                db,
                winner,
                expected_fingerprint=request_fingerprint_value,
                defer_commit=False,
                generation_renderer=services.generation_renderer,
            )
        raise video_http_error(
            "idempotency_conflict",
            "idempotency_key conflict",
            409,
        ) from exc
    except Exception:
        if not context.defer_commit:
            await db.rollback()
        raise
    if not context.defer_commit:
        await db.refresh(generation)
        await services.balance_invalidator(user.id)
        await services.queued_publisher(payload)
    return await services.generation_renderer(db, generation)
