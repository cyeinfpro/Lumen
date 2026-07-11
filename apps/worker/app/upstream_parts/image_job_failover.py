"""Endpoint and provider failover for asynchronous image-job requests."""

from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any, Awaitable, Callable

from lumen_core.providers import ProviderProxyDefinition

from .transport import ImageProgressCallback

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve compatibility dependencies at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


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
    facade = _facade()
    if facade._is_quota_accounting_unavailable(exc):
        return False
    if retriable:
        return True
    error_class = facade._image_job_error_class(exc)
    if error_class in facade._IMAGE_JOB_FAILOVER_CLASSES:
        return True
    if isinstance(exc, facade.UpstreamError):
        if exc.status_code == 429:
            return True
        if exc.status_code is not None and 500 <= exc.status_code < 600:
            return True
        if exc.error_code in {
            facade.EC.NO_IMAGE_RETURNED.value,
            facade.EC.UPSTREAM_TIMEOUT.value,
            facade.EC.TIMEOUT.value,
            facade.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
        }:
            return True
    return isinstance(exc, facade._RETRY_HTTPX_EXC)


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
    facade = _facade()
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
        return await facade._image_job_responses_once(
            action=action,
            images=images,
            user_id=user_id,
            model=model,
            **common,
        )
    if action == "edit":
        if not images:
            raise facade.UpstreamError(
                "edit action requires at least one reference image",
                error_code=facade.EC.MISSING_INPUT_IMAGES.value,
                status_code=400,
            )
        return await facade._image_job_edit_once(
            images=images,
            mask=mask,
            image_edit_input_transport=image_edit_input_transport,
            user_id=user_id,
            **common,
        )
    return await facade._image_job_generate_once(**common)


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
    facade = _facade()
    from ..retry import is_retriable as classify_retriable

    pool = await facade.provider_pool.get_pool()
    forced_kind: str | None = None
    if endpoint_override in ("generations", "responses"):
        forced_kind = endpoint_override
    elif endpoint_preference in ("generations", "responses"):
        forced_kind = endpoint_preference

    lane_owns_inflight = provider_override is None
    inflight_endpoint_kind = forced_kind
    requires_mask = mask is not None
    providers = (
        [provider_override]
        if provider_override is not None
        else await facade._pool_select_compat(
            pool,
            route="image_jobs",
            ignore_cooldown=True,
            endpoint_kind=forced_kind,
            requires_mask=requires_mask,
        )
    )
    errors: list[BaseException] = []
    source_label = "image_jobs" if action == "generate" else "image_jobs_edit"
    fallback_base_url = await facade._resolve_image_job_base_url()

    for provider_index, provider in enumerate(providers):
        if lane_owns_inflight and provider_index > 0:
            facade._pool_acquire_inflight(
                pool,
                provider.name,
                inflight_endpoint_kind,
            )
        try:
            configured_endpoint = getattr(
                provider,
                "image_jobs_endpoint",
                "auto",
            )
            try:
                endpoint_locked = facade.parse_provider_bool(
                    getattr(provider, "image_jobs_endpoint_lock", False),
                    default=False,
                )
            except ValueError:
                endpoint_locked = False
            endpoint_locked = endpoint_locked and configured_endpoint in (
                "generations",
                "responses",
            )

            conflict_kind: str | None = None
            if endpoint_override in ("generations", "responses"):
                conflict_kind = endpoint_override
            elif endpoint_preference in ("generations", "responses"):
                conflict_kind = endpoint_preference
            if conflict_kind is not None:
                unavailable_error = facade._provider_endpoint_unavailable_error(
                    provider,
                    conflict_kind,
                )
                if unavailable_error is not None:
                    facade.logger.info(
                        "image_jobs skip provider=%s configured=%s "
                        "requested_kind=%s reason=%s",
                        getattr(provider, "name", "unknown"),
                        configured_endpoint,
                        conflict_kind,
                        unavailable_error.payload.get("reason"),
                    )
                    errors.append(unavailable_error)
                    continue

            if endpoint_override is not None:
                endpoint_chain = [endpoint_override]
            elif endpoint_locked:
                endpoint_chain = [configured_endpoint]
            elif endpoint_preference is not None:
                endpoint_chain = facade._image_jobs_endpoint_fallback_chain(
                    endpoint_preference
                )
            else:
                endpoint_chain = pool.endpoint_chain(
                    provider.name,
                    action,
                    configured_endpoint,
                )
            endpoint_chain = [
                endpoint
                for endpoint in endpoint_chain
                if facade._provider_allows_image_endpoint(provider, endpoint)
            ]
            if not endpoint_chain:
                errors.append(
                    facade.UpstreamError(
                        f"provider {provider.name} has no supported image-job endpoint",
                        error_code=facade.EC.NO_PROVIDERS.value,
                        status_code=503,
                        payload={
                            "provider": provider.name,
                            "reason": "capability_unsupported",
                        },
                    )
                )
                continue
            provider_base_url = (
                getattr(provider, "image_jobs_base_url", "") or fallback_base_url
            )

            last_exc: BaseException | None = None
            for endpoint_index, endpoint in enumerate(endpoint_chain):
                endpoint_remaining = len(endpoint_chain) - endpoint_index - 1
                started = time.monotonic()
                try:
                    result = await facade._image_job_run_once(
                        action=action,
                        endpoint=endpoint,
                        prompt=prompt,
                        size=size,
                        images=images,
                        mask=mask,
                        n=n,
                        quality=quality,
                        output_format=output_format,
                        output_compression=output_compression,
                        background=background,
                        moderation=moderation,
                        model=model,
                        api_key=provider.api_key,
                        base_url=provider_base_url,
                        proxy=facade._provider_proxy(provider),
                        image_edit_input_transport=(
                            provider.image_edit_input_transport
                        ),
                        progress_callback=progress_callback,
                        user_id=user_id,
                        before_attempt=facade._image_request_attempt_claim(
                            pool,
                            provider,
                            route=f"image_jobs:{endpoint}",
                        ),
                    )
                except (asyncio.CancelledError, facade.UpstreamCancelled):
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if not facade._is_byok_provider(provider):
                        pool.record_endpoint_failure(
                            provider.name,
                            endpoint,
                        )
                    decision = classify_retriable(
                        getattr(exc, "error_code", None),
                        getattr(exc, "status_code", None),
                        error_message=str(exc),
                    )
                    error_class = facade._image_job_error_class(exc)
                    facade.logger.warning(
                        "image job %s/%s endpoint=%s error_class=%s decision=%s: %r",
                        action,
                        provider.name,
                        endpoint,
                        error_class,
                        decision.reason,
                        exc,
                    )
                    if endpoint_remaining > 0:
                        await facade._emit_image_progress(
                            progress_callback,
                            "endpoint_failover",
                            provider=provider.name,
                            from_endpoint=endpoint,
                            remaining=endpoint_remaining,
                            reason=error_class or decision.reason,
                            route="image_jobs",
                            **facade._provider_attempt_context(
                                provider,
                                attempt=provider_index + 1,
                                endpoint_attempt=endpoint_index + 1,
                                duration_ms=(time.monotonic() - started) * 1000,
                                status="failed",
                                reason=error_class or decision.reason,
                                exc=exc,
                            ),
                        )
                        continue
                    should_continue = facade._should_continue_image_job_failover(
                        exc,
                        retriable=(
                            facade._should_continue_image_provider_failover(
                                exc,
                                retriable=decision.retriable,
                            )
                        ),
                    )
                    if not should_continue:
                        raise
                    break
                else:
                    latency_ms = (time.monotonic() - started) * 1000.0
                    if not facade._is_byok_provider(provider):
                        pool.record_endpoint_success(
                            provider.name,
                            endpoint,
                            latency_ms=latency_ms,
                        )
                        facade._pool_report_image_success(
                            pool,
                            provider.name,
                            endpoint_kind=inflight_endpoint_kind,
                            record_endpoint=False,
                        )
                    await facade._emit_image_progress(
                        progress_callback,
                        "provider_used",
                        provider=provider.name,
                        route="image_jobs",
                        source=source_label,
                        endpoint=f"image-jobs:{endpoint}",
                        **facade._provider_attempt_context(
                            provider,
                            attempt=provider_index + 1,
                            endpoint_attempt=endpoint_index + 1,
                            duration_ms=latency_ms,
                            status="succeeded",
                        ),
                    )
                    await facade._emit_image_progress(
                        progress_callback,
                        "final_image",
                        source=source_label,
                        endpoint_used=endpoint,
                    )
                    await facade._emit_image_progress(
                        progress_callback,
                        "completed",
                        source=source_label,
                        endpoint_used=endpoint,
                    )
                    return result

            if last_exc is None:
                continue
            errors.append(last_exc)
            is_rate_limited, retry_after = facade._is_image_rate_limit_error(last_exc)
            if not facade._is_byok_provider(provider):
                if is_rate_limited:
                    pool.report_image_rate_limited(
                        provider.name,
                        retry_after_s=retry_after,
                    )
                else:
                    facade._pool_report_image_failure(pool, provider.name)
            remaining = len(providers) - provider_index - 1
            if remaining > 0:
                facade.logger.warning(
                    "image job provider_failover: from=%s remaining=%d action=%s",
                    provider.name,
                    remaining,
                    action,
                )
                await facade._emit_image_progress(
                    progress_callback,
                    "provider_failover",
                    from_provider=provider.name,
                    remaining=remaining,
                    reason="image_job_failed",
                    route="image_jobs",
                    **facade._provider_attempt_context(
                        provider,
                        attempt=provider_index + 1,
                        duration_ms=None,
                        status="failed",
                        reason="image_job_failed",
                        exc=last_exc,
                    ),
                )
        finally:
            if lane_owns_inflight:
                facade._pool_release_inflight(
                    pool,
                    provider.name,
                    inflight_endpoint_kind,
                )

    merged = facade._merge_fallback_errors(
        errors,
        error_code=facade.EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} image job providers failed",
    )
    merged.payload["provider_errors"] = facade._provider_error_details(
        providers,
        errors,
    )
    raise merged


__all__ = [
    "_image_job_error_class",
    "_image_job_run_once",
    "_image_job_with_failover",
    "_image_jobs_endpoint_fallback_chain",
    "_should_continue_image_job_failover",
]
