"""Provider failover for direct Images API and Responses image calls."""

from __future__ import annotations

from ..provider_runtime.upstream_services import upstream_services

import asyncio
import time
from typing import Any, NoReturn

from .transport import ImageProgressCallback


def _provider_pinned_target(provider: Any, proxy: Any | None) -> Any | None:
    if proxy is not None or not upstream_services().providers.is_byok_provider(
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
) -> NoReturn:
    merged = upstream_services().retry.merge_fallback_errors(
        errors,
        error_code=upstream_services().infrastructure.EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} direct {action_label} providers failed",
    )
    merged.payload["provider_errors"] = (
        upstream_services().retry.provider_error_details(providers, errors)
    )
    raise merged


async def _direct_generate_image_with_failover(
    *,
    prompt: str,
    size: str,
    n: int,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
) -> list[tuple[str, str | None]]:
    """Run direct text-to-image across the configured provider chain."""
    from ..retry import is_retriable as classify_retriable

    pool = await upstream_services().infrastructure.provider_pool.get_pool()
    lane_owns_inflight = provider_override is None
    providers = (
        [provider_override]
        if provider_override is not None
        else await upstream_services().providers.pool_select_compat(
            pool,
            route="image",
            ignore_cooldown=True,
            endpoint_kind="generations",
        )
    )
    errors: list[BaseException] = []

    for index, provider in enumerate(providers):
        if lane_owns_inflight and index > 0:
            upstream_services().providers.pool_acquire_inflight(
                pool,
                provider.name,
                "generations",
            )
        started = time.monotonic()
        try:
            unavailable_error = (
                upstream_services().providers.provider_endpoint_unavailable_error(
                    provider,
                    "generations",
                )
            )
            if unavailable_error is not None:
                errors.append(unavailable_error)
                continue
            try:
                kwargs: dict[str, Any] = {
                    "prompt": prompt,
                    "size": size,
                    "n": n,
                    "quality": quality,
                    "output_format": output_format,
                    "output_compression": output_compression,
                    "background": background,
                    "moderation": moderation,
                    "base_url_override": provider.base_url,
                    "api_key_override": provider.api_key,
                }
                proxy = upstream_services().core.provider_proxy(provider)
                if proxy is not None:
                    kwargs["proxy_override"] = proxy
                pinned_target = _provider_pinned_target(provider, proxy)
                if pinned_target is not None:
                    kwargs["pinned_target_override"] = pinned_target
                kwargs["before_attempt"] = (
                    upstream_services().providers.image_request_attempt_claim(
                        pool,
                        provider,
                        route="image2:generations",
                    )
                )
                result = await upstream_services().direct.direct_generate_image_once(
                    **kwargs
                )
                if not upstream_services().providers.is_byok_provider(provider):
                    upstream_services().providers.pool_report_image_success(
                        pool,
                        provider.name,
                        endpoint_kind="generations",
                    )
                await upstream_services().transport.emit_image_progress(
                    progress_callback,
                    "provider_used",
                    provider=provider.name,
                    route="image2",
                    source="image2_direct",
                    endpoint="images/generations",
                    **upstream_services().providers.provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        duration_ms=(time.monotonic() - started) * 1000,
                        status="succeeded",
                    ),
                )
                await upstream_services().transport.emit_image_progress(
                    progress_callback,
                    "final_image",
                    source="image2_direct",
                )
                await upstream_services().transport.emit_image_progress(
                    progress_callback,
                    "completed",
                    source="image2_direct",
                )
                return result
            except (
                asyncio.CancelledError,
                upstream_services().infrastructure.UpstreamCancelled,
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
                    upstream_services().retry.should_continue_image_provider_failover(
                        exc,
                        retriable=decision.retriable,
                    )
                )
                if not should_continue:
                    upstream_services().infrastructure.logger.warning(
                        "direct image provider %s terminal error: %s",
                        provider.name,
                        decision.reason,
                    )
                    raise
                is_rate_limited, retry_after = (
                    upstream_services().providers.is_image_rate_limit_error(exc)
                )
                if not upstream_services().providers.is_byok_provider(provider):
                    if is_rate_limited:
                        pool.report_image_rate_limited(
                            provider.name,
                            retry_after_s=retry_after,
                        )
                    else:
                        upstream_services().providers.pool_report_image_failure(
                            pool,
                            provider.name,
                            endpoint_kind="generations",
                        )
                remaining = len(providers) - index - 1
                if remaining > 0:
                    upstream_services().infrastructure.logger.warning(
                        "direct image provider_failover: from=%s remaining=%d reason=%s",
                        provider.name,
                        remaining,
                        decision.reason,
                    )
                    await upstream_services().transport.emit_image_progress(
                        progress_callback,
                        "provider_failover",
                        from_provider=provider.name,
                        remaining=remaining,
                        reason=decision.reason,
                        route="image2_direct",
                        **upstream_services().providers.provider_attempt_context(
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
                upstream_services().providers.pool_release_inflight(
                    pool,
                    provider.name,
                    "generations",
                )

    _raise_direct_provider_failures(providers, errors, action_label="image")


async def _direct_edit_image_with_failover(
    *,
    prompt: str,
    size: str,
    images: list[bytes],
    mask: bytes | None = None,
    n: int,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
) -> list[tuple[str, str | None]]:
    """Run direct image edits across the configured provider chain."""
    from ..retry import is_retriable as classify_retriable

    pool = await upstream_services().infrastructure.provider_pool.get_pool()
    lane_owns_inflight = provider_override is None
    requires_mask = mask is not None
    providers = (
        [provider_override]
        if provider_override is not None
        else await upstream_services().providers.pool_select_compat(
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
            upstream_services().providers.pool_acquire_inflight(
                pool,
                provider.name,
                "generations",
            )
        started = time.monotonic()
        try:
            unavailable_error = (
                upstream_services().providers.provider_endpoint_unavailable_error(
                    provider,
                    "generations",
                )
            )
            if unavailable_error is not None:
                errors.append(unavailable_error)
                continue
            try:
                kwargs: dict[str, Any] = {
                    "prompt": prompt,
                    "size": size,
                    "images": images,
                    "mask": mask,
                    "n": n,
                    "quality": quality,
                    "output_format": output_format,
                    "output_compression": output_compression,
                    "background": background,
                    "moderation": moderation,
                    "base_url_override": provider.base_url,
                    "api_key_override": provider.api_key,
                }
                proxy = upstream_services().core.provider_proxy(provider)
                if proxy is not None:
                    kwargs["proxy_override"] = proxy
                pinned_target = _provider_pinned_target(provider, proxy)
                if pinned_target is not None:
                    kwargs["pinned_target_override"] = pinned_target
                async with upstream_services().providers.image_quota_claim(
                    pool,
                    provider,
                    route="image2:edits",
                ) as quota_reservation:
                    if quota_reservation is not None:
                        quota_reservation.state = "started"
                    result = await upstream_services().direct.direct_edit_image_once(
                        **kwargs
                    )
                    if not upstream_services().providers.is_byok_provider(provider):
                        upstream_services().providers.pool_report_image_success(
                            pool,
                            provider.name,
                            endpoint_kind="generations",
                        )
                        await upstream_services().providers.record_admin_image_call_or_raise(
                            pool,
                            provider,
                        )
                await upstream_services().transport.emit_image_progress(
                    progress_callback,
                    "provider_used",
                    provider=provider.name,
                    route="image2",
                    source="image2_edit_direct",
                    endpoint="images/edits",
                    **upstream_services().providers.provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        duration_ms=(time.monotonic() - started) * 1000,
                        status="succeeded",
                    ),
                )
                await upstream_services().transport.emit_image_progress(
                    progress_callback,
                    "final_image",
                    source="image2_edit_direct",
                )
                await upstream_services().transport.emit_image_progress(
                    progress_callback,
                    "completed",
                    source="image2_edit_direct",
                )
                return result
            except (
                asyncio.CancelledError,
                upstream_services().infrastructure.UpstreamCancelled,
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
                    upstream_services().retry.should_continue_image_provider_failover(
                        exc,
                        retriable=decision.retriable,
                    )
                )
                if not should_continue:
                    upstream_services().infrastructure.logger.warning(
                        "direct edit provider %s terminal error: %s",
                        provider.name,
                        decision.reason,
                    )
                    raise
                is_rate_limited, retry_after = (
                    upstream_services().providers.is_image_rate_limit_error(exc)
                )
                if not upstream_services().providers.is_byok_provider(provider):
                    if is_rate_limited:
                        pool.report_image_rate_limited(
                            provider.name,
                            retry_after_s=retry_after,
                        )
                    else:
                        upstream_services().providers.pool_report_image_failure(
                            pool,
                            provider.name,
                            endpoint_kind="generations",
                        )
                remaining = len(providers) - index - 1
                if remaining > 0:
                    upstream_services().infrastructure.logger.warning(
                        "direct edit provider_failover: from=%s remaining=%d reason=%s",
                        provider.name,
                        remaining,
                        decision.reason,
                    )
                    await upstream_services().transport.emit_image_progress(
                        progress_callback,
                        "provider_failover",
                        from_provider=provider.name,
                        remaining=remaining,
                        reason=decision.reason,
                        route="image2_edit_direct",
                        **upstream_services().providers.provider_attempt_context(
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
                upstream_services().providers.pool_release_inflight(
                    pool,
                    provider.name,
                    "generations",
                )

    _raise_direct_provider_failures(providers, errors, action_label="edit")


async def _responses_image_stream_with_failover(
    *,
    prompt: str,
    size: str,
    action: str,
    images: list[bytes] | None,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None,
    use_httpx: bool,
    task_id: str = "",
    provider_override: Any | None = None,
    user_id: str | None = None,
) -> tuple[str, str | None]:
    """Run Responses image generation across the configured provider chain."""
    from ..retry import is_retriable as classify_retriable

    _ = task_id
    pool = await upstream_services().infrastructure.provider_pool.get_pool()
    lane_owns_inflight = provider_override is None
    providers = (
        [provider_override]
        if provider_override is not None
        else await upstream_services().providers.pool_select_compat(
            pool,
            route="image",
            ignore_cooldown=True,
            endpoint_kind="responses",
        )
    )
    errors: list[BaseException] = []

    for index, provider in enumerate(providers):
        if lane_owns_inflight and index > 0:
            upstream_services().providers.pool_acquire_inflight(
                pool,
                provider.name,
                "responses",
            )
        started = time.monotonic()
        try:
            unavailable_error = (
                upstream_services().providers.provider_endpoint_unavailable_error(
                    provider,
                    "responses",
                )
            )
            if unavailable_error is not None:
                errors.append(unavailable_error)
                continue
            try:
                kwargs: dict[str, Any] = {
                    "prompt": prompt,
                    "size": size,
                    "action": action,
                    "images": images,
                    "quality": quality,
                    "output_format": output_format,
                    "output_compression": output_compression,
                    "background": background,
                    "moderation": moderation,
                    "model": model,
                    "progress_callback": progress_callback,
                    "use_httpx": use_httpx,
                    "base_url_override": provider.base_url,
                    "api_key_override": provider.api_key,
                }
                proxy = upstream_services().core.provider_proxy(provider)
                if proxy is not None:
                    kwargs["proxy_override"] = proxy
                pinned_target = _provider_pinned_target(provider, proxy)
                if pinned_target is not None:
                    kwargs["pinned_target_override"] = pinned_target
                if user_id is not None:
                    kwargs["user_id"] = user_id
                kwargs["before_attempt"] = (
                    upstream_services().providers.image_request_attempt_claim(
                        pool,
                        provider,
                        route="responses:image_generation",
                    )
                )
                result = (
                    await upstream_services().retry.responses_image_stream_with_retry(
                        **kwargs
                    )
                )
                if not upstream_services().providers.is_byok_provider(provider):
                    upstream_services().providers.pool_report_image_success(
                        pool,
                        provider.name,
                        endpoint_kind="responses",
                    )
                await upstream_services().transport.emit_image_progress(
                    progress_callback,
                    "provider_used",
                    provider=provider.name,
                    route="responses",
                    source="responses",
                    endpoint="responses:image_generation",
                    **upstream_services().providers.provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        duration_ms=(time.monotonic() - started) * 1000,
                        status="succeeded",
                    ),
                )
                return result
            except (
                asyncio.CancelledError,
                upstream_services().infrastructure.UpstreamCancelled,
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
                    upstream_services().retry.should_continue_image_provider_failover(
                        exc,
                        retriable=decision.retriable,
                    )
                )
                if not should_continue:
                    upstream_services().infrastructure.logger.warning(
                        "provider %s terminal error, not failing over: %s",
                        provider.name,
                        decision.reason,
                    )
                    raise
                is_rate_limited, retry_after = (
                    upstream_services().providers.is_image_rate_limit_error(exc)
                )
                if not upstream_services().providers.is_byok_provider(provider):
                    if is_rate_limited:
                        pool.report_image_rate_limited(
                            provider.name,
                            retry_after_s=retry_after,
                        )
                    else:
                        upstream_services().providers.pool_report_image_failure(
                            pool,
                            provider.name,
                            endpoint_kind="responses",
                        )
                remaining = len(providers) - index - 1
                if remaining > 0:
                    upstream_services().infrastructure.logger.warning(
                        "provider_failover: from=%s remaining=%d reason=%s",
                        provider.name,
                        remaining,
                        decision.reason,
                    )
                    await upstream_services().transport.emit_image_progress(
                        progress_callback,
                        "provider_failover",
                        from_provider=provider.name,
                        remaining=remaining,
                        reason=decision.reason,
                        route="responses",
                        **upstream_services().providers.provider_attempt_context(
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
                upstream_services().providers.pool_release_inflight(
                    pool,
                    provider.name,
                    "responses",
                )

    merged = upstream_services().retry.merge_fallback_errors(
        errors,
        error_code=upstream_services().infrastructure.EC.ALL_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} upstream providers failed",
    )
    merged.payload["provider_errors"] = (
        upstream_services().retry.provider_error_details(
            providers,
            errors,
        )
    )
    raise merged


__all__ = [
    "_direct_edit_image_with_failover",
    "_direct_generate_image_with_failover",
    "_responses_image_stream_with_failover",
]
