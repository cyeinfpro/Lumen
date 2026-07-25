"""Image retry, error merging, and provider failover policy."""

from __future__ import annotations

from ..provider_runtime.upstream_services import upstream_services

import asyncio
import json
from typing import Any, Awaitable, Callable

from lumen_core.providers import ProviderProxyDefinition

from .transport import ImageProgressCallback


def _summarize_exception(exc: BaseException) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }
    if isinstance(exc, upstream_services().infrastructure.UpstreamError):
        item["status_code"] = exc.status_code
        item["error_code"] = exc.error_code
        if exc.payload:
            item["payload"] = exc.payload
    return item


def _truncate_lane_summary(lane: str, exc: BaseException) -> dict[str, Any]:
    out: dict[str, Any] = {
        "lane": lane,
        "type": type(exc).__name__,
        "message": str(exc)[:200],
    }
    if isinstance(exc, upstream_services().infrastructure.UpstreamError):
        out["status_code"] = exc.status_code
        out["error_code"] = exc.error_code
        payload = exc.payload or {}
        if isinstance(payload, dict):
            for key in ("trace_id", "x_trace_id", "url", "path", "method"):
                value = payload.get(key)
                if value is not None:
                    out[key] = value
    return out


def _is_retryable_fallback_exception(exc: BaseException) -> bool:
    if isinstance(exc, upstream_services().infrastructure.UpstreamError):
        if exc.status_code in upstream_services().core.RETRY_STATUS:
            return True
        if exc.status_code == 429:
            return True
        return exc.error_code in upstream_services().core.FALLBACK_RETRY_ERROR_CODES
    return isinstance(exc, upstream_services().core.RETRY_HTTPX_EXC)


def _fallback_retry_backoff_seconds(attempt: int) -> float:
    return min(
        upstream_services().core.FALLBACK_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)),
        upstream_services().core.FALLBACK_RETRY_BACKOFF_MAX_S,
    )


def _max_attempts_for_exception(exc: BaseException) -> int:
    """Return the fallback retry budget for the current error shape."""
    if isinstance(exc, upstream_services().infrastructure.UpstreamError):
        if exc.status_code == 429:
            return upstream_services().core.FALLBACK_MAX_ATTEMPTS_429
        if exc.status_code is not None and 500 <= exc.status_code < 600:
            return upstream_services().core.FALLBACK_MAX_ATTEMPTS_5XX
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            return upstream_services().core.FALLBACK_MAX_ATTEMPTS_4XX
        return upstream_services().core.FALLBACK_MAX_ATTEMPTS
    if isinstance(exc, upstream_services().core.RETRY_HTTPX_EXC):
        return upstream_services().core.FALLBACK_MAX_ATTEMPTS_5XX
    return upstream_services().core.FALLBACK_MAX_ATTEMPTS


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Read and cap a retry-after hint from an upstream error payload."""
    if not isinstance(exc, upstream_services().infrastructure.UpstreamError):
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
            return min(seconds, upstream_services().core.FALLBACK_429_MAX_WAIT_S)
    return None


def _merge_fallback_errors(
    errors: list[BaseException],
    *,
    error_code: str,
    message: str,
) -> Any:
    if not errors:
        return upstream_services().infrastructure.UpstreamError(
            message,
            status_code=200,
            error_code=error_code,
        )
    if any(upstream_services().retry.mentions_safety_policy(exc) for exc in errors):
        payload: dict[str, Any] = {
            "path": "responses",
            "errors": [
                upstream_services().retry.summarize_exception(exc) for exc in errors
            ],
            "wrapped_error_code": error_code,
        }
        merged = upstream_services().infrastructure.UpstreamError(
            "request blocked by upstream safety policy",
            status_code=200,
            error_code=upstream_services().infrastructure.EC.MODERATION_BLOCKED.value,
            payload=payload,
        )
        if len(errors) > 1:
            merged.__cause__ = BaseExceptionGroup(message, errors)
        else:
            merged.__cause__ = errors[0]
        return merged
    first = errors[0]
    status_code = 200
    merged_payload: dict[str, Any] = {}
    if isinstance(first, upstream_services().infrastructure.UpstreamError):
        status_code = first.status_code or 200
        merged_payload.update(first.payload)
    merged_payload.setdefault("path", "responses")
    merged_payload["errors"] = [
        upstream_services().retry.summarize_exception(exc) for exc in errors
    ]
    merged = upstream_services().infrastructure.UpstreamError(
        message,
        status_code=status_code,
        error_code=error_code,
        payload=merged_payload,
    )
    if len(errors) > 1:
        merged.__cause__ = BaseExceptionGroup(message, errors)
    else:
        merged.__cause__ = first
    return merged


def _provider_error_details(
    providers: list[Any],
    errors: list[BaseException],
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for provider, exc in zip(providers, errors, strict=False):
        details.append(
            {
                "provider": getattr(provider, "name", None),
                **upstream_services().retry.summarize_exception(exc),
            }
        )
    return details


def _mentions_safety_policy(exc: BaseException) -> bool:
    """Detect safety blocks hidden inside fallback/provider wrapper errors."""
    text = str(exc).lower()
    if any(
        marker in text
        for marker in upstream_services().core.SAFETY_POLICY_ERROR_MARKERS
    ):
        return True
    if (
        isinstance(exc, upstream_services().infrastructure.UpstreamError)
        and exc.payload
    ):
        try:
            payload_text = json.dumps(exc.payload, ensure_ascii=False).lower()
        except Exception:  # noqa: BLE001
            payload_text = repr(exc.payload).lower()
        if any(
            marker in payload_text
            for marker in upstream_services().core.SAFETY_POLICY_ERROR_MARKERS
        ):
            return True
    nested = getattr(exc, "exceptions", None)
    if nested and any(
        isinstance(child, BaseException)
        and upstream_services().retry.mentions_safety_policy(child)
        for child in nested
    ):
        return True
    cause = getattr(exc, "__cause__", None)
    if isinstance(
        cause, BaseException
    ) and upstream_services().retry.mentions_safety_policy(cause):
        return True
    context = getattr(exc, "__context__", None)
    return isinstance(
        context,
        BaseException,
    ) and upstream_services().retry.mentions_safety_policy(context)


def _should_continue_image_provider_failover(
    exc: BaseException,
    *,
    retriable: bool,
) -> bool:
    """True when another image provider may handle the same request."""
    if upstream_services().providers.is_quota_accounting_unavailable(exc):
        return False
    if retriable:
        return True
    if (
        isinstance(exc, upstream_services().infrastructure.UpstreamError)
        and exc.error_code
        in upstream_services().core.IMAGE_PROVIDER_FAILOVER_ERROR_CODES
    ):
        return True
    return upstream_services().retry.mentions_safety_policy(exc)


def _merge_image_path_errors(
    *,
    action: str,
    primary_path: str,
    primary_error: BaseException,
    fallback_path: str,
    fallback_error: BaseException,
) -> Any:
    status_code = 502
    payload: dict[str, Any] = {}
    if isinstance(primary_error, upstream_services().infrastructure.UpstreamError):
        status_code = primary_error.status_code or status_code
        payload.update(primary_error.payload)
    elif isinstance(fallback_error, upstream_services().infrastructure.UpstreamError):
        status_code = fallback_error.status_code or status_code
        payload.update(fallback_error.payload)
    payload.setdefault("path", primary_path)
    payload["primary_path"] = primary_path
    payload["fallback_path"] = fallback_path
    payload["path_errors"] = [
        {
            "path": primary_path,
            **upstream_services().retry.summarize_exception(primary_error),
        },
        {
            "path": fallback_path,
            **upstream_services().retry.summarize_exception(fallback_error),
        },
    ]
    message = f"{action} image paths failed: {primary_path}, {fallback_path}"
    merged = upstream_services().infrastructure.UpstreamError(
        message,
        status_code=status_code,
        error_code=upstream_services().infrastructure.EC.PROVIDER_EXHAUSTED.value,
        payload=payload,
    )
    merged.__cause__ = BaseExceptionGroup(
        message,
        [primary_error, fallback_error],
    )
    return merged


async def _responses_image_stream_with_retry(
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
    base_url_override: str | None = None,
    api_key_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    pinned_target_override: Any | None = None,
    user_id: str | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    """Retry the Responses image stream with error-specific budgets."""
    errors: list[BaseException] = []
    attempt = 0
    hard_cap = max(
        upstream_services().core.FALLBACK_MAX_ATTEMPTS,
        upstream_services().core.FALLBACK_MAX_ATTEMPTS_5XX,
        upstream_services().core.FALLBACK_MAX_ATTEMPTS_429,
        upstream_services().core.FALLBACK_MAX_ATTEMPTS_4XX,
    )
    outer_attempt = upstream_services().core.image_retry_attempt_ctx.get()
    while attempt < hard_cap:
        effective_attempt = outer_attempt + attempt
        cv_token = upstream_services().core.image_retry_attempt_ctx.set(
            effective_attempt
        )
        try:
            if before_attempt is not None:
                await before_attempt(attempt + 1)
            kwargs: dict[str, Any] = {
                "prompt": prompt,
                "size": size,
                "action": action,
                "images": images,
                "quality": quality,
                "model": model,
                "progress_callback": progress_callback,
                "use_httpx": use_httpx,
                "base_url_override": base_url_override,
                "api_key_override": api_key_override,
            }
            if proxy_override is not None:
                kwargs["proxy_override"] = proxy_override
            if pinned_target_override is not None:
                kwargs["pinned_target_override"] = pinned_target_override
            if output_format is not None:
                kwargs["output_format"] = output_format
            if output_compression is not None:
                kwargs["output_compression"] = output_compression
            if background is not None:
                kwargs["background"] = background
            if moderation is not None:
                kwargs["moderation"] = moderation
            if user_id is not None:
                kwargs["user_id"] = user_id
            return await upstream_services().responses.responses_image_stream(**kwargs)
        except (
            asyncio.CancelledError,
            upstream_services().infrastructure.UpstreamCancelled,
        ):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            attempt += 1
            attempts_for_this = upstream_services().retry.max_attempts_for_exception(
                exc
            )
            if (
                attempt >= attempts_for_this
                or not upstream_services().retry.is_retryable_fallback_exception(exc)
            ):
                raise upstream_services().retry.merge_fallback_errors(
                    errors,
                    error_code=(
                        exc.error_code
                        if isinstance(
                            exc, upstream_services().infrastructure.UpstreamError
                        )
                        and exc.error_code
                        else "responses_fallback_failed"
                    ),
                    message=str(exc) or "responses fallback failed",
                ) from exc
            retry_after = upstream_services().retry.retry_after_seconds(exc)
            if retry_after is not None:
                backoff = retry_after
            elif (
                isinstance(
                    exc,
                    upstream_services().infrastructure.UpstreamError,
                )
                and exc.status_code == 429
            ):
                backoff = min(
                    upstream_services().core.FALLBACK_429_DEFAULT_WAIT_S,
                    upstream_services().core.FALLBACK_429_MAX_WAIT_S,
                )
            else:
                backoff = upstream_services().retry.fallback_retry_backoff_seconds(
                    attempt
                )
            upstream_services().infrastructure.logger.warning(
                "responses fallback retrying action=%s size=%s attempt=%d/%d "
                "backoff=%.1fs err=%r",
                action,
                size,
                attempt + 1,
                attempts_for_this,
                backoff,
                exc,
            )
            await asyncio.sleep(backoff)
        finally:
            upstream_services().core.image_retry_attempt_ctx.reset(cv_token)
    raise upstream_services().retry.merge_fallback_errors(
        errors,
        error_code=upstream_services().infrastructure.EC.RESPONSES_FALLBACK_FAILED.value,
        message="responses fallback exhausted retry budget",
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
