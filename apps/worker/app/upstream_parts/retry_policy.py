"""Image retry, error merging, and provider failover policy."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from typing import Any, Awaitable, Callable

from lumen_core.constants import GenerationErrorCode as EC
from lumen_core.providers import ProviderProxyDefinition
from lumen_core.upstream_billing import (
    IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES,
    UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
)

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from .delivery_evidence import (
    apply_dispatch_receipt,
    dispatch_receipt_reason,
    merged_dispatch_receipt_reason,
)
from .image_execution import ImageExecutionRequest


_POST_RESPONSE_RESULT_UNKNOWN_CODES = frozenset(
    {
        EC.BAD_RESPONSE.value,
        EC.SSE_CURL_FAILED.value,
        EC.STREAM_INTERRUPTED.value,
        EC.STREAM_TOO_LARGE.value,
    }
)


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def _summarize_exception(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> dict[str, Any]:
    services = _runtime_services(runtime)
    item: dict[str, Any] = {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }
    if isinstance(exc, services.infrastructure.UpstreamError):
        item["status_code"] = exc.status_code
        item["error_code"] = exc.error_code
        if exc.payload:
            item["payload"] = exc.payload
    return item


def _truncate_lane_summary(
    lane: str,
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> dict[str, Any]:
    services = _runtime_services(runtime)
    out: dict[str, Any] = {
        "lane": lane,
        "type": type(exc).__name__,
        "message": str(exc)[:200],
    }
    if isinstance(exc, services.infrastructure.UpstreamError):
        out["status_code"] = exc.status_code
        out["error_code"] = exc.error_code
        payload = exc.payload or {}
        if isinstance(payload, dict):
            for key in ("trace_id", "x_trace_id", "url", "path", "method"):
                value = payload.get(key)
                if value is not None:
                    out[key] = value
    return out


def _is_retryable_fallback_exception(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    services = _runtime_services(runtime)
    if _dispatch_proves_undelivered(exc):
        return True
    if isinstance(exc, services.infrastructure.UpstreamError):
        if (exc.error_code or "") in IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES:
            return False
        if exc.status_code in services.core.RETRY_STATUS:
            return True
        if exc.status_code == 429:
            return True
        return exc.error_code in services.core.FALLBACK_RETRY_ERROR_CODES
    return isinstance(exc, services.core.RETRY_HTTPX_EXC)


def _post_response_result_unknown(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    services = _runtime_services(runtime)
    return bool(
        isinstance(exc, services.infrastructure.UpstreamError)
        and isinstance(exc.status_code, int)
        and 200 <= exc.status_code < 300
        and (exc.error_code or "") in _POST_RESPONSE_RESULT_UNKNOWN_CODES
    )


def _dispatch_proves_undelivered(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    return bool(
        not (isinstance(status_code, int) and status_code > 0)
        and dispatch_receipt_reason(exc) == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
    )


def _responses_post_result_may_be_unknown(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    """Return true when replay could create a second billable image request."""

    services = _runtime_services(runtime)
    if _post_response_result_unknown(exc, runtime=runtime):
        return True
    if isinstance(exc, services.infrastructure.UpstreamError):
        status_code = exc.status_code
        if isinstance(status_code, int) and 500 <= status_code < 600:
            return True
    if _dispatch_proves_undelivered(exc):
        return False
    if isinstance(exc, services.infrastructure.UpstreamError):
        return exc.status_code is None or exc.status_code == 0
    return isinstance(exc, services.core.RETRY_HTTPX_EXC)


def _result_unknown_after_dispatch(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    services = _runtime_services(runtime)
    payload = (
        dict(exc.payload)
        if isinstance(exc, services.infrastructure.UpstreamError)
        and isinstance(exc.payload, dict)
        else {}
    )
    status_code = getattr(exc, "status_code", None)
    payload.update(
        {
            "upstream_result_unknown": True,
            "wrapped_error_code": getattr(exc, "error_code", None),
        }
    )
    payload.setdefault("path", "responses")
    payload.setdefault("method", "POST")
    if isinstance(status_code, int) and status_code > 0:
        payload["response_received"] = True
    return services.infrastructure.UpstreamError(
        "responses image POST outcome is unknown after dispatch; "
        "the request was not replayed",
        status_code=status_code if status_code is not None else 0,
        error_code=EC.DIRECT_IMAGE_RESULT_UNKNOWN.value,
        payload=payload,
    )


def _fallback_retry_backoff_seconds(
    attempt: int,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> float:
    services = _runtime_services(runtime)
    return min(
        services.core.FALLBACK_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)),
        services.core.FALLBACK_RETRY_BACKOFF_MAX_S,
    )


def _max_attempts_for_exception(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> int:
    """Return the fallback retry budget for the current error shape."""
    services = _runtime_services(runtime)
    if isinstance(exc, services.infrastructure.UpstreamError):
        if exc.status_code == 429:
            return services.core.FALLBACK_MAX_ATTEMPTS_429
        if exc.status_code is not None and 500 <= exc.status_code < 600:
            return services.core.FALLBACK_MAX_ATTEMPTS_5XX
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            return services.core.FALLBACK_MAX_ATTEMPTS_4XX
        return services.core.FALLBACK_MAX_ATTEMPTS
    if isinstance(exc, services.core.RETRY_HTTPX_EXC):
        return services.core.FALLBACK_MAX_ATTEMPTS_5XX
    return services.core.FALLBACK_MAX_ATTEMPTS


def _retry_after_seconds(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> float | None:
    """Read and cap a retry-after hint from an upstream error payload."""
    services = _runtime_services(runtime)
    if not isinstance(exc, services.infrastructure.UpstreamError):
        return None
    payload = exc.payload or {}
    candidates: list[Any] = []
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            candidates.append(err.get("retry_after"))
            candidates.append(err.get("retry_after_seconds"))
        candidates.append(payload.get("retry_after"))
        candidates.append(payload.get("retry_after_seconds"))
    for value in candidates:
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return min(seconds, services.core.FALLBACK_429_MAX_WAIT_S)
    return None


def _merge_fallback_errors(
    errors: list[BaseException],
    *,
    error_code: str,
    message: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    services = _runtime_services(runtime)
    receipt_reason = merged_dispatch_receipt_reason(errors)
    if not errors:
        return services.infrastructure.UpstreamError(
            message,
            status_code=200,
            error_code=error_code,
        )
    if any(_mentions_safety_policy(exc, runtime=runtime) for exc in errors):
        payload: dict[str, Any] = {
            "path": "responses",
            "errors": [_summarize_exception(exc, runtime=runtime) for exc in errors],
            "wrapped_error_code": error_code,
        }
        merged = services.infrastructure.UpstreamError(
            "request blocked by upstream safety policy",
            status_code=200,
            error_code=services.infrastructure.EC.MODERATION_BLOCKED.value,
            payload=payload,
        )
        if len(errors) > 1:
            merged.__cause__ = BaseExceptionGroup(message, errors)
        else:
            merged.__cause__ = errors[0]
        apply_dispatch_receipt(merged, receipt_reason)
        return merged
    first = errors[0]
    status_code = 200
    merged_payload: dict[str, Any] = {}
    if isinstance(first, services.infrastructure.UpstreamError):
        status_code = first.status_code if first.status_code is not None else 200
        merged_payload.update(first.payload)
    if receipt_reason == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED:
        status_code = 0
    for key in (
        "receipt_reason",
        "upstream_receipt_reason",
        "upstream_dispatch_delivery",
    ):
        merged_payload.pop(key, None)
    merged_payload.setdefault("path", "responses")
    merged_payload["errors"] = [
        _summarize_exception(exc, runtime=runtime) for exc in errors
    ]
    merged = services.infrastructure.UpstreamError(
        message,
        status_code=status_code,
        error_code=error_code,
        payload=merged_payload,
    )
    if len(errors) > 1:
        merged.__cause__ = BaseExceptionGroup(message, errors)
    else:
        merged.__cause__ = first
    apply_dispatch_receipt(merged, receipt_reason)
    return merged


def _provider_error_details(
    providers: list[Any],
    errors: list[BaseException],
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for provider, exc in zip(providers, errors, strict=False):
        details.append(
            {
                "provider": getattr(provider, "name", None),
                **_summarize_exception(exc, runtime=runtime),
            }
        )
    return details


def _mentions_safety_policy(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    """Detect safety blocks hidden inside fallback/provider wrapper errors."""
    services = _runtime_services(runtime)
    text = str(exc).lower()
    if any(marker in text for marker in services.core.SAFETY_POLICY_ERROR_MARKERS):
        return True
    if isinstance(exc, services.infrastructure.UpstreamError) and exc.payload:
        try:
            payload_text = json.dumps(exc.payload, ensure_ascii=False).lower()
        except Exception:  # noqa: BLE001
            payload_text = repr(exc.payload).lower()
        if any(
            marker in payload_text
            for marker in services.core.SAFETY_POLICY_ERROR_MARKERS
        ):
            return True
    nested = getattr(exc, "exceptions", None)
    if nested and any(
        isinstance(child, BaseException)
        and _mentions_safety_policy(child, runtime=runtime)
        for child in nested
    ):
        return True
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException) and _mentions_safety_policy(
        cause, runtime=runtime
    ):
        return True
    context = getattr(exc, "__context__", None)
    return isinstance(
        context,
        BaseException,
    ) and _mentions_safety_policy(context, runtime=runtime)


def _should_continue_image_provider_failover(
    exc: BaseException,
    *,
    retriable: bool,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    """True when another image provider may handle the same request."""
    services = _runtime_services(runtime)
    if services.providers.is_quota_accounting_unavailable(exc):
        return False
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict) and (
        payload.get("sidecar_execution_accepted") is True
        or isinstance(payload.get("sidecar_execution"), dict)
    ):
        return False
    if retriable:
        return True
    if (
        isinstance(exc, services.infrastructure.UpstreamError)
        and exc.error_code in services.core.IMAGE_PROVIDER_FAILOVER_ERROR_CODES
    ):
        return True
    return _mentions_safety_policy(exc, runtime=runtime)


def _merge_image_path_errors(
    *,
    action: str,
    primary_path: str,
    primary_error: BaseException,
    fallback_path: str,
    fallback_error: BaseException,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    services = _runtime_services(runtime)
    receipt_reason = merged_dispatch_receipt_reason([primary_error, fallback_error])
    status_code = 502
    payload: dict[str, Any] = {}
    if isinstance(primary_error, services.infrastructure.UpstreamError):
        status_code = primary_error.status_code or status_code
        payload.update(primary_error.payload)
    elif isinstance(fallback_error, services.infrastructure.UpstreamError):
        status_code = fallback_error.status_code or status_code
        payload.update(fallback_error.payload)
    for key in (
        "receipt_reason",
        "upstream_receipt_reason",
        "upstream_dispatch_delivery",
    ):
        payload.pop(key, None)
    terminal_unknown: tuple[str, Any] | None = None
    for path, error in (
        (fallback_path, fallback_error),
        (primary_path, primary_error),
    ):
        if (
            isinstance(error, services.infrastructure.UpstreamError)
            and (error.error_code or "") in IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES
        ):
            terminal_unknown = (path, error)
            break
    payload.setdefault("path", primary_path)
    payload["primary_path"] = primary_path
    payload["fallback_path"] = fallback_path
    payload["path_errors"] = [
        {
            "path": primary_path,
            **_summarize_exception(primary_error, runtime=runtime),
        },
        {
            "path": fallback_path,
            **_summarize_exception(fallback_error, runtime=runtime),
        },
    ]
    message = f"{action} image paths failed: {primary_path}, {fallback_path}"
    error_code = services.infrastructure.EC.PROVIDER_EXHAUSTED.value
    if terminal_unknown is not None:
        terminal_path, terminal_error = terminal_unknown
        status_code = terminal_error.status_code or status_code
        error_code = terminal_error.error_code
        payload["terminal_path"] = terminal_path
        payload["upstream_result_unknown"] = True
    merged = services.infrastructure.UpstreamError(
        message,
        status_code=status_code,
        error_code=error_code,
        payload=payload,
    )
    merged.__cause__ = BaseExceptionGroup(
        message,
        [primary_error, fallback_error],
    )
    apply_dispatch_receipt(merged, receipt_reason)
    return merged


async def _responses_image_stream_with_retry(
    request: ImageExecutionRequest,
    *,
    use_httpx: bool,
    base_url_override: str | None = None,
    api_key_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    pinned_target_override: Any | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    """Retry the Responses image stream with error-specific budgets."""
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    errors: list[BaseException] = []
    attempt = 0
    hard_cap = max(
        services.core.FALLBACK_MAX_ATTEMPTS,
        services.core.FALLBACK_MAX_ATTEMPTS_5XX,
        services.core.FALLBACK_MAX_ATTEMPTS_429,
        services.core.FALLBACK_MAX_ATTEMPTS_4XX,
    )
    context = request.request_context
    while attempt < hard_cap:
        attempt_context = context.with_retry_attempt(context.retry_attempt + attempt)
        attempt_request = replace(request, request_context=attempt_context)
        try:
            if before_attempt is not None:
                await before_attempt(attempt + 1)
            kwargs: dict[str, Any] = {
                "use_httpx": use_httpx,
                "base_url_override": base_url_override,
                "api_key_override": api_key_override,
            }
            if proxy_override is not None:
                kwargs["proxy_override"] = proxy_override
            if pinned_target_override is not None:
                kwargs["pinned_target_override"] = pinned_target_override
            return await services.responses.responses_image_stream(
                attempt_request,
                **kwargs,
            )
        except (
            asyncio.CancelledError,
            services.infrastructure.UpstreamCancelled,
        ):
            raise
        except Exception as exc:  # noqa: BLE001
            if _responses_post_result_may_be_unknown(
                exc,
                runtime=runtime,
            ):
                raise _result_unknown_after_dispatch(
                    exc,
                    runtime=runtime,
                ) from exc
            errors.append(exc)
            attempt += 1
            attempts_for_this = _max_attempts_for_exception(
                exc,
                runtime=runtime,
            )
            if attempt >= attempts_for_this or not _is_retryable_fallback_exception(
                exc, runtime=runtime
            ):
                raise _merge_fallback_errors(
                    errors,
                    error_code=(
                        exc.error_code
                        if isinstance(exc, services.infrastructure.UpstreamError)
                        and exc.error_code
                        else "responses_fallback_failed"
                    ),
                    message=str(exc) or "responses fallback failed",
                    runtime=runtime,
                ) from exc
            retry_after = _retry_after_seconds(exc, runtime=runtime)
            if retry_after is not None:
                backoff = retry_after
            elif (
                isinstance(
                    exc,
                    services.infrastructure.UpstreamError,
                )
                and exc.status_code == 429
            ):
                backoff = min(
                    services.core.FALLBACK_429_DEFAULT_WAIT_S,
                    services.core.FALLBACK_429_MAX_WAIT_S,
                )
            else:
                backoff = _fallback_retry_backoff_seconds(
                    attempt,
                    runtime=runtime,
                )
            services.infrastructure.logger.warning(
                "responses fallback retrying action=%s size=%s attempt=%d/%d "
                "backoff=%.1fs err=%r",
                request.action,
                request.size,
                attempt + 1,
                attempts_for_this,
                backoff,
                exc,
            )
            await asyncio.sleep(backoff)
    raise _merge_fallback_errors(
        errors,
        error_code=services.infrastructure.EC.RESPONSES_FALLBACK_FAILED.value,
        message="responses fallback exhausted retry budget",
        runtime=runtime,
    )


__all__ = [
    "_fallback_retry_backoff_seconds",
    "_is_retryable_fallback_exception",
    "_max_attempts_for_exception",
    "_mentions_safety_policy",
    "_merge_fallback_errors",
    "_merge_image_path_errors",
    "_provider_error_details",
    "_responses_image_stream_with_retry",
    "_retry_after_seconds",
    "_should_continue_image_provider_failover",
    "_summarize_exception",
    "_truncate_lane_summary",
]
