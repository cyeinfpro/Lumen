"""上游 HTTP 客户端。

生图主路径走 OpenAI Images API 风格的同步端点：
- 文生图: POST /v1/images/generations (application/json)
- 图生图: POST /v1/images/edits       (multipart/form-data, 字段名 image[])

两者响应均为 `{"data":[{"b64_json": "...", "revised_prompt": "..."}]}`，一次性返回。
如果主路径报错或返回无图，会自动降级到 `/v1/responses` + `image_generation`
工具，并用 SSE 抽取最终 `response.output_item.done.item.result`。fallback 的
`partial_image` 事件只用于轻量进度显示，不向前端发布 base64。

Completion（聊天）路径仍走 POST /v1/responses 的 SSE 流式协议，事件名在 `event:` 行、
数据在 `data:` 行里，空行切分事件；关注 `response.output_text.delta` /
`response.completed`。

本模块只负责：
- 组织 httpx 请求（连接复用、超时）
- 生图：优先同步 POST，失败后 streaming fallback，返回 (b64_image, revised_prompt?)
- completion：async generator 逐事件吐 SSE

# 前缀稳定 = prompt cache 命中前提
上游（api.example.com / gpt-5.x）支持 prompt caching，命中体现在响应
`usage.input_tokens_details.cached_tokens` 字段上。命中要求请求的"前缀"逐字节稳定：
- `instructions` 字符串不要含时间戳 / random / 用户 ID 等抖动
- `tools` 数组按工具 name 排序后再发，避免顺序抖动
- 历史 `input` 列表只追加旧轮，不要重写已发过的内容（每次重写 = cache miss = 全量计费）
改动 instructions / tools 顺序 / 历史拼装顺序前，请评估 cache miss 影响。
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import logging
import os
import re
import shutil
import tempfile  # noqa: F401 - compatibility facade for transport tests/hooks
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
import httpx
from PIL import (
    Image as PILImage,
    UnidentifiedImageError,  # noqa: F401 - late-bound reference facade
)

from lumen_core.constants import (
    DEFAULT_IMAGE_INSTRUCTIONS,
    DEFAULT_IMAGE_RESPONSES_MODEL,
    GenerationErrorCode as EC,
    UPSTREAM_MODEL,
)
from lumen_core.providers import (
    ProviderProxyDefinition,
    close_provider_proxy_tunnels,  # noqa: F401 - late-bound lifecycle facade
    endpoint_kind_allowed,  # noqa: F401 - late-bound provider facade
    parse_provider_bool,  # noqa: F401 - late-bound provider facade
    provider_supports_route,  # noqa: F401 - late-bound provider facade
    resolve_provider_proxy_url,
)
from lumen_core.url_security import (
    PublicHttpBodyTooLarge,
    download_public_http_url,
    pinned_async_http_transport,  # noqa: F401 - late-bound reference facade
    resolve_public_http_target,  # noqa: F401 - late-bound reference facade
)

from . import http_retry, provider_pool, upstream_image_requests
from .config import settings
from .runtime_settings import resolve, resolve_db
from .upstream_parts import (
    client_lifecycle as upstream_client_lifecycle,
    direct_failover as upstream_direct_failover,
    direct_images as upstream_direct_images,
    errors as upstream_errors,
    image_dispatch as upstream_image_dispatch,
    image_job_failover as upstream_image_job_failover,
    image_race as upstream_image_race,
    image_stream as upstream_image_stream,
    provider_selection as upstream_provider_selection,
    reference_images as upstream_reference_images,
    request_targets as upstream_request_targets,
    responses as upstream_responses,
    responses_client as upstream_responses_client,
    retry_policy as upstream_retry_policy,
    transport as upstream_transport,
)

_RETRY_HTTPX_EXC = http_retry.RETRY_HTTPX_EXC
_RETRY_STATUS = http_retry.RETRY_STATUS
_parse_retry_after_seconds = http_retry.parse_retry_after_seconds
_post_with_retry = http_retry.post_with_retry

# Prometheus 埋点：metrics_upstream 在共享 packages/core 下；worker 与 api 都通过
# lumen_core import 同一份实现，避免按 cwd 注入 sys.path 的脆弱依赖。极端情况下
# （如 lumen_core 不可用）降级为 no-op，让 worker 仍可启动。
try:
    from lumen_core.metrics_upstream import (
        record_upstream_duration,
        record_upstream_request,
        record_upstream_tokens,
        record_used_percent,
    )
except Exception:  # noqa: BLE001

    def record_upstream_tokens(kind: str, n: int) -> None:  # type: ignore[no-redef]
        return None

    def record_upstream_duration(seconds: float, endpoint: str) -> None:  # type: ignore[no-redef]
        return None

    def record_upstream_request(status_code: int, endpoint: str) -> None:  # type: ignore[no-redef]
        return None

    def record_used_percent(p: int) -> None:  # type: ignore[no-redef]
        return None


logger = logging.getLogger(__name__)


# ---- 上游标识 / trace ----


def _resolve_lumen_version() -> str:
    """resolve "lumen-prod-{ver}" originator 用的版本号。

    优先级：
    1. env LUMEN_VERSION（部署脚本灌入）
    2. lumen_core.__version__（如有）
    3. fallback "unknown"
    """
    raw = os.environ.get("LUMEN_VERSION", "").strip()
    if raw:
        return raw
    try:
        from lumen_core import __version__ as _v  # type: ignore[attr-defined]

        if isinstance(_v, str) and _v:
            return _v
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


_LUMEN_ORIGINATOR = f"lumen-prod-{_resolve_lumen_version()}"


_image_trace_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lumen_image_trace_id",
    default=None,
)


@dataclass
class _ImageQuotaScope:
    task_id: str
    attempt_epoch: int
    logical_call_index: int = 0


@dataclass
class _ImageQuotaReservation:
    provider_name: str
    member: str
    reserved_at: float
    state: str = "reserved"


_image_quota_scope_ctx: contextvars.ContextVar[_ImageQuotaScope | None] = (
    contextvars.ContextVar("lumen_image_quota_scope", default=None)
)
_image_quota_reservation_ctx: contextvars.ContextVar[_ImageQuotaReservation | None] = (
    contextvars.ContextVar("lumen_image_quota_reservation", default=None)
)


def push_image_trace_id(trace_id: str | None) -> contextvars.Token[str | None] | None:
    """Bind a generation-level trace id to downstream image HTTP calls."""
    if not isinstance(trace_id, str) or not trace_id:
        return None
    return _image_trace_id_ctx.set(trace_id)


def pop_image_trace_id(token: contextvars.Token[str | None] | None) -> None:
    if token is None:
        return
    _image_trace_id_ctx.reset(token)


def push_image_quota_context(
    task_id: str,
    attempt_epoch: int,
) -> contextvars.Token[_ImageQuotaScope | None]:
    return _image_quota_scope_ctx.set(
        _ImageQuotaScope(
            task_id=str(task_id),
            attempt_epoch=max(1, int(attempt_epoch or 1)),
        )
    )


def pop_image_quota_context(
    token: contextvars.Token[_ImageQuotaScope | None],
) -> None:
    _image_quota_scope_ctx.reset(token)


def _next_image_quota_member(provider_name: str, route: str) -> str:
    scope = _image_quota_scope_ctx.get()
    if scope is None:
        trace_id = _image_trace_id_ctx.get() or uuid.uuid4().hex
        return f"{trace_id}:1:{provider_name}:{route}"
    scope.logical_call_index += 1
    return (
        f"{scope.task_id}:{scope.attempt_epoch}:{scope.logical_call_index}:"
        f"{provider_name}:{route}"
    )


def _generate_trace_id() -> str:
    """每次上游 HTTP 调用生成一个 x-trace-id，方便和上游下发的 x-request-id 对账。"""
    return _image_trace_id_ctx.get() or uuid.uuid4().hex


# ---- 已知 SSE output[].type 白名单 ----
# 解析 SSE 帧或 compact JSON 时未知 type 仅 warning + 跳过，不抛 KeyError 让整条流挂掉。
_KNOWN_OUTPUT_ITEM_TYPES = frozenset(
    {
        "message",
        "reasoning",
        "function_call",
        "compaction_summary",
        "tool_call",
        "web_search_call",
        "file_search_call",
        "code_interpreter_call",
        "image_generation_call",  # /v1/responses + image_generation 工具的 item 类型
    }
)

# ---- Responses SSE 终止事件白名单 ----
# 兼容网关常见返回形态：除了官方 `response.completed`，部分实现会用 `response.done`
# 作为成功终态；失败时则可能用 `response.failed` / `response.incomplete` / `error`。
# 旧版只认 `response.completed` 会在两种场景误判：
# 1) 上游已经 terminal，但 Lumen 还在等 EOF → 最终报 missing completed。
# 2) 上游已给出 failed/incomplete details，但最终错误只表现为 drained without image，
#    丢掉了上游真实原因（rate_limit / moderation / server_error）。
_RESPONSES_SUCCESS_TERMINAL_EVENTS = frozenset({"response.completed", "response.done"})
_RESPONSES_ERROR_TERMINAL_EVENTS = frozenset(
    {"response.failed", "response.incomplete", "error"}
)
_RESPONSES_TERMINAL_EVENTS = (
    _RESPONSES_SUCCESS_TERMINAL_EVENTS | _RESPONSES_ERROR_TERMINAL_EVENTS
)


def _is_responses_success_terminal(event_type: Any) -> bool:
    return (
        isinstance(event_type, str) and event_type in _RESPONSES_SUCCESS_TERMINAL_EVENTS
    )


def _is_responses_error_terminal(event_type: Any) -> bool:
    return (
        isinstance(event_type, str) and event_type in _RESPONSES_ERROR_TERMINAL_EVENTS
    )


# Sentinel event：iterator 在 200 但 Content-Type 不是 text/event-stream 时 yield，
# 由 _responses_image_stream 主循环识别并按 JSON 提图。命名带 ``_lumen.`` 前缀，
# 与上游事件类型不会冲突。
_JSON_PAYLOAD_SENTINEL_TYPE = "_lumen.image.json_payload"
# 单条非 SSE JSON body 上限：与单条 SSE 行字节上限一致（32 MB），覆盖 4K PNG b64
# 的 ~11MB 上限并留余量；超出直接 STREAM_TOO_LARGE，避免被巨型 body 撑爆 worker 内存。
# 注意：_SSE_MAX_LINE_BYTES 在文件后面定义，这里只能写字面值（保持两处同步）。
_NON_SSE_JSON_MAX_BYTES = 32 * 1024 * 1024

ImageProgressCallback = Callable[[dict[str, Any]], Any]

# GEN-P0-8: 上游图片规范化的严格边界
# 100 MB 原始字节 / 64M 像素 / 100 MB 编码后字节——低于任何已知合理 input.
_MAX_REFERENCE_IMAGE_BYTES = 100 * 1024 * 1024
_MAX_NORMALIZED_IMAGE_BYTES = 100 * 1024 * 1024
_MAX_REFERENCE_IMAGE_PIXELS = 64_000_000


# PIL 默认对 >89M 像素图像抛 DecompressionBombWarning 但不 raise。
# 强制上限到 64M 像素——和 _MAX_REFERENCE_IMAGE_PIXELS 对齐——这样即使 magic bytes
# 绕过 size_bytes 检查（比如 16x 压缩的 PNG），PIL.Image.open 也会直接 DecompressionBombError。
# 必须设为 int，PIL 把 None 当作"无限制"。全进程生效，所以用 max(...) 保证不回退他处更大的值。
def _configure_pil_max_image_pixels() -> None:
    try:
        _pil_current = PILImage.MAX_IMAGE_PIXELS or 0
        if _pil_current == 0 or _pil_current > _MAX_REFERENCE_IMAGE_PIXELS:
            PILImage.MAX_IMAGE_PIXELS = _MAX_REFERENCE_IMAGE_PIXELS
    except Exception:  # noqa: BLE001
        logger.warning(
            "failed to configure PIL MAX_IMAGE_PIXELS=%d",
            _MAX_REFERENCE_IMAGE_PIXELS,
            exc_info=True,
        )


_configure_pil_max_image_pixels()

_SSE_MAX_LINES = 100_000
_SSE_MAX_BYTES = 80 * 1024 * 1024
# partial_image / final image 的 base64 data 会整行塞在一条 SSE `data:` 里。4K PNG
# 压缩后 3–8MB，base64 再 +33% 可以到 11MB 以上——10MB 上限会把 4K 主动打挂
# （"sse exceeded max line bytes"）。32MB 能覆盖 4K 理论上限 + 缓冲，整体 80MB
# 总 budget 不变，DoS 风险没实质放大。
_SSE_MAX_LINE_BYTES = 32 * 1024 * 1024

_FALLBACK_MAX_ATTEMPTS = 2
# GEN-P1-9: fallback 层重试预算按错误码 / HTTP 状态分类动态选择，避免 5xx
# 一次就放弃 / 4xx 还在烧配额。_FALLBACK_MAX_ATTEMPTS 仍是兜底硬上限。
_FALLBACK_MAX_ATTEMPTS_5XX = 3
_FALLBACK_MAX_ATTEMPTS_429 = 5
_FALLBACK_MAX_ATTEMPTS_4XX = 1  # 401/403/404/422 等终态错误，重试无意义
# GEN-P0-9: fallback 层重试指数退避。base*2^attempt，最大 4s 避免叠加 race*lane 预算爆炸。
_FALLBACK_RETRY_BACKOFF_BASE_S = 1.0
_FALLBACK_RETRY_BACKOFF_MAX_S = 4.0
# 429 没有 retry-after 头时按这个保底等；多数上游建议 5–15s。
_FALLBACK_429_DEFAULT_WAIT_S = 10.0
_FALLBACK_429_MAX_WAIT_S = 30.0
_FALLBACK_RETRY_ERROR_CODES = {
    "no_image_returned",
    "race_no_result",
    "stream_interrupted",
    "sse_curl_failed",
    "stream_too_large",
}
_RACE_CANCEL_WAIT_S = 5.0

# reference URL cache：每个 user 一份 hash + LRU zset，TTL 30min，容量 10。
_REFERENCE_CACHE_TTL_S = 30 * 60
_REFERENCE_CACHE_MAX_ENTRIES = 10
_REFERENCE_CACHE_HEAD_TIMEOUT_S = 5.0
_REFERENCE_CACHE_KEY_PREFIX = "lumen:ref_cache:"
_REFERENCE_CACHE_LRU_SUFFIX = ":lru"
_REFERENCE_PUSH_TIMEOUT_S = 30.0


# 单例 client——进程内复用连接池
_client: httpx.AsyncClient | None = None
_client_timeout_config: "_TimeoutConfig | None" = None
_PROXIED_CLIENT_CACHE_MAX = 32
_PROXIED_CLIENT_CLOSE_DELAY_SECONDS = 30.0
_PROXIED_CLIENT_IDLE_CLOSE_TIMEOUT_SECONDS = 30 * 60.0
_proxied_clients: OrderedDict[tuple["_TimeoutConfig", str], httpx.AsyncClient] = (
    OrderedDict()
)
# 专供 /v1/images/* 使用的 client：不设默认 content-type，让 httpx 根据 files
# 自动生成 multipart boundary；JSON 请求则显式传 json= 由 httpx 自己设 header。
_images_client: httpx.AsyncClient | None = None
_images_client_timeout_config: "_TimeoutConfig | None" = None
_proxied_images_clients: OrderedDict[
    tuple["_TimeoutConfig", str], httpx.AsyncClient
] = OrderedDict()
_client_lock = asyncio.Lock()
_images_client_lock = asyncio.Lock()
_retired_client_close_tasks: set[asyncio.Task[None]] = set()
_retired_clients: set[httpx.AsyncClient] = set()

_TEXT_STREAM_INTERRUPTED_ERROR_CODE = EC.TEXT_STREAM_INTERRUPTED.value

_IMAGE_PRIMARY_ROUTE_KEY = "image.primary_route"
# DEPRECATED 2026-04-28：旧键，worker resolve 在新键拿不到时回落到这里。
# 一次性迁移：UPDATE system_settings SET key='image.primary_route' WHERE key='image.text_to_image_primary_route';
_IMAGE_PRIMARY_ROUTE_LEGACY_KEY = "image.text_to_image_primary_route"
_IMAGE_CHANNEL_KEY = "image.channel"
_IMAGE_ENGINE_KEY = "image.engine"
_IMAGE_CHANNEL_AUTO = "auto"
_IMAGE_CHANNEL_STREAM_ONLY = "stream_only"
_IMAGE_CHANNEL_IMAGE_JOBS_ONLY = "image_jobs_only"
_IMAGE_CHANNELS = {
    _IMAGE_CHANNEL_AUTO,
    _IMAGE_CHANNEL_STREAM_ONLY,
    _IMAGE_CHANNEL_IMAGE_JOBS_ONLY,
}
_IMAGE_ROUTE_RESPONSES = "responses"
_IMAGE_ROUTE_IMAGE2 = "image2"
_IMAGE_ROUTE_IMAGE_JOBS = "image_jobs"
_IMAGE_ROUTE_DUAL_RACE = "dual_race"
_IMAGE_ENGINES = {
    _IMAGE_ROUTE_RESPONSES,
    _IMAGE_ROUTE_IMAGE2,
    _IMAGE_ROUTE_DUAL_RACE,
}
# 兼容性别名（保留，避免外部引用 / 历史测试断言失败）
_TEXT_TO_IMAGE_PRIMARY_ROUTE_KEY = _IMAGE_PRIMARY_ROUTE_LEGACY_KEY
_TEXT_TO_IMAGE_ROUTE_RESPONSES = _IMAGE_ROUTE_RESPONSES
_TEXT_TO_IMAGE_ROUTE_IMAGE2 = _IMAGE_ROUTE_IMAGE2
_IMAGE_OUTPUT_FORMATS = {"png", "jpeg", "webp"}
_IMAGE_BACKGROUNDS = {"auto", "opaque", "transparent"}
_IMAGE_MODERATIONS = {"auto", "low"}
_IMAGE_QUALITIES = {"auto", "low", "medium", "high"}
# 实测 OpenAI codex 端 image_generation 工具的 `output_compression` 参数实际不生效——
# 设 100（应该等同 quality 100）输出仍有明显 JPEG 压缩痕迹；同 prompt 切到 PNG 干净无痕迹。
# 因此默认走 PNG（无损）。代价是 4K PNG 体积大（~10MB base64），SSE 流时长长；
# 但 retry-buster（attempt>1 时注入 prompt_cache_key + effort 轮转 + 关 partial_images）
# 已经能把断流场景接住，PNG 路径 reliability 不再是问题。
_DEFAULT_IMAGE_OUTPUT_FORMAT = "png"
# output_compression 仅对 jpeg/webp 生效；PNG 路径下不会进入 body。保留 100 以备显式切 jpeg/webp。
_DEFAULT_IMAGE_OUTPUT_COMPRESSION = 100

# Retry 时打散 prompt cache 的 ContextVar——上层 set 后所有 body 构造点会读到。
# 默认 1 表示首次尝试，body 保持原样享受 cache 命中；> 1 时由 _apply_retry_cache_busters 注入打散字段。
# ContextVar 比逐层透传 retry_attempt 参数好——image dispatch 链路有 9 层函数，全改签名风险大。
_image_retry_attempt_ctx: contextvars.ContextVar[int] = contextvars.ContextVar(
    "lumen_image_retry_attempt", default=1
)


def push_image_retry_attempt(attempt: int) -> contextvars.Token[int]:
    """供 generation.py 等上层在调用 edit_image / generate_image 前设置当前 task 级 retry attempt。

    用法：
        token = push_image_retry_attempt(gen.attempt)
        try:
            image_iter = edit_image(...)
            ...
        finally:
            pop_image_retry_attempt(token)

    attempt == 1 时下游 body 构造点不打散；> 1 时注入 prompt_cache_key 等"打散三件套"。
    """
    return _image_retry_attempt_ctx.set(max(1, int(attempt or 1)))


def pop_image_retry_attempt(token: contextvars.Token[int]) -> None:
    """配对 push_image_retry_attempt——必须在 finally 里调用，避免 ContextVar 漂移到外层环境。"""
    _image_retry_attempt_ctx.reset(token)


def _apply_retry_cache_busters(
    body: dict[str, Any], retry_attempt: int, prompt: str, size: str
) -> None:
    """Retry 时往 body 注入"打散字段"，绕开 ChatGPT codex 端的故障 prompt cache。

    背景：实测同 prompt + 同参考图的 dual-race 会让一条流成功一条流 server_error，
    后续 retry 用同 body → 命中 codex 端"故障 cache" → 反复 server_error 烧账号 quota。

    打散三件套（仅 retry_attempt > 1 时启用）：
    1. prompt_cache_key：OpenAI Responses API 官方支持的 cache 隔离字段。每次 retry 都换 seed
       → OpenAI prompt cache 必然 miss，跳出故障 cache。sub2api 也用此字段做 sticky session
       hash，retry 自然脱离原账号 → 等同账号级 failover。
    2. reasoning.effort：medium → minimal → high → minimal 轮转。effort 也参与 cache key 哈希，
       多一层打散；副作用是 minimal 时 reasoning 阶段更短，整体 SSE 流时长下降，断流率↓。
    3. 移除 tools[0].partial_images：≥2K 大图 partial 实测稳定触发 server_error，retry 时关掉。

    retry_attempt == 1 时无操作，保留首次请求的 cache 命中收益。
    """
    upstream_image_requests._apply_retry_cache_busters(
        body,
        retry_attempt,
        prompt,
        size,
    )


_DEFAULT_IMAGE_BACKGROUND = "auto"
_DEFAULT_IMAGE_MODERATION = "low"
_DEFAULT_IMAGE_JOB_BASE_URL = "https://image-job.example.com"
_IMAGE_JOB_RETENTION_DAYS = 1
_IMAGE_JOB_POLL_INTERVAL_S = 3.0
_IMAGE_JOB_TIMEOUT_S = 1200.0
_IMAGE_JOB_DOWNLOAD_MAX_BYTES = _MAX_NORMALIZED_IMAGE_BYTES
_TRANSPARENT_MATTE_PROMPT_NOTE = (
    "The final image will be post-processed into a transparent PNG. Render the "
    "subject isolated on a perfectly flat, high-contrast, single-color matte "
    "background that does not appear in the subject. Keep the entire outer "
    "border the same matte color and keep the subject fully inside the canvas. "
    "No shadows, reflections, texture, gradients, or background objects."
)
_LOG_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LOG_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=\-]+", re.IGNORECASE)
_LOG_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b")


def _redact_upstream_log_text(value: str) -> str:
    text = _LOG_EMAIL_RE.sub("[email]", value)
    text = _LOG_BEARER_RE.sub("Bearer [redacted]", text)
    text = _LOG_API_KEY_RE.sub("[api_key]", text)
    return text[:300]


def _summarize_upstream_error_detail(
    detail: dict[str, Any] | None,
) -> dict[str, Any] | str:
    if not isinstance(detail, dict):
        return "none"
    summary: dict[str, Any] = {}
    for key in ("code", "type", "param", "status"):
        value = detail.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
    message = detail.get("message")
    if isinstance(message, str) and message:
        summary["message"] = _redact_upstream_log_text(message)
    if summary:
        return summary
    return {"keys": sorted(str(key) for key in detail.keys())[:10]}


def _image_request_policy() -> upstream_image_requests.ImageRequestPolicy:
    """Snapshot current module policy so monkeypatches remain call-time visible."""
    return upstream_image_requests.ImageRequestPolicy(
        upstream_model=UPSTREAM_MODEL,
        default_responses_model=DEFAULT_IMAGE_RESPONSES_MODEL,
        default_image_instructions=DEFAULT_IMAGE_INSTRUCTIONS,
        image_qualities=_IMAGE_QUALITIES,
        image_output_formats=_IMAGE_OUTPUT_FORMATS,
        image_backgrounds=_IMAGE_BACKGROUNDS,
        image_moderations=_IMAGE_MODERATIONS,
        default_image_quality="high",
        default_image_output_format=_DEFAULT_IMAGE_OUTPUT_FORMAT,
        default_image_output_compression=_DEFAULT_IMAGE_OUTPUT_COMPRESSION,
        default_image_background=_DEFAULT_IMAGE_BACKGROUND,
        default_image_moderation=_DEFAULT_IMAGE_MODERATION,
        transparent_matte_prompt_note=_TRANSPARENT_MATTE_PROMPT_NOTE,
        partial_images_max_pixels=_PARTIAL_IMAGES_MAX_PIXELS,
        image_job_retention_days=_IMAGE_JOB_RETENTION_DAYS,
    )


def _normalize_image_quality(value: str | None) -> str:
    return upstream_image_requests._normalize_image_quality(
        value,
        policy=_image_request_policy(),
    )


def _normalize_image_output_format(value: str | None) -> str:
    return upstream_image_requests._normalize_image_output_format(
        value,
        policy=_image_request_policy(),
    )


def _normalize_image_output_compression(
    value: int | None,
    *,
    output_format: str,
) -> int | None:
    return upstream_image_requests._normalize_image_output_compression(
        value,
        output_format=output_format,
        policy=_image_request_policy(),
    )


def _normalize_image_background(value: str | None) -> str:
    return upstream_image_requests._normalize_image_background(
        value,
        policy=_image_request_policy(),
    )


def _normalize_image_moderation(value: str | None) -> str:
    return upstream_image_requests._normalize_image_moderation(
        value,
        policy=_image_request_policy(),
    )


def _add_image_output_options(
    body: dict[str, Any],
    *,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
) -> None:
    upstream_image_requests._add_image_output_options(
        body,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        hooks=upstream_image_requests.ImageOutputOptionsHooks(
            normalize_image_background=_normalize_image_background,
            normalize_image_output_format=_normalize_image_output_format,
            normalize_image_output_compression=_normalize_image_output_compression,
            normalize_image_moderation=_normalize_image_moderation,
        ),
    )


def _is_transparent_image_request(background: str | None) -> bool:
    return upstream_image_requests._is_transparent_image_request(
        background,
        normalize_image_background=_normalize_image_background,
    )


def _append_transparent_matte_prompt(prompt: str) -> str:
    return upstream_image_requests._append_transparent_matte_prompt(
        prompt,
        policy=_image_request_policy(),
    )


def _transparent_matte_upstream_options(
    *,
    prompt: str,
    output_format: str | None,
    background: str | None,
) -> tuple[str, str | None, str | None]:
    return upstream_image_requests._transparent_matte_upstream_options(
        prompt=prompt,
        output_format=output_format,
        background=background,
        hooks=upstream_image_requests.TransparentMatteHooks(
            is_transparent_image_request=_is_transparent_image_request,
            append_transparent_matte_prompt=_append_transparent_matte_prompt,
        ),
    )


_TimeoutConfig = upstream_client_lifecycle._TimeoutConfig
_TrackedStreamContext = upstream_client_lifecycle._TrackedStreamContext
_TrackedAsyncClient = upstream_client_lifecycle._TrackedAsyncClient
_resolve_timeout_config = upstream_client_lifecycle._resolve_timeout_config
_build_client = upstream_client_lifecycle._build_client
_build_images_client = upstream_client_lifecycle._build_images_client
_cache_proxied_client = upstream_client_lifecycle._cache_proxied_client
_delayed_aclose = upstream_client_lifecycle._delayed_aclose
_aclose_client_cancel_safe = upstream_client_lifecycle._aclose_client_cancel_safe
_schedule_delayed_aclose = upstream_client_lifecycle._schedule_delayed_aclose
_close_retired_clients_now = upstream_client_lifecycle._close_retired_clients_now
_get_client = upstream_client_lifecycle._get_client
_get_images_client = upstream_client_lifecycle._get_images_client
close_client = upstream_client_lifecycle.close_client


@dataclass(frozen=True)
class _ResolvedRuntime:
    name: str | None
    base_url: str
    api_key: str
    proxy: ProviderProxyDefinition | None = None

    def __iter__(self):
        yield self.base_url
        yield self.api_key


async def _resolve_runtime() -> _ResolvedRuntime:
    """Resolve a provider without owning a real text attempt."""
    pool = await provider_pool.get_pool()
    p = await pool.peek_one()
    return _ResolvedRuntime(p.name, p.base_url, p.api_key, p.proxy)


_DEFAULT_RESOLVE_RUNTIME = _resolve_runtime


def _provider_proxy(provider: Any) -> ProviderProxyDefinition | None:
    proxy = getattr(provider, "proxy", None)
    return proxy if isinstance(proxy, ProviderProxyDefinition) else None


def _runtime_parts(
    runtime: Any,
) -> tuple[str, str, ProviderProxyDefinition | None]:
    base_url = getattr(runtime, "base_url", None)
    api_key = getattr(runtime, "api_key", None)
    if base_url is None or api_key is None:
        base_url, api_key = runtime
    proxy = getattr(runtime, "proxy", None)
    return (
        str(base_url),
        str(api_key),
        proxy if isinstance(proxy, ProviderProxyDefinition) else None,
    )


def _runtime_provider_name(runtime: Any) -> str | None:
    name = getattr(runtime, "name", None)
    return name.strip() if isinstance(name, str) and name.strip() else None


def _legacy_route_to_channel_engine(route: str | None) -> tuple[str, str]:
    value = (route or "").strip().lower()
    if value == _IMAGE_ROUTE_IMAGE2:
        return _IMAGE_CHANNEL_AUTO, _IMAGE_ROUTE_IMAGE2
    if value == _IMAGE_ROUTE_IMAGE_JOBS:
        return _IMAGE_CHANNEL_IMAGE_JOBS_ONLY, _IMAGE_ROUTE_RESPONSES
    if value == _IMAGE_ROUTE_DUAL_RACE:
        return _IMAGE_CHANNEL_AUTO, _IMAGE_ROUTE_DUAL_RACE
    return _IMAGE_CHANNEL_AUTO, _IMAGE_ROUTE_RESPONSES


async def _resolve_legacy_image_primary_route() -> str | None:
    for key in (_IMAGE_PRIMARY_ROUTE_KEY, _IMAGE_PRIMARY_ROUTE_LEGACY_KEY):
        try:
            raw = await resolve(key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("image route setting resolve fallback key=%s err=%s", key, exc)
            raw = None
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip().lower()
    return None


async def _has_explicit_image_dispatch_setting(key: str, env_name: str) -> bool:
    if os.environ.get(env_name, "").strip():
        return True
    try:
        raw = await resolve_db(key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("image dispatch db setting lookup failed key=%s err=%s", key, exc)
        return False
    return raw is not None and str(raw).strip() != ""


async def _resolve_image_channel() -> str:
    """Resolve async channel strategy with legacy primary_route fallback."""
    has_explicit = await _has_explicit_image_dispatch_setting(
        _IMAGE_CHANNEL_KEY,
        "IMAGE_CHANNEL",
    )
    raw = await resolve(_IMAGE_CHANNEL_KEY) if has_explicit else None
    channel = (raw or "").strip().lower()
    if channel in _IMAGE_CHANNELS:
        return channel

    legacy_route = await _resolve_legacy_image_primary_route()
    legacy_channel, _legacy_engine = _legacy_route_to_channel_engine(legacy_route)
    if channel:
        logger.warning(
            "invalid %s=%r; falling back to %s",
            _IMAGE_CHANNEL_KEY,
            raw,
            legacy_channel,
        )
    return legacy_channel


async def _resolve_image_engine() -> str:
    """Resolve image engine with legacy primary_route fallback."""
    has_explicit = await _has_explicit_image_dispatch_setting(
        _IMAGE_ENGINE_KEY,
        "IMAGE_ENGINE",
    )
    raw = await resolve(_IMAGE_ENGINE_KEY) if has_explicit else None
    engine = (raw or "").strip().lower()
    if engine in _IMAGE_ENGINES:
        return engine

    legacy_route = await _resolve_legacy_image_primary_route()
    _legacy_channel, legacy_engine = _legacy_route_to_channel_engine(legacy_route)
    if engine:
        logger.warning(
            "invalid %s=%r; falling back to %s",
            _IMAGE_ENGINE_KEY,
            raw,
            legacy_engine,
        )
    return legacy_engine


async def _resolve_image_primary_route() -> str:
    """Compatibility label for older callers/tests.

    New dispatch uses ``image.channel`` + ``image.engine``. This function keeps
    the old route-ish return values where possible so queueing and admin
    metadata can continue to treat dual_race specially.
    """
    channel = await _resolve_image_channel()
    engine = await _resolve_image_engine()
    if engine == _IMAGE_ROUTE_DUAL_RACE:
        return _IMAGE_ROUTE_DUAL_RACE
    if channel == _IMAGE_CHANNEL_IMAGE_JOBS_ONLY:
        return _IMAGE_ROUTE_IMAGE_JOBS
    if engine == _IMAGE_ROUTE_IMAGE2:
        return _IMAGE_ROUTE_IMAGE2
    return _IMAGE_ROUTE_RESPONSES


# 兼容性别名（保留旧函数名，避免外部 import / 测试 monkeypatch 断裂）
_resolve_text_to_image_primary_route = _resolve_image_primary_route


def _auth_headers(
    api_key: str,
    *,
    trace_id: str | None = None,
) -> dict[str, str]:
    """构造 outbound headers。

    新增字段：
    - `originator`: lumen-prod-{version}，让上游和我们都能在日志里识别请求来源
    - `x-trace-id`: 调用方自生成的 uuid4，用于对账上游下发的 `x-request-id`
    传入 trace_id=None 时由本函数自动生成；调用方需要事后日志记录时可显式传同一个值。
    """
    headers: dict[str, str] = {
        "authorization": f"Bearer {api_key}",
        "originator": _LUMEN_ORIGINATOR,
        "x-trace-id": trace_id or _generate_trace_id(),
    }
    return headers


def _json_dumps_stable(value: Any) -> str:
    return upstream_image_requests._json_dumps_stable(value)


def _image_file_fingerprints(
    files: list[tuple[str, tuple[str, bytes, str]]] | None,
) -> list[dict[str, Any]]:
    return upstream_image_requests._image_file_fingerprints(files)


def _image_idempotency_key(
    *,
    trace_id: str,
    endpoint: str,
    body: dict[str, Any] | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
) -> str:
    return upstream_image_requests._image_idempotency_key(
        trace_id=trace_id,
        endpoint=endpoint,
        body=body,
        files=files,
        hooks=upstream_image_requests.ImageIdempotencyKeyHooks(
            json_dumps_stable=_json_dumps_stable,
            image_file_fingerprints=_image_file_fingerprints,
        ),
    )


def _attach_image_idempotency_key(
    headers: dict[str, str],
    *,
    trace_id: str,
    endpoint: str,
    body: dict[str, Any] | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
) -> None:
    upstream_image_requests._attach_image_idempotency_key(
        headers,
        trace_id=trace_id,
        endpoint=endpoint,
        body=body,
        files=files,
        hooks=upstream_image_requests.AttachImageIdempotencyKeyHooks(
            image_idempotency_key=_image_idempotency_key,
        ),
    )


def _extract_response_meta_headers(
    response_headers: Any,
) -> dict[str, Any]:
    """从上游响应 headers（dict / httpx.Headers）里抽 lumen 关心的元信息。

    缺失字段以 None 占位，方便统一打日志结构化字段。
    """
    if response_headers is None:
        return {"x_request_id": None, "x_codex_primary_used_percent": None}
    try:
        x_req_id = response_headers.get("x-request-id")
    except Exception:  # noqa: BLE001
        x_req_id = None
    try:
        used_pct = response_headers.get("x-codex-primary-used-percent")
    except Exception:  # noqa: BLE001
        used_pct = None
    used_pct_int: int | None = None
    if isinstance(used_pct, str) and used_pct.strip():
        try:
            used_pct_int = int(float(used_pct))
        except (TypeError, ValueError):
            used_pct_int = None
    return {
        "x_request_id": x_req_id if isinstance(x_req_id, str) else None,
        "x_codex_primary_used_percent": used_pct_int,
    }


def _log_upstream_call(
    *,
    endpoint: str,
    status: int,
    duration_ms: float,
    trace_id: str,
    response_headers: Any = None,
) -> None:
    """统一的上游 HTTP 调用元信息日志 + Prometheus 埋点。

    endpoint 取值受 prom label 约束：当前固定 `responses` / `responses_compact` /
    `images_generations` / `images_edits`。新增端点请同步更新 metrics_upstream 文档。
    """
    meta = _extract_response_meta_headers(response_headers)
    used_pct = meta.get("x_codex_primary_used_percent")
    logger.info(
        "upstream.call endpoint=%s status=%s duration_ms=%.1f trace_id=%s "
        "x_request_id=%s x_codex_primary_used_percent=%s",
        endpoint,
        status,
        duration_ms,
        trace_id,
        meta.get("x_request_id"),
        used_pct,
    )
    try:
        record_upstream_request(status_code=status, endpoint=endpoint)
        record_upstream_duration(
            seconds=max(0.0, duration_ms / 1000.0), endpoint=endpoint
        )
        if isinstance(used_pct, int):
            record_used_percent(p=used_pct)
    except Exception:  # noqa: BLE001
        # metrics 埋点不允许影响主链路；任何异常都吞掉。
        logger.debug("metrics record failed", exc_info=True)


def _record_usage(usage: Any) -> None:
    """从上游 `usage` 字段提取 token 计数并写入 Prometheus + 日志。

    上游字段路径（响应或 SSE response.completed.response.usage）：
    - input_tokens / output_tokens / total_tokens
    - input_tokens_details.cached_tokens
    - output_tokens_details.reasoning_tokens
    """
    if not isinstance(usage, dict):
        return
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    in_details = usage.get("input_tokens_details")
    out_details = usage.get("output_tokens_details")
    cached_tokens = (
        in_details.get("cached_tokens") if isinstance(in_details, dict) else None
    )
    reasoning_tokens = (
        out_details.get("reasoning_tokens") if isinstance(out_details, dict) else None
    )

    def _as_int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return max(0, n)

    inp = _as_int(input_tokens)
    outp = _as_int(output_tokens)
    cached = _as_int(cached_tokens)
    reasoning = _as_int(reasoning_tokens)
    total = _as_int(total_tokens)
    logger.info(
        "upstream.usage input_tokens=%s output_tokens=%s cached_tokens=%s "
        "reasoning_tokens=%s total_tokens=%s",
        inp,
        outp,
        cached,
        reasoning,
        total,
    )
    try:
        if inp is not None:
            record_upstream_tokens(kind="input", n=inp)
        if outp is not None:
            record_upstream_tokens(kind="output", n=outp)
        if cached is not None:
            record_upstream_tokens(kind="cached", n=cached)
        if reasoning is not None:
            record_upstream_tokens(kind="reasoning", n=reasoning)
    except Exception:  # noqa: BLE001
        logger.debug("metrics tokens record failed", exc_info=True)


def _validate_responses_body(body: dict[str, Any]) -> None:
    """请求 schema 预校验——参考 probe report §2.C1 的硬约束：

    - `instructions` 必须存在且为字符串（可为空串；完全缺失 = 上游 400 `Instructions are required`）
    - `input` 必须是 list（不是 = 上游 400 `Input must be a list`）
    - 有 `tools` 时，必须同时带 `parallel_tool_calls` / `tool_choice`，否则上游可能 4xx

    所有错误都按 4xx terminal 处理（重试无意义）。
    """
    instructions = body.get("instructions")
    if not isinstance(instructions, str):
        # 防御性兜底：调用方组 body 时若漏掉 instructions（None / 缺失 / 非 string），
        # 注入空串保持字段存在；图像路径标准模板用 "" 与 Codex CLI 一致，不影响上游接受。
        body["instructions"] = ""
        logger.warning(
            "upstream body missing instructions string; injected empty fallback"
        )
    input_field = body.get("input")
    if not isinstance(input_field, list):
        raise UpstreamError(
            "upstream body.input must be a list",
            status_code=400,
            error_code=EC.INVALID_REQUEST_ERROR.value,
            payload={"input_type": type(input_field).__name__},
        )
    tools = body.get("tools")
    if tools:
        if not isinstance(tools, list):
            raise UpstreamError(
                "upstream body.tools must be a list",
                status_code=400,
                error_code=EC.INVALID_REQUEST_ERROR.value,
            )
        if "tool_choice" not in body:
            raise UpstreamError(
                "upstream body.tools requires tool_choice",
                status_code=400,
                error_code=EC.INVALID_REQUEST_ERROR.value,
            )
        if "parallel_tool_calls" not in body:
            # 上游对该字段在多 tool 场景下要求显式给出；保守默认 False（图像 / chat 场景实际都不并行）。
            body["parallel_tool_calls"] = False


def _stable_sort_tools(tools: list[Any]) -> list[Any]:
    """按工具 name（缺省回退 type）排序——保证 prompt cache 前缀稳定。

    上游 prompt cache 命中要求请求体逐字节相同；tools 数组顺序抖动会让 cache miss。
    本函数不会修改输入 list，返回新副本；非 dict / 没有 name & type 的元素排在尾部。
    """
    return upstream_image_requests._stable_sort_tools(tools)


UpstreamError = upstream_errors.UpstreamError
UpstreamCancelled = upstream_errors.UpstreamCancelled


def _parse_error(payload: dict[str, Any], status_code: int) -> UpstreamError:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or "upstream_error"
        msg = err.get("message") or "upstream error"
        return UpstreamError(
            msg, status_code=status_code, error_code=code, payload=payload
        )
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail:
        return UpstreamError(
            detail,
            status_code=status_code,
            error_code=EC.UPSTREAM_ERROR.value,
            payload=payload,
        )
    return UpstreamError(
        f"upstream http {status_code}",
        status_code=status_code,
        error_code=EC.UPSTREAM_ERROR.value,
        payload=payload if isinstance(payload, dict) else {},
    )


def _with_error_context(
    exc: UpstreamError,
    *,
    path: str,
    method: str,
    url: str,
) -> UpstreamError:
    payload = dict(exc.payload)
    payload.setdefault("path", path)
    payload.setdefault("method", method)
    payload.setdefault("url", url)
    exc.payload = payload
    return exc


async def _download_result_url_bytes(
    image_url: str,
    *,
    path: str,
    log_endpoint: str,
    description: str,
    allowed_base_url: str | None = None,
) -> bytes:
    started = time.monotonic()
    trace_id = _generate_trace_id()
    try:
        response = await download_public_http_url(
            image_url,
            max_bytes=_IMAGE_JOB_DOWNLOAD_MAX_BYTES,
            max_redirects=5,
            allow_http=True,
            allowed_private_origins=((allowed_base_url,) if allowed_base_url else ()),
            dns_timeout_s=2.0,
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout_s,
                read=settings.upstream_read_timeout_s,
                write=settings.upstream_write_timeout_s,
                pool=settings.upstream_connect_timeout_s,
            ),
            headers={"User-Agent": "lumen-worker"},
        )
    except PublicHttpBodyTooLarge as exc:
        _log_upstream_call(
            endpoint=log_endpoint,
            status=exc.status_code or 0,
            duration_ms=(time.monotonic() - started) * 1000.0,
            trace_id=trace_id,
            response_headers=None,
        )
        raise UpstreamError(
            f"{description} exceeded max bytes",
            status_code=exc.status_code,
            error_code=EC.STREAM_TOO_LARGE.value,
            payload={
                "url": image_url,
                "path": path,
                "method": "GET",
                "bytes": exc.received_bytes,
                "max_bytes": exc.max_bytes,
            },
        ) from exc
    except ValueError as exc:
        _log_upstream_call(
            endpoint=log_endpoint,
            status=0,
            duration_ms=(time.monotonic() - started) * 1000.0,
            trace_id=trace_id,
            response_headers=None,
        )
        raise UpstreamError(
            f"unsafe image result URL: {exc}",
            status_code=400,
            error_code=EC.INVALID_VALUE.value,
            payload={"url": image_url, "path": path, "method": "GET"},
        ) from exc
    except (httpx.HTTPError, OSError) as exc:
        _log_upstream_call(
            endpoint=log_endpoint,
            status=0,
            duration_ms=(time.monotonic() - started) * 1000.0,
            trace_id=trace_id,
            response_headers=None,
        )
        raise UpstreamError(
            f"{description} failed: {exc}",
            status_code=0,
            error_code=EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={"url": image_url, "path": path, "method": "GET"},
        ) from exc

    _log_upstream_call(
        endpoint=log_endpoint,
        status=response.status_code,
        duration_ms=(time.monotonic() - started) * 1000.0,
        trace_id=trace_id,
        response_headers=response.headers,
    )
    if not 200 <= response.status_code < 300:
        raise UpstreamError(
            f"{description} http {response.status_code}",
            status_code=response.status_code,
            error_code=EC.UPSTREAM_ERROR.value,
            payload={
                "url": image_url,
                "final_url": response.url,
                "path": path,
                "method": "GET",
            },
        )
    if not response.body:
        raise UpstreamError(
            f"{description} returned empty body",
            status_code=response.status_code,
            error_code=EC.NO_IMAGE_RETURNED.value,
            payload={
                "url": image_url,
                "final_url": response.url,
                "path": path,
                "method": "GET",
            },
        )
    return response.body


async def _fetch_image_url_as_bytes(
    image_url: str,
    *,
    proxy_url: str | None = None,
) -> bytes:
    """下载 images API 在 data[].url 里返回的图片，转成原始字节。

    OpenAI 协议合法的两种响应形态之一：当 response_format=url（旧默认 / 部分
    第三方网关行为）时，图片以 CDN 链接返回而非 b64_json。下载使用逐跳校验和
    DNS-pinned 直连；不复用 provider client，避免代理或二次 DNS 解析绕过 SSRF
    边界。
    """
    _ = proxy_url
    return await _download_result_url_bytes(
        image_url,
        path="images/result",
        log_endpoint="image_url_download",
        description="image url download",
    )


async def _extract_image_results(
    payload: Any,
    status_code: int,
    *,
    proxy_url: str | None = None,
) -> list[tuple[str, str | None]]:
    return await upstream_direct_images._extract_image_results(
        payload,
        status_code,
        fetch_image_url_as_bytes=_fetch_image_url_as_bytes,
        upstream_error_type=UpstreamError,
        bad_response_error_code=EC.BAD_RESPONSE.value,
        no_image_returned_error_code=EC.NO_IMAGE_RETURNED.value,
        proxy_url=proxy_url,
    )


async def _extract_image_result(
    payload: Any,
    status_code: int,
    *,
    proxy_url: str | None = None,
) -> tuple[str, str | None]:
    """Compatibility wrapper for callers that expect the first image only."""
    return await upstream_direct_images._extract_image_result(
        payload,
        status_code,
        extract_image_results=_extract_image_results,
        proxy_url=proxy_url,
    )


_validated_byok_target_for_request = (
    upstream_request_targets._validated_byok_target_for_request
)
_api_base = upstream_request_targets._api_base
_responses_url = upstream_request_targets._responses_url
_image_generations_url = upstream_request_targets._image_generations_url
_image_edits_url = upstream_request_targets._image_edits_url
_image_jobs_url = upstream_request_targets._image_jobs_url
_image_job_status_url = upstream_request_targets._image_job_status_url
_validate_image_job_base_url = upstream_request_targets._validate_image_job_base_url


async def _resolve_image_job_base_url() -> str:
    try:
        raw = await resolve("image.job_base_url")
    except Exception as exc:  # noqa: BLE001
        logger.debug("image job base URL setting fallback err=%s", exc)
        raw = None
    return _validate_image_job_base_url(raw or _DEFAULT_IMAGE_JOB_BASE_URL)


def _minimum_image_read_timeout(size: str) -> float:
    pixels = _parse_size_pixels(size)
    if pixels is not None and pixels > _IMAGE_4K_PIXELS:
        return _IMAGE_READ_TIMEOUT_4K_S
    return _IMAGE_READ_TIMEOUT_MIN_S


async def _image_request_timeout(size: str) -> tuple[httpx.Timeout, float]:
    timeout_config = await _resolve_timeout_config()
    read_timeout_s = max(timeout_config.read, _minimum_image_read_timeout(size))
    return timeout_config.to_httpx(read=read_timeout_s), read_timeout_s


def _direct_image_result_unknown_error(
    exc: BaseException,
    *,
    path: str,
    method: str,
    url: str,
    trace_id: str,
    timeout_s: float,
) -> UpstreamError:
    exc_type = type(exc).__name__
    return UpstreamError(
        (
            f"{path} timed out after {timeout_s:.0f}s; upstream result is unknown. "
            "The request may already have been accepted, so it was not retried automatically."
        ),
        status_code=0,
        error_code=EC.DIRECT_IMAGE_RESULT_UNKNOWN.value,
        payload={
            "path": path,
            "method": method,
            "url": url,
            "x_trace_id": trace_id,
            "timeout_s": timeout_s,
            "upstream_result_unknown": True,
            "exception": exc_type,
        },
    )


def _is_direct_image_result_unknown(exc: BaseException) -> bool:
    return (
        isinstance(exc, UpstreamError)
        and exc.error_code == EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
    )


async def _direct_generate_image_once(
    *,
    prompt: str,
    size: str,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    base_url_override: str,
    api_key_override: str,
    proxy_override: ProviderProxyDefinition | None = None,
    pinned_target_override: Any | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> list[tuple[str, str | None]]:
    """Text-to-image via direct `/v1/images/generations` using gpt-image-2."""
    proxy_url = await resolve_provider_proxy_url(proxy_override)
    url = _image_generations_url(base_url_override)
    pinned_target = (
        None
        if proxy_url
        else _validated_byok_target_for_request(pinned_target_override, url)
    )
    if proxy_url:
        client = await _get_images_client(proxy_url)
    elif pinned_target is not None:
        client = await _get_images_client(pinned_target=pinned_target)
    else:
        client = await _get_images_client()
    # Model 显式 pin：UPSTREAM_MODEL 来自 lumen_core.constants（lumen-core wheel 里固化）。
    # 加 runtime assert 防止未来改动把 model 字段隐式置空 / fallback 到上游默认。
    assert UPSTREAM_MODEL, "model must be set"
    prompt_for_upstream, output_format_for_upstream, background_for_upstream = (
        _transparent_matte_upstream_options(
            prompt=prompt,
            output_format=output_format,
            background=background,
        )
    )
    body: dict[str, Any] = {
        "model": UPSTREAM_MODEL,
        "prompt": prompt_for_upstream,
        "size": size,
        "n": n,
        "quality": _normalize_image_quality(quality),
    }
    _add_image_output_options(
        body,
        output_format=output_format_for_upstream,
        output_compression=output_compression,
        background=background_for_upstream,
        moderation=moderation,
    )
    trace_id = _generate_trace_id()
    headers = _auth_headers(api_key_override, trace_id=trace_id)
    _attach_image_idempotency_key(
        headers,
        trace_id=trace_id,
        endpoint="images/generations",
        body=body,
    )
    request_timeout, read_timeout_s = await _image_request_timeout(size)
    started = time.monotonic()
    try:
        resp = await _post_with_retry(
            client=client,
            url=url,
            headers=headers,
            json_body=body,
            timeout=request_timeout,
            retry_httpx_exceptions=False,
            before_attempt=before_attempt,
        )
    except httpx.TimeoutException as exc:
        duration_ms = (time.monotonic() - started) * 1000.0
        _log_upstream_call(
            endpoint="images_generations",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise _direct_image_result_unknown_error(
            exc,
            path="images/generations",
            method="POST",
            url=url,
            trace_id=trace_id,
            timeout_s=read_timeout_s,
        ) from exc
    except _RETRY_HTTPX_EXC as exc:
        duration_ms = (time.monotonic() - started) * 1000.0
        _log_upstream_call(
            endpoint="images_generations",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise UpstreamError(
            f"direct image request failed: {exc}",
            status_code=0,
            error_code=EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={
                "path": "images/generations",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
            },
        ) from exc

    duration_ms = (time.monotonic() - started) * 1000.0
    _log_upstream_call(
        endpoint="images_generations",
        status=resp.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=getattr(resp, "headers", None),
    )

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(
            "upstream returned invalid JSON",
            status_code=resp.status_code,
            error_code=EC.BAD_RESPONSE.value,
            payload={
                "path": "images/generations",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
            },
        ) from exc

    if resp.status_code >= 400:
        raise _with_error_context(
            _parse_error(
                payload if isinstance(payload, dict) else {}, resp.status_code
            ),
            path="images/generations",
            method="POST",
            url=url,
        )
    # JSON 响应里的 usage（如有）也走标准埋点。
    if isinstance(payload, dict):
        _record_usage(payload.get("usage"))
    return await _extract_image_results(payload, resp.status_code, proxy_url=proxy_url)


def _wrap_inpaint_prompt(user_intent: str) -> str:
    """局部 inpaint 的 prompt 包装。

    OpenAI /v1/images/edits + mask 字段必须用 invariant 模板才能正确 inpaint：
    否则 mask 区会被填黑、prompt 内容画到别处。本地 spike 已验证（mean diff 1.9）。

    前缀（"Inside the masked region,"）和后缀（preserve / do not add / blend 三条）
    都稳定，user_intent 夹在中间——这样 prompt cache prefix 在多次 retry 间保持稳定。
    只在 mask 不为空时调用；mask 为空时直接发原始 prompt（保持 i2i 行为不变）。

    第四行 "Blend ..." 让模型在 remove / replace 类指令下知道用周围像素自然过渡填充
    （否则 "Inside the masked region, remove the apple." 模型常困惑该填什么 → 填黑/灰色）。
    """
    return upstream_image_requests._wrap_inpaint_prompt(user_intent)


async def _direct_edit_image_once(
    *,
    prompt: str,
    size: str,
    images: list[bytes],
    mask: bytes | None = None,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    base_url_override: str,
    api_key_override: str,
    proxy_override: ProviderProxyDefinition | None = None,
    pinned_target_override: Any | None = None,
) -> list[tuple[str, str | None]]:
    """Image-to-image via direct `/v1/images/edits` (multipart) using gpt-image-2.

    image2 模式下 i2i 的单次调用。多个 ref 图通过 multipart 字段名 `image[]` 上传，
    与上游 OpenAI /v1/images/edits 协议一致。复用 `_curl_post_multipart`（见
    "图生图 multipart 走 curl 子进程" 那段注释，httpx 的 multipart 在某些上游网关下
    会持续 502，curl 反而能 200）。

    mask 不为空时把 PNG 字节作为单字段名 `mask`（不是 `mask[]`）一并发送，触发上游
    inpaint 路径（圆外像素级保留，已 spike 验证）。mask 为 None 时不带这个字段，
    走纯图生图路径，保持现有 i2i 行为。
    """
    url = _image_edits_url(base_url_override)
    assert UPSTREAM_MODEL, "model must be set"
    prompt_for_upstream, output_format_for_upstream, background_for_upstream = (
        _transparent_matte_upstream_options(
            prompt=prompt,
            output_format=output_format,
            background=background,
        )
    )
    bg = _normalize_image_background(background_for_upstream)
    fmt = _normalize_image_output_format(output_format_for_upstream)
    compression = _normalize_image_output_compression(
        output_compression, output_format=fmt
    )
    mod_value = _normalize_image_moderation(moderation)
    quality_normalized = _normalize_image_quality(quality)

    data: dict[str, str] = {
        "model": UPSTREAM_MODEL,
        "prompt": prompt_for_upstream,
        "size": size,
        "n": str(n),
        "quality": quality_normalized,
        "output_format": fmt,
        "background": bg,
        "moderation": mod_value,
    }
    if compression is not None:
        data["output_compression"] = str(compression)

    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for i, raw in enumerate(images):
        files.append(("image[]", (f"ref-{i}.png", raw, "image/png")))
    # inpaint mask（可选）：单字段 `mask`，不是 `mask[]`。content-type image/png。
    # 走和 image[] 同款 _curl_post_multipart 路径；不走 httpx multipart（那条路上历史
    # 在某些网关下持续 502，curl 反而能 200，详见上方"图生图 multipart 走 curl 子进程"
    # 注释）。
    if mask is not None:
        files.append(("mask", ("mask.png", mask, "image/png")))

    trace_id = _generate_trace_id()
    headers = _auth_headers(api_key_override, trace_id=trace_id)
    _attach_image_idempotency_key(
        headers,
        trace_id=trace_id,
        endpoint="images/edits",
        body=data,
        files=files,
    )
    proxy_url = await resolve_provider_proxy_url(proxy_override)
    pinned_target = (
        None
        if proxy_url
        else _validated_byok_target_for_request(pinned_target_override, url)
    )
    started = time.monotonic()
    _, read_timeout_s = await _image_request_timeout(size)
    try:
        request_kwargs: dict[str, Any] = {
            "url": url,
            "data": data,
            "files": files,
            "headers": headers,
            "timeout_s": read_timeout_s,
            "proxy_url": proxy_url,
        }
        if pinned_target is not None:
            request_kwargs["pinned_target"] = pinned_target
        status, payload = await _curl_post_multipart(**request_kwargs)
    except httpx.TimeoutException as exc:
        duration_ms = (time.monotonic() - started) * 1000.0
        _log_upstream_call(
            endpoint="images_edits",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise _direct_image_result_unknown_error(
            exc,
            path="images/edits",
            method="POST",
            url=url,
            trace_id=trace_id,
            timeout_s=read_timeout_s,
        ) from exc
    except (asyncio.CancelledError, UpstreamCancelled):
        raise
    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.monotonic() - started) * 1000.0
        _log_upstream_call(
            endpoint="images_edits",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise UpstreamError(
            f"direct edit request failed: {exc}",
            status_code=0,
            error_code=EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={
                "path": "images/edits",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
            },
        ) from exc

    duration_ms = (time.monotonic() - started) * 1000.0
    _log_upstream_call(
        endpoint="images_edits",
        status=status,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=None,  # curl path 不暴露 response headers
    )

    if status >= 400:
        raise _with_error_context(
            _parse_error(payload if isinstance(payload, dict) else {}, status),
            path="images/edits",
            method="POST",
            url=url,
        )
    if isinstance(payload, dict):
        _record_usage(payload.get("usage"))
    return await _extract_image_results(payload, status, proxy_url=proxy_url)


def _image_job_body_base(
    *,
    prompt: str,
    size: str,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
) -> dict[str, Any]:
    return upstream_image_requests._image_job_body_base(
        prompt=prompt,
        size=size,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        policy=_image_request_policy(),
        hooks=upstream_image_requests.ImageJobBodyHooks(
            transparent_matte_upstream_options=_transparent_matte_upstream_options,
            normalize_image_quality=_normalize_image_quality,
            add_image_output_options=_add_image_output_options,
        ),
    )


def _image_job_payload(
    *,
    request_type: str,
    endpoint: str,
    body: dict[str, Any],
    image_edit_input_transport: str | None = None,
) -> dict[str, Any]:
    return upstream_image_requests._image_job_payload(
        request_type=request_type,
        endpoint=endpoint,
        body=body,
        image_edit_input_transport=image_edit_input_transport,
        policy=_image_request_policy(),
    )


def _build_responses_image_body(
    *,
    action: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    model: str | None,
    image_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Build the JSON body posted to ``/v1/responses`` for image generation.

    image_urls vs images：
    - image_urls 优先（http URL 或 data URL，已是上游 image_url 字段值）：调用方先把 reference
      push 到 image-job sidecar 拿短 URL，body 缩到几百字节。这是新优化路径。
    - images（bytes）作为 fallback：旧路径，base64 内联到 body（4-7MB），用于无 sidecar 测试环境。
    - 两者都不传 + action=edit：edit 没参考图，上游会按文生图处理（语义降级）。

    Extracted from ``_responses_image_stream`` so the image-job sidecar path can
    reuse the exact same request shape — keeping prompt-cache prefixes aligned
    between the direct-stream route and the async sidecar route.
    """
    return upstream_image_requests._build_responses_image_body(
        action=action,
        prompt=prompt,
        size=size,
        images=images,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
        image_urls=image_urls,
        retry_attempt=_image_retry_attempt_ctx.get(),
        policy=_image_request_policy(),
        hooks=upstream_image_requests.ResponsesImageBodyHooks(
            normalize_image_quality=_normalize_image_quality,
            transparent_matte_upstream_options=_transparent_matte_upstream_options,
            add_image_output_options=_add_image_output_options,
            parse_size_pixels=_parse_size_pixels,
            normalize_reference_image=_normalize_reference_image,
            stable_sort_tools=_stable_sort_tools,
            apply_retry_cache_busters=_apply_retry_cache_busters,
            validate_responses_body=_validate_responses_body,
        ),
    )


def _image_job_error(job: dict[str, Any], *, status_code: int = 200) -> UpstreamError:
    upstream_status = job.get("upstream_status")
    try:
        status = int(upstream_status) if upstream_status is not None else status_code
    except (TypeError, ValueError):
        status = status_code
    upstream_body = job.get("upstream_body")
    # Sidecar tags every failed job with an error_class describing whether the
    # failure was a transport problem, an upstream HTTP error, a missing image,
    # etc. Lumen's failover layer reads this to decide whether to switch the
    # endpoint kind on the same provider or jump straight to the next provider.
    error_class = job.get("error_class")
    if isinstance(upstream_body, dict):
        exc = _parse_error(upstream_body, status)
        exc.payload = {
            **exc.payload,
            "job_id": job.get("job_id"),
            "path": "image-jobs",
            "method": "GET",
            "image_job_error_class": error_class,
            "image_job_endpoint_used": job.get("endpoint_used"),
        }
        return exc
    err = job.get("error")
    message = err if isinstance(err, str) and err else "image job failed"
    return UpstreamError(
        message,
        status_code=status,
        error_code=EC.UPSTREAM_ERROR.value,
        payload={
            "job_id": job.get("job_id"),
            "path": "image-jobs",
            "method": "GET",
            "upstream_body": upstream_body,
            "image_job_error_class": error_class,
            "image_job_endpoint_used": job.get("endpoint_used"),
        },
    )


async def _download_image_job_result(
    *,
    client: httpx.AsyncClient,
    image_url: str,
    proxy_url: str | None,
    allowed_base_url: str | None = None,
) -> bytes:
    _ = client, proxy_url
    return await _download_result_url_bytes(
        image_url,
        path="image-jobs/result",
        log_endpoint="image_jobs_download",
        description="image job result download",
        allowed_base_url=allowed_base_url,
    )


async def _submit_and_wait_image_job(
    *,
    payload: dict[str, Any],
    base_url: str,
    api_key: str,
    proxy: ProviderProxyDefinition | None,
    progress_callback: ImageProgressCallback | None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    proxy_url = await resolve_provider_proxy_url(proxy)
    # image-job traffic targets the configured sidecar/internal origin. BYOK
    # pins are only passed to direct supplier requests, never inherited here.
    client = await (
        _get_images_client(proxy_url) if proxy_url else _get_images_client()
    )
    submit_url = _image_jobs_url(base_url)
    trace_id = _generate_trace_id()
    headers = _auth_headers(api_key, trace_id=trace_id)
    payload_idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if payload_idempotency_key:
        digest = hashlib.sha256(payload_idempotency_key.encode("utf-8")).hexdigest()
        headers.setdefault("Idempotency-Key", f"lumen-image-job-{digest[:32]}")
    else:
        _attach_image_idempotency_key(
            headers,
            trace_id=trace_id,
            endpoint="image-jobs",
            body=payload,
        )
    started = time.monotonic()
    try:
        resp = await _post_with_retry(
            client=client,
            url=submit_url,
            headers=headers,
            json_body=payload,
            max_attempts=3,
            before_attempt=before_attempt,
        )
    except _RETRY_HTTPX_EXC as exc:
        duration_ms = (time.monotonic() - started) * 1000.0
        _log_upstream_call(
            endpoint="image_jobs_submit",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise UpstreamError(
            f"image job submit failed: {exc}",
            status_code=0,
            error_code=EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={"path": "image-jobs", "method": "POST", "url": submit_url},
        ) from exc

    duration_ms = (time.monotonic() - started) * 1000.0
    _log_upstream_call(
        endpoint="image_jobs_submit",
        status=resp.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=getattr(resp, "headers", None),
    )
    try:
        submit_payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(
            "image job submit returned invalid JSON",
            status_code=resp.status_code,
            error_code=EC.BAD_RESPONSE.value,
            payload={"path": "image-jobs", "method": "POST", "url": submit_url},
        ) from exc
    if resp.status_code >= 400:
        raise _with_error_context(
            _parse_error(
                submit_payload if isinstance(submit_payload, dict) else {},
                resp.status_code,
            ),
            path="image-jobs",
            method="POST",
            url=submit_url,
        )
    if not isinstance(submit_payload, dict):
        raise UpstreamError(
            "image job submit returned non-object",
            status_code=resp.status_code,
            error_code=EC.BAD_RESPONSE.value,
            payload={"path": "image-jobs", "method": "POST", "url": submit_url},
        )
    job_id = submit_payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise UpstreamError(
            "image job submit returned no job_id",
            status_code=resp.status_code,
            error_code=EC.BAD_RESPONSE.value,
            payload=submit_payload,
        )
    await _emit_image_progress(
        progress_callback,
        "fallback_started",
        source="image_jobs",
        job_id=job_id,
    )

    deadline = time.monotonic() + _IMAGE_JOB_TIMEOUT_S
    status_url = _image_job_status_url(base_url, job_id)
    while time.monotonic() < deadline:
        await asyncio.sleep(_IMAGE_JOB_POLL_INTERVAL_S)
        poll_trace_id = _generate_trace_id()
        poll_started = time.monotonic()
        try:
            poll_resp = await client.get(
                status_url,
                headers=_auth_headers(api_key, trace_id=poll_trace_id),
            )
        except _RETRY_HTTPX_EXC as exc:
            logger.warning("image job poll transient err job=%s err=%r", job_id, exc)
            continue
        poll_duration_ms = (time.monotonic() - poll_started) * 1000.0
        _log_upstream_call(
            endpoint="image_jobs_poll",
            status=poll_resp.status_code,
            duration_ms=poll_duration_ms,
            trace_id=poll_trace_id,
            response_headers=getattr(poll_resp, "headers", None),
        )
        if poll_resp.status_code in _RETRY_STATUS:
            continue
        try:
            job = poll_resp.json()
        except Exception as exc:  # noqa: BLE001
            if poll_resp.status_code >= 500:
                continue
            raise UpstreamError(
                "image job poll returned invalid JSON",
                status_code=poll_resp.status_code,
                error_code=EC.BAD_RESPONSE.value,
                payload={"job_id": job_id, "path": "image-jobs", "method": "GET"},
            ) from exc
        if poll_resp.status_code >= 400:
            raise _with_error_context(
                _parse_error(
                    job if isinstance(job, dict) else {}, poll_resp.status_code
                ),
                path="image-jobs",
                method="GET",
                url=status_url,
            )
        if not isinstance(job, dict):
            raise UpstreamError(
                "image job poll returned non-object",
                status_code=poll_resp.status_code,
                error_code=EC.BAD_RESPONSE.value,
                payload={"job_id": job_id, "path": "image-jobs", "method": "GET"},
            )
        status = job.get("status")
        if status in {"queued", "running"}:
            continue
        if status == "failed":
            raise _image_job_error(job, status_code=poll_resp.status_code)
        if status != "succeeded":
            raise UpstreamError(
                f"image job returned unknown status: {status!r}",
                status_code=poll_resp.status_code,
                error_code=EC.BAD_RESPONSE.value,
                payload=job,
            )
        images = job.get("images")
        first = images[0] if isinstance(images, list) and images else None
        image_url = first.get("url") if isinstance(first, dict) else None
        if not isinstance(image_url, str) or not image_url:
            raise UpstreamError(
                "image job succeeded without images[0].url",
                status_code=poll_resp.status_code,
                error_code=EC.NO_IMAGE_RETURNED.value,
                payload=job,
            )
        # Surface the public sidecar URL into the request event stream so the
        # generation detail panel can display it next to the inlined image.
        # We carry the same field forward on `final_image` / `completed` events
        # so a UI that only renders one of them still sees the URL.
        image_meta: dict[str, Any] = {
            "image_job_url": image_url,
            "job_id": job_id,
            "endpoint_used": job.get("endpoint_used") or payload.get("endpoint"),
        }
        if isinstance(first, dict):
            for key in ("expires_at", "bytes", "width", "height", "format"):
                value = first.get(key)
                if value is not None:
                    image_meta[key] = value
        await _emit_image_progress(
            progress_callback,
            "image_job_image",
            **image_meta,
        )
        raw = await _download_image_job_result(
            client=client,
            image_url=image_url,
            proxy_url=proxy_url,
            allowed_base_url=base_url,
        )
        return base64.b64encode(raw).decode("ascii"), None

    raise UpstreamError(
        "image job timeout",
        status_code=None,
        error_code=EC.UPSTREAM_TIMEOUT.value,
        payload={"path": "image-jobs", "method": "GET", "job_id": job_id},
    )


async def _image_job_generate_once(
    *,
    prompt: str,
    size: str,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    api_key_override: str,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    progress_callback: ImageProgressCallback | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    body = _image_job_body_base(
        prompt=prompt,
        size=size,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
    )
    return await _submit_and_wait_image_job(
        payload=_image_job_payload(
            request_type="generations",
            endpoint="/v1/images/generations",
            body=body,
        ),
        base_url=base_url_override or await _resolve_image_job_base_url(),
        api_key=api_key_override,
        proxy=proxy_override,
        progress_callback=progress_callback,
        before_attempt=before_attempt,
    )


async def _image_job_reference_image_entries(
    images: list[bytes],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, str]]:
    image_urls = await _resolve_reference_image_urls(
        images,
        base_url=base_url,
        api_key=api_key,
        user_id=user_id,
    )
    return [{"image_url": url} for url in image_urls]


async def _image_job_edit_once(
    *,
    prompt: str,
    size: str,
    images: list[bytes],
    mask: bytes | None = None,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    api_key_override: str,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    image_edit_input_transport: str = "url",
    progress_callback: ImageProgressCallback | None = None,
    user_id: str | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    sidecar_base_url: str | None = base_url_override
    if sidecar_base_url is None:
        try:
            sidecar_base_url = await _resolve_image_job_base_url()
        except Exception as exc:  # noqa: BLE001
            logger.debug("reference push base_url resolve fallback err=%s", exc)
    body = _image_job_body_base(
        prompt=prompt,
        size=size,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
    )
    body["images"] = await _image_job_reference_image_entries(
        images,
        base_url=sidecar_base_url,
        api_key=api_key_override,
        user_id=user_id,
    )
    # inpaint mask 透传给 image-job sidecar：mask 仍用 data URL 即可。images[] 先走
    # refs cache / sidecar URL，mask 则保持单次任务内最短路径，避免额外 cache 写放大。
    if mask is not None:
        mask_b64 = base64.b64encode(mask).decode("ascii")
        body["mask"] = {"image_url": f"data:image/png;base64,{mask_b64}"}
    submit_base_url = (
        base_url_override or sidecar_base_url or await _resolve_image_job_base_url()
    )
    return await _submit_and_wait_image_job(
        payload=_image_job_payload(
            request_type="edits",
            endpoint="/v1/images/edits",
            body=body,
            image_edit_input_transport=image_edit_input_transport,
        ),
        base_url=submit_base_url,
        api_key=api_key_override,
        proxy=proxy_override,
        progress_callback=progress_callback,
        before_attempt=before_attempt,
    )


async def _image_job_responses_once(
    *,
    action: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    model: str | None,
    api_key_override: str,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    progress_callback: ImageProgressCallback | None = None,
    user_id: str | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    """Submit an image job that points the sidecar at ``/v1/responses``.

    The sidecar will block-wait the SSE stream and extract the final image. We
    pass exactly the same body the direct ``_responses_image_stream`` route
    would build, so prompt-cache prefixes match between the two paths.
    """
    _ = n  # /v1/responses + image_generation tool returns a single image.
    sidecar_base_url = base_url_override or await _resolve_image_job_base_url()
    # 先 push reference 到 image-job sidecar 拿短 URL；失败时 image_urls=[] 让 build 走 base64 fallback。
    # api_key 用同一个（image-job sidecar /v1/refs 和 /v1/image-jobs 共用 Bearer）。
    image_urls = await _resolve_reference_image_urls(
        images,
        base_url=sidecar_base_url,
        api_key=api_key_override,
        user_id=user_id,
    )
    body = _build_responses_image_body(
        action=action,
        prompt=prompt,
        size=size,
        images=images,
        image_urls=image_urls or None,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
    )
    return await _submit_and_wait_image_job(
        payload=_image_job_payload(
            request_type="responses",
            endpoint="/v1/responses",
            body=body,
        ),
        base_url=sidecar_base_url,
        api_key=api_key_override,
        proxy=proxy_override,
        progress_callback=progress_callback,
        before_attempt=before_attempt,
    )


# 图生图 multipart 走 curl 子进程——实测在同一台服务器上 httpx.AsyncClient 发出
# 同样 body 被上游网关持续 502，但 curl 命令发同样请求能 200 出图。原因尚未定位
# （怀疑 httpx 的 multipart boundary / header 组合触发了网关某条规则）。
# 绕法：edit 路径只用 curl，保留 retry + fallback 语义。
_CURL_BIN = shutil.which("curl") or "/usr/bin/curl"

_curl_timeout_arg = upstream_transport._curl_timeout_arg
_write_json_body_file = upstream_transport._write_json_body_file
_write_bytes_file = upstream_transport._write_bytes_file
_terminate_curl_proc_group = upstream_transport._terminate_curl_proc_group
_stage_multipart_bytes_to_tmp = upstream_transport._stage_multipart_bytes_to_tmp
_curl_post_multipart_using_paths = upstream_transport._curl_post_multipart_using_paths
_curl_post_multipart = upstream_transport._curl_post_multipart


_iter_sse_curl = upstream_transport._iter_sse_curl
_maybe_record_usage_from_event = upstream_transport._maybe_record_usage_from_event
_emit_image_progress = upstream_transport._emit_image_progress


def _extract_response_image_b64(event: dict[str, Any]) -> str | None:
    return upstream_responses._extract_response_image_b64(event)


def _extract_response_revised_prompt(event: dict[str, Any]) -> str | None:
    return upstream_responses._extract_response_revised_prompt(event)


def _b64_value_if_str(value: Any) -> str | None:
    return upstream_responses._b64_value_if_str(value)


def _extract_image_b64_from_payload(payload: Any) -> str | None:
    return upstream_responses._extract_image_b64_from_payload(
        payload,
        b64_value_if_str=_b64_value_if_str,
    )


def _extract_image_billable_count(payload: Any) -> int | None:
    return upstream_responses._extract_image_billable_count(payload)


_sniff_image_mime = upstream_reference_images._sniff_image_mime
_normalize_reference_image = upstream_reference_images._normalize_reference_image
_reference_cache_keys = upstream_reference_images._reference_cache_keys
_redis_text = upstream_reference_images._redis_text
_reference_cache_get = upstream_reference_images._reference_cache_get
_reference_cache_store = upstream_reference_images._reference_cache_store
_reference_cache_delete = upstream_reference_images._reference_cache_delete
_reference_cache_trim = upstream_reference_images._reference_cache_trim
_reference_url_is_live = upstream_reference_images._reference_url_is_live
_get_or_upload_reference = upstream_reference_images._get_or_upload_reference
_push_reference_to_image_job = upstream_reference_images._push_reference_to_image_job
_resolve_reference_image_urls = upstream_reference_images._resolve_reference_image_urls


# 带 partial_images 时上游能承载的像素上限（见 _responses_image_stream 里的说明）。
# 1536x864 ≈1.3M 已验证稳定；3840x2160 ≈8.3M 必挂。
# 参考 sub2api_lumen_responses_image_optimization.md §Lumen #4：稳定优先时 2K 起完全
# 不带 partial。把阈值收紧到 1.4MP，让 1024x1536 (1.57MP) 等"小 2K"也走稳定路径，
# 只有 ≤~1.4MP 的纯 1K 才允许 partial 预览。
_PARTIAL_IMAGES_MAX_PIXELS = 1_400_000

# race 单 lane 的像素阈值（与 partial 阈值解耦）。>2MP 强制单 lane 避免同账号
# 大图并发被打挂，1.5MP-2MP 仍允许 race 多 lane（1K 极限风险可控）。
_RACE_SINGLE_LANE_PIXELS = 2_000_000

# 4K 阈值（与 generation.py 同义，避免循环依赖在此重复定义）。
_IMAGE_4K_PIXELS = 4_000_000

# 4K 生图 SSE 总耗时常超 3 分钟（排队 + 渲染 + base64 序列化），
# settings.upstream_read_timeout_s=180s 偏紧。文档 §Lumen #5 建议拉到 300-420s。
_IMAGE_READ_TIMEOUT_MIN_S = 180.0
_IMAGE_READ_TIMEOUT_4K_S = 360.0


def _select_image_read_timeout(size: str) -> float:
    """按图像像素分级选 read/idle timeout。

    1K/2K：至少 180s，避免生图这种有副作用的 POST 被 20s 级运行时设置误杀。
    4K：取 max(默认, 360s)，避免被 settings 改小后误伤
    """
    return max(settings.upstream_read_timeout_s, _minimum_image_read_timeout(size))


def _parse_size_pixels(size: str) -> int | None:
    """把 `WxH` 字面量解析成总像素；`auto` / 非法格式返回 None。"""
    return upstream_image_requests._parse_size_pixels(size)


_responses_image_stream = upstream_image_stream._responses_image_stream


# ---- image retry / failover compatibility facade ----

_SAFETY_POLICY_ERROR_MARKERS = (
    "moderation_blocked",
    "safety_violation",
    "safety_violations",
    "content_policy_violation",
    "content policy",
    "safety system",
    "safety policy",
    "safety_policy",
    "blocked by upstream",
)
_IMAGE_PROVIDER_FAILOVER_ERROR_CODES = frozenset(
    {
        EC.MODERATION_BLOCKED.value,
        EC.CONTENT_POLICY_VIOLATION.value,
        EC.SAFETY_VIOLATION.value,
    }
)
_IMAGE_JOB_FAILOVER_CLASSES = frozenset(
    {"network", "upstream_5xx", "no_image", "image_save", "internal"}
)
_DUAL_RACE_BONUS_GRACE_S = 60.0
_DUAL_RACE_BONUS_GRACE_4K_S = 90.0
_DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S = 120.0
_DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_4K_S = 300.0

_summarize_exception = upstream_retry_policy._summarize_exception
_truncate_lane_summary = upstream_retry_policy._truncate_lane_summary
_is_retryable_fallback_exception = (
    upstream_retry_policy._is_retryable_fallback_exception
)
_fallback_retry_backoff_seconds = upstream_retry_policy._fallback_retry_backoff_seconds
_max_attempts_for_exception = upstream_retry_policy._max_attempts_for_exception
_retry_after_seconds = upstream_retry_policy._retry_after_seconds
_merge_fallback_errors = upstream_retry_policy._merge_fallback_errors
_provider_error_details = upstream_retry_policy._provider_error_details
_mentions_safety_policy = upstream_retry_policy._mentions_safety_policy
_should_continue_image_provider_failover = (
    upstream_retry_policy._should_continue_image_provider_failover
)
_merge_image_path_errors = upstream_retry_policy._merge_image_path_errors
_responses_image_stream_with_retry = (
    upstream_retry_policy._responses_image_stream_with_retry
)

_provider_pool_redis = upstream_provider_selection._provider_pool_redis
_pool_acquire_inflight = upstream_provider_selection._pool_acquire_inflight
_pool_release_inflight = upstream_provider_selection._pool_release_inflight
_is_byok_provider = upstream_provider_selection._is_byok_provider
_provider_attempt_context = upstream_provider_selection._provider_attempt_context
_pool_report_image_success = upstream_provider_selection._pool_report_image_success
_pool_report_image_failure = upstream_provider_selection._pool_report_image_failure
_provider_endpoint_locked_error = (
    upstream_provider_selection._provider_endpoint_locked_error
)
_provider_capability_error = upstream_provider_selection._provider_capability_error
_provider_endpoint_unavailable_error = (
    upstream_provider_selection._provider_endpoint_unavailable_error
)
_provider_allows_image_endpoint = (
    upstream_provider_selection._provider_allows_image_endpoint
)
_pool_select_compat = upstream_provider_selection._pool_select_compat
_is_image_rate_limit_error = upstream_provider_selection._is_image_rate_limit_error
_is_quota_accounting_unavailable = (
    upstream_provider_selection._is_quota_accounting_unavailable
)
_provider_has_image_quota = upstream_provider_selection._provider_has_image_quota
_reserve_admin_image_call = upstream_provider_selection._reserve_admin_image_call
_image_request_attempt_claim = upstream_provider_selection._image_request_attempt_claim
_release_unused_image_reservation = (
    upstream_provider_selection._release_unused_image_reservation
)
_image_quota_claim = upstream_provider_selection._image_quota_claim
_record_admin_image_call_or_raise = (
    upstream_provider_selection._record_admin_image_call_or_raise
)

_direct_generate_image_with_failover = (
    upstream_direct_failover._direct_generate_image_with_failover
)
_direct_edit_image_with_failover = (
    upstream_direct_failover._direct_edit_image_with_failover
)
_responses_image_stream_with_failover = (
    upstream_direct_failover._responses_image_stream_with_failover
)

_image_jobs_endpoint_fallback_chain = (
    upstream_image_job_failover._image_jobs_endpoint_fallback_chain
)
_image_job_error_class = upstream_image_job_failover._image_job_error_class
_should_continue_image_job_failover = (
    upstream_image_job_failover._should_continue_image_job_failover
)
_image_job_run_once = upstream_image_job_failover._image_job_run_once
_image_job_with_failover = upstream_image_job_failover._image_job_with_failover

_drain_task_group_result = upstream_image_race._drain_task_group_result
_cancel_and_wait_tasks = upstream_image_race._cancel_and_wait_tasks
_race_responses_image = upstream_image_race._race_responses_image
_dual_race_image_action = upstream_image_race._dual_race_image_action
_dual_race_image_jobs_action = upstream_image_race._dual_race_image_jobs_action

_image_jobs_endpoint_for_engine = (
    upstream_image_dispatch._image_jobs_endpoint_for_engine
)
_provider_supports_image_jobs = upstream_image_dispatch._provider_supports_image_jobs
_should_use_image_jobs = upstream_image_dispatch._should_use_image_jobs
_image_endpoint_kind_for_engine = (
    upstream_image_dispatch._image_endpoint_kind_for_engine
)
_image_dispatch_candidates = upstream_image_dispatch._image_dispatch_candidates
_run_image_once_for_provider = upstream_image_dispatch._run_image_once_for_provider
_dispatch_image = upstream_image_dispatch._dispatch_image
generate_image = upstream_image_dispatch.generate_image
edit_image = upstream_image_dispatch.edit_image


_iter_sse_with_runtime = upstream_responses_client._iter_sse_with_runtime
_iter_sse = upstream_responses_client._iter_sse
stream_completion = upstream_responses_client.stream_completion


async def responses_call(
    body: dict[str, Any],
    *,
    route: str = "text",
    api_key_override: str | None = None,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    timeout_s: float | None = None,
    endpoint_label: str = "responses",
) -> dict[str, Any]:
    """Run an unowned text Responses call with provider circuit accounting."""
    call = upstream_responses_client.responses_call
    caller_owns_provider = (
        api_key_override is not None and base_url_override is not None
    )
    resolver_is_monkeypatched = _resolve_runtime is not _DEFAULT_RESOLVE_RUNTIME
    if route != "text" or caller_owns_provider or resolver_is_monkeypatched:
        return await call(
            body,
            route=route,
            api_key_override=api_key_override,
            base_url_override=base_url_override,
            proxy_override=proxy_override,
            timeout_s=timeout_s,
            endpoint_label=endpoint_label,
        )

    pool = await provider_pool.get_pool()
    provider = (await pool.select(route="text"))[0]
    with provider_pool.text_provider_attempt(pool, provider) as provider_attempt:
        try:
            payload = await call(
                body,
                route=route,
                api_key_override=provider.api_key,
                base_url_override=provider.base_url,
                proxy_override=getattr(provider, "proxy", None),
                timeout_s=timeout_s,
                endpoint_label=endpoint_label,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            provider_attempt.report_exception(exc)
            raise
        else:
            provider_attempt.report_success()
    return payload


__all__ = [
    "UpstreamError",
    "generate_image",
    "edit_image",
    "stream_completion",
    "responses_call",
    "close_client",
]
