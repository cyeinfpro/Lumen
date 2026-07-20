"""提示词增强（Prompt Enhancement）。

POST /prompts/enhance — 流式返回 AI 优化后的图像生成提示词。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import secrets
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import Image, Video, new_uuid7
from lumen_core.pricing import UsageTokens, parse_usage
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    build_effective_provider_config,
    endpoint_kind_allowed,
    provider_supports_route,
    weighted_priority_order,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.vision_tagging import image_record_to_data_url

from ..billing_cache_state import invalidate_balance_cache
from ..config import settings
from ..db import SessionLocal, get_db
from ..deps import CurrentUser, verify_csrf
from ..audit import hash_email, write_audit
from ..public_urls import resolve_public_base_url
from ..ratelimit import RateLimiter
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from ..task_billing import (
    EnhanceBillingContext as _EnhanceBillingContext,
    EnhanceUsageCapture as _EnhanceUsageCapture,
    enhance_pricing_snapshot_key as _enhance_pricing_snapshot_key,
    rate_multiplier_x10000 as _rate_multiplier_x10000,
)
from ._prompt_enhance_templates import (
    ENHANCE_SYSTEM_PROMPT,
    VIDEO_ENHANCE_SYSTEM_PROMPT,
    VIDEO_ENHANCE_VARIANT_SYSTEM_PROMPT_TEMPLATE,
)
from .prompt_parts import content as _prompt_content
from .prompt_parts import failover as _prompt_failover
from .prompt_parts import keepalive as _prompt_keepalive
from .prompt_parts import upstream as _prompt_upstream

logger = logging.getLogger(__name__)
httpx = _prompt_upstream.httpx

_VIDEO_REFERENCE_ACCESS_TOKEN_TTL = timedelta(hours=24)

router = APIRouter(
    prefix="/prompts",
    tags=["prompts"],
    dependencies=[Depends(verify_csrf)],
)

_PROVIDER_RR_COUNTERS: dict[int, int] = {}
_PROVIDER_RR_LOCK = asyncio.Lock()
_RETRYABLE_HTTP_STATUS = _prompt_upstream.RETRYABLE_HTTP_STATUS
_FALLBACK_400_MARKERS = _prompt_upstream.FALLBACK_400_MARKERS
PROMPTS_ENHANCE_LIMITER = RateLimiter(capacity=20, refill_per_sec=20 / 60)
_PROMPT_ENHANCE_MEDIA_MAX_BYTES = 18 * 1024 * 1024
_PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES = 24 * 1024 * 1024
_PROMPT_ENHANCE_KEEPALIVE_SECONDS = 10.0
_PROMPT_ENHANCE_KEEPALIVE_CHUNK = ": keep-alive\n\n"
_PROMPT_ENHANCE_CONNECT_TIMEOUT_SECONDS = 10.0
_PROMPT_ENHANCE_READ_TIMEOUT_SECONDS = 25.0
_PROMPT_ENHANCE_WRITE_TIMEOUT_SECONDS = 10.0
_PROMPT_ENHANCE_POOL_TIMEOUT_SECONDS = 10.0
_PROMPT_ENHANCE_RELEASE_TASKS: set[asyncio.Task[None]] = set()

_EnhanceAttempt = _prompt_upstream.EnhanceAttempt
_EnhanceProviderError = _prompt_upstream.EnhanceProviderError
_ENHANCE_ATTEMPTS = _prompt_upstream.ENHANCE_ATTEMPTS


class EnhanceIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


VideoEnhanceIn = _prompt_content.VideoEnhanceIn


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


def _responses_url(base_url: str) -> str:
    return _prompt_upstream.responses_url(base_url)


def _video_enhance_system_prompt(variant_count: int) -> str:
    if variant_count <= 1:
        return VIDEO_ENHANCE_SYSTEM_PROMPT
    return VIDEO_ENHANCE_VARIANT_SYSTEM_PROMPT_TEMPLATE.format(
        variant_count=variant_count
    )


def _provider_allows_prompt_enhance(provider: ProviderDefinition) -> bool:
    return (
        "chat" in provider.purposes
        and endpoint_kind_allowed(provider, "responses")
        and provider_supports_route(
            provider,
            route="text",
            endpoint_kind="responses",
        )
    )


async def _resolve_provider_order(db: AsyncSession) -> list[ProviderDefinition]:
    """Read Provider Pool, with legacy UPSTREAM_* env fallback only if absent."""
    spec_providers = get_spec("providers")
    raw_providers = await get_setting(db, spec_providers) if spec_providers else None
    providers, _proxies, errors = build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    for err in errors:
        logger.warning("%s", err)
    providers = [p for p in providers if _provider_allows_prompt_enhance(p)]
    async with _PROVIDER_RR_LOCK:
        return weighted_priority_order(providers, _PROVIDER_RR_COUNTERS)


def _build_enhance_body(
    text: str,
    attempt: _EnhanceAttempt,
    *,
    system_prompt: str = ENHANCE_SYSTEM_PROMPT,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _prompt_upstream.build_enhance_body(
        text,
        attempt,
        system_prompt=system_prompt,
        content=content,
        metadata=metadata,
    )


def _storage_path(storage_key: str) -> Path:
    root = Path(settings.storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise _http("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise _http("invalid_path", "absolute storage paths are not allowed", 400)
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise _http("invalid_path", "storage path escapes root", 400) from None
    return path


async def _owned_image(db: AsyncSession, *, user_id: str, image_id: str) -> Image:
    image = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if image is None:
        raise _http("image_not_found", "image not found", 404)
    return image


async def _owned_video(db: AsyncSession, *, user_id: str, video_id: str) -> Video:
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
        raise _http("video_not_found", "video not found", 404)
    return video


async def _image_data_url(image: Image) -> str | None:
    if image.size_bytes and image.size_bytes > _PROMPT_ENHANCE_MEDIA_MAX_BYTES:
        return None
    try:
        raw = await asyncio.to_thread(_storage_path(image.storage_key).read_bytes)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "prompt enhance read image failed image_id=%s key=%s err=%s",
            image.id,
            image.storage_key,
            exc,
        )
        return None
    if len(raw) > _PROMPT_ENHANCE_MEDIA_MAX_BYTES:
        return None
    return image_record_to_data_url(image, raw)


async def _video_poster_data_url(video: Video) -> str | None:
    key = (video.poster_storage_key or "").strip()
    if not key:
        return None
    try:
        raw = await asyncio.to_thread(_storage_path(key).read_bytes)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "prompt enhance read video poster failed video_id=%s key=%s err=%s",
            video.id,
            key,
            exc,
        )
        return None
    if not raw or len(raw) > _PROMPT_ENHANCE_MEDIA_MAX_BYTES:
        return None
    mime, _encoding = mimetypes.guess_type(key)
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _append_input_image_with_budget(
    content: list[dict[str, Any]],
    image_url: str,
    *,
    media_payload_bytes: int,
) -> tuple[bool, int]:
    return _prompt_content.append_input_image_with_budget(
        content,
        image_url,
        media_payload_bytes=media_payload_bytes,
        media_total_max_bytes=_PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES,
    )


def _external_image_url_for_input(url: str | None) -> str | None:
    return _prompt_content.external_image_url_for_input(url)


def _append_video_context_line(lines: list[str], key: str, value: Any) -> None:
    _prompt_content.append_video_context_line(lines, key, value)


def _reference_anchor(ref_id: str | None, kind: str, index: int) -> str:
    return _prompt_content.reference_anchor(ref_id, kind, index)


def _video_reference_public_url(video: Video, public_base_url: str) -> tuple[str, bool]:
    metadata = dict(video.metadata_jsonb or {})
    token = metadata.get("reference_access_token")
    expires_raw = metadata.get("reference_access_token_expires_at")
    expires_at = None
    if isinstance(expires_raw, str) and expires_raw.strip():
        with suppress(ValueError):
            expires_at = datetime.fromisoformat(expires_raw)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            else:
                expires_at = expires_at.astimezone(timezone.utc)
    changed = False
    if (
        not isinstance(token, str)
        or not token
        or expires_at is None
        or expires_at <= datetime.now(timezone.utc)
    ):
        token = secrets.token_urlsafe(32)
        metadata["reference_access_token"] = token
        changed = True
    metadata["reference_access_token_expires_at"] = (
        datetime.now(timezone.utc) + _VIDEO_REFERENCE_ACCESS_TOKEN_TTL
    ).isoformat()
    video.metadata_jsonb = metadata
    changed = True
    query = urlencode({"token": token})
    return (
        f"{public_base_url.rstrip('/')}/api/videos/reference/{video.id}/binary?{query}",
        changed,
    )


async def _resolve_optional_public_base_url(
    request: Request,
    db: AsyncSession,
) -> str | None:
    try:
        return await resolve_public_base_url(request, db)
    except Exception as exc:  # noqa: BLE001
        logger.info("prompt enhance public base unavailable: %s", exc)
        return None


async def _build_video_enhance_content(
    body: VideoEnhanceIn,
    *,
    request: Request,
    db: AsyncSession,
    user_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    runtime = _prompt_content.ContentRuntime(
        owned_image=_owned_image,
        owned_video=_owned_video,
        image_data_url=_image_data_url,
        video_poster_data_url=_video_poster_data_url,
        resolve_public_base_url=_resolve_optional_public_base_url,
        video_reference_public_url=_video_reference_public_url,
    )
    return await _prompt_content.build_video_enhance_content(
        body,
        request=request,
        db=db,
        user_id=user_id,
        runtime=runtime,
        media_total_max_bytes=_PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES,
    )


async def _setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    try:
        return await get_setting(db, spec)
    except (AssertionError, IndexError):
        if key.startswith("billing."):
            return None
        raise


async def _billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.enabled"),
        False,
    )


async def _billing_cache_aware(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.cache_aware"),
        True,
    )


async def _billing_allow_negative(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.allow_negative_balance"),
        False,
    )


async def _prepare_prompt_enhance_billing(
    db: AsyncSession,
    user: Any,
) -> _EnhanceBillingContext | None:
    if getattr(user, "account_mode", "wallet") != "wallet":
        return None
    if not await _billing_enabled(db):
        return None

    request_id = new_uuid7()
    rate_multiplier_x10000 = _rate_multiplier_x10000(user)
    cache_aware = await _billing_cache_aware(db)
    allow_negative = await _billing_allow_negative(db)
    pricing_snapshots: dict[str, dict[str, Any]] = {}
    preview = 0
    for attempt in _ENHANCE_ATTEMPTS:
        service_tier = attempt.service_tier or "standard"
        snapshot_key = _enhance_pricing_snapshot_key(attempt.model, service_tier)
        if snapshot_key in pricing_snapshots:
            continue
        try:
            snapshot = await billing_core.completion_pricing_snapshot(
                db,
                model=attempt.model,
                service_tier=service_tier,
            )
            attempt_preview = billing_core.completion_breakdown_from_snapshot(
                snapshot,
                model=attempt.model,
                tokens=billing_core.UsageTokens(input_tokens=1, output_tokens=1),
                rate_multiplier_x10000=rate_multiplier_x10000,
                service_tier=service_tier,
            ).actual_cost_micro
        except billing_core.BillingError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": {"code": exc.code, "message": exc.message}},
            ) from exc
        pricing_snapshots[snapshot_key] = snapshot
        preview = max(preview, int(attempt_preview))
    if preview <= 0 and rate_multiplier_x10000 > 0:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "PRICING_MISSING",
                    "message": (
                        "missing enabled chat pricing rule for "
                        f"{_ENHANCE_ATTEMPTS[0].model}"
                    ),
                }
            },
        )
    hold_amount = 0 if rate_multiplier_x10000 == 0 else max(10_000, int(preview or 0))
    if hold_amount > 0:
        try:
            await billing_core.hold(
                db,
                user.id,
                hold_amount,
                ref_type="prompt_enhance",
                ref_id=request_id,
                idempotency_key=f"prompt_enhance:hold:{request_id}",
                allow_negative=allow_negative,
                meta={
                    "route": "prompts.enhance",
                    "model": _ENHANCE_ATTEMPTS[0].model,
                    "service_tier": _ENHANCE_ATTEMPTS[0].service_tier or "standard",
                    "estimated_cost_micro": preview,
                    "preauth_micro": hold_amount,
                    "pricing_snapshots": pricing_snapshots,
                    "rate_multiplier_x10000": rate_multiplier_x10000,
                },
            )
        except billing_core.BillingError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": {"code": exc.code, "message": exc.message}},
            ) from exc
        await db.commit()
        await invalidate_balance_cache(user.id)
    return _EnhanceBillingContext(
        db=db,
        user_id=user.id,
        user_email=getattr(user, "email", None),
        request_id=request_id,
        rate_multiplier_x10000=rate_multiplier_x10000,
        cache_aware=cache_aware,
        allow_negative=allow_negative,
        hold_amount_micro=hold_amount,
        pricing_snapshots=pricing_snapshots,
    )


def _capture_enhance_usage(
    capture: _EnhanceUsageCapture | None,
    event: dict[str, Any],
    *,
    provider: ProviderDefinition,
    attempt: _EnhanceAttempt,
) -> None:
    if capture is None:
        return
    response = event.get("response")
    response_obj = response if isinstance(response, dict) else {}
    usage = event.get("usage")
    if not isinstance(usage, dict):
        usage = response_obj.get("usage")
    if not isinstance(usage, dict):
        return

    response_id = response_obj.get("id") or event.get("response_id")
    model = response_obj.get("model") or event.get("model") or attempt.model
    capture.provider_name = provider.name
    capture.model = model if isinstance(model, str) and model.strip() else attempt.model
    capture.service_tier = attempt.service_tier or "standard"
    capture.pricing_snapshot_key = _enhance_pricing_snapshot_key(
        attempt.model,
        capture.service_tier,
    )
    capture.response_id = (
        response_id if isinstance(response_id, str) and response_id.strip() else None
    )
    capture.usage = usage


def _normalize_usage_for_billing(
    usage: UsageTokens,
    *,
    cache_aware: bool,
) -> UsageTokens:
    if cache_aware:
        return usage.normalized()
    legacy_cache_input_tokens = usage.cache_read_tokens + usage.cache_creation_tokens
    return UsageTokens(
        input_tokens=usage.input_tokens + legacy_cache_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        image_output_tokens=usage.image_output_tokens,
    ).normalized()


def _usage_is_empty(usage: UsageTokens) -> bool:
    return all(
        value <= 0
        for value in (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_creation_tokens,
            usage.cache_creation_5m_tokens,
            usage.cache_creation_1h_tokens,
            usage.reasoning_tokens,
            usage.image_output_tokens,
        )
    )


def _held_amount_breakdown(
    billing: _EnhanceBillingContext,
    *,
    cost: int | None = None,
) -> billing_core.CostBreakdown:
    actual_cost = billing.hold_amount_micro if cost is None else cost
    return billing_core.CostBreakdown(
        input_cost_micro=actual_cost,
        output_cost_micro=0,
        cache_read_cost_micro=0,
        cache_creation_cost_micro=0,
        image_output_cost_micro=0,
        reasoning_cost_micro=0,
        long_context_applied=False,
        priority_tier_applied=False,
        rate_multiplier_x10000=billing.rate_multiplier_x10000,
        total_cost_micro=actual_cost,
        actual_cost_micro=actual_cost,
        pricing_source="held_amount_fallback",
    )


async def _audit_held_amount_fallback(
    billing: _EnhanceBillingContext,
    *,
    model: str,
    usage: UsageTokens,
    error: str,
) -> None:
    await write_audit(
        billing.db,
        event_type="billing.pricing.hold_fallback_after_upstream",
        user_id=billing.user_id,
        actor_email_hash=hash_email(billing.user_email),
        details={
            "scope": "chat_model",
            "model": model,
            "prompt_enhance_id": billing.request_id,
            "usage": usage.model_dump(),
            "actual_micro": billing.hold_amount_micro,
            "error": error,
        },
        autocommit=False,
    )


async def _resolve_prompt_enhance_breakdown(
    billing: _EnhanceBillingContext,
    capture: _EnhanceUsageCapture,
    *,
    model: str,
    usage: UsageTokens,
) -> billing_core.CostBreakdown:
    try:
        snapshot = billing.pricing_snapshots.get(
            capture.pricing_snapshot_key
            or _enhance_pricing_snapshot_key(model, capture.service_tier)
        )
        if snapshot is not None:
            return billing_core.completion_breakdown_from_snapshot(
                snapshot,
                model=model,
                tokens=usage,
                rate_multiplier_x10000=billing.rate_multiplier_x10000,
                service_tier=capture.service_tier,
            )
        return await billing_core.estimate_completion_breakdown(
            billing.db,
            model=model,
            tokens=usage,
            rate_multiplier_x10000=billing.rate_multiplier_x10000,
            service_tier=capture.service_tier,
        )
    except billing_core.BillingError as exc:
        if (
            exc.code not in {"PRICING_MISSING", "PRICING_SNAPSHOT_INVALID"}
            or billing.hold_amount_micro <= 0
        ):
            raise
        await _audit_held_amount_fallback(
            billing,
            model=model,
            usage=usage,
            error=exc.message,
        )
        return _held_amount_breakdown(billing)


def _effective_prompt_enhance_cost(
    billing: _EnhanceBillingContext,
    breakdown: billing_core.CostBreakdown,
) -> tuple[int, billing_core.CostBreakdown]:
    cost = breakdown.actual_cost_micro
    if cost > 0 or billing.hold_amount_micro <= 0:
        return cost, breakdown
    cost = billing.hold_amount_micro
    return cost, _held_amount_breakdown(billing, cost=cost)


async def _audit_fallback_pricing(
    billing: _EnhanceBillingContext,
    *,
    breakdown: billing_core.CostBreakdown,
    model: str,
    ref_id: str,
    usage: UsageTokens,
) -> None:
    if breakdown.pricing_source != "fallback":
        return
    await write_audit(
        billing.db,
        event_type="billing.pricing.fallback_used",
        user_id=billing.user_id,
        actor_email_hash=hash_email(billing.user_email),
        details={
            "model": model,
            "prompt_enhance_id": ref_id,
            "usage": usage.model_dump(),
        },
        autocommit=False,
    )


def _prompt_enhance_tx_meta(
    billing: _EnhanceBillingContext,
    capture: _EnhanceUsageCapture,
    *,
    breakdown: billing_core.CostBreakdown,
    model: str,
    response_id: str,
    usage: UsageTokens,
) -> dict[str, Any]:
    return {
        "route": "prompts.enhance",
        "model": model,
        "provider": capture.provider_name,
        "response_id": response_id,
        "tokens_in": usage.input_tokens,
        "tokens_out": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_creation_5m_tokens": usage.cache_creation_5m_tokens,
        "cache_creation_1h_tokens": usage.cache_creation_1h_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "image_output_tokens": usage.image_output_tokens,
        "cost_breakdown": breakdown.model_dump(),
        "rate_multiplier_x10000": billing.rate_multiplier_x10000,
        "service_tier": capture.service_tier,
    }


async def _settle_or_charge_prompt_enhance(
    billing: _EnhanceBillingContext,
    *,
    cost: int,
    ref_id: str,
    tx_meta: dict[str, Any],
) -> Any:
    if billing.hold_amount_micro > 0:
        return await billing_core.settle(
            billing.db,
            billing.user_id,
            ref_type="prompt_enhance",
            ref_id=ref_id,
            actual_micro=cost,
            idempotency_key=f"prompt_enhance:settle:{ref_id}",
            allow_negative=billing.allow_negative,
            meta={**tx_meta, "preauth_micro": billing.hold_amount_micro},
        )
    return await billing_core.charge(
        billing.db,
        billing.user_id,
        cost,
        ref_type="prompt_enhance",
        ref_id=ref_id,
        idempotency_key=f"prompt_enhance:{ref_id}",
        allow_negative=billing.allow_negative,
        record_zero=True,
        kind="charge_completion",
        meta=tx_meta,
    )


async def _audit_prompt_enhance_charge(
    billing: _EnhanceBillingContext,
    capture: _EnhanceUsageCapture,
    tx: Any,
    *,
    breakdown: billing_core.CostBreakdown,
    cost: int,
    ref_id: str,
    response_id: str,
    usage: UsageTokens,
) -> None:
    if tx is None:
        return
    await write_audit(
        billing.db,
        event_type="wallet.charge.completion",
        user_id=billing.user_id,
        actor_email_hash=hash_email(billing.user_email),
        details={
            "completion_id": ref_id,
            "prompt_enhance_id": ref_id,
            "response_id": response_id,
            "route": "prompts.enhance",
            "cost_micro": cost,
            "usage": usage.model_dump(),
            "cost_breakdown": breakdown.model_dump(),
            "service_tier": capture.service_tier,
            "amount_micro": tx.amount_micro,
            "balance_after": tx.balance_after,
        },
        autocommit=False,
    )
    if cost != 0 or billing.rate_multiplier_x10000 != 0:
        return
    await write_audit(
        billing.db,
        event_type="wallet.charge.zero_rate",
        user_id=billing.user_id,
        actor_email_hash=hash_email(billing.user_email),
        details={
            "prompt_enhance_id": ref_id,
            "response_id": response_id,
            "tx_id": tx.id,
            "ref_type": "prompt_enhance",
            "ref_id": ref_id,
            "rate_multiplier_x10000": 0,
        },
        autocommit=False,
    )


async def _charge_prompt_enhance(
    billing: _EnhanceBillingContext,
    capture: _EnhanceUsageCapture,
) -> None:
    if not capture.usage:
        await _release_prompt_enhance_hold(billing, reason="missing_usage")
        return
    model = capture.model or _ENHANCE_ATTEMPTS[0].model
    usage = _normalize_usage_for_billing(
        parse_usage(model, capture.usage),
        cache_aware=billing.cache_aware,
    )
    if _usage_is_empty(usage):
        await _release_prompt_enhance_hold(billing, reason="zero_usage")
        return
    breakdown = await _resolve_prompt_enhance_breakdown(
        billing,
        capture,
        model=model,
        usage=usage,
    )
    response_id = capture.response_id or billing.request_id
    ref_id = billing.request_id if billing.hold_amount_micro > 0 else response_id
    cost, breakdown = _effective_prompt_enhance_cost(billing, breakdown)
    await _audit_fallback_pricing(
        billing,
        breakdown=breakdown,
        model=model,
        ref_id=ref_id,
        usage=usage,
    )
    tx_meta = _prompt_enhance_tx_meta(
        billing,
        capture,
        breakdown=breakdown,
        model=model,
        response_id=response_id,
        usage=usage,
    )
    tx = await _settle_or_charge_prompt_enhance(
        billing,
        cost=cost,
        ref_id=ref_id,
        tx_meta=tx_meta,
    )
    await _audit_prompt_enhance_charge(
        billing,
        capture,
        tx,
        breakdown=breakdown,
        cost=cost,
        ref_id=ref_id,
        response_id=response_id,
        usage=usage,
    )
    await billing.db.commit()
    if tx is not None:
        await invalidate_balance_cache(billing.user_id)


async def _release_prompt_enhance_hold(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> None:
    if billing is None or billing.hold_amount_micro <= 0:
        return
    try:
        await billing_core.release(
            billing.db,
            billing.user_id,
            ref_type="prompt_enhance",
            ref_id=billing.request_id,
            idempotency_key=f"prompt_enhance:release:{billing.request_id}:{reason}",
            meta={"route": "prompts.enhance", "reason": reason},
        )
        await billing.db.commit()
        await invalidate_balance_cache(billing.user_id)
    except Exception:
        logger.exception("prompt enhance billing hold release failed")


def _track_prompt_enhance_release_task(task: asyncio.Task[None]) -> None:
    _PROMPT_ENHANCE_RELEASE_TASKS.add(task)

    def _done(completed: asyncio.Task[None]) -> None:
        _PROMPT_ENHANCE_RELEASE_TASKS.discard(completed)
        with suppress(asyncio.CancelledError):
            exc = completed.exception()
            if exc is not None:
                logger.error(
                    "prompt enhance detached hold release failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    task.add_done_callback(_done)


async def _release_prompt_enhance_hold_detached(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> None:
    if billing is None or billing.hold_amount_micro <= 0:
        return
    async with SessionLocal() as db:
        detached = replace(billing, db=db)
        await _release_prompt_enhance_hold(detached, reason=reason)


def _schedule_prompt_enhance_hold_release(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> asyncio.Task[None] | None:
    if billing is None or billing.hold_amount_micro <= 0:
        return None
    task = asyncio.create_task(
        _release_prompt_enhance_hold_detached(billing, reason=reason)
    )
    _track_prompt_enhance_release_task(task)
    return task


async def _release_prompt_enhance_hold_after_cancel(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> None:
    task = _schedule_prompt_enhance_hold_release(billing, reason=reason)
    if task is None:
        return
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        logger.info(
            "prompt enhance hold release continues after stream cancellation "
            "request_id=%s reason=%s",
            billing.request_id if billing is not None else None,
            reason,
        )
        raise


def _is_retryable_upstream_error(status_code: int, raw: bytes) -> bool:
    return _prompt_upstream.is_retryable_upstream_error(status_code, raw)


def _extract_error_message(evt: dict[str, Any]) -> str:
    return _prompt_upstream.extract_error_message(evt)


def _extract_response_text(obj: Any) -> str:
    return _prompt_upstream.extract_response_text(obj)


def _iter_sse_payloads_from_buffer(buffer: str) -> tuple[list[str], str]:
    return _prompt_upstream.iter_sse_payloads_from_buffer(buffer)


async def _stream_enhance_one(
    text: str,
    provider: ProviderDefinition,
    attempt: _EnhanceAttempt,
    capture: _EnhanceUsageCapture | None = None,
    *,
    system_prompt: str = ENHANCE_SYSTEM_PROMPT,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    timeouts = _prompt_upstream.StreamTimeouts(
        connect=_PROMPT_ENHANCE_CONNECT_TIMEOUT_SECONDS,
        read=_PROMPT_ENHANCE_READ_TIMEOUT_SECONDS,
        write=_PROMPT_ENHANCE_WRITE_TIMEOUT_SECONDS,
        pool=_PROMPT_ENHANCE_POOL_TIMEOUT_SECONDS,
    )
    async for chunk in _prompt_upstream.stream_enhance_one(
        text,
        provider,
        attempt,
        capture,
        system_prompt=system_prompt,
        content=content,
        metadata=metadata,
        timeouts=timeouts,
    ):
        yield chunk


async def _stream_enhance(
    text: str,
    providers: list[ProviderDefinition],
    billing: _EnhanceBillingContext | None = None,
    *,
    system_prompt: str = ENHANCE_SYSTEM_PROMPT,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    runtime = _prompt_failover.StreamRuntime(
        stream_one=_stream_enhance_one,
        charge=_charge_prompt_enhance,
        release=_release_prompt_enhance_hold,
        release_after_cancel=_release_prompt_enhance_hold_after_cancel,
    )
    stream = _prompt_failover.stream_enhance(
        text,
        providers,
        billing,
        attempts=_ENHANCE_ATTEMPTS,
        runtime=runtime,
        default_system_prompt=ENHANCE_SYSTEM_PROMPT,
        system_prompt=system_prompt,
        content=content,
        metadata=metadata,
    )
    try:
        async for chunk in stream:
            yield chunk
    finally:
        await stream.aclose()


async def _stream_with_keepalive(
    source: AsyncIterator[str],
    *,
    interval_seconds: float = _PROMPT_ENHANCE_KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    async for chunk in _prompt_keepalive.stream_with_keepalive(
        source,
        interval_seconds=interval_seconds,
        keepalive_chunk=_PROMPT_ENHANCE_KEEPALIVE_CHUNK,
    ):
        yield chunk


@router.post("/enhance")
async def enhance_prompt(
    body: EnhanceIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    await PROMPTS_ENHANCE_LIMITER.check(get_redis(), f"rl:prompt_enhance:{user.id}")
    providers = [p for p in await _resolve_provider_order(db) if p.api_key.strip()]
    if not providers:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "not_configured",
                    "message": "upstream API key not set",
                },
            },
        )
    billing = await _prepare_prompt_enhance_billing(db, user)

    return StreamingResponse(
        _stream_with_keepalive(_stream_enhance(body.text, providers, billing)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/video/enhance")
async def enhance_video_prompt(
    body: VideoEnhanceIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    await PROMPTS_ENHANCE_LIMITER.check(get_redis(), f"rl:prompt_enhance:{user.id}")
    providers = [p for p in await _resolve_provider_order(db) if p.api_key.strip()]
    if not providers:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "not_configured",
                    "message": "upstream API key not set",
                },
            },
        )

    content, token_changed = await _build_video_enhance_content(
        body,
        request=request,
        db=db,
        user_id=user.id,
    )
    if token_changed:
        await db.commit()
    billing = await _prepare_prompt_enhance_billing(db, user)

    return StreamingResponse(
        _stream_with_keepalive(
            _stream_enhance(
                body.text,
                providers,
                billing,
                system_prompt=_video_enhance_system_prompt(body.variant_count),
                content=content,
            )
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
