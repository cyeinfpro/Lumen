"""Provider selection, endpoint capability, inflight, and image quota helpers."""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve compatibility dependencies at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


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
) -> dict[str, Any]:
    facade = _facade()
    out: dict[str, Any] = {"byok": facade._is_byok_provider(provider)}
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
    if isinstance(exc, facade.UpstreamError):
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
) -> Any | None:
    facade = _facade()
    if facade.endpoint_kind_allowed(provider, endpoint_kind):
        return None
    provider_name = getattr(provider, "name", "unknown")
    configured = getattr(provider, "image_jobs_endpoint", "auto")
    return facade.UpstreamError(
        f"provider {provider_name} locked to {configured}; refuses {endpoint_kind}",
        error_code=facade.EC.NO_PROVIDERS.value,
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
) -> Any | None:
    facade = _facade()
    if facade.provider_supports_route(
        provider,
        route="image",
        endpoint_kind=endpoint_kind,
    ):
        return None
    provider_name = getattr(provider, "name", "unknown")
    return facade.UpstreamError(
        f"provider {provider_name} does not support image endpoint {endpoint_kind}",
        error_code=facade.EC.NO_PROVIDERS.value,
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
) -> Any | None:
    facade = _facade()
    return facade._provider_endpoint_locked_error(
        provider,
        endpoint_kind,
    ) or facade._provider_capability_error(provider, endpoint_kind)


def _provider_allows_image_endpoint(
    provider: Any,
    endpoint_kind: str,
) -> bool:
    facade = _facade()
    return facade._provider_endpoint_unavailable_error(provider, endpoint_kind) is None


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
    selector: Callable[..., Awaitable[list[Any]]],
    kwargs: dict[str, Any],
) -> tuple[list[Any], bool]:
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
) -> list[Any]:
    facade = _facade()
    if endpoint_fallback and endpoint_kind is not None:
        providers = [
            provider
            for provider in providers
            if facade._provider_allows_image_endpoint(provider, endpoint_kind)
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
    )


def _is_image_rate_limit_error(
    exc: BaseException,
) -> tuple[bool, float | None]:
    """Recognize image account quota and concurrency exhaustion errors."""
    facade = _facade()
    if not isinstance(exc, facade.UpstreamError):
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
        return True, facade._retry_after_seconds(exc)
    return False, None


def _is_quota_accounting_unavailable(exc: BaseException) -> bool:
    facade = _facade()
    return (
        isinstance(exc, facade.UpstreamError)
        and exc.error_code == facade.EC.QUOTA_ACCOUNTING_UNAVAILABLE.value
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
) -> Any | None:
    facade = _facade()
    if facade._is_byok_provider(provider) or not facade._provider_has_image_quota(
        provider
    ):
        return None
    from .. import account_limiter

    provider_name = str(getattr(provider, "name", "unknown"))
    reservation_member = facade._next_image_quota_member(provider_name, route)
    reserved_at = time.time()
    redis = facade._provider_pool_redis(pool)
    if redis is None:
        raise facade.UpstreamError(
            "quota reservation unavailable",
            status_code=503,
            error_code=facade.EC.QUOTA_ACCOUNTING_UNAVAILABLE.value,
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
        raise facade.UpstreamError(
            "quota reservation unavailable",
            status_code=503,
            error_code=facade.EC.QUOTA_ACCOUNTING_UNAVAILABLE.value,
            payload={
                "provider": provider_name,
                "reservation_member": reservation_member,
                "retry_after": account_limiter.REDIS_ERROR_RETRY_AFTER_S,
            },
        ) from exc
    if not allowed:
        raise facade.UpstreamError(
            "image account quota exhausted",
            status_code=429,
            error_code=facade.EC.RATE_LIMIT_ERROR.value,
            payload={
                "provider": provider_name,
                "reservation_member": member or reservation_member,
                "retry_after": retry_after,
            },
        )
    return facade._ImageQuotaReservation(
        provider_name=provider_name,
        member=member or reservation_member,
        reserved_at=reserved_at,
    )


def _image_request_attempt_claim(
    pool: Any,
    provider: Any,
    *,
    route: str,
) -> Callable[[int], Awaitable[None]]:
    async def claim(attempt: int) -> None:
        facade = _facade()
        reservation = await facade._reserve_admin_image_call(
            pool,
            provider,
            route=f"{route}:attempt-{attempt}",
        )
        if reservation is not None:
            reservation.state = "started"

    return claim


async def _release_unused_image_reservation(
    pool: Any,
    reservation: Any | None,
) -> None:
    facade = _facade()
    if reservation is None or reservation.state != "reserved":
        return
    from .. import account_limiter

    try:
        released = await account_limiter.release_quota(
            facade._provider_pool_redis(pool),
            reservation.provider_name,
            reservation.member,
            reserved_at=reservation.reserved_at,
        )
    except account_limiter.AccountLimiterUnavailable:
        facade.logger.exception(
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
) -> AsyncIterator[Any | None]:
    facade = _facade()
    quota_pool = pool
    reservation: Any | None = None
    token: contextvars.Token[Any | None] | None = None
    if not facade._is_byok_provider(provider) and facade._provider_has_image_quota(
        provider
    ):
        if quota_pool is None:
            quota_pool = await facade.provider_pool.get_pool()
        reservation = await facade._reserve_admin_image_call(
            quota_pool,
            provider,
            route=route,
        )
        if reservation is not None:
            token = facade._image_quota_reservation_ctx.set(reservation)
    try:
        yield reservation
    finally:
        if token is not None:
            facade._image_quota_reservation_ctx.reset(token)
        if quota_pool is not None:
            await facade._release_unused_image_reservation(
                quota_pool,
                reservation,
            )


async def _record_admin_image_call_or_raise(
    pool: Any,
    provider: Any,
    *,
    task_id: str = "",
) -> bool:
    facade = _facade()
    provider_name = str(getattr(provider, "name", provider))
    reservation = facade._image_quota_reservation_ctx.get()
    if (
        reservation is not None
        and reservation.provider_name == provider_name
        and reservation.state in {"started", "confirmed"}
    ):
        reservation.state = "confirmed"
        return True
    if not facade._provider_has_image_quota(provider):
        return True
    from .. import account_limiter

    try:
        await asyncio.shield(
            account_limiter.record_image_call(
                facade._provider_pool_redis(pool),
                provider_name,
                task_id=task_id,
            )
        )
        return True
    except account_limiter.AccountLimiterUnavailable as exc:
        retry_after = account_limiter.REDIS_ERROR_RETRY_AFTER_S
        facade.logger.error(
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
