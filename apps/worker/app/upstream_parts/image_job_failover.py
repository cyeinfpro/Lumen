"""Endpoint and provider failover for asynchronous image-job requests."""

from __future__ import annotations

from ..provider_runtime.upstream_services import upstream_services

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from lumen_core.providers import ProviderProxyDefinition

from .image_execution import ImageExecutionRequest, ImageResult
from .transport import ImageProgressCallback


def _image_jobs_endpoint_fallback_chain(primary: str) -> list[str]:
    if primary == "generations":
        return ["generations", "responses"]
    if primary == "responses":
        return ["responses", "generations"]
    return ["generations", "responses"]


def _image_job_error_class(exc: BaseException) -> str | None:
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        error_class = payload.get("image_job_error_class")
        return error_class if isinstance(error_class, str) else None
    return None


def _should_continue_image_job_failover(
    exc: BaseException,
    *,
    retriable: bool,
) -> bool:
    """Return whether endpoint or provider failover can recover the job."""
    if upstream_services().providers.is_quota_accounting_unavailable(exc):
        return False
    if retriable:
        return True
    error_class = upstream_services().image_jobs.image_job_error_class(exc)
    if error_class in upstream_services().core.IMAGE_JOB_FAILOVER_CLASSES:
        return True
    if isinstance(exc, upstream_services().infrastructure.UpstreamError):
        if exc.status_code == 429:
            return True
        if exc.status_code is not None and 500 <= exc.status_code < 600:
            return True
        if exc.error_code in {
            upstream_services().infrastructure.EC.NO_IMAGE_RETURNED.value,
            upstream_services().infrastructure.EC.UPSTREAM_TIMEOUT.value,
            upstream_services().infrastructure.EC.TIMEOUT.value,
            upstream_services().infrastructure.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
        }:
            return True
    return isinstance(exc, upstream_services().core.RETRY_HTTPX_EXC)


async def _image_job_run_once(
    *,
    action: str,
    endpoint: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    mask: bytes | None = None,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    model: str | None,
    api_key: str,
    base_url: str,
    proxy: ProviderProxyDefinition | None,
    progress_callback: ImageProgressCallback | None,
    image_edit_input_transport: str = "url",
    user_id: str | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    """Dispatch one sidecar request by action and endpoint kind."""
    common: dict[str, Any] = {
        "prompt": prompt,
        "size": size,
        "n": n,
        "quality": quality,
        "output_format": output_format,
        "output_compression": output_compression,
        "background": background,
        "moderation": moderation,
        "api_key_override": api_key,
        "base_url_override": base_url or None,
        "progress_callback": progress_callback,
        "before_attempt": before_attempt,
    }
    if proxy is not None:
        common["proxy_override"] = proxy
    if endpoint == "responses":
        return await upstream_services().image_jobs.image_job_responses_once(
            action=action,
            images=images,
            user_id=user_id,
            model=model,
            **common,
        )
    if action == "edit":
        if not images:
            raise upstream_services().infrastructure.UpstreamError(
                "edit action requires at least one reference image",
                error_code=upstream_services().infrastructure.EC.MISSING_INPUT_IMAGES.value,
                status_code=400,
            )
        return await upstream_services().image_jobs.image_job_edit_once(
            images=images,
            mask=mask,
            image_edit_input_transport=image_edit_input_transport,
            user_id=user_id,
            **common,
        )
    return await upstream_services().image_jobs.image_job_generate_once(**common)


@dataclass(frozen=True)
class _ImageJobProviderPlan:
    endpoints: tuple[str, ...]
    base_url: str
    error: BaseException | None = None


@dataclass(frozen=True)
class _ImageJobAttemptFailure:
    error: BaseException
    error_class: str | None
    decision_reason: str
    failover_reason: str
    retriable: bool
    duration_ms: float


@dataclass(frozen=True)
class _ImageJobProviderOutcome:
    result: ImageResult | None = None
    error: BaseException | None = None


def _requested_image_job_kind(
    endpoint_override: str | None,
    endpoint_preference: str | None,
) -> str | None:
    if endpoint_override in ("generations", "responses"):
        return endpoint_override
    if endpoint_preference in ("generations", "responses"):
        return endpoint_preference
    return None


def _provider_image_job_plan(
    provider: Any,
    *,
    action: str,
    pool: Any,
    fallback_base_url: str,
    endpoint_override: str | None,
    endpoint_preference: str | None,
) -> _ImageJobProviderPlan:
    configured = getattr(provider, "image_jobs_endpoint", "auto")
    try:
        endpoint_locked = upstream_services().infrastructure.parse_provider_bool(
            getattr(provider, "image_jobs_endpoint_lock", False),
            default=False,
        )
    except ValueError:
        endpoint_locked = False
    endpoint_locked = endpoint_locked and configured in ("generations", "responses")
    requested_kind = _requested_image_job_kind(
        endpoint_override,
        endpoint_preference,
    )
    if requested_kind is not None:
        unavailable = upstream_services().providers.provider_endpoint_unavailable_error(
            provider,
            requested_kind,
        )
        if unavailable is not None:
            upstream_services().infrastructure.logger.info(
                "image_jobs skip provider=%s configured=%s requested_kind=%s reason=%s",
                getattr(provider, "name", "unknown"),
                configured,
                requested_kind,
                unavailable.payload.get("reason"),
            )
            return _ImageJobProviderPlan((), "", unavailable)
    if endpoint_override is not None:
        endpoints = [endpoint_override]
    elif endpoint_locked:
        endpoints = [configured]
    elif endpoint_preference is not None:
        endpoints = upstream_services().image_jobs.image_jobs_endpoint_fallback_chain(
            endpoint_preference
        )
    else:
        endpoints = pool.endpoint_chain(provider.name, action, configured)
    allowed = tuple(
        endpoint
        for endpoint in endpoints
        if upstream_services().providers.provider_allows_image_endpoint(
            provider, endpoint
        )
    )
    if not allowed:
        error = upstream_services().infrastructure.UpstreamError(
            f"provider {provider.name} has no supported image-job endpoint",
            error_code=upstream_services().infrastructure.EC.NO_PROVIDERS.value,
            status_code=503,
            payload={
                "provider": provider.name,
                "reason": "capability_unsupported",
            },
        )
        return _ImageJobProviderPlan((), "", error)
    raw_base_url = getattr(provider, "image_jobs_base_url", "") or fallback_base_url
    try:
        base_url = upstream_services().requests.validate_image_job_base_url(
            raw_base_url
        )
    except upstream_services().infrastructure.UpstreamError as exc:
        return _ImageJobProviderPlan((), "", exc)
    return _ImageJobProviderPlan(allowed, base_url)


def _classify_image_job_attempt(
    exc: BaseException,
    *,
    duration_ms: float,
) -> _ImageJobAttemptFailure:
    from ..retry import is_retriable as classify_retriable

    decision = classify_retriable(
        getattr(exc, "error_code", None),
        getattr(exc, "status_code", None),
        error_message=str(exc),
    )
    error_class = upstream_services().image_jobs.image_job_error_class(exc)
    return _ImageJobAttemptFailure(
        error=exc,
        error_class=error_class,
        decision_reason=decision.reason,
        failover_reason=error_class or decision.reason,
        retriable=decision.retriable,
        duration_ms=duration_ms,
    )


async def _emit_image_job_success(
    request: ImageExecutionRequest,
    *,
    provider: Any,
    provider_index: int,
    endpoint: str,
    endpoint_index: int,
    latency_ms: float,
    pool: Any,
    inflight_endpoint_kind: str | None,
    source_label: str,
) -> None:
    if not upstream_services().providers.is_byok_provider(provider):
        pool.record_endpoint_success(
            provider.name,
            endpoint,
            latency_ms=latency_ms,
        )
        upstream_services().providers.pool_report_image_success(
            pool,
            provider.name,
            endpoint_kind=inflight_endpoint_kind,
            record_endpoint=False,
        )
    await upstream_services().transport.emit_image_progress(
        request.progress_callback,
        "provider_used",
        provider=provider.name,
        route="image_jobs",
        source=source_label,
        endpoint=f"image-jobs:{endpoint}",
        **upstream_services().providers.provider_attempt_context(
            provider,
            attempt=provider_index + 1,
            endpoint_attempt=endpoint_index + 1,
            duration_ms=latency_ms,
            status="succeeded",
        ),
    )
    await upstream_services().transport.emit_image_progress(
        request.progress_callback,
        "final_image",
        source=source_label,
        endpoint_used=endpoint,
    )
    await upstream_services().transport.emit_image_progress(
        request.progress_callback,
        "completed",
        source=source_label,
        endpoint_used=endpoint,
    )


async def _run_image_job_endpoint(
    request: ImageExecutionRequest,
    *,
    provider: Any,
    provider_index: int,
    endpoint: str,
    endpoint_index: int,
    plan: _ImageJobProviderPlan,
    pool: Any,
    inflight_endpoint_kind: str | None,
    source_label: str,
) -> ImageResult | _ImageJobAttemptFailure:
    started = time.monotonic()
    try:
        result = await upstream_services().image_jobs.image_job_run_once(
            **request.job_run_kwargs(),
            endpoint=endpoint,
            api_key=provider.api_key,
            base_url=plan.base_url,
            proxy=upstream_services().core.provider_proxy(provider),
            image_edit_input_transport=provider.image_edit_input_transport,
            before_attempt=upstream_services().providers.image_request_attempt_claim(
                pool,
                provider,
                route=f"image_jobs:{endpoint}",
            ),
        )
    except (
        asyncio.CancelledError,
        upstream_services().infrastructure.UpstreamCancelled,
    ):
        raise
    except Exception as exc:  # noqa: BLE001
        if not upstream_services().providers.is_byok_provider(provider):
            pool.record_endpoint_failure(provider.name, endpoint)
        failure = _classify_image_job_attempt(
            exc,
            duration_ms=(time.monotonic() - started) * 1000,
        )
        upstream_services().infrastructure.logger.warning(
            "image job %s/%s endpoint=%s error_class=%s decision=%s: %r",
            request.action,
            provider.name,
            endpoint,
            failure.error_class,
            failure.decision_reason,
            exc,
        )
        return failure
    latency_ms = (time.monotonic() - started) * 1000.0
    await _emit_image_job_success(
        request,
        provider=provider,
        provider_index=provider_index,
        endpoint=endpoint,
        endpoint_index=endpoint_index,
        latency_ms=latency_ms,
        pool=pool,
        inflight_endpoint_kind=inflight_endpoint_kind,
        source_label=source_label,
    )
    return result


async def _emit_image_job_endpoint_failover(
    request: ImageExecutionRequest,
    failure: _ImageJobAttemptFailure,
    *,
    provider: Any,
    provider_index: int,
    endpoint: str,
    endpoint_index: int,
    remaining: int,
) -> None:
    await upstream_services().transport.emit_image_progress(
        request.progress_callback,
        "endpoint_failover",
        provider=provider.name,
        from_endpoint=endpoint,
        remaining=remaining,
        reason=failure.failover_reason,
        route="image_jobs",
        **upstream_services().providers.provider_attempt_context(
            provider,
            attempt=provider_index + 1,
            endpoint_attempt=endpoint_index + 1,
            duration_ms=failure.duration_ms,
            status="failed",
            reason=failure.failover_reason,
            exc=failure.error,
        ),
    )


async def _run_image_job_provider(
    request: ImageExecutionRequest,
    *,
    provider: Any,
    provider_index: int,
    plan: _ImageJobProviderPlan,
    pool: Any,
    inflight_endpoint_kind: str | None,
    source_label: str,
) -> _ImageJobProviderOutcome:
    for endpoint_index, endpoint in enumerate(plan.endpoints):
        outcome = await _run_image_job_endpoint(
            request,
            provider=provider,
            provider_index=provider_index,
            endpoint=endpoint,
            endpoint_index=endpoint_index,
            plan=plan,
            pool=pool,
            inflight_endpoint_kind=inflight_endpoint_kind,
            source_label=source_label,
        )
        if not isinstance(outcome, _ImageJobAttemptFailure):
            return _ImageJobProviderOutcome(result=outcome)
        remaining = len(plan.endpoints) - endpoint_index - 1
        if remaining > 0:
            await _emit_image_job_endpoint_failover(
                request,
                outcome,
                provider=provider,
                provider_index=provider_index,
                endpoint=endpoint,
                endpoint_index=endpoint_index,
                remaining=remaining,
            )
            continue
        provider_retriable = (
            upstream_services().retry.should_continue_image_provider_failover(
                outcome.error,
                retriable=outcome.retriable,
            )
        )
        if not upstream_services().image_jobs.should_continue_image_job_failover(
            outcome.error,
            retriable=provider_retriable,
        ):
            raise outcome.error
        return _ImageJobProviderOutcome(error=outcome.error)
    raise RuntimeError("image job provider plan has no endpoints")


async def _record_image_job_provider_failure(
    request: ImageExecutionRequest,
    error: BaseException,
    *,
    provider: Any,
    provider_index: int,
    provider_count: int,
    pool: Any,
) -> None:
    is_rate_limited, retry_after = (
        upstream_services().providers.is_image_rate_limit_error(error)
    )
    if not upstream_services().providers.is_byok_provider(provider):
        if is_rate_limited:
            pool.report_image_rate_limited(
                provider.name,
                retry_after_s=retry_after,
            )
        else:
            upstream_services().providers.pool_report_image_failure(pool, provider.name)
    remaining = provider_count - provider_index - 1
    if remaining <= 0:
        return
    upstream_services().infrastructure.logger.warning(
        "image job provider_failover: from=%s remaining=%d action=%s",
        provider.name,
        remaining,
        request.action,
    )
    await upstream_services().transport.emit_image_progress(
        request.progress_callback,
        "provider_failover",
        from_provider=provider.name,
        remaining=remaining,
        reason="image_job_failed",
        route="image_jobs",
        **upstream_services().providers.provider_attempt_context(
            provider,
            attempt=provider_index + 1,
            duration_ms=None,
            status="failed",
            reason="image_job_failed",
            exc=error,
        ),
    )


async def _image_job_with_failover(
    *,
    action: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    mask: bytes | None = None,
    n: int,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
    user_id: str | None = None,
    endpoint_override: str | None = None,
    endpoint_preference: str | None = None,
) -> tuple[str, str | None]:
    """Fail over across image-job endpoints and providers."""
    upstream_services().image_jobs.image_job_sidecar_token()
    pool = await upstream_services().infrastructure.provider_pool.get_pool()
    forced_kind = _requested_image_job_kind(endpoint_override, endpoint_preference)
    lane_owns_inflight = provider_override is None
    providers = (
        [provider_override]
        if provider_override is not None
        else await upstream_services().providers.pool_select_compat(
            pool,
            route="image_jobs",
            ignore_cooldown=True,
            endpoint_kind=forced_kind,
            requires_mask=mask is not None,
        )
    )
    request = ImageExecutionRequest(
        action,
        prompt,
        size,
        images,
        mask,
        n,
        quality,
        output_format,
        output_compression,
        background,
        moderation,
        model,
        progress_callback,
        provider_override,
        user_id,
    )
    errors: list[BaseException] = []
    source_label = "image_jobs" if action == "generate" else "image_jobs_edit"
    fallback_base_url = ""
    if any(
        not str(getattr(provider, "image_jobs_base_url", "") or "").strip()
        for provider in providers
    ):
        fallback_base_url = (
            await upstream_services().direct.resolve_image_job_base_url()
        )

    for provider_index, provider in enumerate(providers):
        if lane_owns_inflight and provider_index > 0:
            upstream_services().providers.pool_acquire_inflight(
                pool, provider.name, forced_kind
            )
        try:
            plan = _provider_image_job_plan(
                provider,
                action=action,
                pool=pool,
                fallback_base_url=fallback_base_url,
                endpoint_override=endpoint_override,
                endpoint_preference=endpoint_preference,
            )
            if plan.error is not None:
                errors.append(plan.error)
                continue
            outcome = await _run_image_job_provider(
                request,
                provider=provider,
                provider_index=provider_index,
                plan=plan,
                pool=pool,
                inflight_endpoint_kind=forced_kind,
                source_label=source_label,
            )
            if outcome.result is not None:
                return outcome.result
            if outcome.error is None:
                continue
            errors.append(outcome.error)
            await _record_image_job_provider_failure(
                request,
                outcome.error,
                provider=provider,
                provider_index=provider_index,
                provider_count=len(providers),
                pool=pool,
            )
        finally:
            if lane_owns_inflight:
                upstream_services().providers.pool_release_inflight(
                    pool,
                    provider.name,
                    forced_kind,
                )

    merged = upstream_services().retry.merge_fallback_errors(
        errors,
        error_code=upstream_services().infrastructure.EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} image job providers failed",
    )
    merged.payload["provider_errors"] = (
        upstream_services().retry.provider_error_details(
            providers,
            errors,
        )
    )
    raise merged


__all__ = [
    "_image_job_error_class",
    "_image_job_run_once",
    "_image_job_with_failover",
    "_image_jobs_endpoint_fallback_chain",
    "_should_continue_image_job_failover",
]
