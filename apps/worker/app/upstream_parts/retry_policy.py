"""Image retry, error merging, and provider failover policy."""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any, Awaitable, Callable

from lumen_core.providers import ProviderProxyDefinition

from .transport import ImageProgressCallback

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve compatibility dependencies at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


def _summarize_exception(exc: BaseException) -> dict[str, Any]:
    facade = _facade()
    item: dict[str, Any] = {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }
    if isinstance(exc, facade.UpstreamError):
        item["status_code"] = exc.status_code
        item["error_code"] = exc.error_code
        if exc.payload:
            item["payload"] = exc.payload
    return item


def _truncate_lane_summary(lane: str, exc: BaseException) -> dict[str, Any]:
    facade = _facade()
    out: dict[str, Any] = {
        "lane": lane,
        "type": type(exc).__name__,
        "message": str(exc)[:200],
    }
    if isinstance(exc, facade.UpstreamError):
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
    facade = _facade()
    if isinstance(exc, facade.UpstreamError):
        if exc.status_code in facade._RETRY_STATUS:
            return True
        if exc.status_code == 429:
            return True
        return exc.error_code in facade._FALLBACK_RETRY_ERROR_CODES
    return isinstance(exc, facade._RETRY_HTTPX_EXC)


def _fallback_retry_backoff_seconds(attempt: int) -> float:
    facade = _facade()
    return min(
        facade._FALLBACK_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)),
        facade._FALLBACK_RETRY_BACKOFF_MAX_S,
    )


def _max_attempts_for_exception(exc: BaseException) -> int:
    """Return the fallback retry budget for the current error shape."""
    facade = _facade()
    if isinstance(exc, facade.UpstreamError):
        if exc.status_code == 429:
            return facade._FALLBACK_MAX_ATTEMPTS_429
        if exc.status_code is not None and 500 <= exc.status_code < 600:
            return facade._FALLBACK_MAX_ATTEMPTS_5XX
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            return facade._FALLBACK_MAX_ATTEMPTS_4XX
        return facade._FALLBACK_MAX_ATTEMPTS
    if isinstance(exc, facade._RETRY_HTTPX_EXC):
        return facade._FALLBACK_MAX_ATTEMPTS_5XX
    return facade._FALLBACK_MAX_ATTEMPTS


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Read and cap a retry-after hint from an upstream error payload."""
    facade = _facade()
    if not isinstance(exc, facade.UpstreamError):
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
            return min(seconds, facade._FALLBACK_429_MAX_WAIT_S)
    return None


def _merge_fallback_errors(
    errors: list[BaseException],
    *,
    error_code: str,
    message: str,
) -> Any:
    facade = _facade()
    if not errors:
        return facade.UpstreamError(
            message,
            status_code=200,
            error_code=error_code,
        )
    if any(facade._mentions_safety_policy(exc) for exc in errors):
        payload: dict[str, Any] = {
            "path": "responses",
            "errors": [facade._summarize_exception(exc) for exc in errors],
            "wrapped_error_code": error_code,
        }
        merged = facade.UpstreamError(
            "request blocked by upstream safety policy",
            status_code=200,
            error_code=facade.EC.MODERATION_BLOCKED.value,
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
    if isinstance(first, facade.UpstreamError):
        status_code = first.status_code or 200
        merged_payload.update(first.payload)
    merged_payload.setdefault("path", "responses")
    merged_payload["errors"] = [facade._summarize_exception(exc) for exc in errors]
    merged = facade.UpstreamError(
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
    facade = _facade()
    details: list[dict[str, Any]] = []
    for provider, exc in zip(providers, errors, strict=False):
        details.append(
            {
                "provider": getattr(provider, "name", None),
                **facade._summarize_exception(exc),
            }
        )
    return details


def _mentions_safety_policy(exc: BaseException) -> bool:
    """Detect safety blocks hidden inside fallback/provider wrapper errors."""
    facade = _facade()
    text = str(exc).lower()
    if any(marker in text for marker in facade._SAFETY_POLICY_ERROR_MARKERS):
        return True
    if isinstance(exc, facade.UpstreamError) and exc.payload:
        try:
            payload_text = json.dumps(exc.payload, ensure_ascii=False).lower()
        except Exception:  # noqa: BLE001
            payload_text = repr(exc.payload).lower()
        if any(
            marker in payload_text for marker in facade._SAFETY_POLICY_ERROR_MARKERS
        ):
            return True
    nested = getattr(exc, "exceptions", None)
    if nested and any(
        isinstance(child, BaseException) and facade._mentions_safety_policy(child)
        for child in nested
    ):
        return True
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException) and facade._mentions_safety_policy(cause):
        return True
    context = getattr(exc, "__context__", None)
    return isinstance(
        context,
        BaseException,
    ) and facade._mentions_safety_policy(context)


def _should_continue_image_provider_failover(
    exc: BaseException,
    *,
    retriable: bool,
) -> bool:
    """True when another image provider may handle the same request."""
    facade = _facade()
    if facade._is_quota_accounting_unavailable(exc):
        return False
    if retriable:
        return True
    if (
        isinstance(exc, facade.UpstreamError)
        and exc.error_code in facade._IMAGE_PROVIDER_FAILOVER_ERROR_CODES
    ):
        return True
    return facade._mentions_safety_policy(exc)


def _merge_image_path_errors(
    *,
    action: str,
    primary_path: str,
    primary_error: BaseException,
    fallback_path: str,
    fallback_error: BaseException,
) -> Any:
    facade = _facade()
    status_code = 502
    payload: dict[str, Any] = {}
    if isinstance(primary_error, facade.UpstreamError):
        status_code = primary_error.status_code or status_code
        payload.update(primary_error.payload)
    elif isinstance(fallback_error, facade.UpstreamError):
        status_code = fallback_error.status_code or status_code
        payload.update(fallback_error.payload)
    payload.setdefault("path", primary_path)
    payload["primary_path"] = primary_path
    payload["fallback_path"] = fallback_path
    payload["path_errors"] = [
        {
            "path": primary_path,
            **facade._summarize_exception(primary_error),
        },
        {
            "path": fallback_path,
            **facade._summarize_exception(fallback_error),
        },
    ]
    message = f"{action} image paths failed: {primary_path}, {fallback_path}"
    merged = facade.UpstreamError(
        message,
        status_code=status_code,
        error_code=facade.EC.PROVIDER_EXHAUSTED.value,
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
    user_id: str | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    """Retry the Responses image stream with error-specific budgets."""
    facade = _facade()
    errors: list[BaseException] = []
    attempt = 0
    hard_cap = max(
        facade._FALLBACK_MAX_ATTEMPTS,
        facade._FALLBACK_MAX_ATTEMPTS_5XX,
        facade._FALLBACK_MAX_ATTEMPTS_429,
        facade._FALLBACK_MAX_ATTEMPTS_4XX,
    )
    outer_attempt = facade._image_retry_attempt_ctx.get()
    while attempt < hard_cap:
        effective_attempt = outer_attempt + attempt
        cv_token = facade._image_retry_attempt_ctx.set(effective_attempt)
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
            return await facade._responses_image_stream(**kwargs)
        except (asyncio.CancelledError, facade.UpstreamCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            attempt += 1
            attempts_for_this = facade._max_attempts_for_exception(exc)
            if (
                attempt >= attempts_for_this
                or not facade._is_retryable_fallback_exception(exc)
            ):
                raise facade._merge_fallback_errors(
                    errors,
                    error_code=(
                        exc.error_code
                        if isinstance(exc, facade.UpstreamError) and exc.error_code
                        else "responses_fallback_failed"
                    ),
                    message=str(exc) or "responses fallback failed",
                ) from exc
            retry_after = facade._retry_after_seconds(exc)
            if retry_after is not None:
                backoff = retry_after
            elif (
                isinstance(
                    exc,
                    facade.UpstreamError,
                )
                and exc.status_code == 429
            ):
                backoff = min(
                    facade._FALLBACK_429_DEFAULT_WAIT_S,
                    facade._FALLBACK_429_MAX_WAIT_S,
                )
            else:
                backoff = facade._fallback_retry_backoff_seconds(attempt)
            facade.logger.warning(
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
            facade._image_retry_attempt_ctx.reset(cv_token)
    raise facade._merge_fallback_errors(
        errors,
        error_code=facade.EC.RESPONSES_FALLBACK_FAILED.value,
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
