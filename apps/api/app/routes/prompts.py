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
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import Image, Video, new_uuid7
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    RoundRobinState,
    build_effective_provider_config,
    endpoint_kind_allowed,
    weighted_priority_order,
)
from lumen_core.providers_parts.selection import provider_supports_route
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
from .prompt_parts import billing as _prompt_billing
from .prompt_parts import failover as _prompt_failover
from .prompt_parts import keepalive as _prompt_keepalive
from .prompt_parts import upstream as _prompt_upstream

logger = logging.getLogger(__name__)
httpx = _prompt_upstream.httpx

_VIDEO_REFERENCE_ACCESS_TOKEN_TTL = timedelta(hours=24)

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

_PROMPT_RUNTIME_STATE_KEY = "_prompt_enhancement_runtime"


@dataclass(slots=True)
class _PromptRuntime:
    provider_round_robin: RoundRobinState = field(default_factory=RoundRobinState)
    release_tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def track_release_task(self, task: asyncio.Task[None]) -> None:
        self.release_tasks.add(task)

        def _done(completed: asyncio.Task[None]) -> None:
            self.release_tasks.discard(completed)
            with suppress(asyncio.CancelledError):
                exc = completed.exception()
                if exc is not None:
                    logger.error(
                        "prompt enhance detached hold release failed",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )

        task.add_done_callback(_done)

    async def shutdown(self) -> None:
        tasks = list(self.release_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.release_tasks.difference_update(tasks)


def _prompt_runtime(request: Request) -> _PromptRuntime:
    runtime = getattr(request.app.state, _PROMPT_RUNTIME_STATE_KEY, None)
    if not isinstance(runtime, _PromptRuntime):
        raise RuntimeError("prompt enhancement runtime is unavailable")
    return runtime


@asynccontextmanager
async def _prompt_lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = _PromptRuntime()
    setattr(app.state, _PROMPT_RUNTIME_STATE_KEY, runtime)
    try:
        yield
    finally:
        await runtime.shutdown()
        if getattr(app.state, _PROMPT_RUNTIME_STATE_KEY, None) is runtime:
            delattr(app.state, _PROMPT_RUNTIME_STATE_KEY)


router = APIRouter(
    prefix="/prompts",
    tags=["prompts"],
    dependencies=[Depends(verify_csrf)],
    lifespan=_prompt_lifespan,
)

_PromptRuntimeDep = Annotated[_PromptRuntime, Depends(_prompt_runtime)]

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


async def _resolve_provider_order(
    db: AsyncSession,
    runtime: _PromptRuntime,
) -> list[ProviderDefinition]:
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
    return weighted_priority_order(providers, runtime.provider_round_robin)


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


def _prompt_billing_runtime() -> _prompt_billing.BillingRuntime:
    return _prompt_billing.BillingRuntime(
        attempts=_ENHANCE_ATTEMPTS,
        billing_enabled=_billing_enabled,
        billing_cache_aware=_billing_cache_aware,
        billing_allow_negative=_billing_allow_negative,
        new_id=new_uuid7,
        rate_multiplier_x10000=_rate_multiplier_x10000,
        pricing_snapshot_key=_enhance_pricing_snapshot_key,
        invalidate_balance_cache=invalidate_balance_cache,
        write_audit=write_audit,
        hash_email=hash_email,
        release_hold=_release_prompt_enhance_hold,
        logger=logger,
    )


async def _prepare_prompt_enhance_billing(
    db: AsyncSession,
    user: Any,
) -> _EnhanceBillingContext | None:
    return await _prompt_billing.prepare_prompt_enhance_billing(
        db,
        user,
        runtime=_prompt_billing_runtime(),
    )


def _capture_enhance_usage(
    capture: _EnhanceUsageCapture | None,
    event: dict[str, Any],
    *,
    provider: ProviderDefinition,
    attempt: _EnhanceAttempt,
) -> None:
    _prompt_billing.capture_enhance_usage(
        capture,
        event,
        provider=provider,
        attempt=attempt,
        pricing_snapshot_key=_enhance_pricing_snapshot_key,
    )


async def _charge_prompt_enhance(
    billing: _EnhanceBillingContext,
    capture: _EnhanceUsageCapture,
) -> None:
    await _prompt_billing.charge_prompt_enhance(
        billing,
        capture,
        runtime=_prompt_billing_runtime(),
    )


async def _release_prompt_enhance_hold(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> None:
    await _prompt_billing.release_prompt_enhance_hold(
        billing,
        reason=reason,
        runtime=_prompt_billing_runtime(),
    )


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
    runtime: _PromptRuntime,
) -> asyncio.Task[None] | None:
    if billing is None or billing.hold_amount_micro <= 0:
        return None
    task = asyncio.create_task(
        _release_prompt_enhance_hold_detached(billing, reason=reason)
    )
    runtime.track_release_task(task)
    return task


async def _release_prompt_enhance_hold_after_cancel(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
    runtime: _PromptRuntime,
) -> None:
    task = _schedule_prompt_enhance_hold_release(
        billing,
        reason=reason,
        runtime=runtime,
    )
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
    runtime: _PromptRuntime | None = None,
    system_prompt: str = ENHANCE_SYSTEM_PROMPT,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    active_runtime = runtime or _PromptRuntime()

    async def release_after_cancel(
        context: _EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        await _release_prompt_enhance_hold_after_cancel(
            context,
            reason=reason,
            runtime=active_runtime,
        )

    stream_runtime = _prompt_failover.StreamRuntime(
        stream_one=_stream_enhance_one,
        charge=_charge_prompt_enhance,
        release=_release_prompt_enhance_hold,
        release_after_cancel=release_after_cancel,
    )
    stream = _prompt_failover.stream_enhance(
        text,
        providers,
        billing,
        attempts=_ENHANCE_ATTEMPTS,
        runtime=stream_runtime,
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
    runtime: _PromptRuntimeDep,
) -> StreamingResponse:
    await PROMPTS_ENHANCE_LIMITER.check(get_redis(), f"rl:prompt_enhance:{user.id}")
    providers = [
        p for p in await _resolve_provider_order(db, runtime) if p.api_key.strip()
    ]
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
        _stream_with_keepalive(
            _stream_enhance(
                body.text,
                providers,
                billing,
                runtime=runtime,
            )
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


resolve_provider_order = _resolve_provider_order
stream_enhance = _stream_enhance
PromptRuntime = _PromptRuntime
get_prompt_runtime = _prompt_runtime


@router.post("/video/enhance")
async def enhance_video_prompt(
    body: VideoEnhanceIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    runtime: _PromptRuntimeDep,
) -> StreamingResponse:
    await PROMPTS_ENHANCE_LIMITER.check(get_redis(), f"rl:prompt_enhance:{user.id}")
    providers = [
        p for p in await _resolve_provider_order(db, runtime) if p.api_key.strip()
    ]
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
                runtime=runtime,
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
