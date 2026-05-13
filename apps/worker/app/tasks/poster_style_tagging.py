"""海报风格库 vision 自动打标签。

复用 ``model_library_tagging`` 的同款 provider chain 调 vision 模型，对一张
风格样图反推结构化 JSON：

    { "category": "illustration|3d|minimal|retro|traditional|photo|other",
      "style_tags": ["...", "..."],
      "mood": "...",
      "palette": ["#hex", ...],
      "notes": "..." }

设计要点（与模特库 tagging 完全对齐）：

- 解析失败 graceful：返回字段缺省的 ``PosterStyleAutoTagResult``，不 raise；
  调用方（API auto-tag endpoint / worker generation succeeded 钩子）按"没识别出来"处理。
- 不写 caption 缓存：风格库 tagging 是结构化字段，与会话视觉描述无关。
- 不依赖 API 进程的 settings / system_settings；只用 worker 自己的 provider pool。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select

from lumen_core.constants import GenerationErrorCode as EC
from lumen_core.models import Image
from lumen_core.providers import ProviderProxyDefinition, resolve_provider_proxy_url

from ..storage import storage
from ..upstream import UpstreamError, _auth_headers

logger = logging.getLogger(__name__)


# 与 PosterStyleCategory 对齐的合法值集合；模型偶尔输出中文/别名需要归一化。
_VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "illustration",
        "3d",
        "minimal",
        "retro",
        "traditional",
        "photo",
        "other",
    }
)
_CATEGORY_ALIASES: dict[str, str] = {
    # English variants
    "illustrated": "illustration",
    "vector": "illustration",
    "flat": "illustration",
    "3d_render": "3d",
    "3drender": "3d",
    "render": "3d",
    "minimalism": "minimal",
    "minimalist": "minimal",
    "typography": "minimal",
    "retro_pop": "retro",
    "pop": "retro",
    "chinese": "traditional",
    "oriental": "traditional",
    "editorial": "photo",
    "photography": "photo",
    # Chinese aliases (worst-case model returns Chinese label)
    "扁平": "illustration",
    "插画": "illustration",
    "矢量": "illustration",
    "三维": "3d",
    "立体": "3d",
    "极简": "minimal",
    "简约": "minimal",
    "字体": "minimal",
    "复古": "retro",
    "波普": "retro",
    "中式": "traditional",
    "国风": "traditional",
    "东方": "traditional",
    "摄影": "photo",
    "杂志": "photo",
    "其他": "other",
}


_TAGGING_INSTRUCTIONS = (
    "你是海报风格库自动打标签助手。仔细分析这张海报样图的视觉风格，输出严格 JSON。\n\n"
    "字段（全部必填，无法判断填空串/空数组）：\n"
    "- category：英文小写之一：illustration / 3d / minimal / retro / traditional / photo / other。\n"
    "- style_tags：3-6 个中文短词，每个 ≤ 8 字，聚焦视觉风格特征（例：扁平、矢量、低多边形、"
    "暖色调、复古、网格、噪点、霓虹、水墨、孟菲斯、新中式）。\n"
    "    禁止描述具体商品 / 模特 / 文字内容；禁止英文。\n"
    "- mood：≤ 20 字中文，整体情绪关键词（例：温暖治愈、现代锐利、复古怀旧、清冷高级）。\n"
    "- palette：3-6 个主要色彩的 #RRGGBB 十六进制（必须 # 开头）。\n"
    "- notes：≤ 60 字中文一句话，简短点评风格定位。\n\n"
    "只输出 JSON 对象，不要 Markdown / 代码块 / 解释。字段必须用上述英文 key。"
)
_TAGGING_HTTP_TIMEOUT_S = 25.0
_PER_PROVIDER_RETRY_ATTEMPTS = 2
_PER_PROVIDER_RETRY_BACKOFF_S = 1.0
_TAGGING_TOTAL_TIMEOUT_S = 60.0
_DEFAULT_TAGGING_MODEL = "gpt-5.4-mini"


@dataclass(slots=True)
class PosterStyleAutoTagResult:
    """vision 自动识别结果。所有字段都允许缺省（未识别出来时返回保守默认）。"""

    image_id: str
    category: str | None = None
    style_tags: list[str] = field(default_factory=list)
    mood: str | None = None
    palette: list[str] = field(default_factory=list)
    notes: str | None = None

    def __bool__(self) -> bool:
        """让调用方 ``if not result:`` 这种语义直观：全空时视为 falsy。"""
        return bool(
            self.category
            or self.style_tags
            or self.mood
            or self.palette
            or self.notes
        )


def _normalize_category(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s\-]+", "_", value.strip().lower()).strip("_")
    if not key:
        return None
    if key in _VALID_CATEGORIES:
        return key
    aliased = _CATEGORY_ALIASES.get(key)
    if aliased is not None:
        return aliased
    return None


def _clean_style_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [part for part in re.split(r"[,，;；、\s]+", value) if part]
    elif isinstance(value, list):
        candidates = [str(part) for part in value if isinstance(part, (str, int, float))]
    else:
        candidates = []
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        # strip 后再 [:8]：避免标签里带空格被算进 8 字预算。
        tag = str(raw).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag[:8])
        if len(out) >= 6:
            break
    return out


def _clean_palette(value: Any) -> list[str]:
    """提取 #RRGGBB / #RRGGBBAA hex，忽略其他。"""
    if isinstance(value, str):
        candidates: list[str] = re.findall(r"#[0-9a-fA-F]{3,8}", value)
    elif isinstance(value, list):
        candidates = []
        for raw in value:
            if not isinstance(raw, (str, int, float)):
                continue
            text = str(raw).strip()
            if not text:
                continue
            match = re.match(r"^#?([0-9a-fA-F]{3,8})$", text)
            if match:
                candidates.append(f"#{match.group(1)}")
    else:
        candidates = []
    out: list[str] = []
    seen: set[str] = set()
    for hex_value in candidates:
        normalized = hex_value.upper() if hex_value.startswith("#") else f"#{hex_value.upper()}"
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized[:9])  # #RRGGBBAA 最多 9 个字符
        if len(out) >= 6:
            break
    return out


def _clean_optional_text(value: Any, *, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def _strip_markdown_fences(text: str) -> str:
    """模型偶尔会把 JSON 包在 ```json ... ``` 里；把外层 fence 砍掉。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped.strip()


def _parse_tagging_payload(image_id: str, raw_text: str) -> PosterStyleAutoTagResult:
    """解析模型返回的 JSON。失败 graceful：尽力抓字段，再不济返回空字段。"""
    if not raw_text:
        return PosterStyleAutoTagResult(image_id=image_id)
    cleaned = _strip_markdown_fences(raw_text)
    payload: Any = None
    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                payload = json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                payload = None
    if not isinstance(payload, dict):
        return _regex_fallback(image_id, cleaned)
    return PosterStyleAutoTagResult(
        image_id=image_id,
        category=_normalize_category(payload.get("category")),
        style_tags=_clean_style_tags(payload.get("style_tags") or payload.get("tags")),
        mood=_clean_optional_text(payload.get("mood"), max_len=120),
        palette=_clean_palette(payload.get("palette")),
        notes=_clean_optional_text(payload.get("notes"), max_len=200),
    )


def _regex_fallback(image_id: str, text: str) -> PosterStyleAutoTagResult:
    """JSON parse 全失败时，用正则抓常见字段。命中即填，不抛错。"""

    def _grab(key: str) -> str | None:
        match = re.search(
            rf'"?{key}"?\s*[:=]\s*"([^"\n]*)"',
            text,
            flags=re.IGNORECASE,
        )
        return match.group(1).strip() if match else None

    tags_match = re.search(
        r'"?style_tags"?\s*[:=]\s*\[([^\]]*)\]', text, flags=re.IGNORECASE
    )
    palette_match = re.search(
        r'"?palette"?\s*[:=]\s*\[([^\]]*)\]', text, flags=re.IGNORECASE
    )
    return PosterStyleAutoTagResult(
        image_id=image_id,
        category=_normalize_category(_grab("category")),
        style_tags=_clean_style_tags(tags_match.group(1) if tags_match else None),
        mood=_clean_optional_text(_grab("mood"), max_len=120),
        palette=_clean_palette(palette_match.group(1) if palette_match else None),
        notes=_clean_optional_text(_grab("notes"), max_len=200),
    )


def _responses_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return f"{base}/responses"


def _extract_response_text(payload: Any) -> str:
    """从 /v1/responses 非流式响应里抽出 output_text。"""
    if not isinstance(payload, dict):
        return ""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    chunks: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text_part = part.get("text") or part.get("output_text")
                if isinstance(text_part, str) and text_part:
                    chunks.append(text_part)
    return "".join(chunks).strip()


async def _image_data_url(image_record: Any) -> str | None:
    storage_key = getattr(image_record, "storage_key", None)
    if not isinstance(storage_key, str) or not storage_key:
        return None
    try:
        raw = await storage.aget_bytes(storage_key)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "poster_style_tagging skipped: cannot read image id=%s key=%s err=%s",
            getattr(image_record, "id", None),
            storage_key,
            exc,
        )
        return None
    if not raw:
        return None
    mime = getattr(image_record, "mime", None)
    if not isinstance(mime, str) or not mime.startswith("image/"):
        mime = "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def _call_upstream_one(
    image_record: Any,
    image_url: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    proxy: ProviderProxyDefinition | None = None,
) -> str:
    body = {
        "model": model,
        "instructions": _TAGGING_INSTRUCTIONS,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _TAGGING_INSTRUCTIONS},
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        ],
        "metadata": {
            "image_id": str(getattr(image_record, "id", "")),
            "purpose": "poster_style_tagging",
        },
        "stream": False,
        "store": False,
        "max_output_tokens": 600,
    }
    try:
        proxy_url = await resolve_provider_proxy_url(proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=_TAGGING_HTTP_TIMEOUT_S,
                write=_TAGGING_HTTP_TIMEOUT_S,
                pool=10.0,
            ),
            proxy=proxy_url,
        ) as client:
            resp = await client.post(
                _responses_url(base_url),
                json=body,
                headers={
                    **_auth_headers(api_key),
                    "content-type": "application/json",
                },
            )
    except httpx.TimeoutException as exc:
        raise UpstreamError(
            "poster style tagging upstream timeout",
            error_code=EC.UPSTREAM_TIMEOUT.value,
            status_code=None,
        ) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(
            f"poster style tagging upstream network error: {exc}",
            error_code=EC.UPSTREAM_ERROR.value,
            status_code=None,
        ) from exc

    if resp.status_code >= 400:
        raise UpstreamError(
            f"poster style tagging upstream http {resp.status_code}",
            error_code=EC.UPSTREAM_ERROR.value,
            status_code=resp.status_code,
        )

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpstreamError(
            "poster style tagging upstream returned invalid json",
            error_code=EC.BAD_RESPONSE.value,
            status_code=resp.status_code,
        ) from exc
    return _extract_response_text(payload)


async def _call_upstream(
    image_record: Any,
    image_url: str,
    *,
    model: str,
) -> str | None:
    from ..provider_pool import get_pool
    from ..retry import is_retriable as classify_retriable

    pool = await get_pool()
    providers = await pool.select(route="text")
    last_exc: BaseException | None = None
    attempted_providers: list[str] = []

    for provider in providers:
        attempted_providers.append(provider.name)
        for attempt in range(_PER_PROVIDER_RETRY_ATTEMPTS):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "base_url": provider.base_url,
                    "api_key": provider.api_key,
                }
                proxy = getattr(provider, "proxy", None)
                if proxy is not None:
                    kwargs["proxy"] = proxy
                return await _call_upstream_one(image_record, image_url, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                if not decision.retriable:
                    logger.info(
                        "poster_style_tagging terminal upstream failure "
                        "image_id=%s provider=%s reason=%s",
                        getattr(image_record, "id", None),
                        provider.name,
                        decision.reason,
                    )
                    return None
                if attempt + 1 < _PER_PROVIDER_RETRY_ATTEMPTS:
                    await asyncio.sleep(_PER_PROVIDER_RETRY_BACKOFF_S * (2**attempt))

    if last_exc is not None:
        logger.info(
            "poster_style_tagging upstream failed image_id=%s providers=%s err=%.300s",
            getattr(image_record, "id", None),
            ",".join(attempted_providers) or "<none>",
            str(last_exc),
        )
    return None


async def auto_tag_poster_style_image_record(
    image_record: Any,
    *,
    model: str = _DEFAULT_TAGGING_MODEL,
) -> PosterStyleAutoTagResult:
    """对一张已加载的 Image ORM 行做 vision tagging。

    永不 raise expected upstream/parse 错误：返回字段缺省的结果对象。
    """
    image_id = str(getattr(image_record, "id", "") or "")
    if not image_id:
        return PosterStyleAutoTagResult(image_id="")
    image_url = await _image_data_url(image_record)
    if image_url is None:
        return PosterStyleAutoTagResult(image_id=image_id)
    try:
        async with asyncio.timeout(_TAGGING_TOTAL_TIMEOUT_S):
            raw = await _call_upstream(image_record, image_url, model=model)
    except (TimeoutError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "poster_style_tagging failed image_id=%s err=%s",
            image_id,
            exc,
        )
        return PosterStyleAutoTagResult(image_id=image_id)
    if not raw:
        return PosterStyleAutoTagResult(image_id=image_id)
    return _parse_tagging_payload(image_id, raw)


async def auto_tag_poster_style_image(
    session: Any,
    *,
    image_id: str,
    user_id: str,
    model: str = _DEFAULT_TAGGING_MODEL,
) -> PosterStyleAutoTagResult:
    """主入口：从 session 拉 image 行，调 vision，返回结构化字段。

    用户隔离：image_id 必须属于该 user_id，否则返回空结果（防止越权识别他人图）。
    """
    if not image_id or not user_id:
        return PosterStyleAutoTagResult(image_id=image_id or "")
    record = (
        await session.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if record is None:
        return PosterStyleAutoTagResult(image_id=image_id)
    return await auto_tag_poster_style_image_record(record, model=model)


__all__ = [
    "PosterStyleAutoTagResult",
    "auto_tag_poster_style_image",
    "auto_tag_poster_style_image_record",
]
