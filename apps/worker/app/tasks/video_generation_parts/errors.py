"""Video upstream error classification and diagnostic helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ...video_upstream import VideoUpstreamError


SUBMIT_RETRY_DELAYS_S = (8, 24, 60)
RETRYABLE_VIDEO_ERROR_CODES = frozenset(
    {
        "capacity",
        "fetch_failed",
        "provider_error",
        "upstream_network_error",
        "upstream_not_ready",
        "upstream_timeout",
        "upstream_unknown",
    }
)

# httpx 传输异常里可以**证明请求从未送达上游**的那一类：全部发生在连接、
# 代理或连接池阶段，请求字节一个也没写进 socket，因此上游不可能计费。
#
# 与之相对的 ReadTimeout / WriteTimeout / ReadError / WriteError /
# RemoteProtocolError 都意味着请求已经（至少部分）发出去了，结果不可知，
# 必须按「上游可能已扣费」处理。asyncio.TimeoutError 是外层包装超时，
# 同样无法证明未送达，一并留在不可知那一侧。
UNDELIVERED_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)


def submit_delivery_proven_absent(exc: Exception) -> bool:
    """提交请求是否**可证明**从未送达上游。

    纯转嫁铁律是双向的：上游可能扣费时必须结算（不能让平台吸收成本），
    但能证明上游没扣费时也不能按上限扣费（那是多收用户的钱）。连接失败、
    代理失败、连接池超时属于后者——请求根本没发出去，既可以安全重试
    （不会产生第二笔上游成本），耗尽重试后也必须 release。
    """
    return isinstance(exc, UNDELIVERED_TRANSPORT_ERRORS)


def video_exception_code(exc: Exception, *, default: str) -> str:
    if isinstance(exc, VideoUpstreamError):
        value = (exc.error_code or "").strip()
        return value or default
    raw_code = getattr(exc, "error_code", None)
    if isinstance(raw_code, str) and raw_code.strip():
        return raw_code.strip()[:64]
    if isinstance(exc, httpx.TimeoutException) or isinstance(exc, asyncio.TimeoutError):
        return "upstream_timeout"
    if isinstance(exc, httpx.TransportError):
        return "upstream_network_error"
    return default


def video_exception_message(exc: Exception, *, phase: str) -> str:
    raw = str(exc).strip()
    if raw:
        return raw[:1000]
    code = video_exception_code(exc, default="provider_unavailable")
    status_code = getattr(exc, "status_code", None)
    suffix = f" status={status_code}" if status_code else ""
    return f"video upstream {phase} failed: {code} ({exc.__class__.__name__}){suffix}"[
        :1000
    ]


def is_retryable_video_exception(exc: Exception) -> bool:
    if isinstance(exc, VideoUpstreamError):
        if exc.status_code in {408, 409, 425, 429}:
            return True
        if exc.status_code is not None and exc.status_code >= 500:
            return True
        return exc.error_code in RETRYABLE_VIDEO_ERROR_CODES
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return True
    return isinstance(exc, httpx.TransportError)


def submit_outcome_unknown(exc: Exception) -> bool:
    # 能证明请求没送达的传输失败不是「结果不可知」：它是确定的失败。
    # 归进 unknown 会把任务钉在 SUBMIT_UNKNOWN，既不重试也要结算。
    if submit_delivery_proven_absent(exc):
        return False
    if isinstance(
        exc, (httpx.TimeoutException, asyncio.TimeoutError, httpx.TransportError)
    ):
        return True
    if not isinstance(exc, VideoUpstreamError):
        return False
    if exc.status_code in {408, 409}:
        return True
    if exc.status_code is not None and exc.status_code >= 500:
        return True
    return exc.error_code in {"bad_response", "upstream_unknown"}


def submit_retry_delay_s(attempt: int) -> int:
    index = max(0, min(attempt - 1, len(SUBMIT_RETRY_DELAYS_S) - 1))
    return SUBMIT_RETRY_DELAYS_S[index]


def generation_attempt(generation: Any) -> int:
    return int(getattr(generation, "attempt", 0) or 0)


def generation_diagnostics(generation: Any) -> dict[str, Any]:
    raw = getattr(generation, "diagnostics", None)
    return dict(raw or {}) if isinstance(raw, dict) else {}


def submit_failure_billable_hint(exc: Exception) -> bool | None:
    # 先判「可证明未送达」：这类异常同时也是 retryable，若先走下面的
    # retryable 分支就会被降级成 None（不可知）而按上限扣费。
    # False + submit_failed_before_upstream_cost 收据 → 决策表判 RELEASE。
    if submit_delivery_proven_absent(exc):
        return False
    if is_retryable_video_exception(exc):
        return None
    if isinstance(exc, VideoUpstreamError) and exc.error_code in {
        "bad_response",
        "upstream_unknown",
    }:
        return None
    return False


def exception_log_info(exc: Exception):
    return (type(exc), exc, exc.__traceback__)


def append_bounded_history(
    diagnostics: dict[str, Any], key: str, item: dict[str, Any], *, limit: int = 10
) -> None:
    raw = diagnostics.get(key)
    history = list(raw) if isinstance(raw, list) else []
    history.append(item)
    diagnostics[key] = history[-limit:]
