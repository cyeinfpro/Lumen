"""Provider failover for direct Images API and Responses image calls."""

from __future__ import annotations

import asyncio
import time
from typing import Any, NoReturn

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from .image_execution import ImageExecutionRequest


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def _provider_pinned_target(
    provider: Any,
    proxy: Any | None,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> Any | None:
    services = _runtime_services(runtime)
    if proxy is not None or not services.providers.is_byok_provider(
        provider
    ):
        return None
    target = getattr(provider, "_byok_http_target", None)
    if target is None or not getattr(target, "resolved_ips", ()):
        return None
    return target


def _raise_direct_provider_failures(
    providers: list[Any],
    errors: list[BaseException],
    *,
    action_label: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> NoReturn:
    services = _runtime_services(runtime)
    merged = services.retry.merge_fallback_errors(
        errors,
        error_code=services.infrastructure.EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} direct {action_label} providers failed",
        runtime=runtime,
    )
    merged.payload["provider_errors"] = (
        services.retry.provider_error_details(
            providers,
            errors,
            runtime=runtime,
        )
    )
    raise merged


async def _direct_generate_image_with_failover(
    request: ImageExecutionRequest,
) -> list[tuple[str, str | None]]:
    """Run direct text-to-image across the configured provider chain."""
    from ..retry import is_retriable as classify_retriable

    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    context = request.request_context
    progress_callback = request.progress_callback
    provider_override = request.provider_override
    pool = await services.infrastructure.provider_pool.get_pool()
    lane_owns_inflight = provider_override is None
    providers = (
        [provider_override]
        if provider_override is not None
        else await services.providers.pool_select_compat(
            pool,
            route="image",
            ignore_cooldown=True,
            endpoint_kind="generations",
        )
    )
    errors: list[BaseException] = []

    for index, provider in enumerate(providers):
        if lane_owns_inflight and index > 0:
            services.providers.pool_acquire_inflight(
                pool,
                provider.name,
                "generations",
            )
        started = time.monotonic()
        try:
            unavailable_error = (
                services.providers.provider_endpoint_unavailable_error(
                    provider,
                    "generations",
                )
            )
            if unavailable_error is not None:
                errors.append(unavailable_error)
                continue
            try:
                kwargs: dict[str, Any] = {
                    "base_url_override": provider.base_url,
                    "api_key_override": provider.api_key,
                }
                proxy = services.core.provider_proxy(provider)
                if proxy is not None:
                    kwargs["proxy_override"] = proxy
                pinned_target = _provider_pinned_target(
                    provider,
                    proxy,
                    runtime=runtime,
                )
                if pinned_target is not None:
                    kwargs["pinned_target_override"] = pinned_target
                kwargs["before_attempt"] = (
                    services.providers.image_request_attempt_claim(
                        pool,
                        provider,
                        route="image2:generations",
                        request_context=context,
                    )
                )
                result = await services.direct.direct_generate_image_once(
                    request,
                    **kwargs,
                )
                if not services.providers.is_byok_provider(provider):
                    services.providers.pool_report_image_success(
                        pool,
                        provider.name,
                        endpoint_kind="generations",
                    )
                await services.transport.emit_image_progress(
                    progress_callback,
                    "provider_used",
                    provider=provider.name,
                    route="image2",
                    source="image2_direct",
                    endpoint="images/generations",
                    **services.providers.provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        duration_ms=(time.monotonic() - started) * 1000,
                        status="succeeded",
                    ),
                )
                await services.transport.emit_image_progress(
                    progress_callback,
                    "final_image",
                    source="image2_direct",
                )
                await services.transport.emit_image_progress(
                    progress_callback,
                    "completed",
                    source="image2_direct",
                )
                return result
            except (
                asyncio.CancelledError,
                services.infrastructure.UpstreamCancelled,
            ):
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                should_continue = (
                    services.retry.should_continue_image_provider_failover(
                        exc,
                        retriable=decision.retriable,
                        runtime=runtime,
                    )
                )
                if not should_continue:
                    services.infrastructure.logger.warning(
                        "direct image provider %s terminal error: %s",
                        provider.name,
                        decision.reason,
                    )
                    raise
                is_rate_limited, retry_after = (
                    services.providers.is_image_rate_limit_error(exc)
                )
                if not services.providers.is_byok_provider(provider):
                    if is_rate_limited:
                        pool.report_image_rate_limited(
                            provider.name,
                            retry_after_s=retry_after,
                        )
                    else:
                        services.providers.pool_report_image_failure(
                            pool,
                            provider.name,
                            endpoint_kind="generations",
                        )
                remaining = len(providers) - index - 1
                if remaining > 0:
                    services.infrastructure.logger.warning(
                        "direct image provider_failover: from=%s remaining=%d reason=%s",
                        provider.name,
                        remaining,
                        decision.reason,
                    )
                    await services.transport.emit_image_progress(
                        progress_callback,
                        "provider_failover",
                        from_provider=provider.name,
                        remaining=remaining,
                        reason=decision.reason,
                        route="image2_direct",
                        **services.providers.provider_attempt_context(
                            provider,
                            attempt=index + 1,
                            duration_ms=(time.monotonic() - started) * 1000,
                            status="failed",
                            reason=decision.reason,
                            exc=exc,
                        ),
                    )
        finally:
            if lane_owns_inflight:
                services.providers.pool_release_inflight(
                    pool,
                    provider.name,
                    "generations",
                )

    _raise_direct_provider_failures(
        providers,
        errors,
        action_label="image",
        runtime=runtime,
    )


async def _direct_edit_image_with_failover(
    request: ImageExecutionRequest,
) -> list[tuple[str, str | None]]:
    """Run direct image edits across the configured provider chain."""
    from ..retry import is_retriable as classify_retriable

    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    context = request.request_context
    progress_callback = request.progress_callback
    provider_override = request.provider_override
    pool = await services.infrastructure.provider_pool.get_pool()
    lane_owns_inflight = provider_override is None
    requires_mask = request.mask is not None
    providers = (
        [provider_override]
        if provider_override is not None
        else await services.providers.pool_select_compat(
            pool,
            route="image",
            ignore_cooldown=True,
            endpoint_kind="generations",
            requires_mask=requires_mask,
            mask_transport_required=False,
        )
    )
    errors: list[BaseException] = []

    for index, provider in enumerate(providers):
        if lane_owns_inflight and index > 0:
            services.providers.pool_acquire_inflight(
                pool,
                provider.name,
                "generations",
            )
        started = time.monotonic()
        try:
            unavailable_error = (
                services.providers.provider_endpoint_unavailable_error(
                    provider,
                    "generations",
                )
            )
            if unavailable_error is not None:
                errors.append(unavailable_error)
                continue
            try:
                kwargs: dict[str, Any] = {
                    "base_url_override": provider.base_url,
                    "api_key_override": provider.api_key,
                }
                proxy = services.core.provider_proxy(provider)
                if proxy is not None:
                    kwargs["proxy_override"] = proxy
                pinned_target = _provider_pinned_target(
                    provider,
                    proxy,
                    runtime=runtime,
                )
                if pinned_target is not None:
                    kwargs["pinned_target_override"] = pinned_target
                async with services.providers.image_quota_claim(
                    pool,
                    provider,
                    route="image2:edits",
                    request_context=context,
                ) as quota_reservation:
                    if quota_reservation is not None:
                        quota_reservation.state = "started"
                    result = await services.direct.direct_edit_image_once(
                        request,
                        **kwargs,
                    )
                    if not services.providers.is_byok_provider(provider):
                        services.providers.pool_report_image_success(
                            pool,
                            provider.name,
                            endpoint_kind="generations",
                        )
                        await services.providers.record_admin_image_call_or_raise(
                            pool,
                            provider,
                            reservation=quota_reservation,
                        )
                await services.transport.emit_image_progress(
                    progress_callback,
                    "provider_used",
                    provider=provider.name,
                    route="image2",
                    source="image2_edit_direct",
                    endpoint="images/edits",
                    **services.providers.provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        duration_ms=(time.monotonic() - started) * 1000,
                        status="succeeded",
                    ),
                )
                await services.transport.emit_image_progress(
                    progress_callback,
                    "final_image",
                    source="image2_edit_direct",
                )
                await services.transport.emit_image_progress(
                    progress_callback,
                    "completed",
                    source="image2_edit_direct",
                )
                return result
            except (
                asyncio.CancelledError,
                services.infrastructure.UpstreamCancelled,
            ):
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                should_continue = (
                    services.retry.should_continue_image_provider_failover(
                        exc,
                        retriable=decision.retriable,
                        runtime=runtime,
                    )
                )
                if not should_continue:
                    services.infrastructure.logger.warning(
                        "direct edit provider %s terminal error: %s",
                        provider.name,
                        decision.reason,
                    )
                    raise
                is_rate_limited, retry_after = (
                    services.providers.is_image_rate_limit_error(exc)
                )
                if not services.providers.is_byok_provider(provider):
                    if is_rate_limited:
                        pool.report_image_rate_limited(
                            provider.name,
                            retry_after_s=retry_after,
                        )
                    else:
                        services.providers.pool_report_image_failure(
                            pool,
                            provider.name,
                            endpoint_kind="generations",
                        )
                remaining = len(providers) - index - 1
                if remaining > 0:
                    services.infrastructure.logger.warning(
                        "direct edit provider_failover: from=%s remaining=%d reason=%s",
                        provider.name,
                        remaining,
                        decision.reason,
                    )
                    await services.transport.emit_image_progress(
                        progress_callback,
                        "provider_failover",
                        from_provider=provider.name,
                        remaining=remaining,
                        reason=decision.reason,
                        route="image2_edit_direct",
                        **services.providers.provider_attempt_context(
                            provider,
                            attempt=index + 1,
                            duration_ms=(time.monotonic() - started) * 1000,
                            status="failed",
                            reason=decision.reason,
                            exc=exc,
                        ),
                    )
        finally:
            if lane_owns_inflight:
                services.providers.pool_release_inflight(
                    pool,
                    provider.name,
                    "generations",
                )

    _raise_direct_provider_failures(
        providers,
        errors,
        action_label="edit",
        runtime=runtime,
    )


async def _responses_image_stream_with_failover(
    request: ImageExecutionRequest,
    *,
    use_httpx: bool,
) -> tuple[str, str | None]:
    """Run Responses image generation across the configured provider chain."""
    from ..retry import is_retriable as classify_retriable

    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    context = request.request_context
    progress_callback = request.progress_callback
    provider_override = request.provider_override
    pool = await services.infrastructure.provider_pool.get_pool()
    lane_owns_inflight = provider_override is None
    providers = (
        [provider_override]
        if provider_override is not None
        else await services.providers.pool_select_compat(
            pool,
            route="image",
            ignore_cooldown=True,
            endpoint_kind="responses",
        )
    )
    errors: list[BaseException] = []

    for index, provider in enumerate(providers):
        if lane_owns_inflight and index > 0:
            services.providers.pool_acquire_inflight(
                pool,
                provider.name,
                "responses",
            )
        started = time.monotonic()
        try:
            unavailable_error = (
                services.providers.provider_endpoint_unavailable_error(
                    provider,
                    "responses",
                )
            )
            if unavailable_error is not None:
                errors.append(unavailable_error)
                continue
            try:
                kwargs: dict[str, Any] = {
                    "use_httpx": use_httpx,
                    "base_url_override": provider.base_url,
                    "api_key_override": provider.api_key,
                }
                proxy = services.core.provider_proxy(provider)
                if proxy is not None:
                    kwargs["proxy_override"] = proxy
                pinned_target = _provider_pinned_target(
                    provider,
                    proxy,
                    runtime=runtime,
                )
                if pinned_target is not None:
                    kwargs["pinned_target_override"] = pinned_target
                kwargs["before_attempt"] = (
                    services.providers.image_request_attempt_claim(
                        pool,
                        provider,
                        route="responses:image_generation",
                        request_context=context,
                    )
                )
                result = (
                    await services.retry.responses_image_stream_with_retry(
                        request,
                        **kwargs
                    )
                )
                if not services.providers.is_byok_provider(provider):
                    services.providers.pool_report_image_success(
                        pool,
                        provider.name,
                        endpoint_kind="responses",
                    )
                await services.transport.emit_image_progress(
                    progress_callback,
                    "provider_used",
                    provider=provider.name,
                    route="responses",
                    source="responses",
                    endpoint="responses:image_generation",
                    **services.providers.provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        duration_ms=(time.monotonic() - started) * 1000,
                        status="succeeded",
                    ),
                )
                return result
            except (
                asyncio.CancelledError,
                services.infrastructure.UpstreamCancelled,
            ):
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                should_continue = (
                    services.retry.should_continue_image_provider_failover(
                        exc,
                        retriable=decision.retriable,
                        runtime=runtime,
                    )
                )
                if not should_continue:
                    services.infrastructure.logger.warning(
                        "provider %s terminal error, not failing over: %s",
                        provider.name,
                        decision.reason,
                    )
                    raise
                is_rate_limited, retry_after = (
                    services.providers.is_image_rate_limit_error(exc)
                )
                if not services.providers.is_byok_provider(provider):
                    if is_rate_limited:
                        pool.report_image_rate_limited(
                            provider.name,
                            retry_after_s=retry_after,
                        )
                    else:
                        services.providers.pool_report_image_failure(
                            pool,
                            provider.name,
                            endpoint_kind="responses",
                        )
                remaining = len(providers) - index - 1
                if remaining > 0:
                    services.infrastructure.logger.warning(
                        "provider_failover: from=%s remaining=%d reason=%s",
                        provider.name,
                        remaining,
                        decision.reason,
                    )
                    await services.transport.emit_image_progress(
                        progress_callback,
                        "provider_failover",
                        from_provider=provider.name,
                        remaining=remaining,
                        reason=decision.reason,
                        route="responses",
                        **services.providers.provider_attempt_context(
                            provider,
                            attempt=index + 1,
                            duration_ms=(time.monotonic() - started) * 1000,
                            status="failed",
                            reason=decision.reason,
                            exc=exc,
                        ),
                    )
        finally:
            if lane_owns_inflight:
                services.providers.pool_release_inflight(
                    pool,
                    provider.name,
                    "responses",
                )

    merged = services.retry.merge_fallback_errors(
        errors,
        error_code=services.infrastructure.EC.ALL_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} upstream providers failed",
        runtime=runtime,
    )
    merged.payload["provider_errors"] = (
        services.retry.provider_error_details(
            providers,
            errors,
            runtime=runtime,
        )
    )
    raise merged


__all__ = [
    "_direct_edit_image_with_failover",
    "_direct_generate_image_with_failover",
    "_responses_image_stream_with_failover",
]
