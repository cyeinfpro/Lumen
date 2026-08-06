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

import asyncio  # noqa: F401 - composed infrastructure dependency
import base64  # noqa: F401 - late-bound image-job facade
import hashlib  # noqa: F401 - late-bound image-job facade
import logging
import re
import shutil
import tempfile  # noqa: F401 - compatibility facade for transport tests/hooks
import time  # noqa: F401 - late-bound request facade
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any
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
    endpoint_kind_allowed,  # noqa: F401 - late-bound provider facade
    parse_provider_bool,  # noqa: F401 - late-bound provider facade
)
from lumen_core.providers_parts.selection import (
    provider_supports_route,  # noqa: F401 - late-bound provider facade
)
from lumen_core.url_security import (
    PublicHttpBodyTooLarge,  # noqa: F401 - late-bound request facade
    download_public_http_url,  # noqa: F401 - late-bound request facade
    download_public_http_url_to_file,  # noqa: F401 - late-bound request facade
    pinned_async_http_transport,  # noqa: F401 - late-bound reference facade
    resolve_public_http_target,  # noqa: F401 - late-bound reference facade
)

from .. import http_retry, provider_pool, upstream_image_requests
from ..config import (
    settings,  # noqa: F401 - explicit upstream composition dependency
    validate_image_job_sidecar_token,  # noqa: F401 - composed image-job dependency
)
from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamLifecycleState,
    UpstreamServices,
    bind_upstream_runtime,
    build_upstream_services,
    compose_upstream_namespace,
    resolve_image_upstream_services,
)
from ..provider_runtime.errors import UpstreamCancelled, UpstreamError
from ..provider_runtime.http_headers import upstream_auth_headers
from ..provider_pool_parts.provider_control_plane import (
    image_control_plane_error as _image_control_plane_error,
    legacy_route_to_channel_engine as _legacy_route_to_channel_engine,
    resolve_explicit_image_dispatch_setting,
    resolve_legacy_image_primary_route as resolve_legacy_route_setting,
    validated_image_dispatch_value as _validated_image_dispatch_value,
)
from ..runtime_settings import (
    SettingResolution,
    resolve,  # noqa: F401 - composed infrastructure dependency
    resolve_db,  # noqa: F401 - composed core dependency
)
from . import (
    client_lifecycle as upstream_client_lifecycle,
    direct_failover as upstream_direct_failover,
    direct_images as upstream_direct_images,
    direct_requests as upstream_direct_requests,
    image_dispatch as upstream_image_dispatch,
    image_job_failover as upstream_image_job_failover,
    image_jobs as upstream_image_jobs,
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
from .observability_helpers import (
    log_upstream_call as _log_upstream_call,
    record_usage as _record_usage,
)
from .request_options import (
    add_image_output_options as _add_image_output_options,
    append_transparent_matte_prompt as _append_transparent_matte_prompt,
    image_request_policy as _image_request_policy,
    is_transparent_image_request as _is_transparent_image_request,
    normalize_image_background as _normalize_image_background,
    normalize_image_moderation as _normalize_image_moderation,
    normalize_image_output_compression as _normalize_image_output_compression,
    normalize_image_output_format as _normalize_image_output_format,
    normalize_image_quality as _normalize_image_quality,
    summarize_upstream_error_detail as _summarize_upstream_error_detail,
    transparent_matte_upstream_options as _transparent_matte_upstream_options,
)
from .response_helpers import (
    b64_value_if_str as _b64_value_if_str,
    extract_image_b64_from_payload as _extract_image_b64_from_payload,
    extract_image_billable_count as _extract_image_billable_count,
    extract_image_result as _extract_image_result,
    extract_image_results as _extract_image_results,
    extract_response_image_b64 as _extract_response_image_b64,
    extract_response_revised_prompt as _extract_response_revised_prompt,
    is_responses_error_terminal as _is_responses_error_terminal,
    is_responses_success_terminal as _is_responses_success_terminal,
    parse_error as _parse_error,
    stable_sort_tools as _stable_sort_tools,
    validate_responses_body as _validate_responses_body,
    with_error_context as _with_error_context,
)

# Referenced by the composed public service graph below.
_COMPOSED_INFRASTRUCTURE_DEPENDENCIES = (
    PILImage,
    DEFAULT_IMAGE_INSTRUCTIONS,
    DEFAULT_IMAGE_RESPONSES_MODEL,
    UPSTREAM_MODEL,
)
_COMPOSED_SERVICE_MODULES = (
    upstream_client_lifecycle,
    upstream_direct_failover,
    upstream_direct_images,
    upstream_direct_requests,
    upstream_image_dispatch,
    upstream_image_job_failover,
    upstream_image_jobs,
    upstream_image_race,
    upstream_image_stream,
    upstream_provider_selection,
    upstream_reference_images,
    upstream_request_targets,
    upstream_responses,
    upstream_responses_client,
    upstream_retry_policy,
    upstream_transport,
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
close_provider_proxy_tunnels = provider_pool.close_provider_proxy_tunnels
resolve_provider_proxy_url = provider_pool.resolve_provider_proxy_url


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def _generate_trace_id() -> str:
    """每次上游 HTTP 调用生成一个 x-trace-id，方便和上游下发的 x-request-id 对账。"""
    return uuid.uuid4().hex


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

# Sentinel event：iterator 在 200 但 Content-Type 不是 text/event-stream 时 yield，
# 由 _responses_image_stream 主循环识别并按 JSON 提图。命名带 ``_lumen.`` 前缀，
# 与上游事件类型不会冲突。
_JSON_PAYLOAD_SENTINEL_TYPE = "_lumen.image.json_payload"
# 单条非 SSE JSON body 上限：与单条 SSE 行字节上限一致（32 MB），覆盖 4K PNG b64
# 的 ~11MB 上限并留余量；超出直接 STREAM_TOO_LARGE，避免被巨型 body 撑爆 worker 内存。
# 注意：_SSE_MAX_LINE_BYTES 在文件后面定义，这里只能写字面值（保持两处同步）。
_NON_SSE_JSON_MAX_BYTES = 32 * 1024 * 1024

ImageProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

# GEN-P0-8: 上游图片规范化的严格边界
# 100 MB 原始字节 / 64M 像素 / 100 MB 编码后字节——低于任何已知合理 input.
_MAX_REFERENCE_IMAGE_BYTES = 100 * 1024 * 1024
_MAX_NORMALIZED_IMAGE_BYTES = 100 * 1024 * 1024
_MAX_REFERENCE_IMAGE_PIXELS = 64_000_000


# PIL 默认对 >89M 像素图像抛 DecompressionBombWarning 但不 raise。
# 强制上限到 64M 像素——和 _MAX_REFERENCE_IMAGE_PIXELS 对齐——这样即使 magic bytes
# 绕过 size_bytes 检查（比如 16x 压缩的 PNG），PIL.Image.open 也会直接 DecompressionBombError。
# 必须设为 int，PIL 把 None 当作"无限制"。全进程生效，所以用 max(...) 保证不回退他处更大的值。
def _configure_pil_max_image_pixels(
    services: UpstreamServices,
) -> None:
    try:
        image_api = services.infrastructure.PILImage
        max_pixels = services.core.MAX_REFERENCE_IMAGE_PIXELS
        _pil_current = image_api.MAX_IMAGE_PIXELS or 0
        if _pil_current == 0 or _pil_current > max_pixels:
            image_api.MAX_IMAGE_PIXELS = max_pixels
    except Exception:  # noqa: BLE001
        services.infrastructure.logger.warning(
            "failed to configure PIL MAX_IMAGE_PIXELS=%d",
            services.core.MAX_REFERENCE_IMAGE_PIXELS,
            exc_info=True,
        )


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
_FALLBACK_RETRY_ERROR_CODES = frozenset(
    {
        "no_image_returned",
        "race_no_result",
        "stream_interrupted",
        "sse_curl_failed",
        "stream_too_large",
    }
)
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
_client_timeout_config: Any | None = None
_PROXIED_CLIENT_CACHE_MAX = 32
_PROXIED_CLIENT_CLOSE_DELAY_SECONDS = 30.0
_PROXIED_CLIENT_IDLE_CLOSE_TIMEOUT_SECONDS = 30 * 60.0
_proxied_clients: OrderedDict[tuple[Any, str], httpx.AsyncClient] = OrderedDict()
# 专供 /v1/images/* 使用的 client：不设默认 content-type，让 httpx 根据 files
# 自动生成 multipart boundary；JSON 请求则显式传 json= 由 httpx 自己设 header。
_images_client: httpx.AsyncClient | None = None
_images_client_timeout_config: Any | None = None
_proxied_images_clients: OrderedDict[tuple[Any, str], httpx.AsyncClient] = OrderedDict()

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
_IMAGE_CHANNELS = frozenset(
    {
        _IMAGE_CHANNEL_AUTO,
        _IMAGE_CHANNEL_STREAM_ONLY,
        _IMAGE_CHANNEL_IMAGE_JOBS_ONLY,
    }
)
_IMAGE_ROUTE_RESPONSES = "responses"
_IMAGE_ROUTE_IMAGE2 = "image2"
_IMAGE_ROUTE_IMAGE_JOBS = "image_jobs"
_IMAGE_ROUTE_DUAL_RACE = "dual_race"
_IMAGE_ENGINES = frozenset(
    {
        _IMAGE_ROUTE_RESPONSES,
        _IMAGE_ROUTE_IMAGE2,
        _IMAGE_ROUTE_DUAL_RACE,
    }
)
# 兼容性别名（保留，避免外部引用 / 历史测试断言失败）
_TEXT_TO_IMAGE_PRIMARY_ROUTE_KEY = _IMAGE_PRIMARY_ROUTE_LEGACY_KEY
_TEXT_TO_IMAGE_ROUTE_RESPONSES = _IMAGE_ROUTE_RESPONSES
_TEXT_TO_IMAGE_ROUTE_IMAGE2 = _IMAGE_ROUTE_IMAGE2
_IMAGE_OUTPUT_FORMATS = frozenset({"png", "jpeg", "webp"})
_IMAGE_BACKGROUNDS = frozenset({"auto", "opaque", "transparent"})
_IMAGE_MODERATIONS = frozenset({"auto", "low"})
_IMAGE_QUALITIES = frozenset({"auto", "low", "medium", "high"})
# 实测 OpenAI codex 端 image_generation 工具的 `output_compression` 参数实际不生效——
# 设 100（应该等同 quality 100）输出仍有明显 JPEG 压缩痕迹；同 prompt 切到 PNG 干净无痕迹。
# 因此默认走 PNG（无损）。代价是 4K PNG 体积大（~10MB base64），SSE 流时长长；
# 但 retry-buster（attempt>1 时注入 prompt_cache_key + effort 轮转 + 关 partial_images）
# 已经能把断流场景接住，PNG 路径 reliability 不再是问题。
_DEFAULT_IMAGE_OUTPUT_FORMAT = "png"
# output_compression 仅对 jpeg/webp 生效；PNG 路径下不会进入 body。保留 100 以备显式切 jpeg/webp。
_DEFAULT_IMAGE_OUTPUT_COMPRESSION = 100


def _apply_retry_cache_busters(
    body: dict[str, Any],
    retry_attempt: int,
    prompt: str,
    size: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
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
    services = _runtime_services(runtime)
    services.requests.apply_retry_cache_busters(
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


async def close_client(*, runtime: ImageUpstreamRuntime) -> None:
    await upstream_client_lifecycle.close_client(runtime=runtime)


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


async def _explicit_image_dispatch_setting(
    key: str,
    env_name: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> SettingResolution:
    services = _runtime_services(runtime)
    return await resolve_explicit_image_dispatch_setting(
        services.core.resolve_db,
        key,
        env_name,
        logger=logger,
    )


async def _resolve_legacy_image_primary_route(
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    services = _runtime_services(runtime)
    return await resolve_legacy_route_setting(
        services.infrastructure.resolve,
        keys=(_IMAGE_PRIMARY_ROUTE_KEY, _IMAGE_PRIMARY_ROUTE_LEGACY_KEY),
        allowed=frozenset(
            {
            _IMAGE_ROUTE_RESPONSES,
            _IMAGE_ROUTE_IMAGE2,
            _IMAGE_ROUTE_IMAGE_JOBS,
            _IMAGE_ROUTE_DUAL_RACE,
            }
        ),
        logger=logger,
    )


async def _has_explicit_image_dispatch_setting(
    key: str,
    env_name: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    resolution = await _explicit_image_dispatch_setting(
        key,
        env_name,
        runtime=runtime,
    )
    if resolution.state == "unavailable":
        raise _image_control_plane_error(key)
    return resolution.state == "value"


async def _resolve_image_channel(
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    """Resolve async channel strategy with legacy primary_route fallback."""
    resolution = await _explicit_image_dispatch_setting(
        _IMAGE_CHANNEL_KEY,
        "IMAGE_CHANNEL",
        runtime=runtime,
    )
    channel = _validated_image_dispatch_value(
        resolution,
        key=_IMAGE_CHANNEL_KEY,
        allowed=_IMAGE_CHANNELS,
    )
    if channel is not None:
        return channel

    legacy_route = await _resolve_legacy_image_primary_route(runtime=runtime)
    legacy_channel, _legacy_engine = _legacy_route_to_channel_engine(legacy_route)
    return legacy_channel


async def _resolve_image_engine(
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    """Resolve image engine with legacy primary_route fallback."""
    resolution = await _explicit_image_dispatch_setting(
        _IMAGE_ENGINE_KEY,
        "IMAGE_ENGINE",
        runtime=runtime,
    )
    engine = _validated_image_dispatch_value(
        resolution,
        key=_IMAGE_ENGINE_KEY,
        allowed=_IMAGE_ENGINES,
    )
    if engine is not None:
        return engine

    legacy_route = await _resolve_legacy_image_primary_route(runtime=runtime)
    _legacy_channel, legacy_engine = _legacy_route_to_channel_engine(legacy_route)
    return legacy_engine


async def resolve_image_primary_route(
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    """Route label for queueing and admin metadata.

    New dispatch uses ``image.channel`` + ``image.engine``. This function keeps
    the old route-ish return values where possible so queueing and admin
    metadata can continue to treat dual_race specially.
    """
    channel = await _resolve_image_channel(runtime=runtime)
    engine = await _resolve_image_engine(runtime=runtime)
    if engine == _IMAGE_ROUTE_DUAL_RACE:
        return _IMAGE_ROUTE_DUAL_RACE
    if channel == _IMAGE_CHANNEL_IMAGE_JOBS_ONLY:
        return _IMAGE_ROUTE_IMAGE_JOBS
    if engine == _IMAGE_ROUTE_IMAGE2:
        return _IMAGE_ROUTE_IMAGE2
    return _IMAGE_ROUTE_RESPONSES


def _auth_headers(
    api_key: str,
    *,
    trace_id: str | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> dict[str, str]:
    """构造 outbound headers。

    新增字段：
    - `originator`: lumen-prod-{version}，让上游和我们都能在日志里识别请求来源
    - `x-trace-id`: 调用方自生成的 uuid4，用于对账上游下发的 `x-request-id`
    传入 trace_id=None 时由本函数自动生成；调用方需要事后日志记录时可显式传同一个值。
    """
    del runtime
    return upstream_auth_headers(api_key, trace_id=trace_id)


def _json_dumps_stable(
    value: Any,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = _runtime_services(runtime)
    return services.requests.json_dumps_stable(value)


def _image_file_fingerprints(
    files: list[tuple[str, tuple[str, bytes, str]]] | None,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> list[dict[str, Any]]:
    services = _runtime_services(runtime)
    return services.requests.image_file_fingerprints(files)


def _image_idempotency_key(
    *,
    trace_id: str,
    endpoint: str,
    body: dict[str, Any] | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = _runtime_services(runtime)
    return services.requests.image_idempotency_key(
        trace_id=trace_id,
        endpoint=endpoint,
        body=body,
        files=files,
        hooks=upstream_image_requests.ImageIdempotencyKeyHooks(
            json_dumps_stable=services.core.json_dumps_stable,
            image_file_fingerprints=services.core.image_file_fingerprints,
        ),
    )


def _attach_image_idempotency_key(
    headers: dict[str, str],
    *,
    trace_id: str,
    endpoint: str,
    body: dict[str, Any] | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = _runtime_services(runtime)
    services.requests.attach_image_idempotency_key(
        headers,
        trace_id=trace_id,
        endpoint=endpoint,
        body=body,
        files=files,
        hooks=upstream_image_requests.AttachImageIdempotencyKeyHooks(
            image_idempotency_key=services.core.image_idempotency_key,
        ),
    )


# 图生图 multipart 走 curl 子进程——实测在同一台服务器上 httpx.AsyncClient 发出
# 同样 body 被上游网关持续 502，但 curl 命令发同样请求能 200 出图。原因尚未定位
# （怀疑 httpx 的 multipart boundary / header 组合触发了网关某条规则）。
# 绕法：edit 路径只用 curl，保留 retry + fallback 语义。
_CURL_BIN = shutil.which("curl") or "/usr/bin/curl"


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


# ---- image retry / failover policy ----

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

generate_image = upstream_image_dispatch.generate_image
edit_image = upstream_image_dispatch.edit_image


validate_effective_image_job_configuration = (
    upstream_image_jobs.validate_effective_image_job_configuration
)


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
    runtime: ImageUpstreamRuntime,
) -> dict[str, Any]:
    """Run an unowned text Responses call with provider circuit accounting."""
    services = runtime.services
    call = services.responses.responses_client_call
    caller_owns_provider = (
        api_key_override is not None and base_url_override is not None
    )
    resolver_is_injected = (
        services.core.resolve_runtime is not services.core.DEFAULT_RESOLVE_RUNTIME
    )
    if route != "text" or caller_owns_provider or resolver_is_injected:
        return await call(
            body,
            route=route,
            api_key_override=api_key_override,
            base_url_override=base_url_override,
            proxy_override=proxy_override,
            timeout_s=timeout_s,
            endpoint_label=endpoint_label,
        )

    pool = await services.infrastructure.provider_pool.get_pool()
    provider = (await pool.select(route="text"))[0]
    with services.infrastructure.provider_pool.text_provider_attempt(
        pool,
        provider,
    ) as provider_attempt:
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
        except services.infrastructure.asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            provider_attempt.report_exception(exc)
            raise
        else:
            provider_attempt.report_success()
    return payload


def build_image_upstream_runtime() -> ImageUpstreamRuntime:
    namespace = compose_upstream_namespace(
        core_values=(
            _DEFAULT_RESOLVE_RUNTIME,
            _CURL_BIN,
            _DEFAULT_IMAGE_BACKGROUND,
            DEFAULT_IMAGE_INSTRUCTIONS,
            _DEFAULT_IMAGE_JOB_BASE_URL,
            _DEFAULT_IMAGE_MODERATION,
            _DEFAULT_IMAGE_OUTPUT_COMPRESSION,
            _DEFAULT_IMAGE_OUTPUT_FORMAT,
            DEFAULT_IMAGE_RESPONSES_MODEL,
            _DUAL_RACE_BONUS_GRACE_4K_S,
            _DUAL_RACE_BONUS_GRACE_S,
            _DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_4K_S,
            _DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S,
            _FALLBACK_429_DEFAULT_WAIT_S,
            _FALLBACK_429_MAX_WAIT_S,
            _FALLBACK_MAX_ATTEMPTS,
            _FALLBACK_MAX_ATTEMPTS_429,
            _FALLBACK_MAX_ATTEMPTS_4XX,
            _FALLBACK_MAX_ATTEMPTS_5XX,
            _FALLBACK_RETRY_BACKOFF_BASE_S,
            _FALLBACK_RETRY_BACKOFF_MAX_S,
            _FALLBACK_RETRY_ERROR_CODES,
            _IMAGE_4K_PIXELS,
            _IMAGE_BACKGROUNDS,
            _IMAGE_CHANNEL_AUTO,
            _IMAGE_CHANNEL_IMAGE_JOBS_ONLY,
            _IMAGE_CHANNEL_STREAM_ONLY,
            _IMAGE_JOB_DOWNLOAD_MAX_BYTES,
            _IMAGE_JOB_FAILOVER_CLASSES,
            _IMAGE_JOB_POLL_INTERVAL_S,
            _IMAGE_JOB_RETENTION_DAYS,
            _IMAGE_JOB_TIMEOUT_S,
            _IMAGE_MODERATIONS,
            _IMAGE_OUTPUT_FORMATS,
            _IMAGE_PROVIDER_FAILOVER_ERROR_CODES,
            _IMAGE_QUALITIES,
            _IMAGE_READ_TIMEOUT_4K_S,
            _IMAGE_READ_TIMEOUT_MIN_S,
            _IMAGE_ROUTE_DUAL_RACE,
            _IMAGE_ROUTE_IMAGE2,
            _IMAGE_ROUTE_RESPONSES,
            _JSON_PAYLOAD_SENTINEL_TYPE,
            _KNOWN_OUTPUT_ITEM_TYPES,
            _MAX_REFERENCE_IMAGE_PIXELS,
            _MAX_NORMALIZED_IMAGE_BYTES,
            _MAX_REFERENCE_IMAGE_BYTES,
            _NON_SSE_JSON_MAX_BYTES,
            _PARTIAL_IMAGES_MAX_PIXELS,
            _PROXIED_CLIENT_CACHE_MAX,
            _PROXIED_CLIENT_CLOSE_DELAY_SECONDS,
            _PROXIED_CLIENT_IDLE_CLOSE_TIMEOUT_SECONDS,
            _RACE_CANCEL_WAIT_S,
            _RACE_SINGLE_LANE_PIXELS,
            _REFERENCE_CACHE_HEAD_TIMEOUT_S,
            _REFERENCE_CACHE_KEY_PREFIX,
            _REFERENCE_CACHE_LRU_SUFFIX,
            _REFERENCE_CACHE_MAX_ENTRIES,
            _REFERENCE_CACHE_TTL_S,
            _REFERENCE_PUSH_TIMEOUT_S,
            _RETRY_HTTPX_EXC,
            _RETRY_STATUS,
            _SAFETY_POLICY_ERROR_MARKERS,
            _SSE_MAX_BYTES,
            _SSE_MAX_LINES,
            _SSE_MAX_LINE_BYTES,
            _TEXT_STREAM_INTERRUPTED_ERROR_CODE,
            _TRANSPARENT_MATTE_PROMPT_NOTE,
            _add_image_output_options,
            _append_transparent_matte_prompt,
            _apply_retry_cache_busters,
            _attach_image_idempotency_key,
            _auth_headers,
            _b64_value_if_str,
            _client,
            _client_timeout_config,
            _configure_pil_max_image_pixels,
            _extract_image_b64_from_payload,
            _extract_image_billable_count,
            _extract_image_result,
            _extract_image_results,
            _extract_response_image_b64,
            _extract_response_revised_prompt,
            _has_explicit_image_dispatch_setting,
            _image_file_fingerprints,
            _image_idempotency_key,
            _image_request_policy,
            _images_client,
            _images_client_timeout_config,
            _generate_trace_id,
            _is_transparent_image_request,
            _is_responses_error_terminal,
            _is_responses_success_terminal,
            _json_dumps_stable,
            _log_upstream_call,
            _normalize_image_background,
            _normalize_image_moderation,
            _normalize_image_output_compression,
            _normalize_image_output_format,
            _normalize_image_quality,
            _parse_error,
            _parse_retry_after_seconds,
            _post_with_retry,
            _provider_proxy,
            _proxied_clients,
            _proxied_images_clients,
            _record_usage,
            _resolve_image_channel,
            _resolve_image_engine,
            _resolve_legacy_image_primary_route,
            _resolve_runtime,
            _runtime_parts,
            _runtime_provider_name,
            _stable_sort_tools,
            _summarize_upstream_error_detail,
            _transparent_matte_upstream_options,
            _validate_responses_body,
            _with_error_context,
            resolve_db,
            resolve_image_primary_route,
            responses_call,
            tempfile,
        ),
        infrastructure_values=(
            EC,
            PILImage,
            PublicHttpBodyTooLarge,
            UPSTREAM_MODEL,
            UnidentifiedImageError,
            UpstreamCancelled,
            UpstreamError,
            asyncio,
            base64,
            close_provider_proxy_tunnels,
            download_public_http_url,
            download_public_http_url_to_file,
            endpoint_kind_allowed,
            hashlib,
            httpx,
            logger,
            parse_provider_bool,
            pinned_async_http_transport,
            provider_pool,
            provider_supports_route,
            resolve,
            resolve_provider_proxy_url,
            resolve_public_http_target,
            settings,
            time,
            upstream_image_requests,
            validate_image_job_sidecar_token,
        ),
        module_values=(
            upstream_client_lifecycle,
            upstream_direct_failover,
            upstream_direct_images,
            upstream_direct_requests,
            upstream_image_dispatch,
            upstream_image_job_failover,
            upstream_image_jobs,
            upstream_image_race,
            upstream_image_stream,
            upstream_provider_selection,
            upstream_reference_images,
            upstream_request_targets,
            upstream_responses,
            upstream_responses_client,
            upstream_retry_policy,
            upstream_transport,
        ),
        lifecycle_state=UpstreamLifecycleState.create(),
    )
    runtime = ImageUpstreamRuntime(build_upstream_services(namespace))
    services = runtime.services
    _configure_pil_max_image_pixels(services)
    services.core.configure_pil_max_image_pixels = partial(
        _configure_pil_max_image_pixels,
        services,
    )
    return bind_upstream_runtime(runtime)


__all__ = [
    "UpstreamError",
    "generate_image",
    "edit_image",
    "stream_completion",
    "responses_call",
    "build_image_upstream_runtime",
    "close_client",
    "validate_effective_image_job_configuration",
]
