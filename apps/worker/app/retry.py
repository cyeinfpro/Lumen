"""重试规则（DESIGN §6.4 严格对齐网关观察）。

Retriable（值得 backoff 重试）：
- 5xx / 连接/读超时 / 网络错
- `rate_limit_error` 或消息含 `Concurrency limit exceeded`
- `tool_choice=required` 被降级成纯文本（output 里没有 image_generation_call）
- SSE 断流且未收到任何 partial_image

Terminal（直接失败，用户手动 retry）：
- 400 + `invalid_value`（典型：`Requested resolution exceeds the current pixel budget`）
- 上传图超限 / 参数错 / 认证错（401/403）

规则封装成 pure function，方便在 generation.py / completion.py 共用。
"""

from __future__ import annotations

from dataclasses import dataclass

from lumen_core.constants import GenerationErrorCode as EC


# 安全审核类 error_code 子集——单独暴露给调用方做"换号再试"决策；
# is_retriable 仍把它们视为 terminal（避免单 provider 时浪费配额）。
_MODERATION_ERROR_CODES: frozenset[str] = frozenset(
    {
        EC.MODERATION_BLOCKED.value,
        EC.CONTENT_POLICY_VIOLATION.value,
        EC.SAFETY_VIOLATION.value,
    }
)

_MODERATION_MESSAGE_MARKERS: tuple[str, ...] = (
    "moderation_blocked",
    "safety_violation",
    "safety_violations",
    "content_policy_violation",
    "content policy",
    "safety policy",
    "safety_policy",
    "blocked by upstream",
)

# 网关返回的 retriable / terminal 错误码枚举（guide + test summary）
# 字符串值集中在 lumen_core.constants.GenerationErrorCode 中,retry 仅引用枚举值；
# 上游若透传 enum 之外的新 code,is_retriable 会落到 http_status / 关键词 / 兜底分支。
_TERMINAL_ERROR_CODES: frozenset[str] = frozenset(
    {
        EC.INVALID_VALUE.value,
        EC.INVALID_REQUEST_ERROR.value,
        EC.INVALID_PARAM.value,
        EC.IMAGE_GENERATION_USER_ERROR.value,
        EC.AUTHENTICATION_ERROR.value,
        EC.PERMISSION_ERROR.value,
        EC.UNAUTHORIZED.value,
        # 输入类（本地预检抛出）——重试永远不会好转
        EC.BAD_REFERENCE_IMAGE.value,
        EC.REFERENCE_MISSING.value,
        EC.MISSING_INPUT_IMAGES.value,
        EC.REFERENCE_IMAGE_TOO_LARGE.value,
        # 安全审核拒绝——OpenAI 明确拒图/拒 prompt,重试也是拒。
        # 调用方仍可通过 is_moderation_block + provider 上下文做"换号再试"。
        *_MODERATION_ERROR_CODES,
    }
)

_RETRIABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        EC.RATE_LIMIT_ERROR.value,
        EC.RATE_LIMIT_EXCEEDED.value,
        EC.TIMEOUT.value,
        EC.UPSTREAM_TIMEOUT.value,
        EC.SERVER_ERROR.value,
        EC.INTERNAL_ERROR.value,
        EC.BAD_GATEWAY.value,
        EC.SERVICE_UNAVAILABLE.value,
        EC.UPSTREAM_ERROR.value,  # 未分类默认可重试（会被 HTTP 状态进一步精化）
        EC.UPSTREAM_ERROR_EVENT.value,  # sub2api SSE 包装的 error 事件
        EC.TEXT_STREAM_INTERRUPTED.value,  # chat SSE 已输出文本后断链，任务层可重放
        EC.RESPONSE_FAILED.value,  # SSE response.failed 在 HTTP 200 流里出现
        # provider/fallback 层包装错误：单 provider 也应让 task 层重试，而不是直接 terminal。
        # 来自 upstream.py _merge_fallback_errors 的 error_code。
        EC.ALL_PROVIDERS_FAILED.value,
        EC.RESPONSES_FALLBACK_FAILED.value,
        EC.FALLBACK_LANES_FAILED.value,
        EC.IMAGE_GENERATION_FAILED.value,  # OpenAI image_generation tool 显式 failed
        # 账号级调度产生的"暂时不可用"：所有账号都在 cooldown / quota 用完，
        # 让 task 层 backoff 后再来一轮。
        EC.ALL_ACCOUNTS_FAILED.value,
        EC.ACCOUNT_IMAGE_QUOTA_EXCEEDED.value,
        EC.QUOTA_ACCOUNTING_UNAVAILABLE.value,
        # 本地 sem 排队等待超时（4K/大图并发 = 1，前一张还没跑完）。retriable 让
        # arq 退避后重新入队；与上游 rate_limit_error 区分开避免误读监控。
        EC.LOCAL_QUEUE_FULL.value,
        EC.DISK_FULL.value,
        # P2 worker 内 failover 全部失败后的兜底——task 层退避再来一次。
        EC.PROVIDER_EXHAUSTED.value,
        # direct/image-job provider wrappers are only produced after the inner
        # path saw retriable image failures. Keep outer provider dispatch moving
        # even when the wrapped failure was a 200/no_image.
        EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        # direct edit / image job download wrappers on network faults.
        EC.DIRECT_IMAGE_REQUEST_FAILED.value,
    }
)

_REFERENCE_DOWNLOAD_MARKERS = (
    "timeout while downloading",
    "failed to download",
    "could not download",
)
_RATE_LIMIT_MARKERS = (
    "rate_limit_exceeded",
    "rate_limit_error",
    "rate_limited",
    "rate_limit:",
    "too many requests",
    "too_many_requests",
    "concurrency limit exceeded",
    "concurrency_limit_exceeded",
    "concurrency_limit_error",
)
_UPSTREAM_WRAPPED_FAILURE_MARKERS = (
    "all upstream providers failed",
    "upstream providers failed",
    "all 1 upstream providers failed",
    "responses fallback failed",
    "fallback lanes",
    "response.failed",
    "no upstream account",
)
_TERMINAL_HTTP_STATUSES = {400, 401, 403, 404, 422}
_EXPLICIT_RATE_LIMIT_CODES = {
    EC.RATE_LIMIT_ERROR.value,
    EC.RATE_LIMIT_EXCEEDED.value,
}


@dataclass(frozen=True)
class RetryDecision:
    retriable: bool
    reason: str


def _matches(message: str, markers: tuple[str, ...]) -> bool:
    return any(marker in message for marker in markers)


def _terminal_decision(
    err_code: str | None,
    http_status: int | None,
    message: str,
) -> RetryDecision | None:
    if err_code in _TERMINAL_ERROR_CODES:
        return RetryDecision(False, f"terminal error_code={err_code}")
    if _matches(message, _MODERATION_MESSAGE_MARKERS):
        return RetryDecision(False, "terminal safety_policy")
    if "pixel budget" in message or "invalid size" in message:
        return RetryDecision(False, "terminal pixel_budget")
    if http_status not in _TERMINAL_HTTP_STATUSES:
        return None
    if err_code in _EXPLICIT_RATE_LIMIT_CODES or _matches(
        message,
        _RATE_LIMIT_MARKERS,
    ):
        return None
    return RetryDecision(False, f"terminal http={http_status}")


def _transient_decision(
    err_code: str | None,
    http_status: int | None,
    message: str,
) -> RetryDecision | None:
    if (
        "concurrency limit exceeded" in message
        or "rate limit" in message
        or "rate_limit" in message
        or "too many requests" in message
        or http_status == 429
    ):
        return RetryDecision(True, "rate_limited")
    if _matches(message, _UPSTREAM_WRAPPED_FAILURE_MARKERS):
        return RetryDecision(True, "retriable upstream_wrapped_failure")
    if err_code in _RETRIABLE_ERROR_CODES:
        return RetryDecision(True, f"retriable error_code={err_code}")
    if http_status is None:
        return RetryDecision(True, "retriable network_error")
    if 500 <= http_status < 600:
        return RetryDecision(True, f"retriable http={http_status}")
    if err_code in (EC.NO_IMAGE_RETURNED.value, EC.TOOL_CHOICE_DOWNGRADE.value):
        return RetryDecision(True, f"retriable {err_code}")
    return None


def _stream_failure_decision(
    err_code: str | None,
    http_status: int | None,
    has_partial: bool,
) -> RetryDecision | None:
    if err_code == EC.STREAM_INTERRUPTED.value:
        if not has_partial:
            return RetryDecision(True, "retriable stream_interrupted no_partial")
        if http_status is None:
            return RetryDecision(True, "retriable stream_interrupted text_partial")
        return RetryDecision(False, "terminal stream_interrupted with_partial")
    if err_code == EC.SSE_CURL_FAILED.value:
        if not has_partial:
            return RetryDecision(True, "retriable sse_curl_failed no_partial")
        return RetryDecision(False, "terminal sse_curl_failed with_partial")
    if err_code == EC.BAD_RESPONSE.value:
        if not has_partial:
            return RetryDecision(True, "retriable bad_response no_partial")
        return RetryDecision(False, "terminal bad_response with_partial")
    return None


def is_retriable(
    err_code: str | None,
    http_status: int | None,
    has_partial: bool = False,
    *,
    error_message: str | None = None,
) -> RetryDecision:
    """判断一次上游失败是否值得重试。

    Args:
        err_code: 网关返回的 `error.code` / `error.type`；也可以是我们自己标的
                  内部错误码（例如 `no_image_returned`、`sha_echo`）。
        http_status: HTTP 状态码；None 表示网络错（比如 `httpx.ConnectError`）
        has_partial: 是否已经收到过至少一个 partial 事件——generation stream 模式下用
        error_message: 原始错误消息，便于抓关键词（e.g. "Concurrency limit exceeded"）
    """
    msg = (error_message or "").lower()

    # 上游 reference 下载超时通常会被包装成 invalid_value，但本质是网络/供应链抖动。
    # 命中后应换 provider / endpoint 重试，而不是把用户输入判成终态错误。
    if _matches(msg, _REFERENCE_DOWNLOAD_MARKERS):
        return RetryDecision(True, "retriable upstream_reference_download_timeout")

    # 1) terminal 优先于 retriable（pixel budget / 上传图超限 / 参数错）
    terminal = _terminal_decision(err_code, http_status, msg)
    if terminal is not None:
        return terminal

    # 2) retriable signals — 关键词优先，其次 status_code
    transient = _transient_decision(err_code, http_status, msg)
    if transient is not None:
        return transient

    # 3) 流中断按是否已经产生 partial 区分，避免重复消耗图片配额。
    stream_failure = _stream_failure_decision(err_code, http_status, has_partial)
    if stream_failure is not None:
        return stream_failure

    # 4) 兜底：未识别 → 不重试（保守，避免烧钱 / 烧配额）
    return RetryDecision(False, f"unknown err_code={err_code} http={http_status}")


def is_moderation_block(
    err_code: str | None,
    error_message: str | None = None,
) -> bool:
    """True iff err_code/message indicates an upstream moderation/safety block.

    is_retriable 仍把 moderation 视为 terminal——单 provider 时重试纯浪费配额。
    调用方（task 层）可以再叠加 "还有未试 provider" 的上下文，决定是否换号再试。
    """
    if err_code in _MODERATION_ERROR_CODES:
        return True
    msg = (error_message or "").lower()
    return any(marker in msg for marker in _MODERATION_MESSAGE_MARKERS)


__all__ = ["RetryDecision", "is_moderation_block", "is_retriable"]
