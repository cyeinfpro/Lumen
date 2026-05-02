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
import inspect
import io
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image as PILImage, UnidentifiedImageError

from lumen_core.constants import (
    DEFAULT_IMAGE_INSTRUCTIONS,
    DEFAULT_IMAGE_RESPONSES_MODEL,
    GenerationErrorCode as EC,
    UPSTREAM_MODEL,
)
from lumen_core.providers import (
    ProviderProxyDefinition,
    close_provider_proxy_tunnels,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
)

from .config import settings
from .runtime_settings import resolve, resolve_db
from .validation import (
    ProviderBaseUrlValidationError,
    validate_provider_base_url,
)
from . import provider_pool

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


def _generate_trace_id() -> str:
    """每次上游 HTTP 调用生成一个 x-trace-id，方便和上游下发的 x-request-id 对账。"""
    return uuid.uuid4().hex


# ---- 已知 SSE output[].type 白名单 ----
# 解析 SSE 帧或 compact JSON 时未知 type 仅 warning + 跳过，不抛 KeyError 让整条流挂掉。
_KNOWN_OUTPUT_ITEM_TYPES = frozenset({
    "message",
    "reasoning",
    "function_call",
    "compaction_summary",
    "tool_call",
    "web_search_call",
    "file_search_call",
    "code_interpreter_call",
    "image_generation_call",  # /v1/responses + image_generation 工具的 item 类型
})

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
_FALLBACK_MAX_ATTEMPTS_5XX = 5
_FALLBACK_MAX_ATTEMPTS_429 = 5
_FALLBACK_MAX_ATTEMPTS_4XX = 1  # 401/403/404/422 等终态错误，重试无意义
# GEN-P0-9: fallback 层重试指数退避。base*2^attempt，最大 8s 避免叠加 race*lane 预算爆炸。
_FALLBACK_RETRY_BACKOFF_BASE_S = 1.0
_FALLBACK_RETRY_BACKOFF_MAX_S = 8.0
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


# 单例 client——进程内复用连接池
_client: httpx.AsyncClient | None = None
_client_timeout_config: "_TimeoutConfig | None" = None
_proxied_clients: dict[tuple["_TimeoutConfig", str], httpx.AsyncClient] = {}
# 专供 /v1/images/* 使用的 client：不设默认 content-type，让 httpx 根据 files
# 自动生成 multipart boundary；JSON 请求则显式传 json= 由 httpx 自己设 header。
_images_client: httpx.AsyncClient | None = None
_images_client_timeout_config: "_TimeoutConfig | None" = None
_proxied_images_clients: dict[tuple["_TimeoutConfig", str], httpx.AsyncClient] = {}
_client_lock = asyncio.Lock()
_images_client_lock = asyncio.Lock()

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
    if retry_attempt <= 1:
        return
    seed = hashlib.md5(
        f"{prompt[:200]}|{size}|{retry_attempt}".encode("utf-8")
    ).hexdigest()[:16]
    body["prompt_cache_key"] = f"lumen-retry-{seed}"
    rotation = ("medium", "minimal", "high", "minimal")
    body["reasoning"] = {
        "effort": rotation[(retry_attempt - 1) % len(rotation)],
        "summary": "auto",
    }
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "image_generation":
            tool.pop("partial_images", None)
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


def _summarize_upstream_error_detail(detail: dict[str, Any] | None) -> dict[str, Any] | str:
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


def _normalize_image_quality(value: str | None) -> str:
    return value if isinstance(value, str) and value in _IMAGE_QUALITIES else "high"


def _normalize_image_output_format(value: str | None) -> str:
    return (
        value
        if isinstance(value, str) and value in _IMAGE_OUTPUT_FORMATS
        else _DEFAULT_IMAGE_OUTPUT_FORMAT
    )


def _normalize_image_output_compression(
    value: int | None,
    *,
    output_format: str,
) -> int | None:
    if output_format not in {"jpeg", "webp"}:
        return None
    if value is None:
        return _DEFAULT_IMAGE_OUTPUT_COMPRESSION
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return _DEFAULT_IMAGE_OUTPUT_COMPRESSION


def _normalize_image_background(value: str | None) -> str:
    return (
        value
        if isinstance(value, str) and value in _IMAGE_BACKGROUNDS
        else _DEFAULT_IMAGE_BACKGROUND
    )


def _normalize_image_moderation(value: str | None) -> str:
    return (
        value
        if isinstance(value, str) and value in _IMAGE_MODERATIONS
        else _DEFAULT_IMAGE_MODERATION
    )


def _add_image_output_options(
    body: dict[str, Any],
    *,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
) -> None:
    bg = _normalize_image_background(background)
    fmt = _normalize_image_output_format(output_format)
    if bg == "transparent":
        fmt = "png"
    body["output_format"] = fmt
    compression = _normalize_image_output_compression(
        output_compression,
        output_format=fmt,
    )
    if compression is not None:
        body["output_compression"] = compression
    body["background"] = bg
    body["moderation"] = _normalize_image_moderation(moderation)


def _is_transparent_image_request(background: str | None) -> bool:
    return _normalize_image_background(background) == "transparent"


def _append_transparent_matte_prompt(prompt: str) -> str:
    prompt_stripped = prompt.rstrip()
    if _TRANSPARENT_MATTE_PROMPT_NOTE in prompt_stripped:
        return prompt_stripped
    if not prompt_stripped:
        return _TRANSPARENT_MATTE_PROMPT_NOTE
    return f"{prompt_stripped}\n\n{_TRANSPARENT_MATTE_PROMPT_NOTE}"


def _transparent_matte_upstream_options(
    *,
    prompt: str,
    output_format: str | None,
    background: str | None,
) -> tuple[str, str | None, str | None]:
    if not _is_transparent_image_request(background):
        return prompt, output_format, background
    return (
        _append_transparent_matte_prompt(prompt),
        "png",
        "opaque",
    )


@dataclass(frozen=True)
class _TimeoutConfig:
    connect: float
    read: float
    write: float

    def to_httpx(self, *, read: float | None = None) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.read if read is None else read,
            write=self.write,
            pool=self.connect,
        )


async def _resolve_timeout_config() -> _TimeoutConfig:
    async def _resolve_float(spec_key: str, fallback: float) -> float:
        try:
            raw = await resolve(spec_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("runtime timeout setting fallback key=%s err=%s", spec_key, exc)
            return fallback
        if raw is None:
            return fallback
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning("invalid runtime timeout setting key=%s value=%r", spec_key, raw)
            return fallback
        if not math.isfinite(value) or value <= 0:
            logger.warning("invalid runtime timeout setting key=%s value=%r", spec_key, raw)
            return fallback
        return value

    return _TimeoutConfig(
        connect=await _resolve_float(
            "upstream.connect_timeout_s", settings.upstream_connect_timeout_s
        ),
        read=await _resolve_float(
            "upstream.read_timeout_s", settings.upstream_read_timeout_s
        ),
        write=await _resolve_float(
            "upstream.write_timeout_s", settings.upstream_write_timeout_s
        ),
    )


def _build_client(
    timeout_config: _TimeoutConfig | None = None,
    *,
    proxy_url: str | None = None,
) -> httpx.AsyncClient:
    """没有 base_url / authorization 的 client；这俩在每次请求时按需注入。"""
    timeout_config = timeout_config or _TimeoutConfig(
        connect=settings.upstream_connect_timeout_s,
        read=settings.upstream_read_timeout_s,
        write=settings.upstream_write_timeout_s,
    )
    return httpx.AsyncClient(
        timeout=timeout_config.to_httpx(),
        headers={"content-type": "application/json"},
        proxy=proxy_url,
    )


def _build_images_client(
    timeout_config: _TimeoutConfig | None = None,
    *,
    proxy_url: str | None = None,
) -> httpx.AsyncClient:
    """供 images API 使用的 client——不设默认 content-type。"""
    timeout_config = timeout_config or _TimeoutConfig(
        connect=settings.upstream_connect_timeout_s,
        read=settings.upstream_read_timeout_s,
        write=settings.upstream_write_timeout_s,
    )
    return httpx.AsyncClient(timeout=timeout_config.to_httpx(), proxy=proxy_url)


async def _get_client(proxy_url: str | None = None) -> httpx.AsyncClient:
    global _client, _client_timeout_config
    timeout_config = await _resolve_timeout_config()
    if proxy_url:
        key = (timeout_config, proxy_url)
        client = _proxied_clients.get(key)
        if client is None:
            async with _client_lock:
                client = _proxied_clients.get(key)
                if client is None:
                    client = _build_client(timeout_config, proxy_url=proxy_url)
                    _proxied_clients[key] = client
        return client
    if _client is None or _client_timeout_config != timeout_config:
        async with _client_lock:
            if _client is None or _client_timeout_config != timeout_config:
                old_client = _client
                _client = _build_client(timeout_config)
                _client_timeout_config = timeout_config
                if old_client is not None:
                    await old_client.aclose()
    return _client


async def _get_images_client(proxy_url: str | None = None) -> httpx.AsyncClient:
    global _images_client, _images_client_timeout_config
    timeout_config = await _resolve_timeout_config()
    if proxy_url:
        key = (timeout_config, proxy_url)
        client = _proxied_images_clients.get(key)
        if client is None:
            async with _images_client_lock:
                client = _proxied_images_clients.get(key)
                if client is None:
                    client = _build_images_client(timeout_config, proxy_url=proxy_url)
                    _proxied_images_clients[key] = client
        return client
    if _images_client is None or _images_client_timeout_config != timeout_config:
        async with _images_client_lock:
            if _images_client is None or _images_client_timeout_config != timeout_config:
                old_client = _images_client
                _images_client = _build_images_client(timeout_config)
                _images_client_timeout_config = timeout_config
                if old_client is not None:
                    await old_client.aclose()
    return _images_client


async def close_client() -> None:
    """Worker shutdown 钩子可调用此方法关闭连接池。"""
    global _client, _images_client, _client_timeout_config, _images_client_timeout_config
    if _client is not None:
        await _client.aclose()
        _client = None
        _client_timeout_config = None
    for client in list(_proxied_clients.values()):
        await client.aclose()
    _proxied_clients.clear()
    if _images_client is not None:
        await _images_client.aclose()
        _images_client = None
        _images_client_timeout_config = None
    for client in list(_proxied_images_clients.values()):
        await client.aclose()
    _proxied_images_clients.clear()
    await close_provider_proxy_tunnels()


@dataclass(frozen=True)
class _ResolvedRuntime:
    base_url: str
    api_key: str
    proxy: ProviderProxyDefinition | None = None

    def __iter__(self):
        yield self.base_url
        yield self.api_key


async def _resolve_runtime() -> _ResolvedRuntime:
    """读 provider pool 返回最优 provider 的 (base_url, api_key)。"""
    pool = await provider_pool.get_pool()
    p = await pool.select_one()
    return _ResolvedRuntime(p.base_url, p.api_key, p.proxy)


def _provider_proxy(provider: Any) -> ProviderProxyDefinition | None:
    proxy = getattr(provider, "proxy", None)
    return proxy if isinstance(proxy, ProviderProxyDefinition) else None


def _runtime_parts(
    runtime: Any,
) -> tuple[str, str, ProviderProxyDefinition | None]:
    base_url, api_key = runtime
    proxy = getattr(runtime, "proxy", None)
    return (
        str(base_url),
        str(api_key),
        proxy if isinstance(proxy, ProviderProxyDefinition) else None,
    )


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
        record_upstream_duration(seconds=max(0.0, duration_ms / 1000.0), endpoint=endpoint)
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
    if not isinstance(tools, list):
        return tools

    def _key(t: Any) -> tuple[int, str]:
        if not isinstance(t, dict):
            return (1, "")
        name = t.get("name") or t.get("type") or ""
        return (0 if name else 1, str(name))

    return sorted(tools, key=_key)


class UpstreamError(Exception):
    """上游错误的统一包装，带上 HTTP status / error code，便于重试判定。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.payload = payload or {}


class UpstreamCancelled(BaseException):
    """GEN-P1-4: 调用方在 progress callback 中表达"取消"——上游路径需要立即终止
    所有 race lane / fallback 重试，而不是当作普通错误重试。继承 BaseException 才能
    穿透各层 `except Exception` 的兜底。"""


async def _validate_provider_base_url(raw_base: str) -> str:
    try:
        return await validate_provider_base_url(raw_base)
    except ProviderBaseUrlValidationError as exc:
        raise UpstreamError(
            str(exc),
            status_code=exc.status_code,
            error_code=exc.error_code,
            payload=exc.payload,
        ) from exc


def _parse_error(payload: dict[str, Any], status_code: int) -> UpstreamError:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or "upstream_error"
        msg = err.get("message") or "upstream error"
        return UpstreamError(msg, status_code=status_code, error_code=code, payload=payload)
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


def _extract_image_result(
    payload: Any, status_code: int
) -> tuple[str, str | None]:
    """从 images API 响应体里抽出 (b64_json, revised_prompt?)。缺失则抛 UpstreamError。"""
    if not isinstance(payload, dict):
        raise UpstreamError(
            "upstream returned non-object",
            status_code=status_code,
            error_code=EC.BAD_RESPONSE.value,
        )
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise UpstreamError(
            "upstream returned no image",
            status_code=status_code,
            error_code=EC.NO_IMAGE_RETURNED.value,
            payload=payload,
        )
    first = data[0]
    if not isinstance(first, dict):
        raise UpstreamError(
            "upstream returned no image",
            status_code=status_code,
            error_code=EC.NO_IMAGE_RETURNED.value,
            payload=payload,
        )
    b64 = first.get("b64_json")
    if not isinstance(b64, str) or not b64:
        raise UpstreamError(
            "upstream returned no image",
            status_code=status_code,
            error_code=EC.NO_IMAGE_RETURNED.value,
            payload=payload,
        )
    revised = first.get("revised_prompt")
    if not isinstance(revised, str):
        revised = None
    return b64, revised


def _api_base(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/v1"):
        return base
    return base + "/v1"


def _responses_url(base: str) -> str:
    return _api_base(base) + "/responses"


def _image_generations_url(base: str) -> str:
    return _api_base(base) + "/images/generations"


def _image_edits_url(base: str) -> str:
    return _api_base(base) + "/images/edits"


def _image_jobs_url(base: str) -> str:
    return _api_base(base) + "/image-jobs"


def _image_job_status_url(base: str, job_id: str) -> str:
    return f"{_image_jobs_url(base)}/{quote(job_id, safe='')}"


def _validate_image_job_base_url(raw_base: str) -> str:
    base = (raw_base or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise UpstreamError(
            "image job base URL must be an http or https URL with a hostname",
            status_code=400,
            error_code=EC.INVALID_VALUE.value,
            payload={"base_url": raw_base},
        )
    if parts.username or parts.password:
        raise UpstreamError(
            "image job base URL must not include credentials",
            status_code=400,
            error_code=EC.INVALID_VALUE.value,
            payload={"base_url": raw_base},
        )
    if parts.query or parts.fragment:
        raise UpstreamError(
            "image job base URL must not include query or fragment",
            status_code=400,
            error_code=EC.INVALID_VALUE.value,
            payload={"base_url": raw_base},
        )
    return base


async def _resolve_image_job_base_url() -> str:
    try:
        raw = await resolve("image.job_base_url")
    except Exception as exc:  # noqa: BLE001
        logger.debug("image job base URL setting fallback err=%s", exc)
        raw = None
    return _validate_image_job_base_url(raw or _DEFAULT_IMAGE_JOB_BASE_URL)


# 主链路临时性错误重试策略。
# 上游网关对 4K 图生图偶发返回 502 "Upstream request failed"（后端 backend 抖动），
# 直接真测 curl 同样请求 93s 能成功——所以策略：先在主链路重试，耗尽才降级到备用。
# 否则"一抖就跑去备用"，而备用走 chat+image_tool 反而更脆弱。
_RETRY_STATUS = {502, 503, 504}
_RETRY_HTTPX_EXC: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


async def _post_with_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    max_attempts: int = 2,  # 和 curl 版对齐；上游网关失败每次 ~80s
    backoff_base_s: float = 1.0,
) -> httpx.Response:
    """对主链路 POST 做有界重试。

    - httpx ConnectError/ReadTimeout/WriteTimeout/PoolTimeout/RemoteProtocolError → 重试
    - HTTP 502/503/504 → 重试
    - 其他情况（非 retriable httpx / 其他 status）→ 直接返回/抛出，交给调用方处理

    backoff: 1s, 2s（指数退避）。attempts 耗尽仍失败时，如有 last_resp 则返回（让
    调用方用 _parse_error 转 UpstreamError → 走 fallback），否则重抛 last_exc。
    """
    last_exc: BaseException | None = None
    last_resp: httpx.Response | None = None
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(backoff_base_s * (2 ** (attempt - 1)))
        try:
            if json_body is not None:
                resp = await client.post(url, json=json_body, headers=headers)
            else:
                resp = await client.post(
                    url, data=data, files=files, headers=headers
                )
        except _RETRY_HTTPX_EXC as exc:
            last_exc = exc
            logger.warning(
                "upstream transient httpx error attempt=%d/%d url=%s err=%r",
                attempt + 1, max_attempts, url, exc,
            )
            continue
        if resp.status_code in _RETRY_STATUS:
            last_resp = resp
            logger.warning(
                "upstream transient status attempt=%d/%d url=%s status=%d",
                attempt + 1, max_attempts, url, resp.status_code,
            )
            continue
        return resp
    if last_resp is not None:
        return last_resp
    assert last_exc is not None
    raise last_exc


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
) -> tuple[str, str | None]:
    """Text-to-image via direct `/v1/images/generations` using gpt-image-2."""
    proxy_url = await resolve_provider_proxy_url(proxy_override)
    client = await (_get_images_client(proxy_url) if proxy_url else _get_images_client())
    url = _image_generations_url(base_url_override)
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
    started = time.monotonic()
    try:
        resp = await _post_with_retry(
            client=client,
            url=url,
            headers=headers,
            json_body=body,
        )
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
            _parse_error(payload if isinstance(payload, dict) else {}, resp.status_code),
            path="images/generations",
            method="POST",
            url=url,
        )
    # JSON 响应里的 usage（如有）也走标准埋点。
    if isinstance(payload, dict):
        _record_usage(payload.get("usage"))
    return _extract_image_result(payload, resp.status_code)


async def _direct_edit_image_once(
    *,
    prompt: str,
    size: str,
    images: list[bytes],
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    base_url_override: str,
    api_key_override: str,
    proxy_override: ProviderProxyDefinition | None = None,
) -> tuple[str, str | None]:
    """Image-to-image via direct `/v1/images/edits` (multipart) using gpt-image-2.

    image2 模式下 i2i 的单次调用。多个 ref 图通过 multipart 字段名 `image[]` 上传，
    与上游 OpenAI /v1/images/edits 协议一致。复用 `_curl_post_multipart`（见
    "图生图 multipart 走 curl 子进程" 那段注释，httpx 的 multipart 在某些上游网关下
    会持续 502，curl 反而能 200）。
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

    trace_id = _generate_trace_id()
    headers = _auth_headers(api_key_override, trace_id=trace_id)
    timeout_config = await _resolve_timeout_config()
    started = time.monotonic()
    try:
        status, payload = await _curl_post_multipart(
            url=url,
            data=data,
            files=files,
            headers=headers,
            timeout_s=timeout_config.read,
            proxy_url=await resolve_provider_proxy_url(proxy_override),
        )
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
    return _extract_image_result(payload, status)


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
        "quality": _normalize_image_quality(quality),
        "n": n,
    }
    _add_image_output_options(
        body,
        output_format=output_format_for_upstream,
        output_compression=output_compression,
        background=background_for_upstream,
        moderation=moderation,
    )
    return body


def _image_job_payload(
    *,
    request_type: str,
    endpoint: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    return {
        "request_type": request_type,
        "endpoint": endpoint,
        "body": body,
        "retention_days": _IMAGE_JOB_RETENTION_DAYS,
    }


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
    assert UPSTREAM_MODEL, "model must be set"
    image_model = model or DEFAULT_IMAGE_RESPONSES_MODEL
    assert image_model, "model must be set"
    image_quality = _normalize_image_quality(quality)
    prompt_for_upstream, output_format_for_upstream, background_for_upstream = (
        _transparent_matte_upstream_options(
            prompt=prompt,
            output_format=output_format,
            background=background,
        )
    )
    tool: dict[str, Any] = {
        "type": "image_generation",
        "model": UPSTREAM_MODEL,
        "action": action,
        "size": size,
        "quality": image_quality,
    }
    _add_image_output_options(
        tool,
        output_format=output_format_for_upstream,
        output_compression=output_compression,
        background=background_for_upstream,
        moderation=moderation,
    )
    pixels = _parse_size_pixels(size)
    if (
        image_quality != "low"
        and pixels is not None
        and pixels <= _PARTIAL_IMAGES_MAX_PIXELS
    ):
        tool["partial_images"] = 3
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": prompt_for_upstream}
    ]
    if action == "edit":
        if image_urls:
            # 新路径：调用方已经 push 到 image-job sidecar 拿到 URL；上游直接拉，body 极小。
            for url in image_urls:
                if isinstance(url, str) and url:
                    content.append({"type": "input_image", "image_url": url})
        else:
            # Fallback：base64 内联（老路径）。仅测试 / 无 sidecar 环境进入。
            for raw in images or []:
                ref_bytes, mime = _normalize_reference_image(raw)
                image_b64 = base64.b64encode(ref_bytes).decode("ascii")
                content.append(
                    {"type": "input_image", "image_url": f"data:{mime};base64,{image_b64}"}
                )
    # input item 显式带 `type: "message"`：Codex 私有 /responses 端点对 input
    # 数组项的字段验证比公网 OpenAI Responses API 更严，sub2api / CLIProxyAPI
    # 标准模板都明确带这个字段；缺失时 ChatGPT 端可能间歇性 422 或丢 message。
    input_payload: list[dict[str, Any]] = [
        {"type": "message", "role": "user", "content": content}
    ]
    tools_sorted = _stable_sort_tools([tool])
    body: dict[str, Any] = {
        "model": image_model,
        "instructions": DEFAULT_IMAGE_INSTRUCTIONS,
        "input": input_payload,
        "tools": tools_sorted,
        # tool_choice 用对象形式而非 "required" 字符串：Codex CLI 客户端实际发的就是
        # {"type":"image_generation"}，私有端点对此格式校验更宽松，避免 invalid_tool_choice。
        "tool_choice": {"type": "image_generation"},
        # parallel_tool_calls=true 对齐 Codex CLI 标准（image_generation 单 tool 场景下与 false 等价，
        # 但偏离标准的请求体可能命中 codex 端的反向风控）。
        "parallel_tool_calls": True,
        # include 字段是 Codex CLI 标准客户端的必带项；缺了上游某些路径可能行为异常。
        "include": ["reasoning.encrypted_content"],
        "stream": True,
        "store": False,
        # 主驱动模型统一带 medium reasoning + summary auto，对齐 sub2api / CLIProxyAPI 标准模板。
        # 移除 effort=high 与 service_tier=priority：前者拉长 SSE 总耗时（断流概率↑），
        # 后者对普通 ChatGPT Plus OAuth 账号未必有资格，可能直接被 codex 端拒绝。
        "reasoning": {"effort": "medium", "summary": "auto"},
    }
    # Retry 打散：上层（task retry / fallback inner retry）通过 ContextVar 传递当前 attempt，
    # >1 时本函数末尾会改写 prompt_cache_key / reasoning.effort / 移除 partial_images。
    _apply_retry_cache_busters(body, _image_retry_attempt_ctx.get(), prompt, size)
    _validate_responses_body(body)
    return body


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
) -> bytes:
    _ = proxy_url  # client is already constructed with the provider proxy.
    started = time.monotonic()
    trace_id = _generate_trace_id()
    try:
        resp = await client.get(image_url)
    except _RETRY_HTTPX_EXC as exc:
        duration_ms = (time.monotonic() - started) * 1000.0
        _log_upstream_call(
            endpoint="image_jobs_download",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise UpstreamError(
            f"image job result download failed: {exc}",
            status_code=0,
            error_code=EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={"url": image_url, "path": "image-jobs/result", "method": "GET"},
        ) from exc

    duration_ms = (time.monotonic() - started) * 1000.0
    _log_upstream_call(
        endpoint="image_jobs_download",
        status=resp.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=getattr(resp, "headers", None),
    )
    if resp.status_code >= 400:
        raise UpstreamError(
            f"image job result download http {resp.status_code}",
            status_code=resp.status_code,
            error_code=EC.UPSTREAM_ERROR.value,
            payload={"url": image_url, "path": "image-jobs/result", "method": "GET"},
        )
    raw = resp.content
    if not raw:
        raise UpstreamError(
            "image job result download returned empty body",
            status_code=resp.status_code,
            error_code=EC.NO_IMAGE_RETURNED.value,
            payload={"url": image_url, "path": "image-jobs/result", "method": "GET"},
        )
    if len(raw) > _IMAGE_JOB_DOWNLOAD_MAX_BYTES:
        raise UpstreamError(
            "image job result download exceeded max bytes",
            status_code=resp.status_code,
            error_code=EC.STREAM_TOO_LARGE.value,
            payload={
                "url": image_url,
                "bytes": len(raw),
                "max_bytes": _IMAGE_JOB_DOWNLOAD_MAX_BYTES,
            },
        )
    return raw


async def _submit_and_wait_image_job(
    *,
    payload: dict[str, Any],
    base_url: str,
    api_key: str,
    proxy: ProviderProxyDefinition | None,
    progress_callback: ImageProgressCallback | None,
) -> tuple[str, str | None]:
    proxy_url = await resolve_provider_proxy_url(proxy)
    client = await (_get_images_client(proxy_url) if proxy_url else _get_images_client())
    submit_url = _image_jobs_url(base_url)
    trace_id = _generate_trace_id()
    headers = _auth_headers(api_key, trace_id=trace_id)
    started = time.monotonic()
    try:
        resp = await _post_with_retry(
            client=client,
            url=submit_url,
            headers=headers,
            json_body=payload,
            max_attempts=3,
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
                _parse_error(job if isinstance(job, dict) else {}, poll_resp.status_code),
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
    )


def _image_job_reference_image_entries(images: list[bytes]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for raw in images:
        ref_bytes, mime = _normalize_reference_image(raw)
        image_b64 = base64.b64encode(ref_bytes).decode("ascii")
        entries.append({"image_url": f"data:{mime};base64,{image_b64}"})
    return entries


async def _image_job_edit_once(
    *,
    prompt: str,
    size: str,
    images: list[bytes],
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
    body["images"] = _image_job_reference_image_entries(images)
    return await _submit_and_wait_image_job(
        payload=_image_job_payload(
            request_type="edits",
            endpoint="/v1/images/edits",
            body=body,
        ),
        base_url=base_url_override or await _resolve_image_job_base_url(),
        api_key=api_key_override,
        proxy=proxy_override,
        progress_callback=progress_callback,
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
        images, base_url=sidecar_base_url, api_key=api_key_override
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
    )


# 图生图 multipart 走 curl 子进程——实测在同一台服务器上 httpx.AsyncClient 发出
# 同样 body 被上游网关持续 502，但 curl 命令发同样请求能 200 出图。原因尚未定位
# （怀疑 httpx 的 multipart boundary / header 组合触发了网关某条规则）。
# 绕法：edit 路径只用 curl，保留 retry + fallback 语义。
_CURL_BIN = shutil.which("curl") or "/usr/bin/curl"


def _curl_timeout_arg(timeout_s: float) -> str:
    timeout = math.ceil(timeout_s) if math.isfinite(timeout_s) else 1
    return str(max(1, timeout))


def _write_json_body_file(fd: int, json_body: dict[str, Any]) -> None:
    os.write(fd, json.dumps(json_body).encode("utf-8"))


def _write_bytes_file(fd: int, raw: bytes) -> None:
    os.write(fd, raw)


async def _curl_post_multipart(
    *,
    url: str,
    data: dict[str, str],
    files: list[tuple[str, tuple[str, bytes, str]]],
    headers: dict[str, str],
    timeout_s: float,
    proxy_url: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """用 curl 发 multipart POST，返回 (status_code, parsed_body)。

    - files 里的 bytes 写临时文件再用 `-F name=@path;filename=...;type=...` 送出
    - 响应体读到 stdout，status 用 `-w \\n__HTTP_STATUS__:%{http_code}` 带出
    - 子进程非 0 退出抛 httpx.HTTPError（与 httpx 调用方一致，便于外层 except）
    """
    tmpfiles: list[str] = []
    proc: asyncio.subprocess.Process | None = None
    try:
        form_args: list[str] = []
        for k, v in data.items():
            form_args += ["-F", f"{k}={v}"]
        for field_name, (filename, raw, mime) in files:
            fd, tmp_path = tempfile.mkstemp(prefix="lumen_curl_", suffix=".bin")
            try:
                await asyncio.to_thread(_write_bytes_file, fd, raw)
            finally:
                os.close(fd)
            tmpfiles.append(tmp_path)
            form_args += [
                "-F",
                f"{field_name}=@{tmp_path};filename={filename};type={mime}",
            ]
        header_args: list[str] = []
        for k, v in headers.items():
            header_args += ["-H", f"{k}: {v}"]
        status_marker = "\n__HTTP_STATUS__:"
        cmd = [
            _CURL_BIN,
            "-sS",
            "-m",
            _curl_timeout_arg(timeout_s),
            "-w",
            f"{status_marker}%{{http_code}}",
            *(["--proxy", proxy_url] if proxy_url else []),
            *header_args,
            *form_args,
            url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        curl_timeout_s = float(_curl_timeout_arg(timeout_s))
        guard_timeout_s = curl_timeout_s + min(5.0, max(0.25, curl_timeout_s * 0.1))
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=guard_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise httpx.TimeoutException(
                f"curl multipart timed out after {guard_timeout_s:.2f}s"
            ) from exc
        if proc.returncode != 0:
            raise httpx.HTTPError(
                f"curl failed rc={proc.returncode} stderr={stderr_b.decode('utf-8', 'replace')[:500]}"
            )
        out = stdout_b.decode("utf-8", "replace")
        if status_marker not in out:
            raise httpx.HTTPError(
                f"curl output missing status marker (head={out[:200]!r})"
            )
        body_s, _, status_s = out.rpartition(status_marker)
        try:
            payload = json.loads(body_s)
        except Exception:
            payload = {"raw": body_s[:2000]}
        return int(status_s.strip()), payload
    except asyncio.CancelledError:
        raise
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
        for p in tmpfiles:
            try:
                os.unlink(p)
            except Exception:
                pass


async def _curl_post_multipart_with_retry(
    *,
    url: str,
    data: dict[str, str],
    files: list[tuple[str, tuple[str, bytes, str]]],
    headers: dict[str, str],
    timeout_s: float,
    proxy_url: str | None = None,
    max_attempts: int = 2,  # 上游网关每次 502 要等 ~80s；3 次会吃掉任务预算的一半
    backoff_base_s: float = 1.0,
) -> tuple[int, dict[str, Any]]:
    """带重试的 curl POST。语义与 _post_with_retry 保持对称：
    httpx.HTTPError / 502 / 503 / 504 都重试，其他情况直接返回。
    """
    last_status: int | None = None
    last_payload: dict[str, Any] | None = None
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(backoff_base_s * (2 ** (attempt - 1)))
        try:
            status, payload = await _curl_post_multipart(
                url=url,
                data=data,
                files=files,
                headers=headers,
                timeout_s=timeout_s,
                proxy_url=proxy_url,
            )
        except httpx.HTTPError as exc:
            last_exc = exc
            logger.warning(
                "curl upstream transient error attempt=%d/%d url=%s err=%r",
                attempt + 1, max_attempts, url, exc,
            )
            continue
        if status in _RETRY_STATUS:
            last_status = status
            last_payload = payload
            logger.warning(
                "curl upstream transient status attempt=%d/%d url=%s status=%d",
                attempt + 1, max_attempts, url, status,
            )
            continue
        return status, payload
    if last_status is not None and last_payload is not None:
        return last_status, last_payload
    assert last_exc is not None
    raise last_exc


async def _iter_sse_curl(
    *,
    url: str,
    json_body: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
    proxy_url: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """用 curl -N 子进程做 SSE 流式 POST，yield 每个解析后的事件 dict。

    和 _iter_sse_with_runtime yield 格式对齐：每个 dict 里带 "type"（若事件行给了
    `event: xxx` 则用它，否则用 data payload 的 `type` 字段）。`-i` 让 curl 把
    HTTP response headers 输出到 stdout 头部，由本函数先读状态行、再读 headers
    直到空行、再进 SSE 解析阶段。非 2xx 状态直接读剩余 body 抛 UpstreamError。

    原因：上游网关对 httpx 流式 POST /v1/responses 返回的事件里会夹杂
    `response.failed`（上游拒绝服务），但同样 body 的 curl -N 请求能正常出
    `response.image_generation_call.*` + `response.output_item.done(result=b64)`。
    主链路 multipart 也换成 curl 后，SSE 这里继续用 httpx 会被网关同样挑剔，
    所以备链路也走 curl 一条路线。

    取消安全：finally 段会显式 terminate/kill curl 子进程并删除 tmp body file，
    asyncio.CancelledError 透传给调用方，不会 swallow。

    元信息埋点：从 `headers` 里取出 `x-trace-id`（调用方传入）；从上游响应头读
    `x-request-id` / `x-codex-primary-used-percent` 并用 _log_upstream_call 一次性
    打日志 + 写 Prometheus。
    """
    trace_id = headers.get("x-trace-id") or _generate_trace_id()
    fd, body_path = tempfile.mkstemp(prefix="lumen_sse_body_", suffix=".json")
    try:
        await asyncio.to_thread(_write_json_body_file, fd, json_body)
    finally:
        os.close(fd)

    header_args: list[str] = []
    for k, v in headers.items():
        header_args += ["-H", f"{k}: {v}"]
    header_args += ["-H", "Content-Type: application/json"]
    cmd = [
        _CURL_BIN,
        "-sS",
        "-N",
        "-i",  # 把 response headers 输出到 stdout 头部
        *(["--proxy", proxy_url] if proxy_url else []),
        *header_args,
        "--data-binary",
        f"@{body_path}",
        url,
    ]
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    # 上游响应头收集（用于 _log_upstream_call）。在解析 HTTP headers 行时填充。
    response_headers: dict[str, str] = {}
    final_status: int = 0

    # Chunk-based 行分帧：不依赖 StreamReader 的内置 readline limit（默认 64KB）。
    # 同时限制总字节和单行字节，避免一个畸形 data: 行把 worker 缓冲撑到 OOM。
    buf = bytearray()
    search_from = 0
    stream_eof = False
    byte_count = 0
    line_count = 0
    idle_timeout_s = max(0.001, float(timeout_s))

    async def next_line() -> bytes | None:
        nonlocal search_from, stream_eof, byte_count, line_count
        while True:
            idx = buf.find(b"\n", search_from)
            if idx >= 0:
                line = bytes(buf[: idx + 1])
                del buf[: idx + 1]
                search_from = 0
                line_count += 1
                if len(line) > _SSE_MAX_LINE_BYTES:
                    raise UpstreamError(
                        "sse exceeded max line bytes",
                        error_code=EC.STREAM_TOO_LARGE.value,
                        status_code=200,
                    )
                if line_count > _SSE_MAX_LINES:
                    raise UpstreamError(
                        "sse exceeded max lines",
                        error_code=EC.STREAM_TOO_LARGE.value,
                        status_code=200,
                    )
                return line
            search_from = len(buf)
            if stream_eof:
                if buf:
                    line = bytes(buf)
                    if len(line) > _SSE_MAX_LINE_BYTES:
                        raise UpstreamError(
                            "sse exceeded max line bytes",
                            error_code=EC.STREAM_TOO_LARGE.value,
                            status_code=200,
                        )
                    buf.clear()
                    search_from = 0
                    line_count += 1
                    return line
                return None
            try:
                chunk = await asyncio.wait_for(
                    proc.stdout.read(65536),
                    timeout=idle_timeout_s,
                )
            except asyncio.TimeoutError as exc:
                raise UpstreamError(
                    f"curl sse idle timeout after {idle_timeout_s:.0f}s",
                    error_code=EC.SSE_CURL_FAILED.value,
                    status_code=200,
                ) from exc
            if not chunk:
                stream_eof = True
                continue
            byte_count += len(chunk)
            if byte_count > _SSE_MAX_BYTES:
                raise UpstreamError(
                    "sse exceeded max bytes",
                    error_code=EC.STREAM_TOO_LARGE.value,
                    status_code=200,
                )
            buf.extend(chunk)
            if len(buf) > _SSE_MAX_LINE_BYTES and b"\n" not in buf:
                raise UpstreamError(
                    "sse exceeded max line bytes",
                    error_code=EC.STREAM_TOO_LARGE.value,
                    status_code=200,
                )

    async def drain_remaining() -> bytes:
        chunks: list[bytes] = []
        if buf:
            chunks.append(bytes(buf))
            buf.clear()
        while True:
            ln = await next_line()
            if ln is None:
                break
            chunks.append(ln)
        return b"".join(chunks)

    try:
        # 1) 读状态行："HTTP/1.1 200 OK" / "HTTP/2 200"
        status_line = await next_line()
        if not status_line:
            raise UpstreamError(
                "curl sse empty response",
                error_code=EC.SSE_CURL_FAILED.value,
                status_code=0,
            )
        status_s = status_line.decode("utf-8", "replace").strip()
        m = re.match(r"HTTP/[\d.]+\s+(\d+)", status_s)
        status_code = int(m.group(1)) if m else 0
        final_status = status_code

        # 2) 跳过余下 header 行，直到遇到空行——同时把关注的 header 字段收下来
        while True:
            ln = await next_line()
            if ln is None:
                break
            if ln.strip() == b"":
                break
            try:
                hdr = ln.decode("utf-8", "replace").rstrip("\r\n")
            except Exception:  # noqa: BLE001
                continue
            if ":" in hdr:
                k, _, v = hdr.partition(":")
                response_headers[k.strip().lower()] = v.strip()

        # 3) 非 2xx：把剩余 body 读完抛错
        if status_code >= 400 or status_code == 0:
            err_raw = await drain_remaining()
            err_text = err_raw.decode("utf-8", "replace")
            logger.warning(
                "curl sse non-2xx status=%s url=%s body=%.1000s trace_id=%s x_request_id=%s",
                status_code, url, err_text, trace_id,
                response_headers.get("x-request-id"),
            )
            try:
                payload = json.loads(err_text)
            except Exception:
                payload = {"raw": err_text[:2000]}
            raise _with_error_context(
                _parse_error(
                    payload if isinstance(payload, dict) else {}, status_code or 0
                ),
                path="responses",
                method="POST",
                url=url,
            )

        # 4) 解析 SSE：按行累积 event/data，空行切分事件
        buf_type: str | None = None
        buf_data: list[str] = []

        while True:
            raw = await next_line()
            if raw is None:
                break
            s = raw.decode("utf-8", "replace").rstrip("\r\n")
            if s == "":
                if buf_data:
                    data_s = "\n".join(buf_data)
                    if data_s and data_s != "[DONE]":
                        try:
                            ev = json.loads(data_s)
                        except Exception:
                            ev = None
                        if isinstance(ev, dict):
                            if buf_type and "type" not in ev:
                                ev["type"] = buf_type
                            # SSE response.completed 帧里嵌着 usage——抓出来打 metrics。
                            _maybe_record_usage_from_event(ev)
                            yield ev
                buf_type = None
                buf_data = []
                continue
            if s.startswith(":"):
                continue  # comment / keepalive
            if s.startswith("event:"):
                buf_type = s[6:].strip()
            elif s.startswith("data:"):
                buf_data.append(s[5:].lstrip())

        # 5) 残余（无结尾空行）
        if buf_data:
            data_s = "\n".join(buf_data)
            if data_s and data_s != "[DONE]":
                try:
                    ev = json.loads(data_s)
                    if isinstance(ev, dict):
                        if buf_type and "type" not in ev:
                            ev["type"] = buf_type
                        _maybe_record_usage_from_event(ev)
                        yield ev
                except Exception:
                    pass

        rc = await proc.wait()
        if rc != 0:
            stderr_s = ""
            if proc.stderr is not None:
                stderr_s = (await proc.stderr.read()).decode("utf-8", "replace")
            raise UpstreamError(
                f"curl sse exited rc={rc} stderr={stderr_s[:500]}",
                error_code=EC.SSE_CURL_FAILED.value,
                status_code=200,
            )
    except asyncio.CancelledError:
        # cancellation safe: aclose() 等价物——下方 finally 块负责 terminate 子进程
        # 并删除 tmp body file；这里只 reraise，不要把 CancelledError 当成普通异常吞。
        raise
    finally:
        # cancellation safe: 终止 curl 子进程 + 删除 tmp body file（在 except / 正常退出
        # 都生效）。即使外层 cancel 也保证不留僵尸进程 / 临时文件。
        if proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
        try:
            os.unlink(body_path)
        except Exception:
            pass
        # 元信息埋点：endpoint 用 responses（curl SSE 路径只服务于 /v1/responses）
        duration_ms = (time.monotonic() - started) * 1000.0
        try:
            _log_upstream_call(
                endpoint="responses",
                status=final_status,
                duration_ms=duration_ms,
                trace_id=trace_id,
                response_headers=response_headers,
            )
        except Exception:  # noqa: BLE001
            logger.debug("failed to log upstream call meta", exc_info=True)


def _maybe_record_usage_from_event(event: dict[str, Any]) -> None:
    """SSE 事件里如果带 usage 字段（多见于 response.completed），抽出并埋点。

    上游 SSE 在 `response.completed` 帧的 `response.usage` 上挂 token 计数；少数情况下
    也可能直接挂在事件 root 的 `usage`。两者都尝试。
    """
    usage = event.get("usage")
    if not isinstance(usage, dict):
        resp = event.get("response")
        if isinstance(resp, dict):
            usage = resp.get("usage")
    if isinstance(usage, dict):
        _record_usage(usage)
    # 响应完成帧里如果有 output 数组，扫一遍未知 type 给 warning（不让整条流挂掉）。
    if event.get("type") == "response.completed":
        resp_obj = event.get("response")
        if isinstance(resp_obj, dict):
            outputs = resp_obj.get("output")
            if isinstance(outputs, list):
                for it in outputs:
                    if isinstance(it, dict):
                        t = it.get("type")
                        if isinstance(t, str) and t not in _KNOWN_OUTPUT_ITEM_TYPES:
                            logger.warning(
                                "upstream output item with unknown type=%r; skipping",
                                t,
                            )


async def _emit_image_progress(
    progress_callback: ImageProgressCallback | None,
    event_type: str,
    **payload: Any,
) -> None:
    if progress_callback is None:
        return
    event = {"type": event_type, **payload}
    try:
        result = progress_callback(event)
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001
        logger.warning("image progress callback failed", exc_info=True)


def _extract_response_image_b64(event: dict[str, Any]) -> str | None:
    if isinstance(event.get("result"), str):
        return event["result"]
    item = event.get("item")
    if isinstance(item, dict) and isinstance(item.get("result"), str):
        return item["result"]
    return None


def _extract_response_revised_prompt(event: dict[str, Any]) -> str | None:
    if isinstance(event.get("revised_prompt"), str):
        return event["revised_prompt"]
    item = event.get("item")
    if isinstance(item, dict) and isinstance(item.get("revised_prompt"), str):
        return item["revised_prompt"]
    return None


def _sniff_image_mime(raw: bytes) -> str | None:
    """按 magic bytes 探测图片 MIME；未知返回 None。

    上传路由允许 png/jpeg/webp 且只 resize、不转码。只能信真实字节、
    不能信 data URL 前缀声明——否则上游按 PNG 解析 JPEG/WEBP 会走异常路径。
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_reference_image(raw: bytes) -> tuple[bytes, str]:
    """把 reference image 统一重编码为干净 8-bit RGB/RGBA WebP 发给上游。

    即使 magic bytes 命中 png/jpeg/webp 也过 PIL：上游（OpenAI gpt-5.x image_generation）
    对含 EXIF / ICC profile / 16-bit / APNG / 非标 filter 的 PNG/JPEG 会稳定返回
    server_error（实测：文生图产物（PIL.save 出的干净 PNG）edit 成功，用户相册原图
    edit 必挂）。重编码 → 标准 WebP 去掉所有 metadata + 统一色深。
    PIL 解不开 → UpstreamError(bad_reference_image, terminal)——用户输入问题，重试没意义。

    GEN-P0-8 加固：
    - 输入字节：严格 ≤ _MAX_REFERENCE_IMAGE_BYTES（100 MB），terminal 错误。
    - 像素上限：PIL.Image.MAX_IMAGE_PIXELS 全局已收紧到 64M；这里再 explicit check。
    - DecompressionBombError：捕获并转 terminal，防止解压炸弹 OOM worker。
    """
    if len(raw) > _MAX_REFERENCE_IMAGE_BYTES:
        # terminal 语义：reference_image_too_large 不在 _FALLBACK_RETRY_ERROR_CODES 中，不会重试。
        raise UpstreamError(
            "reference image exceeds size limit",
            error_code=EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={"max_bytes": _MAX_REFERENCE_IMAGE_BYTES, "actual_bytes": len(raw)},
        )
    try:
        with PILImage.open(io.BytesIO(raw)) as im:
            width, height = im.size
            if width <= 0 or height <= 0 or width * height > _MAX_REFERENCE_IMAGE_PIXELS:
                raise UpstreamError(
                    "reference image exceeds pixel limit",
                    error_code=EC.REFERENCE_IMAGE_TOO_LARGE.value,
                    status_code=413,
                    payload={
                        "max_pixels": _MAX_REFERENCE_IMAGE_PIXELS,
                        "actual_pixels": max(width, 0) * max(height, 0),
                    },
                )
            # im.load() 可能抛 PILImage.DecompressionBombError（像素 > MAX_IMAGE_PIXELS 时）。
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                target_mode = "RGBA" if "A" in im.getbands() else "RGB"
                im = im.convert(target_mode)
            out = io.BytesIO()
            im.save(out, format="WEBP", quality=90, method=4)
        normalized = out.getvalue()
        if len(normalized) > _MAX_NORMALIZED_IMAGE_BYTES:
            raise UpstreamError(
                "normalized reference image exceeds size limit",
                error_code=EC.REFERENCE_IMAGE_TOO_LARGE.value,
                status_code=413,
                payload={
                    "max_bytes": _MAX_NORMALIZED_IMAGE_BYTES,
                    "actual_bytes": len(normalized),
                },
            )
        return normalized, "image/webp"
    except UpstreamError:
        raise
    except PILImage.DecompressionBombError as exc:
        # GEN-P0-8: 解压炸弹（小文件→超大画布）→ terminal 硬拒，不给 worker OOM 的机会。
        raise UpstreamError(
            f"reference image decompression bomb: {exc}",
            error_code=EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={"max_pixels": _MAX_REFERENCE_IMAGE_PIXELS},
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UpstreamError(
            f"reference image not decodable: {exc}",
            error_code=EC.BAD_REFERENCE_IMAGE.value,
            status_code=400,
        ) from exc


# Reference 图 push 到 image-job sidecar 的超时——跨地域部署单程 RTT 可能 200ms 起步，
# 大图多次 RTT；30s 给充足窗口。失败 → 降级 base64 内联（老路径）。
_REFERENCE_PUSH_TIMEOUT_S = 30.0


async def _push_reference_to_image_job(
    raw: bytes,
    mime: str,
    *,
    base_url: str,
    api_key: str,
) -> str | None:
    """把已 normalize 的 reference 图 POST 给 image-job sidecar /v1/refs，返回公网 URL。

    Why：把 base64 内联（4-7MB body）从 codex /responses 请求里搬出来，body 缩到几百字节。
    上游同区拉公网 URL 极快；跨地域链路上行带宽节省 99%+，断流概率显著下降。

    失败时返回 None；调用方应降级 base64 内联（老路径）保命。任何异常都吞——
    push 失败不能让 task 失败，只是丢失这次的 URL 优化收益。
    """
    if not base_url or not api_key:
        return None
    url = base_url.rstrip("/") + "/v1/refs"
    headers = {
        "Content-Type": mime,
        "Authorization": f"Bearer {api_key}",
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_REFERENCE_PUSH_TIMEOUT_S)
        ) as client:
            resp = await client.post(url, content=raw, headers=headers)
        if resp.status_code != 200:
            logger.warning(
                "reference push to image-job failed status=%d url=%s body=%s",
                resp.status_code,
                url,
                resp.text[:200],
            )
            return None
        try:
            data = resp.json()
        except ValueError:
            logger.warning("reference push returned non-JSON: %s", resp.text[:200])
            return None
        public_url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(public_url, str) or not public_url:
            logger.warning("reference push response missing url: %r", data)
            return None
        return public_url
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("reference push to image-job error: %r", exc)
        return None


async def _resolve_reference_image_urls(
    images: list[bytes] | None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> list[str]:
    """把 reference bytes 列表转换成给上游用的 image_url 字符串列表。

    优先 push 到 image-job sidecar 拿短 URL；任一图 push 失败就**那一张**降级 base64 内联，
    其他成功的仍用 URL（混合也可以——OpenAI Responses API 的 input_image.image_url 字段
    同时接受 https:// 和 data:）。

    base_url/api_key 同时给才尝试 push；任一为空 → 整批走 base64（测试 / 没 sidecar 环境）。
    """
    if not images:
        return []
    out: list[str] = []
    for raw in images:
        ref_bytes, mime = _normalize_reference_image(raw)
        ref_url: str | None = None
        if base_url and api_key:
            ref_url = await _push_reference_to_image_job(
                ref_bytes, mime, base_url=base_url, api_key=api_key
            )
        if ref_url:
            out.append(ref_url)
        else:
            # Fallback: base64 data URL（老路径）；保命但不享受短 body 收益。
            b64 = base64.b64encode(ref_bytes).decode("ascii")
            out.append(f"data:{mime};base64,{b64}")
    return out


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
_IMAGE_READ_TIMEOUT_4K_S = 360.0


def _select_image_read_timeout(size: str) -> float:
    """按图像像素分级选 read/idle timeout。

    1K/2K：用 settings.upstream_read_timeout_s（默认 180s）
    4K：取 max(默认, 360s)，避免被 settings 改小后误伤
    """
    pixels = _parse_size_pixels(size)
    if pixels is not None and pixels > _IMAGE_4K_PIXELS:
        return max(settings.upstream_read_timeout_s, _IMAGE_READ_TIMEOUT_4K_S)
    return settings.upstream_read_timeout_s


def _parse_size_pixels(size: str) -> int | None:
    """把 `WxH` 字面量解析成总像素；`auto` / 非法格式返回 None。"""
    if not isinstance(size, str) or "x" not in size:
        return None
    w_s, _, h_s = size.partition("x")
    try:
        w, h = int(w_s), int(h_s)
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return w * h


async def _responses_image_stream(
    *,
    prompt: str,
    size: str,
    action: str,
    images: list[bytes] | None = None,
    quality: str = "high",
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None = None,
    use_httpx: bool = False,
    base_url_override: str | None = None,
    api_key_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
) -> tuple[str, str | None]:
    """Use `/v1/responses` + `image_generation` as streaming image fallback.

    The gateway returns the complete image in `response.output_item.done.item.result`.
    `partial_image` events are intentionally exposed only as small progress signals; callers
    must not publish their base64 payloads to Redis/frontend.

    use_httpx=True 走 httpx `stream()`（异构冗余路径）；False 走 curl 子进程（历史稳定主路）。
    edit race 的第 1 路传 True，让 client 选型异构——curl 挂时 httpx 可能救，反之亦然。

    base_url_override / api_key_override: provider failover 路径传入，跳过 _resolve_runtime。
    """
    proxy = proxy_override
    if base_url_override is not None and api_key_override is not None:
        base, api_key = base_url_override, api_key_override
    else:
        runtime = await _resolve_runtime()
        base, api_key, proxy = _runtime_parts(runtime)
    # Model 显式 pin：UPSTREAM_MODEL（图像工具底层模型）+ DEFAULT_IMAGE_RESPONSES_MODEL
    # （reasoning 主模型）都来自 lumen_core.constants；assert 防止配置项隐式置空。
    assert UPSTREAM_MODEL, "model must be set"
    image_model = model or DEFAULT_IMAGE_RESPONSES_MODEL
    assert image_model, "model must be set"
    image_quality = _normalize_image_quality(quality)
    prompt_for_upstream, output_format_for_upstream, background_for_upstream = (
        _transparent_matte_upstream_options(
            prompt=prompt,
            output_format=output_format,
            background=background,
        )
    )
    tool: dict[str, Any] = {
        "type": "image_generation",
        "model": UPSTREAM_MODEL,
        "action": action,
        "size": size,
        "quality": image_quality,
    }
    _add_image_output_options(
        tool,
        output_format=output_format_for_upstream,
        output_compression=output_compression,
        background=background_for_upstream,
        moderation=moderation,
    )
    # partial_images 在大尺寸下会稳定触发上游 server_error——实测 3840x2160 带
    # partial_images=3 SSE 跑到 generating 阶段就 failed；去掉后同一 body 200 出图。
    # 收紧到 ≤1.4MP 才带 partial（仅 ~1024x1024 / 1024x1280 一类纯 1K），
    # 2K 起一律走稳定路径。size="auto" 不可预估，按保守（不带 partial）处理。
    pixels = _parse_size_pixels(size)
    if (
        image_quality != "low"
        and pixels is not None
        and pixels <= _PARTIAL_IMAGES_MAX_PIXELS
    ):
        tool["partial_images"] = 3
    # 上游网关 /v1/responses 对 `input` 严格要求 list（见
    # responses-image-integration-guide.md §Text-to-Image / Image-to-Image）。
    # edit 路径原本就是 list；generate 路径之前沿用 /v1/images/generations 的 raw string 没来得及改，
    # 切到 responses 统一链路后稳定返回 400 `{"detail":"Input must be a list"}`。
    # 统一包成 user message，text 放在 input_text；edit 多一个 input_image 项。
    # 显式带 `type: "message"` 对齐 sub2api / CLIProxyAPI 标准模板——Codex 私有
    # /responses 端点对 input 数组项字段验证比公网 OpenAI 更严，缺这个字段会间歇 422。
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": prompt_for_upstream}
    ]
    if action == "edit":
        # 优先 push reference 到 image-job sidecar 拿短 URL（body 缩到几百字节，避免跨地域链路上行
        # 4-7MB body 易断流）。push 失败 → helper 内部降级 base64 内联（老路径）。
        # base/api_key 是 sub2api 的，但 image-job sidecar /v1/refs 共用 Bearer 鉴权（require_auth 仅
        # 检 Bearer 格式，不验内容）。image-job sidecar 通常异地部署，base_url 单独 resolve。
        sidecar_base_url: str | None = None
        try:
            sidecar_base_url = await _resolve_image_job_base_url()
        except Exception as exc:  # noqa: BLE001
            logger.debug("reference push base_url resolve fallback err=%s", exc)
        ref_urls = await _resolve_reference_image_urls(
            images, base_url=sidecar_base_url, api_key=api_key
        )
        for url in ref_urls:
            if isinstance(url, str) and url:
                content.append({"type": "input_image", "image_url": url})
    input_payload: list[dict[str, Any]] = [
        {"type": "message", "role": "user", "content": content}
    ]

    # tools 数组按 name/type 排序，保证 prompt cache 前缀稳定（即使当前只 1 项也排）
    tools_sorted = _stable_sort_tools([tool])
    body: dict[str, Any] = {
        "model": image_model,
        # 上游（上游 / gpt-5.x）强制要求顶层 `instructions` 字段。completion / auto_title
        # 已经加过；图像 responses 路径之前漏了，症状是上游稳定 HTTP 400
        # `{"detail":"Instructions are required"}`。
        # 注意：DEFAULT_IMAGE_INSTRUCTIONS 是字面常量（""，对齐 Codex CLI 标准），
        # 保证 prompt cache 前缀稳定；如果未来要加变量，需评估 cache miss 影响。
        "instructions": DEFAULT_IMAGE_INSTRUCTIONS,
        "input": input_payload,
        "tools": tools_sorted,
        # tool_choice 用对象形式（Codex CLI 实际发的格式）而非 "required" 字符串——
        # 私有端点对此格式校验更宽松，避免 invalid_tool_choice。
        "tool_choice": {"type": "image_generation"},
        # parallel_tool_calls=true 对齐 Codex CLI 标准；单 tool 场景与 false 等价，
        # 但偏离标准的请求体可能命中 codex 端反向风控。
        "parallel_tool_calls": True,
        # include 是 Codex CLI 标准客户端的必带项；缺了上游某些路径可能行为异常。
        "include": ["reasoning.encrypted_content"],
        "stream": True,
        "store": False,
        # 主驱动模型统一带 medium reasoning + summary auto，对齐 sub2api / CLIProxyAPI 标准模板。
        # 不再分 fast / 非 fast——effort=high 拉长 SSE 总耗时（断流率↑），原 service_tier=priority
        # 普通 ChatGPT Plus OAuth 账号未必有资格，可能被 codex 端直接拒绝。
        "reasoning": {"effort": "medium", "summary": "auto"},
    }
    # Retry 打散：上层通过 ContextVar 传递当前 attempt，>1 时改写 prompt_cache_key /
    # reasoning.effort / 移除 partial_images，绕开 ChatGPT codex 端故障 cache。
    _apply_retry_cache_busters(body, _image_retry_attempt_ctx.get(), prompt, size)
    # 请求 schema 预校验（probe report §2.C1 严格约束）。违反时直接 4xx 抛错，
    # 避免把上游 400 暴露给用户。
    _validate_responses_body(body)
    final_b64: str | None = None
    revised_prompt: str | None = None
    partial_count = 0
    last_event_type: str | None = None
    # 捕获上游 response.failed / incomplete 等事件里的 error 字段，便于定位
    # （rate_limit / policy / backend_unavailable 等）。SSE payload 扔掉就再也
    # 找不回来了。
    upstream_error_detail: dict[str, Any] | None = None
    await _emit_image_progress(progress_callback, "fallback_started", action=action, size=size)
    # curl 历史上是主稳定路径（详见函数上方注释）；use_httpx=True 时走 httpx——用在
    # edit race 的冗余 lane 上，换一套 client fingerprint，赌某次 curl 挂时 httpx 活。
    # read_timeout 按图像像素分级（4K 需要 ≥360s）。
    read_timeout_s = _select_image_read_timeout(size)
    # 单次调用一个 trace_id：curl / httpx 路径都用同一个，便于下游对账上游 x-request-id
    call_trace_id = _generate_trace_id()
    call_headers = _auth_headers(api_key, trace_id=call_trace_id)
    proxy_url = await resolve_provider_proxy_url(proxy)
    sse_source = (
        _iter_sse_with_runtime(
            base=base,
            api_key=api_key,
            body=body,
            read_timeout_s=read_timeout_s,
            trace_id=call_trace_id,
            proxy_url=proxy_url,
        )
        if use_httpx
        else _iter_sse_curl(
            url=_responses_url(base),
            json_body=body,
            headers=call_headers,
            timeout_s=read_timeout_s,
            proxy_url=proxy_url,
        )
    )
    async for event in sse_source:
        event_type = event.get("type")
        if isinstance(event_type, str):
            last_event_type = event_type
        if event_type == "response.image_generation_call.partial_image":
            partial_count += 1
            await _emit_image_progress(
                progress_callback,
                "partial_image",
                index=event.get("partial_image_index", partial_count - 1),
                count=partial_count,
                has_preview=isinstance(
                    event.get("partial_image") or event.get("partial_image_b64"), str
                ),
            )
        b64 = _extract_response_image_b64(event)
        if b64:
            final_b64 = b64
            revised_prompt = _extract_response_revised_prompt(event) or revised_prompt
            await _emit_image_progress(progress_callback, "final_image")
        if event_type == "response.completed":
            await _emit_image_progress(progress_callback, "completed")
        # 捕获失败类事件里的 error payload（上游告诉我们"为什么没出图"）
        if event_type in ("response.failed", "response.incomplete", "error"):
            resp_obj = event.get("response")
            err = None
            if isinstance(resp_obj, dict):
                err = resp_obj.get("error") or resp_obj.get("incomplete_details")
            if err is None:
                err = event.get("error")
            if isinstance(err, dict):
                upstream_error_detail = err
        # image_generation_call item 的 failure 状态也可能携带 error
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                # 字段防御：未知 output[].type 仅 warning 跳过，不抛 KeyError
                if (
                    isinstance(item_type, str)
                    and item_type not in _KNOWN_OUTPUT_ITEM_TYPES
                ):
                    logger.warning(
                        "output_item.done with unknown item.type=%r last_event=%s",
                        item_type, last_event_type,
                    )
                if item.get("status") in {"failed", "incomplete"}:
                    item_err = item.get("error") or item.get("incomplete_details")
                    if isinstance(item_err, dict):
                        upstream_error_detail = item_err

    if not final_b64:
        safe_upstream_error = _summarize_upstream_error_detail(upstream_error_detail)
        logger.warning(
            "responses fallback drained without image: action=%s size=%s "
            "last_event_type=%s partial_count=%d upstream_error=%s",
            action, size, last_event_type, partial_count,
            json.dumps(safe_upstream_error, ensure_ascii=False, separators=(",", ":"))
            if isinstance(safe_upstream_error, dict)
            else safe_upstream_error,
        )
        # 把上游明确的 error.code 透传出去，让 classifier 按真实原因决定 terminal/retriable——
        # 否则 moderation_blocked 这类硬拒会被当成 no_image_returned 去重试 6 次，既拿不回图也烧配额。
        upstream_code: str | None = None
        upstream_msg: str | None = None
        if isinstance(upstream_error_detail, dict):
            raw_code = upstream_error_detail.get("code") or upstream_error_detail.get("type")
            if isinstance(raw_code, str) and raw_code:
                upstream_code = raw_code
            raw_msg = upstream_error_detail.get("message")
            if isinstance(raw_msg, str) and raw_msg:
                upstream_msg = raw_msg
        raise UpstreamError(
            upstream_msg or "responses image fallback returned no image",
            status_code=200,
            error_code=upstream_code or EC.NO_IMAGE_RETURNED.value,
            payload={
                "path": "responses",
                "action": action,
                "size": size,
                "last_event_type": last_event_type,
                "partial_count": partial_count,
                "upstream_error": upstream_error_detail,
            },
        )
    return final_b64, revised_prompt


def _summarize_exception(exc: BaseException) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }
    if isinstance(exc, UpstreamError):
        item["status_code"] = exc.status_code
        item["error_code"] = exc.error_code
        if exc.payload:
            item["payload"] = exc.payload
    return item


def _is_retryable_fallback_exception(exc: BaseException) -> bool:
    if isinstance(exc, UpstreamError):
        if exc.status_code in _RETRY_STATUS:
            return True
        if exc.status_code == 429:
            return True
        return exc.error_code in _FALLBACK_RETRY_ERROR_CODES
    return isinstance(exc, _RETRY_HTTPX_EXC)


def _max_attempts_for_exception(exc: BaseException) -> int:
    """GEN-P1-9: 按错误形态决定 fallback 层重试预算。

    - 5xx / 网络错：5 次（高价值——网关抖动 / 后端冷启动多见）
    - 429：5 次（搭配 _retry_after_seconds 等到限速窗口过去）
    - 4xx (401/403/404/422)：1 次（token / param 错——重试只会再 4xx）
    - 其他可重试 error_code（no_image_returned / sse_curl_failed 等）：fallback 默认值
    """
    if isinstance(exc, UpstreamError):
        if exc.status_code == 429:
            return _FALLBACK_MAX_ATTEMPTS_429
        if exc.status_code is not None and 500 <= exc.status_code < 600:
            return _FALLBACK_MAX_ATTEMPTS_5XX
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            return _FALLBACK_MAX_ATTEMPTS_4XX
        return _FALLBACK_MAX_ATTEMPTS
    if isinstance(exc, _RETRY_HTTPX_EXC):
        return _FALLBACK_MAX_ATTEMPTS_5XX
    return _FALLBACK_MAX_ATTEMPTS


def _retry_after_seconds(exc: BaseException) -> float | None:
    """从上游 429 错误体里抓 retry-after 提示（秒）；找不到返回 None。

    上游可能放在 payload.error.retry_after 或 payload.retry_after；都查一下。
    """
    if not isinstance(exc, UpstreamError):
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
    for v in candidates:
        if v is None:
            continue
        try:
            secs = float(v)
        except (TypeError, ValueError):
            continue
        if secs > 0:
            return min(secs, _FALLBACK_429_MAX_WAIT_S)
    return None


def _merge_fallback_errors(
    errors: list[BaseException],
    *,
    error_code: str,
    message: str,
) -> UpstreamError:
    if not errors:
        return UpstreamError(message, status_code=200, error_code=error_code)
    if any(_mentions_safety_policy(exc) for exc in errors):
        payload: dict[str, Any] = {
            "path": "responses",
            "errors": [_summarize_exception(exc) for exc in errors],
            "wrapped_error_code": error_code,
        }
        merged = UpstreamError(
            "request blocked by upstream safety policy",
            status_code=200,
            error_code=EC.MODERATION_BLOCKED.value,
            payload=payload,
        )
        if len(errors) > 1:
            merged.__cause__ = ExceptionGroup(message, errors)
        else:
            merged.__cause__ = errors[0]
        return merged
    first = errors[0]
    status_code = 200
    payload: dict[str, Any] = {}
    if isinstance(first, UpstreamError):
        status_code = first.status_code or 200
        payload.update(first.payload)
    payload.setdefault("path", "responses")
    payload["errors"] = [_summarize_exception(exc) for exc in errors]
    merged = UpstreamError(
        message,
        status_code=status_code,
        error_code=error_code,
        payload=payload,
    )
    if len(errors) > 1:
        merged.__cause__ = ExceptionGroup(message, errors)
    else:
        merged.__cause__ = first
    return merged


def _provider_error_details(
    providers: list[Any], errors: list[BaseException]
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for provider, exc in zip(providers, errors, strict=False):
        item = {
            "provider": getattr(provider, "name", None),
            **_summarize_exception(exc),
        }
        details.append(item)
    return details


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


def _mentions_safety_policy(exc: BaseException) -> bool:
    """Detect safety blocks hidden inside fallback/provider wrapper errors."""
    text = str(exc).lower()
    if any(marker in text for marker in _SAFETY_POLICY_ERROR_MARKERS):
        return True
    if isinstance(exc, UpstreamError) and exc.payload:
        try:
            payload_text = json.dumps(exc.payload, ensure_ascii=False).lower()
        except Exception:  # noqa: BLE001
            payload_text = repr(exc.payload).lower()
        if any(marker in payload_text for marker in _SAFETY_POLICY_ERROR_MARKERS):
            return True
    nested = getattr(exc, "exceptions", None)
    if nested and any(
        isinstance(child, BaseException) and _mentions_safety_policy(child)
        for child in nested
    ):
        return True
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException) and _mentions_safety_policy(cause):
        return True
    context = getattr(exc, "__context__", None)
    return isinstance(context, BaseException) and _mentions_safety_policy(context)


def _should_continue_image_provider_failover(
    exc: BaseException,
    *,
    retriable: bool,
) -> bool:
    """True when another image provider may handle the same prompt differently."""
    if retriable:
        return True
    if (
        isinstance(exc, UpstreamError)
        and exc.error_code in _IMAGE_PROVIDER_FAILOVER_ERROR_CODES
    ):
        return True
    return _mentions_safety_policy(exc)


def _merge_image_path_errors(
    *,
    action: str,
    primary_path: str,
    primary_error: BaseException,
    fallback_path: str,
    fallback_error: BaseException,
) -> UpstreamError:
    status_code = 502
    payload: dict[str, Any] = {}
    if isinstance(primary_error, UpstreamError):
        status_code = primary_error.status_code or status_code
        payload.update(primary_error.payload)
    elif isinstance(fallback_error, UpstreamError):
        status_code = fallback_error.status_code or status_code
        payload.update(fallback_error.payload)
    payload.setdefault("path", primary_path)
    payload["primary_path"] = primary_path
    payload["fallback_path"] = fallback_path
    payload["path_errors"] = [
        {"path": primary_path, **_summarize_exception(primary_error)},
        {"path": fallback_path, **_summarize_exception(fallback_error)},
    ]
    message = f"{action} image paths failed: {primary_path}, {fallback_path}"
    merged = UpstreamError(
        message,
        status_code=status_code,
        error_code=EC.PROVIDER_EXHAUSTED.value,
        payload=payload,
    )
    merged.__cause__ = ExceptionGroup(message, [primary_error, fallback_error])
    return merged


def _provider_pool_redis(pool: Any) -> Any:
    getter = getattr(pool, "get_redis", None)
    if callable(getter):
        return getter()
    return None


def _provider_endpoint_locked_error(provider: Any, endpoint_kind: str) -> UpstreamError | None:
    if endpoint_kind_allowed(provider, endpoint_kind):
        return None
    provider_name = getattr(provider, "name", "unknown")
    configured = getattr(provider, "image_jobs_endpoint", "auto")
    return UpstreamError(
        f"provider {provider_name} locked to {configured}; refuses {endpoint_kind}",
        error_code=EC.NO_PROVIDERS.value,
        status_code=503,
        payload={
            "provider": str(provider_name),
            "endpoint_kind": endpoint_kind,
            "locked_endpoint": str(configured),
            "reason": "endpoint_locked",
        },
    )


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
) -> tuple[str, str | None]:
    """GEN-P1-9: 重试预算按上游错误码动态调整。

    第一次尝试不 backoff；之后按错误形态决定剩余次数 + 等待时长。
    硬上限：_FALLBACK_MAX_ATTEMPTS_5XX（=5）。

    Retry 打散：每次内层 retry 把 ContextVar 累加到 outer_attempt + inner_attempt - 1，
    底层 body 构造点读到 >1 就注入 prompt_cache_key / 切换 reasoning.effort / 关掉
    partial_images，绕开 ChatGPT codex 端故障 cache。

    base_url_override / api_key_override: provider failover 路径透传。
    """
    errors: list[BaseException] = []
    attempt = 0
    hard_cap = _FALLBACK_MAX_ATTEMPTS_5XX
    # Outer attempt 从 ContextVar 取——可能是 generation.py 设置的 task 级 retry 编号。
    # 内层 retry 在它基础上累加，确保每次 attempt 都给 body 构造点不同的 cache 打散种子。
    outer_attempt = _image_retry_attempt_ctx.get()
    while attempt < hard_cap:
        # 第 attempt 次内层 retry 对应总 retry 编号 = outer + attempt（attempt 从 0 起）。
        # outer=1, attempt=0 → 1（首次，不打散）；outer=1, attempt=1 → 2（首次内层重试，开始打散）。
        effective_attempt = outer_attempt + attempt
        cv_token = _image_retry_attempt_ctx.set(effective_attempt)
        try:
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
            return await _responses_image_stream(**kwargs)
        except (asyncio.CancelledError, UpstreamCancelled):
            # GEN-P1-4: 用户取消信号——立即抛，不进 fallback retry。
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            attempt += 1
            # GEN-P1-9: 这次失败按错误形态算"该错码下应给的预算"——攻顶后停。
            attempts_for_this = _max_attempts_for_exception(exc)
            if (
                attempt >= attempts_for_this
                or not _is_retryable_fallback_exception(exc)
            ):
                raise _merge_fallback_errors(
                    errors,
                    error_code=(
                        exc.error_code
                        if isinstance(exc, UpstreamError) and exc.error_code
                        else "responses_fallback_failed"
                    ),
                    message=str(exc) or "responses fallback failed",
                ) from exc
            # GEN-P1-9: 429 优先尊重上游 retry-after；其他走指数 backoff。
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                backoff = retry_after
            elif (
                isinstance(exc, UpstreamError) and exc.status_code == 429
            ):
                backoff = min(_FALLBACK_429_DEFAULT_WAIT_S, _FALLBACK_429_MAX_WAIT_S)
            else:
                backoff = min(
                    _FALLBACK_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)),
                    _FALLBACK_RETRY_BACKOFF_MAX_S,
                )
            logger.warning(
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
            # 每次循环都 reset ContextVar，保证退出本函数后外层 attempt 不漂移。
            # 失败路径（except 内 raise）和成功路径（return await）都会经过 finally。
            _image_retry_attempt_ctx.reset(cv_token)
    # 触不到的兜底——hard_cap 耗尽：合并错误并抛出。
    raise _merge_fallback_errors(
        errors,
        error_code=EC.RESPONSES_FALLBACK_FAILED.value,
        message="responses fallback exhausted retry budget",
    )


async def _pool_select_compat(
    pool: Any,
    *,
    route: str,
    ignore_cooldown: bool = False,
    task_id: str | None = None,
    endpoint_kind: str | None = None,
) -> list[Any]:
    selector = getattr(pool, "select")
    kwargs: dict[str, Any] = {
        "route": route,
        "ignore_cooldown": ignore_cooldown,
    }
    if task_id is not None:
        kwargs["task_id"] = task_id
    if endpoint_kind is not None:
        kwargs["endpoint_kind"] = endpoint_kind
    try:
        return await selector(**kwargs)
    except TypeError as exc:
        if endpoint_kind is None or "endpoint_kind" not in str(exc):
            raise
        kwargs.pop("endpoint_kind", None)
        providers = await selector(**kwargs)
        return [
            provider
            for provider in providers
            if endpoint_kind_allowed(provider, endpoint_kind)
        ]


def _is_image_rate_limit_error(exc: BaseException) -> tuple[bool, float | None]:
    """识别 image route 的"账号无额度"信号，返回 (是否限速, retry_after_s)。

    匹配三类：
    - HTTP 429（OpenAI / sub2api 直接透传）
    - error_code=rate_limit_error / rate_limit_exceeded
    - message 含 quota / "rate limit" / "concurrency limit exceeded"

    retry_after_s 优先来自 UpstreamError.payload 里的 retry_after / retry_after_seconds，
    取不到时返回 None 让上游用默认 cooldown（_IMAGE_RATE_LIMITED_DEFAULT_S）。
    """
    if not isinstance(exc, UpstreamError):
        return False, None
    code = (getattr(exc, "error_code", None) or "").lower()
    msg = str(exc).lower()
    if (
        exc.status_code == 429
        or code in ("rate_limit_error", "rate_limit_exceeded")
        or "rate limit" in msg
        or "rate_limit" in msg
        or "quota" in msg
        or "concurrency limit exceeded" in msg
    ):
        return True, _retry_after_seconds(exc)
    return False, None


async def _direct_generate_image_with_failover(
    *,
    prompt: str,
    size: str,
    n: int,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
) -> tuple[str, str | None]:
    """Layer 2 provider failover for direct gpt-image-2 text-to-image."""
    from . import account_limiter
    from .retry import is_retriable as classify_retriable

    pool = await provider_pool.get_pool()
    providers = (
        [provider_override]
        if provider_override is not None
        else await _pool_select_compat(
            pool,
            route="image",
            ignore_cooldown=True,
            endpoint_kind="generations",
        )
    )
    errors: list[BaseException] = []

    for i, provider in enumerate(providers):
        lock_error = _provider_endpoint_locked_error(provider, "generations")
        if lock_error is not None:
            errors.append(lock_error)
            continue
        try:
            kwargs: dict[str, Any] = {
                "prompt": prompt,
                "size": size,
                "n": n,
                "quality": quality,
                "output_format": output_format,
                "output_compression": output_compression,
                "background": background,
                "moderation": moderation,
                "base_url_override": provider.base_url,
                "api_key_override": provider.api_key,
            }
            proxy = _provider_proxy(provider)
            if proxy is not None:
                kwargs["proxy_override"] = proxy
            result = await _direct_generate_image_once(**kwargs)
            pool.report_image_success(provider.name)
            await account_limiter.record_image_call(
                _provider_pool_redis(pool), provider.name
            )
            await _emit_image_progress(
                progress_callback,
                "provider_used",
                provider=provider.name,
                route="image2",
                source="image2_direct",
                endpoint="images/generations",
            )
            await _emit_image_progress(
                progress_callback,
                "final_image",
                source="image2_direct",
            )
            await _emit_image_progress(
                progress_callback,
                "completed",
                source="image2_direct",
            )
            return result
        except (asyncio.CancelledError, UpstreamCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            decision = classify_retriable(
                getattr(exc, "error_code", None),
                getattr(exc, "status_code", None),
                error_message=str(exc),
            )
            should_continue = _should_continue_image_provider_failover(
                exc,
                retriable=decision.retriable,
            )
            if not should_continue:
                logger.warning(
                    "direct image provider %s terminal error: %s",
                    provider.name,
                    decision.reason,
                )
                raise
            is_rl, retry_after = _is_image_rate_limit_error(exc)
            if is_rl:
                pool.report_image_rate_limited(
                    provider.name, retry_after_s=retry_after
                )
            else:
                pool.report_image_failure(provider.name)
            remaining = len(providers) - i - 1
            if remaining > 0:
                logger.warning(
                    "direct image provider_failover: from=%s remaining=%d reason=%s",
                    provider.name,
                    remaining,
                    decision.reason,
                )
                # P2: 把"换号"通知给前端，避免用户在 retriable 错误时看到长时间无响应。
                # 调用方（generation.publish_image_progress）会把它转成 SSE
                # generation.progress(substage=provider_selected, provider_failover=true)。
                await _emit_image_progress(
                    progress_callback,
                    "provider_failover",
                    from_provider=provider.name,
                    remaining=remaining,
                    reason=decision.reason,
                    route="image2_direct",
                )

    merged = _merge_fallback_errors(
        errors,
        error_code=EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} direct image providers failed",
    )
    merged.payload["provider_errors"] = _provider_error_details(providers, errors)
    raise merged


# Sidecar error classes that should keep the image-job failover machine moving.
# They map to the two axes the caller can still try:
# - endpoint kind: generations <-> responses on the same provider
# - provider/account: next configured provider after endpoint choices are spent
_IMAGE_JOB_FAILOVER_CLASSES = frozenset(
    {"network", "upstream_5xx", "no_image", "image_save", "internal"}
)


def _image_jobs_endpoint_fallback_chain(primary: str) -> list[str]:
    if primary == "generations":
        return ["generations", "responses"]
    if primary == "responses":
        return ["responses", "generations"]
    return ["generations", "responses"]


def _image_job_error_class(exc: BaseException) -> str | None:
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        error_class = payload.get("image_job_error_class")
        return error_class if isinstance(error_class, str) else None
    return None


def _should_continue_image_job_failover(
    exc: BaseException,
    *,
    retriable: bool,
) -> bool:
    """True when endpoint/provider failover may still recover this image job."""
    if retriable:
        return True
    error_class = _image_job_error_class(exc)
    if error_class in _IMAGE_JOB_FAILOVER_CLASSES:
        return True
    if isinstance(exc, UpstreamError):
        if exc.status_code == 429:
            return True
        if exc.status_code is not None and 500 <= exc.status_code < 600:
            return True
        if exc.error_code in {
            EC.NO_IMAGE_RETURNED.value,
            EC.UPSTREAM_TIMEOUT.value,
            EC.TIMEOUT.value,
            EC.DIRECT_IMAGE_REQUEST_FAILED.value,
        }:
            return True
    return isinstance(exc, _RETRY_HTTPX_EXC)


async def _image_job_run_once(
    *,
    action: str,
    endpoint: str,
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
    api_key: str,
    base_url: str,
    proxy: ProviderProxyDefinition | None,
    progress_callback: ImageProgressCallback | None,
) -> tuple[str, str | None]:
    """Single image-job submit dispatched by (action, endpoint).

    endpoint is the high-level kind: ``generations`` (which means generations or
    edits depending on action) or ``responses``. Choosing /v1/images/generations
    vs /v1/images/edits is action-driven and stays inside the once functions.
    """
    common: dict[str, Any] = {
        "prompt": prompt,
        "size": size,
        "n": n,
        "quality": quality,
        "output_format": output_format,
        "output_compression": output_compression,
        "background": background,
        "moderation": moderation,
        "api_key_override": api_key,
        "base_url_override": base_url or None,
        "progress_callback": progress_callback,
    }
    if proxy is not None:
        common["proxy_override"] = proxy
    if endpoint == "responses":
        return await _image_job_responses_once(
            action=action,
            images=images,
            model=model,
            **common,
        )
    if action == "edit":
        if not images:
            raise UpstreamError(
                "edit action requires at least one reference image",
                error_code=EC.MISSING_INPUT_IMAGES.value,
                status_code=400,
            )
        return await _image_job_edit_once(images=images, **common)
    return await _image_job_generate_once(**common)


async def _image_job_with_failover(
    *,
    action: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    n: int,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
    endpoint_override: str | None = None,
    endpoint_preference: str | None = None,
) -> tuple[str, str | None]:
    """Two-axis failover for the async image-job route.

    Outer loop: provider failover (existing semantics — circuit breaker, rate
    limit, account quota all live in ProviderPool).

    Inner loop: per-provider endpoint failover. The sidecar tags every failed
    job with an ``error_class``; recoverable classes and retriable statuses
    (502 / 429 / no_image / timeout, etc.) try the next endpoint on the same
    provider before consuming a provider failover slot.

    Endpoint ordering comes from ``ProviderPool.endpoint_chain`` which
    combines per-provider preference (``image_jobs_endpoint``) with auto-mode
    learning from per-endpoint health stats.

    Per-provider sidecar URL: when ``provider.image_jobs_base_url`` is set we
    use it; otherwise we fall back to the global ``image.job_base_url``
    runtime setting.

    ``endpoint_override`` 让 dual_race 的两条 lane 各自锁定一个 endpoint kind
    （``generations`` / ``responses``），跑完整的 provider failover 链但**不
    切换 endpoint**——切换由 race 层负责（另一条 lane 已经在跑另一种）。

    ``endpoint_preference`` 是普通单路模式的首选 endpoint；失败时仍然尝试另一条
    生图 endpoint，保证 image2 / responses 互为 fallback。
    """
    from . import account_limiter
    from .retry import is_retriable as classify_retriable

    pool = await provider_pool.get_pool()
    # 当本次调用已经锁定了具体 endpoint kind（来自 dual_race lane 的
    # endpoint_override，或单路 image_jobs 的 endpoint_preference），把它透传给
    # provider 选号——locked 但 endpoint 不一致的号会在那一层被剔除，进入这里
    # 的候选不会出现"选了号又被 failover 跳过"的尴尬。
    forced_kind: str | None = None
    if endpoint_override in ("generations", "responses"):
        forced_kind = endpoint_override
    elif endpoint_preference in ("generations", "responses"):
        forced_kind = endpoint_preference
    providers = (
        [provider_override]
        if provider_override is not None
        else await _pool_select_compat(
            pool,
            route="image_jobs",
            ignore_cooldown=True,
            endpoint_kind=forced_kind,
        )
    )
    errors: list[BaseException] = []

    source_label = "image_jobs" if action == "generate" else "image_jobs_edit"
    fallback_base_url = await _resolve_image_job_base_url()

    for i, provider in enumerate(providers):
        configured_endpoint = getattr(provider, "image_jobs_endpoint", "auto")
        endpoint_locked = bool(
            getattr(provider, "image_jobs_endpoint_lock", False)
        ) and configured_endpoint in ("generations", "responses")

        # lock 防御层：override / preference 任一与本号 lock 冲突都视为本号
        # 不可用——dual_race lane 由对端 lane 兜底，单路场景由下一个号兜底。
        # 不再有"override 强制跑对端 endpoint"或"preference 被 lock 静默改写"
        # 的隐式路径；所有锁定不一致都通过 _provider_endpoint_locked_error
        # 统一报到 errors 列表，便于上层 merge 后生成可观测的失败聚合。
        conflict_kind: str | None = None
        if endpoint_override in ("generations", "responses"):
            conflict_kind = endpoint_override
        elif endpoint_preference in ("generations", "responses"):
            conflict_kind = endpoint_preference
        if conflict_kind is not None:
            locked_err = _provider_endpoint_locked_error(provider, conflict_kind)
            if locked_err is not None:
                logger.info(
                    "image_jobs skip locked provider=%s configured=%s requested_kind=%s",
                    getattr(provider, "name", "unknown"),
                    configured_endpoint,
                    conflict_kind,
                )
                errors.append(locked_err)
                continue

        if endpoint_override is not None:
            endpoint_chain = [endpoint_override]
        elif endpoint_locked:
            # lock 已通过上面的 conflict_kind 校验保证与 preference 一致（或
            # preference=None 时由本号自决），这里直接锁单 endpoint。
            endpoint_chain = [configured_endpoint]
        elif endpoint_preference is not None:
            endpoint_chain = _image_jobs_endpoint_fallback_chain(endpoint_preference)
        else:
            endpoint_chain = pool.endpoint_chain(
                provider.name, action, configured_endpoint
            )
        provider_base_url = (
            getattr(provider, "image_jobs_base_url", "") or fallback_base_url
        )

        provider_done = False
        last_exc: BaseException | None = None
        for ep_idx, endpoint in enumerate(endpoint_chain):
            ep_remaining = len(endpoint_chain) - ep_idx - 1
            started = time.monotonic()
            try:
                result = await _image_job_run_once(
                    action=action,
                    endpoint=endpoint,
                    prompt=prompt,
                    size=size,
                    images=images,
                    n=n,
                    quality=quality,
                    output_format=output_format,
                    output_compression=output_compression,
                    background=background,
                    moderation=moderation,
                    model=model,
                    api_key=provider.api_key,
                    base_url=provider_base_url,
                    proxy=_provider_proxy(provider),
                    progress_callback=progress_callback,
                )
            except (asyncio.CancelledError, UpstreamCancelled):
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                pool.record_endpoint_failure(provider.name, endpoint)
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                error_class = _image_job_error_class(exc)
                logger.warning(
                    "image job %s/%s endpoint=%s error_class=%s decision=%s: %r",
                    action,
                    provider.name,
                    endpoint,
                    error_class,
                    decision.reason,
                    exc,
                )
                if ep_remaining > 0:
                    await _emit_image_progress(
                        progress_callback,
                        "endpoint_failover",
                        provider=provider.name,
                        from_endpoint=endpoint,
                        remaining=ep_remaining,
                        reason=error_class or decision.reason,
                        route="image_jobs",
                    )
                    continue
                should_continue = _should_continue_image_job_failover(
                    exc,
                    retriable=_should_continue_image_provider_failover(
                        exc,
                        retriable=decision.retriable,
                    ),
                )
                if not should_continue:
                    raise
                # Bubble up to the provider-level failover branch.
                break
            else:
                # Success path.
                latency_ms = (time.monotonic() - started) * 1000.0
                pool.record_endpoint_success(
                    provider.name, endpoint, latency_ms=latency_ms
                )
                pool.report_image_success(provider.name)
                await account_limiter.record_image_call(
                    _provider_pool_redis(pool), provider.name
                )
                await _emit_image_progress(
                    progress_callback,
                    "provider_used",
                    provider=provider.name,
                    route="image_jobs",
                    source=source_label,
                    endpoint=f"image-jobs:{endpoint}",
                )
                await _emit_image_progress(
                    progress_callback,
                    "final_image",
                    source=source_label,
                    endpoint_used=endpoint,
                )
                await _emit_image_progress(
                    progress_callback,
                    "completed",
                    source=source_label,
                    endpoint_used=endpoint,
                )
                provider_done = True
                return result

        if provider_done:
            return  # unreachable — return inside the loop already happened.

        # Provider failover (mirrors the original semantics).
        if last_exc is None:
            continue
        errors.append(last_exc)
        is_rl, retry_after = _is_image_rate_limit_error(last_exc)
        if is_rl:
            pool.report_image_rate_limited(
                provider.name, retry_after_s=retry_after
            )
        else:
            pool.report_image_failure(provider.name)
        remaining = len(providers) - i - 1
        if remaining > 0:
            logger.warning(
                "image job provider_failover: from=%s remaining=%d action=%s",
                provider.name,
                remaining,
                action,
            )
            await _emit_image_progress(
                progress_callback,
                "provider_failover",
                from_provider=provider.name,
                remaining=remaining,
                reason="image_job_failed",
                route="image_jobs",
            )

    merged = _merge_fallback_errors(
        errors,
        error_code=EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} image job providers failed",
    )
    merged.payload["provider_errors"] = _provider_error_details(providers, errors)
    raise merged


# Compatibility shims: keep the old names so any unforeseen caller (and
# eyeballs reading recent logs) finds them. Both delegate to the unified
# implementation. Safe to delete in a follow-up once we confirm no callers
# remain outside this file.

async def _image_job_generate_with_failover(
    *,
    prompt: str,
    size: str,
    n: int,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
) -> tuple[str, str | None]:
    return await _image_job_with_failover(
        action="generate",
        prompt=prompt,
        size=size,
        images=None,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
        progress_callback=progress_callback,
        provider_override=provider_override,
    )


async def _image_job_edit_with_failover(
    *,
    prompt: str,
    size: str,
    images: list[bytes],
    n: int,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
) -> tuple[str, str | None]:
    return await _image_job_with_failover(
        action="edit",
        prompt=prompt,
        size=size,
        images=images,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
        progress_callback=progress_callback,
        provider_override=provider_override,
    )


async def _direct_edit_image_with_failover(
    *,
    prompt: str,
    size: str,
    images: list[bytes],
    n: int,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
) -> tuple[str, str | None]:
    """Layer 2 provider failover for direct gpt-image-2 image-to-image (/v1/images/edits).

    与 `_direct_generate_image_with_failover` 对称：复用同一个 image route 池
    （`pool.select(route="image")`），失败后切下一个 provider；认证/参数等
    terminal 错误直接 raise。安全策略类拒绝允许继续切 provider，因为不同上游策略可能不同。
    上层 `edit_image` 在 image2 模式下调用本函数；本函数耗尽所有 provider 后抛
    ALL_DIRECT_IMAGE_PROVIDERS_FAILED，由 `edit_image` 捕获并 fallback 到 responses。
    """
    from . import account_limiter
    from .retry import is_retriable as classify_retriable

    pool = await provider_pool.get_pool()
    providers = (
        [provider_override]
        if provider_override is not None
        else await _pool_select_compat(
            pool,
            route="image",
            ignore_cooldown=True,
            endpoint_kind="generations",
        )
    )
    errors: list[BaseException] = []

    for i, provider in enumerate(providers):
        lock_error = _provider_endpoint_locked_error(provider, "generations")
        if lock_error is not None:
            errors.append(lock_error)
            continue
        try:
            kwargs: dict[str, Any] = {
                "prompt": prompt,
                "size": size,
                "images": images,
                "n": n,
                "quality": quality,
                "output_format": output_format,
                "output_compression": output_compression,
                "background": background,
                "moderation": moderation,
                "base_url_override": provider.base_url,
                "api_key_override": provider.api_key,
            }
            proxy = _provider_proxy(provider)
            if proxy is not None:
                kwargs["proxy_override"] = proxy
            result = await _direct_edit_image_once(**kwargs)
            pool.report_image_success(provider.name)
            await account_limiter.record_image_call(
                _provider_pool_redis(pool), provider.name
            )
            await _emit_image_progress(
                progress_callback,
                "provider_used",
                provider=provider.name,
                route="image2",
                source="image2_edit_direct",
                endpoint="images/edits",
            )
            await _emit_image_progress(
                progress_callback,
                "final_image",
                source="image2_edit_direct",
            )
            await _emit_image_progress(
                progress_callback,
                "completed",
                source="image2_edit_direct",
            )
            return result
        except (asyncio.CancelledError, UpstreamCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            decision = classify_retriable(
                getattr(exc, "error_code", None),
                getattr(exc, "status_code", None),
                error_message=str(exc),
            )
            should_continue = _should_continue_image_provider_failover(
                exc,
                retriable=decision.retriable,
            )
            if not should_continue:
                logger.warning(
                    "direct edit provider %s terminal error: %s",
                    provider.name,
                    decision.reason,
                )
                raise
            is_rl, retry_after = _is_image_rate_limit_error(exc)
            if is_rl:
                pool.report_image_rate_limited(
                    provider.name, retry_after_s=retry_after
                )
            else:
                pool.report_image_failure(provider.name)
            remaining = len(providers) - i - 1
            if remaining > 0:
                logger.warning(
                    "direct edit provider_failover: from=%s remaining=%d reason=%s",
                    provider.name,
                    remaining,
                    decision.reason,
                )
                await _emit_image_progress(
                    progress_callback,
                    "provider_failover",
                    from_provider=provider.name,
                    remaining=remaining,
                    reason=decision.reason,
                    route="image2_edit_direct",
                )

    merged = _merge_fallback_errors(
        errors,
        error_code=EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} direct edit providers failed",
    )
    merged.payload["provider_errors"] = _provider_error_details(providers, errors)
    raise merged


async def _responses_image_stream_with_failover(
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
    task_id: str = "",
    provider_override: Any | None = None,
) -> tuple[str, str | None]:
    """Layer 2: provider failover。retriable/策略差异错误立即切下一个 provider（零延迟）。

    image route 走 pool.select(route="image")：每个 provider = 一个账号，调度按
    image_last_used_at 升序选最久未用，自然分散到不同账号；429 / quota 触发的
    号自动 cooldown，普通失败累计 3 次后 image circuit 熔断 60s。认证 / 参数错等
    terminal 错误不 failover；审核/安全策略类拒绝允许切 provider。
    """
    from . import account_limiter
    from .retry import is_retriable as classify_retriable

    pool = await provider_pool.get_pool()
    providers = (
        [provider_override]
        if provider_override is not None
        else await _pool_select_compat(
            pool,
            route="image",
            ignore_cooldown=True,
            endpoint_kind="responses",
        )
    )
    errors: list[BaseException] = []

    for i, provider in enumerate(providers):
        lock_error = _provider_endpoint_locked_error(provider, "responses")
        if lock_error is not None:
            errors.append(lock_error)
            continue
        try:
            kwargs: dict[str, Any] = {
                "prompt": prompt,
                "size": size,
                "action": action,
                "images": images,
                "quality": quality,
                "output_format": output_format,
                "output_compression": output_compression,
                "background": background,
                "moderation": moderation,
                "model": model,
                "progress_callback": progress_callback,
                "use_httpx": use_httpx,
                "base_url_override": provider.base_url,
                "api_key_override": provider.api_key,
            }
            proxy = _provider_proxy(provider)
            if proxy is not None:
                kwargs["proxy_override"] = proxy
            result = await _responses_image_stream_with_retry(**kwargs)
            pool.report_image_success(provider.name)
            # 入账：滑动窗口 + 当日计数（rate_limit/daily_quota 都为空时短路不查 Redis）
            await account_limiter.record_image_call(
                _provider_pool_redis(pool), provider.name, task_id=task_id
            )
            await _emit_image_progress(
                progress_callback,
                "provider_used",
                provider=provider.name,
                route="responses",
                source="responses",
                endpoint="responses:image_generation",
            )
            return result
        except (asyncio.CancelledError, UpstreamCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            decision = classify_retriable(
                getattr(exc, "error_code", None),
                getattr(exc, "status_code", None),
                error_message=str(exc),
            )
            should_continue = _should_continue_image_provider_failover(
                exc,
                retriable=decision.retriable,
            )
            if not should_continue:
                # terminal（invalid_request/auth）：输入或配置问题，换号也一样，不动 image health 计数。
                logger.warning(
                    "provider %s terminal error, not failing over: %s",
                    provider.name,
                    decision.reason,
                )
                raise
            # 以下都是 retriable：分流到 image_rate_limited（号没额度，定时冷却）
            # 或 image_failure（号在抖动，3 次累计触发 image cooldown）。
            is_rl, retry_after = _is_image_rate_limit_error(exc)
            if is_rl:
                pool.report_image_rate_limited(
                    provider.name, retry_after_s=retry_after
                )
            else:
                pool.report_image_failure(provider.name)
            remaining = len(providers) - i - 1
            if remaining > 0:
                logger.warning(
                    "provider_failover: from=%s remaining=%d reason=%s",
                    provider.name,
                    remaining,
                    decision.reason,
                )
                # P2: 同 _direct_generate_image_with_failover——把切号通知给前端。
                await _emit_image_progress(
                    progress_callback,
                    "provider_failover",
                    from_provider=provider.name,
                    remaining=remaining,
                    reason=decision.reason,
                    route="responses",
                )

    merged = _merge_fallback_errors(
        errors,
        error_code=EC.ALL_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} upstream providers failed",
    )
    merged.payload["provider_errors"] = _provider_error_details(providers, errors)
    raise merged


async def _race_responses_image(
    *,
    action: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    lanes: int,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
) -> tuple[str, str | None]:
    """按 lanes 数并发 /v1/responses SSE 请求，first-win cancel 其他。

    lanes=1 → 单路（无 race 开销）。lanes>=2 → lane 0 走 curl、lane 1 走 httpx
    （client 异构冗余：不同 TLS 指纹 / header 组合，某一路偶发挂时另一路可能救回）；
    lane ≥2 继续用 curl 做多并发保险。

    大图（>_RACE_SINGLE_LANE_PIXELS，≈2M 像素及以上，含 4K 3840x2160）强制单 lane——
    老 gateway 对同账号 4K 并发敏感，race 会把单请求能过的 4K 打成 server_error，
    见 §test-summary §11 "Concurrency limit exceeded"。

    每条 lane 内部走 provider failover（Layer 2）。
    """
    if provider_override is not None:
        lanes = 1
    pixels = _parse_size_pixels(size)
    if pixels is not None and pixels > _RACE_SINGLE_LANE_PIXELS:
        lanes = 1
    if lanes <= 1:
        return await _responses_image_stream_with_failover(
            prompt=prompt,
            size=size,
            action=action,
            images=images,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=progress_callback,
            use_httpx=False,
            provider_override=provider_override,
        )

    async def _metadata_only_progress(event: dict[str, Any]) -> None:
        if event.get("type") != "provider_used":
            return
        await _emit_image_progress(
            progress_callback,
            "provider_used",
            provider=event.get("provider"),
            route=event.get("route"),
            source=event.get("source"),
            endpoint=event.get("endpoint"),
        )

    async def _run_lane(idx: int) -> tuple[str, str | None]:
        # 仅 lane 0 透传 progress——其他 lane 不发事件，避免前端进度抖动
        cb = progress_callback if idx == 0 else _metadata_only_progress
        use_httpx = idx == 1
        return await _responses_image_stream_with_failover(
            prompt=prompt,
            size=size,
            action=action,
            images=images,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=cb,
            use_httpx=use_httpx,
            provider_override=provider_override,
        )

    tasks: list[asyncio.Task[tuple[str, str | None]]] = [
        asyncio.create_task(_run_lane(i), name=f"{action}-race-lane-{i}")
        for i in range(lanes)
    ]
    errors: list[BaseException] = []
    try:
        pending: set[asyncio.Task[tuple[str, str | None]]] = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for finished in done:
                exc = finished.exception()
                if exc is None:
                    winner_name = finished.get_name()
                    losers = [t for t in pending if not t.done()]
                    for loser in losers:
                        loser.cancel()
                    if losers:
                        await asyncio.gather(*losers, return_exceptions=True)
                    logger.info(
                        "%s race: %s won, cancelled %d lane(s)",
                        action, winner_name, len(losers),
                    )
                    return finished.result()
                # GEN-P1-4: 调用方主动取消 → 立即 cancel 残余 lane 并透传，不再 race。
                if isinstance(exc, UpstreamCancelled):
                    losers = [t for t in pending if not t.done()]
                    for loser in losers:
                        loser.cancel()
                    if losers:
                        await asyncio.gather(*losers, return_exceptions=True)
                    logger.info(
                        "%s race: cancelled by caller; aborting %d lane(s)",
                        action, len(losers),
                    )
                    raise exc
                errors.append(exc)
                logger.warning(
                    "%s race: %s failed: %r", action, finished.get_name(), exc
                )
        # GEN-P0-9: 全部 lane 失败——把每条 lane 的异常摘要打到 WARN 级，
        # 并在 merged 里附带 ExceptionGroup（Python 3.11+，_merge_fallback_errors
        # 内部处理）；方便线上诊断 4K 降级是单点问题还是多路都炸。
        logger.warning(
            "%s race: all %d lane(s) failed; summaries=%s",
            action,
            len(errors),
            json.dumps(
                [_summarize_exception(e) for e in errors],
                ensure_ascii=False,
            )[:2000],
        )
        raise _merge_fallback_errors(
            errors,
            error_code=EC.FALLBACK_LANES_FAILED.value,
            message=f"{action} fallback lanes all failed",
        )
    finally:
        # 兜底：GEN-P0-9 确保残留 lane 被 cancel，并 gather(return_exceptions=True) 收割，
        # 避免泄漏 Task 造成 "Task exception was never retrieved" noisy log。
        leftovers = [t for t in tasks if not t.done()]
        for t in leftovers:
            t.cancel()
        if leftovers:
            try:
                await asyncio.gather(*leftovers, return_exceptions=True)
            except Exception:  # noqa: BLE001
                pass


# dual_race "bonus 图" 宽限期：winner yield 后给 loser 多少时间出图。
# 普通图 60s / 4K 图 90s——4K 渲染 + base64 编码可能比 winner 多 1-2 分钟，
# 给点余地；超时则静默 cancel，只显示 winner，不浪费上游已生成的内容也不拖长 task。
_DUAL_RACE_BONUS_GRACE_S = 60.0
_DUAL_RACE_BONUS_GRACE_4K_S = 90.0


async def _dual_race_image_action(
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
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None,
    allow_provider_override_race: bool = False,
) -> AsyncIterator[tuple[str, str | None]]:
    """dual_race: image2 直连 + responses 两条路径同时跑（async generator）。

    新行为（2026-04 起）：winner 完成后**不再 cancel loser**——loser 继续跑，若也
    成功则二次 yield 让 caller 把 bonus 图挂到同一条 assistant message 上；loser
    失败/超时则静默吞掉，用户只看到 winner。grace：普通 60s / 4K 90s。

    yield 次数：
      - 两路都失败 → 抛 fallback_lanes_failed，0 yield
      - winner 成功 / loser 失败或超时 → 1 yield
      - winner 成功 / loser 也成功 → 2 yield（caller 用 async for 消费）

    progress 只透传 image2 一路，responses 静默；caller 提前 aclose 时 finally 段
    cancel 残余 lane（cancellation safe）。

    默认保持历史语义：provider_override 给定时不进 race，走 responses 单路。
    新 channel/engine dispatcher 会传 allow_provider_override_race=True，让同一
    provider 在 stream 通道下跑 image2 + responses 双 lane。

    image-jobs 通道由上层 channel/engine dispatcher 显式选择；本函数只负责
    stream 通道下的 image2 + responses 竞速。
    """
    if provider_override is not None and not allow_provider_override_race:
        result = await _responses_image_stream_with_failover(
            prompt=prompt,
            size=size,
            action=action,
            images=images,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=progress_callback,
            use_httpx=False,
            provider_override=provider_override,
        )
        yield result
        return

    async def _metadata_only_progress(event: dict[str, Any]) -> None:
        if event.get("type") != "provider_used":
            return
        await _emit_image_progress(
            progress_callback,
            "provider_used",
            provider=event.get("provider"),
            route=event.get("route"),
            source=event.get("source"),
            endpoint=event.get("endpoint"),
        )

    async def _lane_image2() -> tuple[str, str | None]:
        if action == "edit":
            if not images:
                raise UpstreamError(
                    "edit action requires at least one reference image",
                    error_code=EC.MISSING_INPUT_IMAGES.value,
                    status_code=400,
                )
            return await _direct_edit_image_with_failover(
                prompt=prompt,
                size=size,
                images=images,
                n=n,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
                progress_callback=progress_callback,
                provider_override=provider_override,
            )
        return await _direct_generate_image_with_failover(
            prompt=prompt,
            size=size,
            n=n,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            progress_callback=progress_callback,
            provider_override=provider_override,
        )

    async def _lane_responses() -> tuple[str, str | None]:
        return await _responses_image_stream_with_failover(
            prompt=prompt,
            size=size,
            action=action,
            images=images,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=_metadata_only_progress,
            use_httpx=False,
            provider_override=provider_override,
        )

    pixels = _parse_size_pixels(size)
    grace_s = (
        _DUAL_RACE_BONUS_GRACE_4K_S
        if pixels is not None and pixels > _IMAGE_4K_PIXELS
        else _DUAL_RACE_BONUS_GRACE_S
    )

    tasks: list[asyncio.Task[tuple[str, str | None]]] = [
        asyncio.create_task(_lane_image2(), name=f"{action}-dual-image2"),
        asyncio.create_task(_lane_responses(), name=f"{action}-dual-responses"),
    ]
    lane_names: dict[asyncio.Task[Any], str] = {
        tasks[0]: "image2",
        tasks[1]: "responses",
    }
    errors: list[tuple[str, BaseException]] = []
    pending: set[asyncio.Task[tuple[str, str | None]]] = set(tasks)
    winner_yielded = False
    try:
        # Phase 1：race 至有一路成功（不 cancel loser）；两路都失败则抛错。
        while pending and not winner_yielded:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for finished in done:
                lane_name = lane_names[finished]
                exc = finished.exception()
                if exc is None:
                    logger.info(
                        "%s dual_race: %s won, loser keeps running (grace=%.0fs)",
                        action, lane_name, grace_s,
                    )
                    winner_yielded = True
                    yield finished.result()
                    break  # 跳出 for；while 由 winner_yielded 控制
                if isinstance(exc, UpstreamCancelled):
                    # caller 取消 → finally 段会收割残余 lane
                    raise exc
                errors.append((lane_name, exc))
                logger.warning(
                    "%s dual_race: %s failed: %r", action, lane_name, exc
                )

        if not winner_yielded:
            logger.warning(
                "%s dual_race: both lanes failed; summaries=%s",
                action,
                json.dumps(
                    [{"lane": ln, **_summarize_exception(e)} for ln, e in errors],
                    ensure_ascii=False,
                )[:2000],
            )
            merged_msg = " | ".join(f"[{ln}] {exc!s}" for ln, exc in errors)
            raise _merge_fallback_errors(
                [e for _, e in errors],
                error_code=EC.FALLBACK_LANES_FAILED.value,
                message=f"{action} dual_race: {merged_msg}",
            )

        # Phase 2：winner 已 yield；等 loser 在 grace 内完成。注意 caller 的 finalize
        # 工作（写 storage / DB / publish SSE）发生在 yield 控制权交给 caller 期间，
        # 重新 next() 后才进入这里——所以 grace 计时从 caller 完成 finalize 才开始。
        if pending:
            done, still = await asyncio.wait(
                pending,
                timeout=grace_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if still:
                for t in still:
                    t.cancel()
                await asyncio.gather(*still, return_exceptions=True)
                logger.info(
                    "%s dual_race: loser exceeded grace=%.0fs, cancelled silently",
                    action, grace_s,
                )
                return
            for finished in done:
                lane_name = lane_names[finished]
                exc = finished.exception()
                if exc is None:
                    logger.info(
                        "%s dual_race: bonus from %s succeeded", action, lane_name
                    )
                    yield finished.result()
                    return
                if isinstance(exc, UpstreamCancelled):
                    # 极少见：loser 自己被上游取消；视同失败静默吞。
                    return
                logger.info(
                    "%s dual_race: bonus %s failed silently: %r",
                    action, lane_name, exc,
                )
                return
    finally:
        leftovers = [t for t in tasks if not t.done()]
        for t in leftovers:
            t.cancel()
        if leftovers:
            try:
                await asyncio.gather(*leftovers, return_exceptions=True)
            except Exception:  # noqa: BLE001
                pass


# image_jobs dual_race 的 bonus grace。
#
# 取值依据（不浪费 bonus 图优先）
# ================================
# image-job 路径全程异步轮询：generations 端点和 responses 端点的实际耗时
# 差距比 image2 直连大（1K/2K 1-2min 差距常见，4K 2-4min 差距常见）。
# grace 太短会让 loser 还在轮询时被 cancel，bonus 图就被废了——而用户
# 既然开了 dual_race 就是不希望浪费任何已经在跑的算力。
#
# 时间预算（memory: task 1500s / upstream 660s envelope）：
# - 1K/2K：winner ~60-120s + finalize ~3s + grace 120s = ~245s，预算内
# - 4K   ：winner ~240s + finalize ~5s + grace 300s    = ~545s，预算内
#
# 上限由 image-job sidecar 单 lane 自身的 _IMAGE_JOB_TIMEOUT_S=1200s 兜底，
# loser 自然会在那条 lane 内部超时 → 静默吞掉，不会无限挂着 SSE。
_DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S = 120.0
_DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_4K_S = 300.0


async def _dual_race_image_jobs_action(
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
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
) -> AsyncIterator[tuple[str, str | None]]:
    """号池版 dual_race：两条 lane 都通过 image-job sidecar 提交，但 endpoint
    一个 ``generations`` 一个 ``responses``。

    设计动机
    ========
    号池场景（gateway 后接多账号）单上游对 lumen 表面是"1 个 provider"，但
    上游 gateway 内部按账号轮询。两条不同 endpoint 的并发请求会被分给两个
    不同账号，吃到真正的并发收益——这是单路 image_jobs（串行 endpoint
    failover）拿不到的。

    鲁棒性
    ======
    - 每条 lane 内部仍跑完整 ``_image_job_with_failover`` provider 链，但
      ``endpoint_override`` 锁定单个 endpoint kind（race 层负责 endpoint 互补）。
    - winner 优先 yield，loser 在 grace 内继续（不 cancel）；loser 也成功 → 二次
      yield bonus 图；超时 / 失败 → 静默吞掉。grace 比 image2 race 短一档，因为
      image-job 路径整体更慢，没必要让用户等 90s+ bonus。
    - **4K 也跑 race**——号池上游会把两条不同 endpoint 的请求分到两个不同账号，
      不存在 _race_responses_image 那种"同账号双 4K 打挂"的问题。4K 反而是
      并发收益最大的场景（单图慢、并发省时间最明显），所以只把 grace 拉长到
      75s 让 bonus 也有机会落地，不强制单 lane。
    - progress callback 只透传 generations lane（与现有 dual_race 设计一致：
      避免重复推 final_image / completed）；responses lane 只透传
      ``provider_used`` 元事件。
    - 两路都失败时抛 ``fallback_lanes_failed``，错误聚合两条 lane 的 provider
      失败明细，调用方接住后会 fallback 到 image2/responses 路径。
    - cancellation safe：caller 提前 aclose / cancel 时 finally 收割残余 lane。

    yield 次数
    ==========
      - 两路都失败 → raise，0 yield
      - winner 成功 / loser 失败或超时 → 1 yield
      - winner 成功 / loser 也成功 → 2 yield
    """
    pixels = _parse_size_pixels(size)
    grace_s = (
        _DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_4K_S
        if pixels is not None and pixels > _IMAGE_4K_PIXELS
        else _DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S
    )

    async def _metadata_only_progress(event: dict[str, Any]) -> None:
        if event.get("type") != "provider_used":
            return
        await _emit_image_progress(
            progress_callback,
            "provider_used",
            provider=event.get("provider"),
            route=event.get("route"),
            source=event.get("source"),
            endpoint=event.get("endpoint"),
        )

    async def _lane(endpoint: str, lane_progress: ImageProgressCallback | None) -> tuple[str, str | None]:
        return await _image_job_with_failover(
            action=action,
            prompt=prompt,
            size=size,
            images=images,
            n=n,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=lane_progress,
            provider_override=provider_override,
            endpoint_override=endpoint,
        )

    tasks: list[asyncio.Task[tuple[str, str | None]]] = [
        asyncio.create_task(
            _lane("generations", progress_callback),
            name=f"{action}-image-jobs-dual-generations",
        ),
        asyncio.create_task(
            _lane("responses", _metadata_only_progress),
            name=f"{action}-image-jobs-dual-responses",
        ),
    ]
    lane_names: dict[asyncio.Task[Any], str] = {
        tasks[0]: "image_jobs:generations",
        tasks[1]: "image_jobs:responses",
    }
    errors: list[tuple[str, BaseException]] = []
    pending: set[asyncio.Task[tuple[str, str | None]]] = set(tasks)
    winner_yielded = False
    try:
        # Phase 1：race 至有一路成功；两路都失败则抛 fallback_lanes_failed。
        while pending and not winner_yielded:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for finished in done:
                lane_name = lane_names[finished]
                exc = finished.exception()
                if exc is None:
                    logger.info(
                        "%s image_jobs dual_race: %s won, loser keeps running (grace=%.0fs)",
                        action, lane_name, grace_s,
                    )
                    winner_yielded = True
                    yield finished.result()
                    break
                if isinstance(exc, UpstreamCancelled):
                    raise exc
                errors.append((lane_name, exc))
                logger.warning(
                    "%s image_jobs dual_race: %s failed: %r",
                    action, lane_name, exc,
                )

        if not winner_yielded:
            logger.warning(
                "%s image_jobs dual_race: both lanes failed; summaries=%s",
                action,
                json.dumps(
                    [{"lane": ln, **_summarize_exception(e)} for ln, e in errors],
                    ensure_ascii=False,
                )[:2000],
            )
            merged_msg = " | ".join(f"[{ln}] {exc!s}" for ln, exc in errors)
            raise _merge_fallback_errors(
                [e for _, e in errors],
                error_code=EC.FALLBACK_LANES_FAILED.value,
                message=f"{action} image_jobs dual_race: {merged_msg}",
            )

        # Phase 2：bonus grace。grace 计时从 caller 完成 finalize 重新 next() 起算
        # （和 _dual_race_image_action 的语义一致）。
        if pending:
            done, still = await asyncio.wait(
                pending,
                timeout=grace_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if still:
                for t in still:
                    t.cancel()
                await asyncio.gather(*still, return_exceptions=True)
                logger.info(
                    "%s image_jobs dual_race: loser exceeded grace=%.0fs, cancelled silently",
                    action, grace_s,
                )
                return
            for finished in done:
                lane_name = lane_names[finished]
                exc = finished.exception()
                if exc is None:
                    logger.info(
                        "%s image_jobs dual_race: bonus from %s succeeded",
                        action, lane_name,
                    )
                    yield finished.result()
                    return
                if isinstance(exc, UpstreamCancelled):
                    return
                logger.info(
                    "%s image_jobs dual_race: bonus %s failed silently: %r",
                    action, lane_name, exc,
                )
                return
    finally:
        leftovers = [t for t in tasks if not t.done()]
        for t in leftovers:
            t.cancel()
        if leftovers:
            try:
                await asyncio.gather(*leftovers, return_exceptions=True)
            except Exception:  # noqa: BLE001
                pass


def _image_jobs_endpoint_for_engine(engine: str) -> str:
    if engine == _IMAGE_ROUTE_IMAGE2:
        return "generations"
    return "responses"


def _provider_supports_image_jobs(provider: Any) -> bool:
    return bool(getattr(provider, "image_jobs_enabled", False))


def _should_use_image_jobs(channel: str, provider: Any) -> bool:
    supports_jobs = _provider_supports_image_jobs(provider)
    if channel == _IMAGE_CHANNEL_IMAGE_JOBS_ONLY:
        if not supports_jobs:
            provider_name = getattr(provider, "name", "unknown")
            raise UpstreamError(
                f"provider {provider_name} does not support image_jobs "
                "(channel=image_jobs_only)",
                error_code=EC.ALL_ACCOUNTS_FAILED.value,
                status_code=503,
                payload={
                    "provider": str(provider_name),
                    "channel": channel,
                    "reason": "image_jobs_not_enabled",
                },
            )
        return True
    if channel == _IMAGE_CHANNEL_STREAM_ONLY:
        return False
    return supports_jobs


def _image_endpoint_kind_for_engine(engine: str) -> str | None:
    if engine == _IMAGE_ROUTE_IMAGE2:
        return "generations"
    if engine == _IMAGE_ROUTE_RESPONSES:
        return "responses"
    return None


async def _image_dispatch_candidates(
    provider_override: Any | None,
    *,
    engine: str,
) -> list[Any]:
    if provider_override is not None:
        endpoint_kind = _image_endpoint_kind_for_engine(engine)
        if endpoint_kind is not None:
            lock_error = _provider_endpoint_locked_error(provider_override, endpoint_kind)
            if lock_error is not None:
                raise lock_error
        return [provider_override]

    pool = await provider_pool.get_pool()
    return await _pool_select_compat(
        pool,
        route="image",
        ignore_cooldown=True,
        endpoint_kind=_image_endpoint_kind_for_engine(engine),
    )


async def _run_image_once_for_provider(
    *,
    action: str,
    provider: Any,
    channel: str,
    engine: str,
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
    progress_callback: ImageProgressCallback | None,
) -> AsyncIterator[tuple[str, str | None]]:
    use_jobs = _should_use_image_jobs(channel, provider)
    provider_name = getattr(provider, "name", "unknown")
    logger.info(
        "%s image dispatch provider=%s channel=%s engine=%s use_jobs=%s",
        action,
        provider_name,
        channel,
        engine,
        use_jobs,
    )

    if engine == _IMAGE_ROUTE_DUAL_RACE:
        if use_jobs:
            async for item in _dual_race_image_jobs_action(
                action=action,
                prompt=prompt,
                size=size,
                images=images,
                n=n,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
                model=model,
                progress_callback=progress_callback,
                provider_override=provider,
            ):
                yield item
            return
        async for item in _dual_race_image_action(
            action=action,
            prompt=prompt,
            size=size,
            images=images,
            n=n,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=progress_callback,
            provider_override=provider,
            allow_provider_override_race=True,
        ):
            yield item
        return

    if use_jobs:
        yield await _image_job_with_failover(
            action=action,
            prompt=prompt,
            size=size,
            images=images,
            n=n,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=progress_callback,
            provider_override=provider,
            endpoint_preference=_image_jobs_endpoint_for_engine(engine),
        )
        return

    if engine == _IMAGE_ROUTE_IMAGE2:
        try:
            if action == "edit":
                if not images:
                    raise UpstreamError(
                        "edit action requires at least one reference image",
                        error_code=EC.MISSING_INPUT_IMAGES.value,
                        status_code=400,
                    )
                yield await _direct_edit_image_with_failover(
                    prompt=prompt,
                    size=size,
                    images=images,
                    n=n,
                    quality=quality,
                    output_format=output_format,
                    output_compression=output_compression,
                    background=background,
                    moderation=moderation,
                    progress_callback=progress_callback,
                    provider_override=provider,
                )
                return
            yield await _direct_generate_image_with_failover(
                prompt=prompt,
                size=size,
                n=n,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
                progress_callback=progress_callback,
                provider_override=provider,
            )
            return
        except (asyncio.CancelledError, UpstreamCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s image2 provider=%s failed; falling back to responses: %r",
                action,
                provider_name,
                exc,
            )
            try:
                yield await _race_responses_image(
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
                    lanes=max(1, int(settings.edit_race_lanes)),
                    progress_callback=progress_callback,
                    provider_override=provider,
                )
            except (asyncio.CancelledError, UpstreamCancelled):
                raise
            except Exception as fallback_exc:  # noqa: BLE001
                raise _merge_image_path_errors(
                    action=action,
                    primary_path="image2",
                    primary_error=exc,
                    fallback_path="responses",
                    fallback_error=fallback_exc,
                ) from fallback_exc
            return

    lanes = max(1, int(settings.edit_race_lanes))
    try:
        yield await _race_responses_image(
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
            lanes=lanes,
            progress_callback=progress_callback,
            provider_override=provider,
        )
        return
    except (asyncio.CancelledError, UpstreamCancelled):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s responses provider=%s failed; falling back to image2: %r",
            action,
            provider_name,
            exc,
        )
        if action == "edit":
            if not images:
                raise UpstreamError(
                    "edit action requires at least one reference image",
                    error_code=EC.MISSING_INPUT_IMAGES.value,
                    status_code=400,
                ) from exc
            try:
                yield await _direct_edit_image_with_failover(
                    prompt=prompt,
                    size=size,
                    images=images,
                    n=n,
                    quality=quality,
                    output_format=output_format,
                    output_compression=output_compression,
                    background=background,
                    moderation=moderation,
                    progress_callback=progress_callback,
                    provider_override=provider,
                )
            except (asyncio.CancelledError, UpstreamCancelled):
                raise
            except Exception as fallback_exc:  # noqa: BLE001
                raise _merge_image_path_errors(
                    action=action,
                    primary_path="responses",
                    primary_error=exc,
                    fallback_path="image2",
                    fallback_error=fallback_exc,
                ) from fallback_exc
            return
        try:
            yield await _direct_generate_image_with_failover(
                prompt=prompt,
                size=size,
                n=n,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
                progress_callback=progress_callback,
                provider_override=provider,
            )
        except (asyncio.CancelledError, UpstreamCancelled):
            raise
        except Exception as fallback_exc:  # noqa: BLE001
            raise _merge_image_path_errors(
                action=action,
                primary_path="responses",
                primary_error=exc,
                fallback_path="image2",
                fallback_error=fallback_exc,
            ) from fallback_exc
        return


async def _dispatch_image(
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
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None,
) -> AsyncIterator[tuple[str, str | None]]:
    from .retry import is_retriable as classify_retriable

    channel = await _resolve_image_channel()
    engine = await _resolve_image_engine()
    try:
        providers = await _image_dispatch_candidates(provider_override, engine=engine)
    except TypeError as exc:
        if "engine" not in str(exc):
            raise
        providers = await _image_dispatch_candidates(provider_override)  # type: ignore[call-arg]
        endpoint_kind = _image_endpoint_kind_for_engine(engine)
        if endpoint_kind is not None:
            providers = [
                provider
                for provider in providers
                if endpoint_kind_allowed(provider, endpoint_kind)
            ]
    errors: list[BaseException] = []

    for idx, provider in enumerate(providers):
        any_yielded = False
        try:
            async for item in _run_image_once_for_provider(
                action=action,
                provider=provider,
                channel=channel,
                engine=engine,
                prompt=prompt,
                size=size,
                images=images,
                n=n,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
                model=model,
                progress_callback=progress_callback,
            ):
                any_yielded = True
                yield item
            return
        except (asyncio.CancelledError, UpstreamCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            if any_yielded:
                raise
            errors.append(exc)
            decision = classify_retriable(
                getattr(exc, "error_code", None),
                getattr(exc, "status_code", None),
                error_message=str(exc),
            )
            should_continue = _should_continue_image_provider_failover(
                exc,
                retriable=decision.retriable,
            )
            if channel == _IMAGE_CHANNEL_IMAGE_JOBS_ONLY and not _provider_supports_image_jobs(provider):
                raise
            if not should_continue:
                raise
            remaining = len(providers) - idx - 1
            if remaining <= 0:
                continue
            provider_name = getattr(provider, "name", "unknown")
            logger.warning(
                "%s image dispatch provider_failover: from=%s remaining=%d "
                "channel=%s engine=%s reason=%s",
                action,
                provider_name,
                remaining,
                channel,
                engine,
                decision.reason,
            )
            await _emit_image_progress(
                progress_callback,
                "provider_failover",
                from_provider=provider_name,
                remaining=remaining,
                reason=decision.reason,
                route=f"{channel}:{engine}",
            )

    merged = _merge_fallback_errors(
        errors,
        error_code=EC.ALL_ACCOUNTS_FAILED.value,
        message=f"all {len(providers)} image dispatch provider(s) failed",
    )
    merged.payload["provider_errors"] = _provider_error_details(providers, errors)
    merged.payload["channel"] = channel
    merged.payload["engine"] = engine
    raise merged


async def generate_image(
    *,
    prompt: str,
    size: str,
    n: int = 1,
    quality: str = "high",
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None = None,
    provider_override: Any | None = None,
) -> AsyncIterator[tuple[str, str | None]]:
    """Text-to-image dispatch using image.channel + image.engine."""
    async for item in _dispatch_image(
        action="generate",
        prompt=prompt,
        size=size,
        images=None,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
        progress_callback=progress_callback,
        provider_override=provider_override,
    ):
        yield item


async def edit_image(
    *,
    prompt: str,
    size: str,
    images: list[bytes],
    n: int = 1,
    quality: str = "high",
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None = None,
    provider_override: Any | None = None,
) -> AsyncIterator[tuple[str, str | None]]:
    """Image-to-image dispatch using image.channel + image.engine."""
    # 防御性：调用方（generation.py）理论上不会传空 images，但这里再兜一层——
    # 空 images 进 /v1/responses + action=edit 会被上游当成无参考图的文生图，
    # 静默降级体验比抛错更糟。
    if not images or not any(images):
        raise UpstreamError(
            "edit action requires at least one reference image",
            error_code=EC.MISSING_INPUT_IMAGES.value,
            status_code=400,
        )

    async for item in _dispatch_image(
        action="edit",
        prompt=prompt,
        size=size,
        images=images,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
        progress_callback=progress_callback,
        provider_override=provider_override,
    ):
        yield item


async def _iter_sse_with_runtime(
    *,
    base: str,
    api_key: str,
    body: dict[str, Any],
    read_timeout_s: float | None = None,
    interruption_error_code: str = "stream_interrupted",
    trace_id: str | None = None,
    proxy_url: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """httpx 流式 POST /v1/responses 并迭代解析 SSE 事件。

    取消安全：httpx `client.stream(...)` 的 async context 在 CancelledError 沿
    yield 出去时也会执行 __aexit__ → response.aclose()，连接和 stream 都会被释放。
    本函数额外捕获 CancelledError 并 reraise（不 swallow），以及在主路径里使用
    try/finally 把 `resp.aclose()` 显式调用一次（context manager 已经做了，但显式
    写出更便于排查）。
    """
    # 调用方未提供 trace_id 时本函数自生成；与 _iter_sse_curl 保持一致
    call_trace_id = trace_id or _generate_trace_id()
    timeout_config = await _resolve_timeout_config()
    client = await (_get_client(proxy_url) if proxy_url else _get_client())
    url = _responses_url(base)
    # 按 size 选 read timeout：默认沿用 client 上的 settings.upstream_read_timeout_s（180s），
    # 4K 等大图传 ≥360s 避免 httpx ReadTimeout。其他 timeout 维度（connect/write/pool）
    # 与 client 一致；只覆盖 read。
    stream_kwargs: dict[str, Any] = {
        "json": body,
        "headers": _auth_headers(api_key, trace_id=call_trace_id),
    }
    if (
        read_timeout_s is not None
        and read_timeout_s > timeout_config.read
    ):
        stream_kwargs["timeout"] = timeout_config.to_httpx(read=read_timeout_s)
    started = time.monotonic()
    final_status = 0
    final_resp_headers: Any = None
    try:
        async with client.stream("POST", url, **stream_kwargs) as resp:
            final_status = resp.status_code
            # 真实 httpx.Response 一定有 headers；测试桩可能没有，做兜底。
            final_resp_headers = getattr(resp, "headers", None)
            if resp.status_code >= 400:
                raw = await resp.aread()
                raw_text = raw.decode("utf-8", errors="replace")
                req_id = (
                    final_resp_headers.get("x-request-id")
                    if final_resp_headers is not None
                    else None
                )
                logger.warning(
                    "httpx sse non-2xx status=%s url=%s body=%.1000s trace_id=%s "
                    "x_request_id=%s",
                    resp.status_code, url, raw_text, call_trace_id, req_id,
                )
                try:
                    payload = json.loads(raw_text)
                except Exception:
                    payload = {"raw": raw_text}
                raise _with_error_context(
                    _parse_error(payload if isinstance(payload, dict) else {}, resp.status_code),
                    path="responses",
                    method="POST",
                    url=url,
                )

            current_event: str | None = None
            line_count = 0
            byte_count = 0
            try:
                async for line in resp.aiter_lines():
                    line_count += 1
                    byte_count += len(line)
                    if line_count > _SSE_MAX_LINES:
                        raise UpstreamError(
                            "sse exceeded max lines",
                            error_code=EC.STREAM_TOO_LARGE.value,
                            status_code=resp.status_code,
                        )
                    if len(line) > _SSE_MAX_LINE_BYTES:
                        raise UpstreamError(
                            "sse exceeded max line bytes",
                            error_code=EC.STREAM_TOO_LARGE.value,
                            status_code=resp.status_code,
                        )
                    if byte_count > _SSE_MAX_BYTES:
                        raise UpstreamError(
                            "sse exceeded max bytes",
                            error_code=EC.STREAM_TOO_LARGE.value,
                            status_code=resp.status_code,
                        )
                    if line == "":
                        current_event = None
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:") :].strip() or None
                        continue
                    if line.startswith("data:"):
                        data_raw = line[len("data:") :].lstrip()
                        if data_raw == "[DONE]":
                            return
                        try:
                            data = json.loads(data_raw)
                        except json.JSONDecodeError:
                            logger.warning("sse: invalid json line: %s", data_raw[:200])
                            continue
                        if isinstance(data, dict):
                            if "type" not in data and current_event:
                                data["type"] = current_event
                            # SSE response.completed 帧里嵌着 usage——抓出来打 metrics
                            _maybe_record_usage_from_event(data)
                            yield data
            except UpstreamError:
                raise
            except asyncio.CancelledError:
                # cancellation safe: 显式 aclose() 释放底层连接；async with 的 __aexit__
                # 也会做同样的事，这里写出便于排查。reraise 不 swallow CancelledError。
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass
                raise
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    f"responses stream interrupted: {exc}",
                    status_code=resp.status_code,
                    error_code=interruption_error_code,
                    payload={
                        "path": "responses",
                        "method": "POST",
                        "url": url,
                        "x_trace_id": call_trace_id,
                    },
                ) from exc
    except asyncio.CancelledError:
        # 上一层 try/except 已经显式 aclose；这里仅 reraise，让取消信号穿透到调用方。
        raise
    finally:
        # 元信息埋点：endpoint=responses（httpx 路径）。final_status/headers 在请求
        # 失败的早期阶段可能还没赋值，_log_upstream_call 内部对 None 兜底。
        duration_ms = (time.monotonic() - started) * 1000.0
        try:
            _log_upstream_call(
                endpoint="responses",
                status=final_status,
                duration_ms=duration_ms,
                trace_id=call_trace_id,
                response_headers=final_resp_headers,
            )
        except Exception:  # noqa: BLE001
            logger.debug("failed to log upstream call meta", exc_info=True)


async def _iter_sse(body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """共享的 SSE 读循环。按 OpenAI Responses SSE 协议逐事件 yield。

    事件形状：`{ "type": "<event name>", ...payload }`。上游的事件名通常同时出现在
    `event:` 行和 `data` JSON 的 `type` 字段，我们优先相信 data.type（更权威）。
    """
    # completion / chat 路径前置 schema 校验，缺 instructions / input list 时直接 4xx
    _validate_responses_body(body)
    # 如果 body 带 tools，按 name/type 排序保证 prompt cache 前缀稳定
    if isinstance(body.get("tools"), list):
        body["tools"] = _stable_sort_tools(body["tools"])
    # Model 显式 pin：上层 completion.py 已经从 settings.upstream_default_model 等地方
    # 读出来，这里只做运行时断言防漏发。
    assert body.get("model"), "model must be set"
    runtime = await _resolve_runtime()
    base, api_key, proxy = _runtime_parts(runtime)
    proxy_url = await resolve_provider_proxy_url(proxy)
    async for event in _iter_sse_with_runtime(
        base=base,
        api_key=api_key,
        body=body,
        interruption_error_code=_TEXT_STREAM_INTERRUPTED_ERROR_CODE,
        proxy_url=proxy_url,
    ):
        yield event


async def stream_completion(body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """流式 completion：消费者关注 `response.output_text.delta` 的 `delta` 字段，
    收到 `response.completed` 后结束。

    取消安全：内部 `_iter_sse_with_runtime` 的 try/except/finally 已经显式 aclose
    httpx response 并 reraise CancelledError，不 swallow。
    """
    async for ev in _iter_sse(body):
        yield ev


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
    """非流式调用上游 /v1/responses，返回完整 response JSON dict。

    设计目标：让 context_summary 等"非流式 / 一次性结果"调用方共享 upstream.py 已经
    沉淀好的基础设施——schema 校验、tools 排序、trace_id、x-request-id 元信息日志、
    Prometheus 埋点、usage 计数、取消安全。本函数 **只做单次调用**，重试由上层负责
    （context_summary 自己有 provider_pool 循环 + per-provider retry）。

    上游网关在某些组合下即便请求体写了 `stream:false` 仍会回 SSE（参考 probe report
    §2.D）。本函数对两种返回都做兼容：
    - Content-Type: `text/event-stream` → 逐行扫 SSE 帧，找 `response.completed` 中的
      `response` dict 返回；usage 在帧内由 `_record_usage` 自动埋点。
    - Content-Type: `application/json`（compact 端点 / 或者 stream:false 被尊重时）
      → 直接 `resp.json()` 返回；usage 字段 fallback 调一次 `_record_usage`。

    Args:
        body: /v1/responses 请求体，调用前会被 `_validate_responses_body` 修订
              （缺 instructions 时注入默认值）/ tools 排序。**会原地修改 body**
              ——和 `_iter_sse` 行为一致，调用方已知。
        route: provider_pool 选号路由（`"text"` / `"image"`）。仅在没有 override 时
              用于 `_resolve_runtime`（当前 `_resolve_runtime()` 默认走 text，本参数
              暂时不传给它，留作 future-compat）。
        api_key_override / base_url_override: 调用方已经选好 provider 时透传，跳过
              `_resolve_runtime`。两者必须同时给（和 `_responses_image_stream` 一致）。
        timeout_s: 覆盖默认 read timeout（默认走 settings.upstream_read_timeout_s）。
              context_summary 当前用 `_SUMMARY_HTTP_TIMEOUT_S=45.0`。
        endpoint_label: Prometheus / 日志 label。新值需要更新 metrics_upstream 文档。
              context_summary 用 `"responses_summary"`。

    Returns:
        完整 response dict。SSE 路径返回 `response.completed` 帧里的 `response` 子对象
        （含 `output` / `usage` / `output_text` 等）；JSON 路径返回 body 顶层 dict。

    Raises:
        UpstreamError: 4xx/5xx HTTP / 上游 JSON error 字段 / 网络错误 / SSE 截断 /
            上游 timeout（携带 status_code & error_code 便于 caller 走 retry classifier）。
        asyncio.CancelledError: 直接透传不吞，上层取消信号必须能穿透。

    取消安全：使用 `client.stream()` async context manager + 显式 aclose 双保险，
    即使 caller 在 await 期间被 cancel，连接和底层 socket 都会被释放。
    """
    # 1) body 前置处理——和 stream 路径完全一致，保证 prompt cache 前缀稳定
    _validate_responses_body(body)
    if isinstance(body.get("tools"), list):
        body["tools"] = _stable_sort_tools(body["tools"])
    assert body.get("model"), "model must be set"

    # 2) provider 选择：override 优先，否则走 _resolve_runtime（当前默认 text 路由）
    proxy = proxy_override
    if api_key_override is not None and base_url_override is not None:
        base, api_key = base_url_override, api_key_override
    else:
        # _resolve_runtime 当前不接受 route 参数（始终 select_one），保留 route 入参
        # 仅作为未来扩展占位，避免后续 caller 重新拉接口签名。
        _ = route
        runtime = await _resolve_runtime()
        base, api_key, proxy = _runtime_parts(runtime)

    url = _responses_url(base)
    call_trace_id = _generate_trace_id()
    headers = _auth_headers(api_key, trace_id=call_trace_id)

    # 3) 单次请求 timeout 覆盖：仅当 caller 给的更长时构造 httpx.Timeout 注入
    stream_kwargs: dict[str, Any] = {"json": body, "headers": headers}
    timeout_config = await _resolve_timeout_config()
    effective_timeout = float(timeout_s) if timeout_s is not None else timeout_config.read
    stream_kwargs["timeout"] = timeout_config.to_httpx(read=effective_timeout)

    proxy_url = await resolve_provider_proxy_url(proxy)
    client = await (_get_client(proxy_url) if proxy_url else _get_client())
    started = time.monotonic()
    final_status = 0
    final_resp_headers: Any = None
    try:
        try:
            async with client.stream("POST", url, **stream_kwargs) as resp:
                final_status = resp.status_code
                final_resp_headers = getattr(resp, "headers", None)

                # 3.a) 4xx/5xx：完整读 body，转 UpstreamError；保留 status_code 让
                # caller 的 retry classifier 工作正常。
                if resp.status_code >= 400:
                    raw = await resp.aread()
                    raw_text = raw.decode("utf-8", errors="replace")
                    req_id = (
                        final_resp_headers.get("x-request-id")
                        if final_resp_headers is not None
                        else None
                    )
                    logger.warning(
                        "responses_call non-2xx status=%s url=%s body=%.1000s "
                        "trace_id=%s x_request_id=%s",
                        resp.status_code,
                        url,
                        raw_text,
                        call_trace_id,
                        req_id,
                    )
                    try:
                        err_payload = json.loads(raw_text)
                    except Exception:  # noqa: BLE001
                        err_payload = {"raw": raw_text}
                    raise _with_error_context(
                        _parse_error(
                            err_payload if isinstance(err_payload, dict) else {},
                            resp.status_code,
                        ),
                        path="responses",
                        method="POST",
                        url=url,
                    )

                # 3.b) 根据 Content-Type 分流。SSE 帧靠 `aiter_lines` 解析；
                # JSON 直接 aread + json.loads。
                content_type = (
                    final_resp_headers.get("content-type")
                    if final_resp_headers is not None
                    else ""
                ) or ""
                ct_lower = content_type.lower()

                if "text/event-stream" in ct_lower:
                    completed: dict[str, Any] | None = None
                    line_count = 0
                    byte_count = 0
                    current_event: str | None = None
                    try:
                        async for line in resp.aiter_lines():
                            line_count += 1
                            byte_count += len(line)
                            if line_count > _SSE_MAX_LINES:
                                raise UpstreamError(
                                    "sse exceeded max lines",
                                    error_code=EC.STREAM_TOO_LARGE.value,
                                    status_code=resp.status_code,
                                )
                            if len(line) > _SSE_MAX_LINE_BYTES:
                                raise UpstreamError(
                                    "sse exceeded max line bytes",
                                    error_code=EC.STREAM_TOO_LARGE.value,
                                    status_code=resp.status_code,
                                )
                            if byte_count > _SSE_MAX_BYTES:
                                raise UpstreamError(
                                    "sse exceeded max bytes",
                                    error_code=EC.STREAM_TOO_LARGE.value,
                                    status_code=resp.status_code,
                                )
                            if line == "":
                                current_event = None
                                continue
                            if line.startswith(":"):
                                continue
                            if line.startswith("event:"):
                                current_event = line[len("event:") :].strip() or None
                                continue
                            if line.startswith("data:"):
                                data_raw = line[len("data:") :].lstrip()
                                if data_raw == "[DONE]":
                                    break
                                try:
                                    event = json.loads(data_raw)
                                except json.JSONDecodeError:
                                    logger.warning(
                                        "responses_call sse invalid json line=%s",
                                        data_raw[:200],
                                    )
                                    continue
                                if not isinstance(event, dict):
                                    continue
                                if "type" not in event and current_event:
                                    event["type"] = current_event
                                # 在帧内抓 usage 走标准埋点；与 stream 路径口径一致
                                _maybe_record_usage_from_event(event)
                                if event.get("type") == "response.completed":
                                    resp_obj = event.get("response")
                                    if isinstance(resp_obj, dict):
                                        completed = resp_obj
                    except UpstreamError:
                        raise
                    except asyncio.CancelledError:
                        try:
                            await resp.aclose()
                        except Exception:  # noqa: BLE001
                            pass
                        raise
                    except httpx.HTTPError as exc:
                        raise UpstreamError(
                            f"responses_call sse interrupted: {exc}",
                            status_code=resp.status_code,
                            error_code=EC.TEXT_STREAM_INTERRUPTED.value,
                            payload={
                                "path": "responses",
                                "method": "POST",
                                "url": url,
                                "x_trace_id": call_trace_id,
                            },
                        ) from exc

                    if completed is None:
                        raise UpstreamError(
                            "responses_call sse missing response.completed frame",
                            status_code=resp.status_code,
                            error_code=EC.BAD_RESPONSE.value,
                            payload={
                                "path": "responses",
                                "method": "POST",
                                "url": url,
                                "x_trace_id": call_trace_id,
                            },
                        )
                    return completed

                # JSON 路径
                raw = await resp.aread()
                try:
                    payload = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception as exc:  # noqa: BLE001
                    raise UpstreamError(
                        "responses_call returned invalid JSON",
                        status_code=resp.status_code,
                        error_code=EC.BAD_RESPONSE.value,
                        payload={
                            "path": "responses",
                            "method": "POST",
                            "url": url,
                            "x_trace_id": call_trace_id,
                        },
                    ) from exc
                if not isinstance(payload, dict):
                    raise UpstreamError(
                        "responses_call returned non-object payload",
                        status_code=resp.status_code,
                        error_code=EC.BAD_RESPONSE.value,
                        payload={
                            "path": "responses",
                            "method": "POST",
                            "url": url,
                            "x_trace_id": call_trace_id,
                        },
                    )
                # JSON 顶层 usage 单独埋点（SSE 路径已经在帧解析里调过了）
                if isinstance(payload.get("usage"), dict):
                    _record_usage(payload["usage"])
                return payload
        except httpx.TimeoutException as exc:
            raise UpstreamError(
                f"responses_call upstream timeout: {exc}",
                status_code=None,
                error_code=EC.UPSTREAM_TIMEOUT.value,
                payload={
                    "path": "responses",
                    "method": "POST",
                    "url": url,
                    "x_trace_id": call_trace_id,
                },
            ) from exc
        except httpx.HTTPError as exc:
            # 非超时网络错（ConnectError / ReadError / RemoteProtocolError 等）
            raise UpstreamError(
                f"responses_call upstream network error: {exc}",
                status_code=None,
                error_code=EC.UPSTREAM_ERROR.value,
                payload={
                    "path": "responses",
                    "method": "POST",
                    "url": url,
                    "x_trace_id": call_trace_id,
                },
            ) from exc
    except asyncio.CancelledError:
        # 取消信号必须穿透——和 _iter_sse_with_runtime 一致，不吞 CancelledError。
        raise
    finally:
        # 元信息埋点：endpoint label 由 caller 决定（默认 "responses"），允许细分到
        # responses_summary / responses_compact 等子端点。
        duration_ms = (time.monotonic() - started) * 1000.0
        try:
            _log_upstream_call(
                endpoint=endpoint_label,
                status=final_status,
                duration_ms=duration_ms,
                trace_id=call_trace_id,
                response_headers=final_resp_headers,
            )
        except Exception:  # noqa: BLE001
            logger.debug("responses_call meta log failed", exc_info=True)


__all__ = [
    "UpstreamError",
    "generate_image",
    "edit_image",
    "stream_completion",
    "responses_call",
    "close_client",
]
