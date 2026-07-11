"""Provider failover for direct Images API and Responses image calls."""

from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any

from .transport import ImageProgressCallback

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve compatibility dependencies at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


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
    facade = _facade()
    from ..retry import is_retriable as classify_retriable

    pool = await facade.provider_pool.get_pool()
    lane_owns_inflight = provider_override is None
    providers = (
        [provider_override]
        if provider_override is not None
        else await facade._pool_select_compat(
            pool,
            route="image",
            ignore_cooldown=True,
            endpoint_kind="generations",
        )
    )
    errors: list[BaseException] = []

    for index, provider in enumerate(providers):
        if lane_owns_inflight and index > 0:
            facade._pool_acquire_inflight(
                pool,
                provider.name,
                "generations",
            )
        started = time.monotonic()
        try:
            unavailable_error = facade._provider_endpoint_unavailable_error(
                provider,
                "generations",
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
                proxy = facade._provider_proxy(provider)
                if proxy is not None:
                    kwargs["proxy_override"] = proxy
                kwargs["before_attempt"] = facade._image_request_attempt_claim(
                    pool,
                    provider,
                    route="image2:generations",
                )
                result = await facade._direct_generate_image_once(**kwargs)
                if not facade._is_byok_provider(provider):
                    facade._pool_report_image_success(
                        pool,
                        provider.name,
                        endpoint_kind="generations",
                    )
                await facade._emit_image_progress(
                    progress_callback,
                    "provider_used",
                    provider=provider.name,
                    route="image2",
                    source="image2_direct",
                    endpoint="images/generations",
                    **facade._provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        duration_ms=(time.monotonic() - started) * 1000,
                        status="succeeded",
                    ),
                )
                await facade._emit_image_progress(
                    progress_callback,
                    "final_image",
                    source="image2_direct",
                )
                await facade._emit_image_progress(
                    progress_callback,
                    "completed",
                    source="image2_direct",
                )
                return result
            except (asyncio.CancelledError, facade.UpstreamCancelled):
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                should_continue = facade._should_continue_image_provider_failover(
                    exc,
                    retriable=decision.retriable,
                )
                if not should_continue:
                    facade.logger.warning(
                        "direct image provider %s terminal error: %s",
                        provider.name,
                        decision.reason,
                    )
                    raise
                is_rate_limited, retry_after = facade._is_image_rate_limit_error(exc)
                if not facade._is_byok_provider(provider):
                    if is_rate_limited:
                        pool.report_image_rate_limited(
                            provider.name,
                            retry_after_s=retry_after,
                        )
                    else:
                        facade._pool_report_image_failure(
                            pool,
                            provider.name,
                            endpoint_kind="generations",
                        )
                remaining = len(providers) - index - 1
                if remaining > 0:
                    facade.logger.warning(
                        "direct image provider_failover: from=%s remaining=%d reason=%s",
                        provider.name,
                        remaining,
                        decision.reason,
                    )
                    await facade._emit_image_progress(
                        progress_callback,
                        "provider_failover",
                        from_provider=provider.name,
                        remaining=remaining,
                        reason=decision.reason,
                        route="image2_direct",
                        **facade._provider_attempt_context(
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
                facade._pool_release_inflight(
                    pool,
                    provider.name,
                    "generations",
                )

    merged = facade._merge_fallback_errors(
        errors,
        error_code=facade.EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} direct image providers failed",
    )
    merged.payload["provider_errors"] = facade._provider_error_details(
        providers,
        errors,
    )
    raise merged


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
    facade = _facade()
    from ..retry import is_retriable as classify_retriable

    pool = await facade.provider_pool.get_pool()
    lane_owns_inflight = provider_override is None
    requires_mask = mask is not None
    providers = (
        [provider_override]
        if provider_override is not None
        else await facade._pool_select_compat(
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
            facade._pool_acquire_inflight(
                pool,
                provider.name,
                "generations",
            )
        started = time.monotonic()
        try:
            unavailable_error = facade._provider_endpoint_unavailable_error(
                provider,
                "generations",
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
                proxy = facade._provider_proxy(provider)
                if proxy is not None:
                    kwargs["proxy_override"] = proxy
                async with facade._image_quota_claim(
                    pool,
                    provider,
                    route="image2:edits",
                ) as quota_reservation:
                    if quota_reservation is not None:
                        quota_reservation.state = "started"
                    result = await facade._direct_edit_image_once(**kwargs)
                    if not facade._is_byok_provider(provider):
                        facade._pool_report_image_success(
                            pool,
                            provider.name,
                            endpoint_kind="generations",
                        )
                        await facade._record_admin_image_call_or_raise(
                            pool,
                            provider,
                        )
                await facade._emit_image_progress(
                    progress_callback,
                    "provider_used",
                    provider=provider.name,
                    route="image2",
                    source="image2_edit_direct",
                    endpoint="images/edits",
                    **facade._provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        duration_ms=(time.monotonic() - started) * 1000,
                        status="succeeded",
                    ),
                )
                await facade._emit_image_progress(
                    progress_callback,
                    "final_image",
                    source="image2_edit_direct",
                )
                await facade._emit_image_progress(
                    progress_callback,
                    "completed",
                    source="image2_edit_direct",
                )
                return result
            except (asyncio.CancelledError, facade.UpstreamCancelled):
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                should_continue = facade._should_continue_image_provider_failover(
                    exc,
                    retriable=decision.retriable,
                )
                if not should_continue:
                    facade.logger.warning(
                        "direct edit provider %s terminal error: %s",
                        provider.name,
                        decision.reason,
                    )
                    raise
                is_rate_limited, retry_after = facade._is_image_rate_limit_error(exc)
                if not facade._is_byok_provider(provider):
                    if is_rate_limited:
                        pool.report_image_rate_limited(
                            provider.name,
                            retry_after_s=retry_after,
                        )
                    else:
                        facade._pool_report_image_failure(
                            pool,
                            provider.name,
                            endpoint_kind="generations",
                        )
                remaining = len(providers) - index - 1
                if remaining > 0:
                    facade.logger.warning(
                        "direct edit provider_failover: from=%s remaining=%d reason=%s",
                        provider.name,
                        remaining,
                        decision.reason,
                    )
                    await facade._emit_image_progress(
                        progress_callback,
                        "provider_failover",
                        from_provider=provider.name,
                        remaining=remaining,
                        reason=decision.reason,
                        route="image2_edit_direct",
                        **facade._provider_attempt_context(
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
                facade._pool_release_inflight(
                    pool,
                    provider.name,
                    "generations",
                )

    merged = facade._merge_fallback_errors(
        errors,
        error_code=facade.EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} direct edit providers failed",
    )
    merged.payload["provider_errors"] = facade._provider_error_details(
        providers,
        errors,
    )
    raise merged


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
    facade = _facade()
    from ..retry import is_retriable as classify_retriable

    _ = task_id
    pool = await facade.provider_pool.get_pool()
    lane_owns_inflight = provider_override is None
    providers = (
        [provider_override]
        if provider_override is not None
        else await facade._pool_select_compat(
            pool,
            route="image",
            ignore_cooldown=True,
            endpoint_kind="responses",
        )
    )
    errors: list[BaseException] = []

    for index, provider in enumerate(providers):
        if lane_owns_inflight and index > 0:
            facade._pool_acquire_inflight(
                pool,
                provider.name,
                "responses",
            )
        started = time.monotonic()
        try:
            unavailable_error = facade._provider_endpoint_unavailable_error(
                provider,
                "responses",
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
                proxy = facade._provider_proxy(provider)
                if proxy is not None:
                    kwargs["proxy_override"] = proxy
                if user_id is not None:
                    kwargs["user_id"] = user_id
                kwargs["before_attempt"] = facade._image_request_attempt_claim(
                    pool,
                    provider,
                    route="responses:image_generation",
                )
                result = await facade._responses_image_stream_with_retry(**kwargs)
                if not facade._is_byok_provider(provider):
                    facade._pool_report_image_success(
                        pool,
                        provider.name,
                        endpoint_kind="responses",
                    )
                await facade._emit_image_progress(
                    progress_callback,
                    "provider_used",
                    provider=provider.name,
                    route="responses",
                    source="responses",
                    endpoint="responses:image_generation",
                    **facade._provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        duration_ms=(time.monotonic() - started) * 1000,
                        status="succeeded",
                    ),
                )
                return result
            except (asyncio.CancelledError, facade.UpstreamCancelled):
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                should_continue = facade._should_continue_image_provider_failover(
                    exc,
                    retriable=decision.retriable,
                )
                if not should_continue:
                    facade.logger.warning(
                        "provider %s terminal error, not failing over: %s",
                        provider.name,
                        decision.reason,
                    )
                    raise
                is_rate_limited, retry_after = facade._is_image_rate_limit_error(exc)
                if not facade._is_byok_provider(provider):
                    if is_rate_limited:
                        pool.report_image_rate_limited(
                            provider.name,
                            retry_after_s=retry_after,
                        )
                    else:
                        facade._pool_report_image_failure(
                            pool,
                            provider.name,
                            endpoint_kind="responses",
                        )
                remaining = len(providers) - index - 1
                if remaining > 0:
                    facade.logger.warning(
                        "provider_failover: from=%s remaining=%d reason=%s",
                        provider.name,
                        remaining,
                        decision.reason,
                    )
                    await facade._emit_image_progress(
                        progress_callback,
                        "provider_failover",
                        from_provider=provider.name,
                        remaining=remaining,
                        reason=decision.reason,
                        route="responses",
                        **facade._provider_attempt_context(
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
                facade._pool_release_inflight(
                    pool,
                    provider.name,
                    "responses",
                )

    merged = facade._merge_fallback_errors(
        errors,
        error_code=facade.EC.ALL_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} upstream providers failed",
    )
    merged.payload["provider_errors"] = facade._provider_error_details(
        providers,
        errors,
    )
    raise merged


__all__ = [
    "_direct_edit_image_with_failover",
    "_direct_generate_image_with_failover",
    "_responses_image_stream_with_failover",
]
