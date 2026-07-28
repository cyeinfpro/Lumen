"""Provider selection, endpoint capability, inflight, and image quota helpers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from .image_execution import ImageRequestContext, ensure_image_request_context


@dataclass(slots=True)
class ImageQuotaReservation:
    provider_name: str
    member: str
    reserved_at: float
    state: str = "reserved"


class ProviderSelector(Protocol):
    async def __call__(self, **kwargs: object) -> list[object]: ...


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def _provider_pool_redis(pool: Any) -> Any:
    getter = getattr(pool, "get_redis", None)
    if callable(getter):
        return getter()
    return None


def _pool_acquire_inflight(
    pool: Any,
    name: str,
    endpoint_kind: str | None,
) -> None:
    """Acquire endpoint inflight state when supported by the pool."""
    fn = getattr(pool, "acquire_image_inflight", None)
    if callable(fn):
        fn(name, endpoint_kind)


def _pool_release_inflight(
    pool: Any,
    name: str,
    endpoint_kind: str | None,
) -> None:
    fn = getattr(pool, "release_image_inflight", None)
    if callable(fn):
        fn(name, endpoint_kind)


def _is_byok_provider(provider: Any) -> bool:
    """Return whether a provider is backed by a user credential."""
    name = getattr(provider, "name", "") or ""
    return isinstance(name, str) and name.startswith("user:")


def _provider_attempt_context(
    provider: Any,
    *,
    attempt: int | None = None,
    duration_ms: int | float | None = None,
    status: str | None = None,
    reason: str | None = None,
    exc: BaseException | None = None,
    endpoint_attempt: int | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> dict[str, Any]:
    services = _runtime_services(runtime)
    out: dict[str, Any] = {
        "byok": services.providers.is_byok_provider(provider)
    }
    if attempt is not None:
        out["attempt"] = attempt
    if endpoint_attempt is not None:
        out["endpoint_attempt"] = endpoint_attempt
    if duration_ms is not None:
        out["duration_ms"] = max(0, int(duration_ms))
    if status:
        out["status"] = status
    if reason:
        out["attempt_reason"] = reason
    if isinstance(exc, services.infrastructure.UpstreamError):
        if exc.error_code:
            out["error_code"] = exc.error_code
        if exc.status_code is not None:
            out["status_code"] = exc.status_code
    elif exc is not None:
        out["error_code"] = type(exc).__name__
    return out


def _pool_report_image_success(
    pool: Any,
    name: str,
    *,
    endpoint_kind: str | None = None,
    record_endpoint: bool = True,
) -> None:
    """Call endpoint-aware success reporting with legacy mock compatibility."""
    fn = getattr(pool, "report_image_success", None)
    if not callable(fn):
        return
    try:
        fn(
            name,
            endpoint_kind=endpoint_kind,
            record_endpoint=record_endpoint,
        )
    except TypeError as exc:
        message = str(exc)
        if "record_endpoint" in message:
            try:
                fn(name, endpoint_kind=endpoint_kind)
            except TypeError as inner_exc:
                if "endpoint_kind" not in str(inner_exc):
                    raise
                fn(name)
            return
        if "endpoint_kind" not in message:
            raise
        fn(name)


def _pool_report_image_failure(
    pool: Any,
    name: str,
    *,
    endpoint_kind: str | None = None,
) -> None:
    """Call endpoint-aware failure reporting with legacy mock compatibility."""
    fn = getattr(pool, "report_image_failure", None)
    if not callable(fn):
        return
    try:
        fn(name, endpoint_kind=endpoint_kind)
    except TypeError as exc:
        if "endpoint_kind" not in str(exc):
            raise
        fn(name)


def _provider_endpoint_locked_error(
    provider: Any,
    endpoint_kind: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> Any | None:
    services = _runtime_services(runtime)
    if services.infrastructure.endpoint_kind_allowed(
        provider, endpoint_kind
    ):
        return None
    provider_name = getattr(provider, "name", "unknown")
    configured = getattr(provider, "image_jobs_endpoint", "auto")
    return services.infrastructure.UpstreamError(
        f"provider {provider_name} locked to {configured}; refuses {endpoint_kind}",
        error_code=services.infrastructure.EC.NO_PROVIDERS.value,
        status_code=503,
        payload={
            "provider": str(provider_name),
            "endpoint_kind": endpoint_kind,
            "locked_endpoint": str(configured),
            "reason": "endpoint_locked",
        },
    )


def _provider_capability_error(
    provider: Any,
    endpoint_kind: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> Any | None:
    services = _runtime_services(runtime)
    if services.infrastructure.provider_supports_route(
        provider,
        route="image",
        endpoint_kind=endpoint_kind,
    ):
        return None
    provider_name = getattr(provider, "name", "unknown")
    return services.infrastructure.UpstreamError(
        f"provider {provider_name} does not support image endpoint {endpoint_kind}",
        error_code=services.infrastructure.EC.NO_PROVIDERS.value,
        status_code=503,
        payload={
            "provider": str(provider_name),
            "endpoint_kind": endpoint_kind,
            "reason": "capability_unsupported",
        },
    )


def _provider_endpoint_unavailable_error(
    provider: Any,
    endpoint_kind: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> Any | None:
    services = _runtime_services(runtime)
    return services.providers.provider_endpoint_locked_error(
        provider,
        endpoint_kind,
    ) or services.providers.provider_capability_error(
        provider, endpoint_kind
    )


def _provider_allows_image_endpoint(
    provider: Any,
    endpoint_kind: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    services = _runtime_services(runtime)
    return (
        services.providers.provider_endpoint_unavailable_error(
            provider, endpoint_kind
        )
        is None
    )


def _pool_select_kwargs(
    *,
    route: str,
    ignore_cooldown: bool,
    task_id: str | None,
    endpoint_kind: str | None,
    acquire_inflight: bool,
    requires_mask: bool,
    mask_transport_required: bool,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "route": route,
        "ignore_cooldown": ignore_cooldown,
        "acquire_inflight": acquire_inflight,
    }
    if task_id is not None:
        kwargs["task_id"] = task_id
    if endpoint_kind is not None:
        kwargs["endpoint_kind"] = endpoint_kind
    if requires_mask:
        kwargs["requires_mask"] = True
        if not mask_transport_required:
            kwargs["mask_transport_required"] = False
    return kwargs


def _unsupported_select_kwarg(
    exc: TypeError,
    kwargs: dict[str, Any],
) -> str | None:
    message = str(exc)
    for name in (
        "mask_transport_required",
        "requires_mask",
        "acquire_inflight",
        "endpoint_kind",
    ):
        if name in kwargs and name in message:
            return name
    return None


async def _call_pool_select_compat(
    selector: ProviderSelector,
    kwargs: dict[str, Any],
) -> tuple[list[object], bool]:
    endpoint_fallback = False
    while True:
        try:
            return await selector(**kwargs), endpoint_fallback
        except TypeError as exc:
            unsupported = _unsupported_select_kwarg(exc, kwargs)
            if unsupported is None:
                raise
            kwargs.pop(unsupported)
            endpoint_fallback = endpoint_fallback or unsupported == "endpoint_kind"


def _filter_mask_providers(
    providers: list[Any],
    *,
    requires_mask: bool,
    mask_transport_required: bool,
) -> list[Any]:
    if not requires_mask or not mask_transport_required:
        return list(providers)
    file_mode = [
        provider
        for provider in providers
        if getattr(provider, "image_edit_input_transport", "url") == "file"
    ]
    return file_mode or list(providers)


def _filter_legacy_select_result(
    providers: list[Any],
    *,
    endpoint_kind: str | None,
    endpoint_fallback: bool,
    requires_mask: bool,
    mask_transport_required: bool,
    runtime: ImageUpstreamRuntime | None = None,
) -> list[Any]:
    services = _runtime_services(runtime)
    if endpoint_fallback and endpoint_kind is not None:
        providers = [
            provider
            for provider in providers
            if services.providers.provider_allows_image_endpoint(
                provider, endpoint_kind
            )
        ]
    if requires_mask:
        return _filter_mask_providers(
            providers,
            requires_mask=requires_mask,
            mask_transport_required=mask_transport_required,
        )
    return providers


async def _pool_select_compat(
    pool: Any,
    *,
    route: str,
    ignore_cooldown: bool = False,
    task_id: str | None = None,
    endpoint_kind: str | None = None,
    acquire_inflight: bool = True,
    requires_mask: bool = False,
    mask_transport_required: bool = True,
    runtime: ImageUpstreamRuntime | None = None,
) -> list[Any]:
    """Call ProviderPool.select while retaining compatibility with older mocks."""
    selector = getattr(pool, "select")
    kwargs = _pool_select_kwargs(
        route=route,
        ignore_cooldown=ignore_cooldown,
        task_id=task_id,
        endpoint_kind=endpoint_kind,
        acquire_inflight=acquire_inflight,
        requires_mask=requires_mask,
        mask_transport_required=mask_transport_required,
    )
    providers, endpoint_fallback = await _call_pool_select_compat(selector, kwargs)
    return _filter_legacy_select_result(
        providers,
        endpoint_kind=endpoint_kind,
        endpoint_fallback=endpoint_fallback,
        requires_mask=requires_mask,
        mask_transport_required=mask_transport_required,
        runtime=runtime,
    )


def _is_image_rate_limit_error(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[bool, float | None]:
    """Recognize image account quota and concurrency exhaustion errors."""
    services = _runtime_services(runtime)
    if not isinstance(exc, services.infrastructure.UpstreamError):
        return False, None
    code = (getattr(exc, "error_code", None) or "").lower()
    message = str(exc).lower()
    if (
        exc.status_code == 429
        or code in ("rate_limit_error", "rate_limit_exceeded")
        or "rate limit" in message
        or "rate_limit" in message
        or "quota" in message
        or "concurrency limit exceeded" in message
    ):
        return True, services.retry.retry_after_seconds(exc)
    return False, None


def _is_quota_accounting_unavailable(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    services = _runtime_services(runtime)
    return (
        isinstance(exc, services.infrastructure.UpstreamError)
        and exc.error_code
        == services.infrastructure.EC.QUOTA_ACCOUNTING_UNAVAILABLE.value
    )


def _provider_has_image_quota(provider: Any) -> bool:
    rate_limit = getattr(provider, "image_rate_limit", None)
    daily_quota = getattr(provider, "image_daily_quota", None)
    return bool(rate_limit) or (
        isinstance(daily_quota, int)
        and not isinstance(daily_quota, bool)
        and daily_quota > 0
    )


async def _reserve_admin_image_call(
    pool: Any,
    provider: Any,
    *,
    route: str,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> Any | None:
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    if services.providers.is_byok_provider(
        provider
    ) or not services.providers.provider_has_image_quota(provider):
        return None
    from .. import account_limiter

    provider_name = str(getattr(provider, "name", "unknown"))
    reservation_member = context.next_quota_member(provider_name, route)
    reserved_at = time.time()
    redis = services.providers.provider_pool_redis(pool)
    if redis is None:
        raise services.infrastructure.UpstreamError(
            "quota reservation unavailable",
            status_code=503,
            error_code=services.infrastructure.EC.QUOTA_ACCOUNTING_UNAVAILABLE.value,
            payload={
                "provider": provider_name,
                "reservation_member": reservation_member,
                "retry_after": account_limiter.REDIS_ERROR_RETRY_AFTER_S,
            },
        )
    try:
        allowed, retry_after, member = await account_limiter.reserve_quota(
            redis,
            provider_name,
            getattr(provider, "image_rate_limit", None),
            getattr(provider, "image_daily_quota", None),
            task_id=reservation_member,
            now=reserved_at,
        )
    except account_limiter.AccountLimiterUnavailable as exc:
        raise services.infrastructure.UpstreamError(
            "quota reservation unavailable",
            status_code=503,
            error_code=services.infrastructure.EC.QUOTA_ACCOUNTING_UNAVAILABLE.value,
            payload={
                "provider": provider_name,
                "reservation_member": reservation_member,
                "retry_after": account_limiter.REDIS_ERROR_RETRY_AFTER_S,
            },
        ) from exc
    if not allowed:
        raise services.infrastructure.UpstreamError(
            "image account quota exhausted",
            status_code=429,
            error_code=services.infrastructure.EC.RATE_LIMIT_ERROR.value,
            payload={
                "provider": provider_name,
                "reservation_member": member or reservation_member,
                "retry_after": retry_after,
            },
        )
    return ImageQuotaReservation(
        provider_name=provider_name,
        member=member or reservation_member,
        reserved_at=reserved_at,
    )


def _image_request_attempt_claim(
    pool: Any,
    provider: Any,
    *,
    route: str,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> Callable[[int], Awaitable[None]]:
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)

    async def claim(attempt: int) -> None:
        reservation = await services.providers.reserve_admin_image_call(
            pool,
            provider,
            route=f"{route}:attempt-{attempt}",
            request_context=context,
        )
        if reservation is not None:
            reservation.state = "started"

    return claim


async def _release_unused_image_reservation(
    pool: Any,
    reservation: Any | None,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = _runtime_services(runtime)
    if reservation is None or reservation.state != "reserved":
        return
    from .. import account_limiter

    try:
        released = await account_limiter.release_quota(
            services.providers.provider_pool_redis(pool),
            reservation.provider_name,
            reservation.member,
            reserved_at=reservation.reserved_at,
        )
    except account_limiter.AccountLimiterUnavailable:
        services.infrastructure.logger.exception(
            "unused image quota reservation release failed provider=%s member=%s",
            reservation.provider_name,
            reservation.member,
        )
        return
    if released:
        reservation.state = "released"


@asynccontextmanager
async def _image_quota_claim(
    pool: Any | None,
    provider: Any,
    *,
    route: str,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> AsyncIterator[Any | None]:
    quota_pool = pool
    reservation: Any | None = None
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    if not services.providers.is_byok_provider(
        provider
    ) and services.providers.provider_has_image_quota(provider):
        if quota_pool is None:
            quota_pool = (
                await services.infrastructure.provider_pool.get_pool()
            )
        reservation = await services.providers.reserve_admin_image_call(
            quota_pool,
            provider,
            route=route,
            request_context=context,
        )
    try:
        yield reservation
    finally:
        if quota_pool is not None:
            await services.providers.release_unused_image_reservation(
                quota_pool,
                reservation,
            )


async def _record_admin_image_call_or_raise(
    pool: Any,
    provider: Any,
    *,
    task_id: str = "",
    reservation: ImageQuotaReservation | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    services = _runtime_services(runtime)
    provider_name = str(getattr(provider, "name", provider))
    if (
        reservation is not None
        and reservation.provider_name == provider_name
        and reservation.state in {"started", "confirmed"}
    ):
        reservation.state = "confirmed"
        return True
    if not services.providers.provider_has_image_quota(provider):
        return True
    from .. import account_limiter

    try:
        await asyncio.shield(
            account_limiter.record_image_call(
                services.providers.provider_pool_redis(pool),
                provider_name,
                task_id=task_id,
            )
        )
        return True
    except account_limiter.AccountLimiterUnavailable as exc:
        retry_after = account_limiter.REDIS_ERROR_RETRY_AFTER_S
        services.infrastructure.logger.error(
            "quota accounting deferred after upstream success provider=%s "
            "task=%s retry_after=%.1fs err=%s",
            provider_name,
            task_id,
            retry_after,
            exc,
        )
        return False


__all__ = [
    "_image_quota_claim",
    "_image_request_attempt_claim",
    "_is_byok_provider",
    "_is_image_rate_limit_error",
    "_is_quota_accounting_unavailable",
    "_pool_acquire_inflight",
    "_pool_release_inflight",
    "_pool_report_image_failure",
    "_pool_report_image_success",
    "_pool_select_compat",
    "_provider_allows_image_endpoint",
    "_provider_attempt_context",
    "_provider_capability_error",
    "_provider_endpoint_locked_error",
    "_provider_endpoint_unavailable_error",
    "_provider_has_image_quota",
    "_provider_pool_redis",
    "_record_admin_image_call_or_raise",
    "_release_unused_image_reservation",
    "_reserve_admin_image_call",
]
