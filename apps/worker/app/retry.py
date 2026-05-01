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
    }
)


@dataclass(frozen=True)
class RetryDecision:
    retriable: bool
    reason: str


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

    # 1) terminal 优先于 retriable（pixel budget / 上传图超限 / 参数错）
    if err_code in _TERMINAL_ERROR_CODES:
        return RetryDecision(False, f"terminal error_code={err_code}")
    if any(marker in msg for marker in _MODERATION_MESSAGE_MARKERS):
        return RetryDecision(False, "terminal safety_policy")
    if "pixel budget" in msg or "invalid size" in msg:
        return RetryDecision(False, "terminal pixel_budget")

    # 2) retriable signals — 关键词优先，其次 status_code
    if (
        "concurrency limit exceeded" in msg
        or "rate limit" in msg
        or "rate_limit" in msg
        or "too many requests" in msg
    ):
        return RetryDecision(True, "rate_limited")
    if http_status == 429:
        return RetryDecision(True, "rate_limited")
    # sub2api / provider 层的失败包装：单 provider 时 `all 1 upstream providers failed`
    # 会被 generation 直接当 terminal 抛给用户；按文档建议纳入 retriable，让 task 层
    # 走 RETRY_BACKOFF_SECONDS 退避后再试一次（多半是临时账号容量问题）。
    if (
        "all upstream providers failed" in msg
        or "upstream providers failed" in msg
        or "all 1 upstream providers failed" in msg
        or "responses fallback failed" in msg
        or "fallback lanes" in msg
        or "response.failed" in msg
        or "no upstream account" in msg
    ):
        return RetryDecision(True, "retriable upstream_wrapped_failure")
    if http_status is not None and http_status in (400, 401, 403, 404, 422):
        return RetryDecision(False, f"terminal http={http_status}")
    if err_code in _RETRIABLE_ERROR_CODES:
        return RetryDecision(True, f"retriable error_code={err_code}")
    if http_status is None:
        # 网络错 / 未拿到响应
        return RetryDecision(True, "retriable network_error")
    if 500 <= http_status < 600:
        return RetryDecision(True, f"retriable http={http_status}")

    # 3) `tool_choice=required` 降级成文本（no image_generation_call in output）
    if err_code in (EC.NO_IMAGE_RETURNED.value, EC.TOOL_CHOICE_DOWNGRADE.value):
        return RetryDecision(True, f"retriable {err_code}")

    # 4) SSE 断流。图片 partial 已开始消耗生图配额，保守 terminal；文本 partial
    #    中断通常只是 HTTP/SSE 断链，允许任务层重试。当前 completion 路径用
    #    http_status=None 标记这类本地网络中断，image Responses 流会带 200。
    if err_code == EC.STREAM_INTERRUPTED.value and not has_partial:
        return RetryDecision(True, "retriable stream_interrupted no_partial")
    if err_code == EC.STREAM_INTERRUPTED.value and has_partial and http_status is None:
        return RetryDecision(True, "retriable stream_interrupted text_partial")
    if err_code == EC.STREAM_INTERRUPTED.value and has_partial:
        # 有图片 partial 仍然失败——保守不重试（图已被部分渲染，再来一遍可能拿到不同图）
        return RetryDecision(False, "terminal stream_interrupted with_partial")

    # curl 子进程级故障（rc=28 超时 / rc=7 连接失败 / 空响应等）和上面 SSE 断流对称处理：
    # 没收到 partial 就值得再试一次；已经开始渲染了就别白花配额。
    if err_code == EC.SSE_CURL_FAILED.value and not has_partial:
        return RetryDecision(True, "retriable sse_curl_failed no_partial")
    if err_code == EC.SSE_CURL_FAILED.value and has_partial:
        return RetryDecision(False, "terminal sse_curl_failed with_partial")

    # bad_response：上游 HTTP 200 但 base64 解码失败 / PNG 头损坏 / PIL 解不开。
    # 大多数是 SSE 流中途截断或上游账号偶发吐坏 chunk，retry 通常会好；
    # 与 stream_interrupted 对称按 has_partial 区分，避免已渲染图重复烧配额。
    if err_code == EC.BAD_RESPONSE.value and not has_partial:
        return RetryDecision(True, "retriable bad_response no_partial")
    if err_code == EC.BAD_RESPONSE.value and has_partial:
        return RetryDecision(False, "terminal bad_response with_partial")

    # 5) 兜底：未识别 → 不重试（保守，避免烧钱 / 烧配额）
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
