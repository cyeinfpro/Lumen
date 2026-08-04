"""提示词增强（Prompt Enhancement）。

POST /prompts/enhance — 流式返回 AI 优化后的图像生成提示词。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import new_uuid7
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

from ..billing_cache_state import invalidate_balance_cache
from ..db import SessionLocal, get_db
from ..deps import CurrentUser, verify_csrf
from ..audit import hash_email, write_audit
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
from .prompt_parts import active_user as _prompt_active_user
from .prompt_parts import content as _prompt_content
from .prompt_parts import billing as _prompt_billing
from .prompt_parts import failover as _prompt_failover
from .prompt_parts import idempotency as _prompt_idempotency
from .prompt_parts import keepalive as _prompt_keepalive
from .prompt_parts import responses as _prompt_responses
from .prompt_parts import upstream as _prompt_upstream
from .prompt_parts.enhance_content import (
    PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES as _PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES,  # noqa: F401 - test-facing re-export
    append_input_image_with_budget as _append_input_image_with_budget,  # noqa: F401 - test-facing re-export
    build_video_enhance_content as _build_video_enhance_content,
)

logger = logging.getLogger(__name__)
httpx = _prompt_upstream.httpx

_RETRYABLE_HTTP_STATUS = _prompt_upstream.RETRYABLE_HTTP_STATUS
_FALLBACK_400_MARKERS = _prompt_upstream.FALLBACK_400_MARKERS
PROMPTS_ENHANCE_LIMITER = RateLimiter(capacity=20, refill_per_sec=20 / 60)
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
    operation_tasks: set[asyncio.Task[None]] = field(default_factory=set)

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

    def track_operation_task(self, task: asyncio.Task[None]) -> None:
        self.operation_tasks.add(task)

        def _done(completed: asyncio.Task[None]) -> None:
            self.operation_tasks.discard(completed)
            with suppress(asyncio.CancelledError):
                exc = completed.exception()
                if exc is not None:
                    logger.error(
                        "prompt enhancement durable operation failed",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )

        task.add_done_callback(_done)

    async def shutdown(self) -> None:
        tasks = list(self.release_tasks | self.operation_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.release_tasks.difference_update(tasks)
        self.operation_tasks.difference_update(tasks)


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
        logger=logger,
    )


async def _prepare_prompt_enhance_billing(
    db: AsyncSession,
    user: Any,
    *,
    request_id: str | None = None,
    commit: bool = True,
) -> _EnhanceBillingContext | None:
    return await _prompt_billing.prepare_prompt_enhance_billing(
        db,
        user,
        runtime=_prompt_billing_runtime(),
        request_id=request_id,
        commit=commit,
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
) -> bool:
    return await _prompt_billing.charge_prompt_enhance(
        billing,
        capture,
        runtime=_prompt_billing_runtime(),
    )


async def _release_prompt_enhance_hold(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> bool:
    return await _prompt_billing.release_prompt_enhance_hold(
        billing,
        reason=reason,
        runtime=_prompt_billing_runtime(),
    )


async def _settle_prompt_enhance_default_hold(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> bool:
    return await _prompt_billing.settle_prompt_enhance_default_hold(
        billing,
        reason=reason,
        runtime=_prompt_billing_runtime(),
    )


async def _release_prompt_enhance_hold_detached(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> None:
    await _prompt_responses.release_hold_detached(
        billing,
        reason=reason,
        session_factory=SessionLocal,
        release=_release_prompt_enhance_hold,
    )


def _schedule_prompt_enhance_hold_release(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
    runtime: _PromptRuntime,
) -> asyncio.Task[None] | None:
    return _prompt_responses.schedule_hold_release(
        billing,
        reason=reason,
        release_detached=_release_prompt_enhance_hold_detached,
        track_task=runtime.track_release_task,
    )


async def _release_prompt_enhance_hold_after_cancel(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
    runtime: _PromptRuntime,
) -> None:
    await _prompt_responses.wait_for_hold_release(
        billing,
        reason=reason,
        schedule_release=lambda value, *, reason: _schedule_prompt_enhance_hold_release(
            value,
            reason=reason,
            runtime=runtime,
        ),
        logger=logger,
    )


async def _settle_prompt_enhance_default_hold_detached(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> None:
    if billing is None or billing.hold_amount_micro <= 0:
        return
    async with SessionLocal() as db:
        detached = replace(billing, db=db)
        await _settle_prompt_enhance_default_hold(detached, reason=reason)


def _schedule_prompt_enhance_default_settle(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
    runtime: _PromptRuntime,
) -> asyncio.Task[None] | None:
    if billing is None or billing.hold_amount_micro <= 0:
        return None
    billing.settle_outcome.attempted = True
    task = asyncio.create_task(
        _settle_prompt_enhance_default_hold_detached(billing, reason=reason)
    )
    runtime.track_release_task(task)
    return task


async def _settle_prompt_enhance_hold_after_cancel(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
    runtime: _PromptRuntime,
) -> None:
    task = _schedule_prompt_enhance_default_settle(
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
            "prompt enhance default settle continues after stream cancellation "
            "request_id=%s reason=%s",
            billing.request_id if billing is not None else None,
            reason,
        )
        raise


_is_retryable_upstream_error = _prompt_upstream.is_retryable_upstream_error
_extract_error_message = _prompt_upstream.extract_error_message
_extract_response_text = _prompt_upstream.extract_response_text
_iter_sse_payloads_from_buffer = _prompt_upstream.iter_sse_payloads_from_buffer


async def _stream_enhance_one(
    text: str,
    provider: ProviderDefinition,
    attempt: _EnhanceAttempt,
    capture: _EnhanceUsageCapture | None = None,
    *,
    system_prompt: str = ENHANCE_SYSTEM_PROMPT,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
    on_dispatching: Callable[[], Awaitable[None]] | None = None,
    on_dispatched: Callable[[], None] | None = None,
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
        on_dispatching=on_dispatching,
        on_dispatched=on_dispatched,
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
    record_dispatch_intent: Callable[[], Awaitable[None]] | None = None,
    record_candidate_outcome: Callable[[bool], Awaitable[None]] | None = None,
    checkpoint_finalization: Callable[..., Awaitable[None]] | None = None,
    require_billing_confirmation: bool = False,
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

    async def settle_after_cancel(
        context: _EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        await _settle_prompt_enhance_hold_after_cancel(
            context,
            reason=reason,
            runtime=active_runtime,
        )

    stream_runtime = _prompt_failover.StreamRuntime(
        stream_one=_stream_enhance_one,
        charge=_charge_prompt_enhance,
        release=_release_prompt_enhance_hold,
        release_after_cancel=release_after_cancel,
        settle_default=_settle_prompt_enhance_default_hold,
        settle_default_after_cancel=settle_after_cancel,
        record_dispatch_intent=record_dispatch_intent,
        record_candidate_outcome=record_candidate_outcome,
        checkpoint_finalization=checkpoint_finalization,
        require_billing_confirmation=require_billing_confirmation,
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


_stream_with_keepalive = partial(
    _prompt_responses.keepalive_stream,
    interval_seconds=_PROMPT_ENHANCE_KEEPALIVE_SECONDS,
    keepalive_chunk=_PROMPT_ENHANCE_KEEPALIVE_CHUNK,
    implementation=_prompt_keepalive.stream_with_keepalive,
)


def _durability_runtime(
    runtime: _PromptRuntime,
) -> _prompt_responses.PromptDurabilityRuntime:
    return _prompt_responses.PromptDurabilityRuntime(
        session_factory=SessionLocal,
        logger=logger,
        prepare_billing=_prepare_prompt_enhance_billing,
        charge=_charge_prompt_enhance,
        settle_default=_settle_prompt_enhance_default_hold,
        release=_release_prompt_enhance_hold,
        stream_enhance=_stream_enhance,
        track_operation_task=runtime.track_operation_task,
    )


async def _prepare_reserved_billing(
    *args: Any,
    runtime: _PromptRuntime,
) -> tuple[_EnhanceBillingContext | None, bool]:
    return await _prompt_responses.prepare_reserved_billing(
        *args,
        runtime=_durability_runtime(runtime),
    )


def _durable_prompt_enhance_stream(
    *args: Any,
    **kwargs: Any,
) -> tuple[AsyncIterator[str], asyncio.Task[None]]:
    runtime = kwargs.pop("runtime")
    return _prompt_responses.durable_prompt_stream(
        *args,
        prompt_runtime=runtime,
        runtime=_durability_runtime(runtime),
        system_prompt=kwargs.pop("system_prompt", ENHANCE_SYSTEM_PROMPT),
        **kwargs,
    )


def _durable_prompt_enhance_response(
    *args: Any,
    **kwargs: Any,
) -> StreamingResponse:
    runtime = kwargs.pop("runtime")
    return _prompt_responses.durable_prompt_response(
        *args,
        prompt_runtime=runtime,
        runtime=_durability_runtime(runtime),
        system_prompt=kwargs.pop("system_prompt", ENHANCE_SYSTEM_PROMPT),
        with_keepalive=_stream_with_keepalive,
        **kwargs,
    )


_GuardedEnhanceStreamingResponse = _prompt_responses.GuardedEnhanceStreamingResponse


def _schedule_orphan_hold_release(
    billing: _EnhanceBillingContext | None,
    runtime: _PromptRuntime,
) -> Callable[[], None]:
    return _prompt_responses.orphan_hold_release_callback(
        billing,
        logger=logger,
        schedule_release=partial(
            _schedule_prompt_enhance_hold_release,
            runtime=runtime,
        ),
    )


@router.post("/enhance")
async def enhance_prompt(
    body: EnhanceIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    runtime: _PromptRuntimeDep,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
    request: Request = None,
) -> StreamingResponse:
    client_key = _prompt_idempotency.resolve_client_idempotency_key(idempotency_key)
    operation = _prompt_idempotency.prompt_enhance_operation(
        user_id=user.id,
        idempotency_key=client_key,
        operation_namespace=_prompt_idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        payload=body.model_dump(mode="json"),
    )
    reservation = await _prompt_active_user.reserve_active_prompt_operation(
        db,
        operation,
        user=user,
        request=request,
    )
    replay = await _prompt_active_user.commit_replay_response(
        db, reservation, client_key, _stream_with_keepalive
    )
    if replay is not None:
        return replay
    user = _prompt_active_user.reserved_user(reservation)

    if reservation.recovery is None:
        await PROMPTS_ENHANCE_LIMITER.check(
            get_redis(),
            f"rl:prompt_enhance:{user.id}",
        )
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
    else:
        providers = []
    billing, invalidate_hold = await _prepare_reserved_billing(
        db,
        user,
        operation,
        reservation,
        runtime=runtime,
    )
    commit = getattr(db, "commit", None)
    if callable(commit):
        await commit()
    if invalidate_hold:
        await invalidate_balance_cache(user.id)

    return _durable_prompt_enhance_response(
        operation,
        reservation,
        text=body.text,
        providers=providers,
        billing=billing,
        runtime=runtime,
    )


resolve_provider_order = _resolve_provider_order
stream_enhance = _stream_enhance
prepare_prompt_enhance_billing = _prepare_prompt_enhance_billing
prepare_reserved_prompt_billing = _prepare_reserved_billing
durable_prompt_enhance_stream = _durable_prompt_enhance_stream
PromptRuntime = _PromptRuntime
get_prompt_runtime = _prompt_runtime


@router.post("/video/enhance")
async def enhance_video_prompt(
    body: VideoEnhanceIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    runtime: _PromptRuntimeDep,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> StreamingResponse:
    client_key = _prompt_idempotency.resolve_client_idempotency_key(idempotency_key)
    operation = _prompt_idempotency.prompt_enhance_operation(
        user_id=user.id,
        idempotency_key=client_key,
        operation_namespace=_prompt_idempotency.VIDEO_PROMPT_ENHANCE_OPERATION,
        payload=body.model_dump(mode="json"),
    )
    reservation = await _prompt_active_user.reserve_active_prompt_operation(
        db,
        operation,
        user=user,
        request=request,
    )
    replay = await _prompt_active_user.commit_replay_response(
        db, reservation, client_key, _stream_with_keepalive
    )
    if replay is not None:
        return replay
    user = _prompt_active_user.reserved_user(reservation)

    if reservation.recovery is None:
        await PROMPTS_ENHANCE_LIMITER.check(
            get_redis(),
            f"rl:prompt_enhance:{user.id}",
        )
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
    else:
        providers = []
        content = None
        token_changed = False
    billing, invalidate_hold = await _prepare_reserved_billing(
        db,
        user,
        operation,
        reservation,
        runtime=runtime,
    )
    commit = getattr(db, "commit", None)
    if callable(commit):
        await commit()
    if invalidate_hold:
        await invalidate_balance_cache(user.id)

    if token_changed:
        logger.debug(
            "prompt enhancement video reference token committed with operation "
            "record_id=%s",
            operation.record_id,
        )
    return _durable_prompt_enhance_response(
        operation,
        reservation,
        text=body.text,
        providers=providers,
        billing=billing,
        runtime=runtime,
        system_prompt=_video_enhance_system_prompt(body.variant_count),
        content=content,
    )
