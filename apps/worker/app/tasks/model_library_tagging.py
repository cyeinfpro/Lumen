"""模特库 vision 自动打标签。

复用 batch_caption_images 同款 provider chain 调用 vision 模型，对一张模特图
让模型输出结构化 JSON：

    { "appearance_direction": "...",
      "style_tags": ["...", "..."],
      "age_segment": "young_adult|...",
      "gender": "female|male",
      "notes": "..." }

设计要点：
- 解析失败 graceful：返回字段缺省（style_tags=[]）的 AutoTagResult，不 raise；
  调用方（worker generation succeeded 钩子 / API auto-tag 端点）只需当成"模型没识别出来"。
- 不写 caption 缓存：caption 是会话层的视觉描述，模特库 tagging 是结构化字段。
- 不依赖 context_image_caption 内部细节，少量代码复制比循环 import 更稳。
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


# 与模特库 schema 对齐的取值集合：解析时把模型返回的非法/英文别名规整到合法值。
_VALID_AGE_SEGMENTS: frozenset[str] = frozenset(
    {
        "user_favorites",
        "toddler",
        "child",
        "teen",
        "young_adult",
        "adult",
        "middle_aged",
        "senior",
    }
)
_AGE_ALIASES: dict[str, str] = {
    "young": "young_adult",
    "youngadult": "young_adult",
    "young-adult": "young_adult",
    "kid": "child",
    "kids": "child",
    "baby": "toddler",
    "elder": "senior",
    "elderly": "senior",
    "old": "senior",
    "middleaged": "middle_aged",
    "middle-aged": "middle_aged",
    "teenager": "teen",
}
_GENDER_ALIASES: dict[str, str] = {
    "female": "female",
    "woman": "female",
    "girl": "female",
    "f": "female",
    "male": "male",
    "man": "male",
    "boy": "male",
    "m": "male",
}

_TAGGING_INSTRUCTIONS = (
    "你是模特库自动打标签助手。仔细分析这张模特图，输出严格 JSON。\n\n"
    "字段（全部必填，无法判断填空串/空数组）：\n"
    "- appearance_direction：英文小写之一：asian / east_asian / southeast_asian / "
    "south_asian / european / latin / middle_eastern / african / mixed / other。\n"
    "- style_tags：3-6 个中文短词，每个 ≤ 8 字，只写两类：\n"
    "    1) 相貌气质 — 五官 / 脸型 / 肤色 / 发型 / 骨相 / 整体观感"
    "（例：清冷、高颅顶、英气、邻家感、奶油感、骨相清秀、温柔、酷感）\n"
    "    2) 适合风格定位（例：少女感、高级感、知性、御姐感、复古、运动、文艺、街头）\n"
    "  禁止描述衣服 / 单品 / 拍摄场景 / 光线 / 品牌 / 营销词；禁止英文。\n"
    "- age_segment：toddler / child / teen / young_adult / adult / middle_aged / senior 之一。\n"
    "- gender：female 或 male 之一。\n"
    "- notes：≤ 60 字中文一句话，聚焦相貌与风格定位，不评价衣服。\n\n"
    "只输出 JSON 对象，不要 Markdown / 代码块 / 解释。"
)
# 与 _apparel_library.MODEL_LIBRARY_APPEARANCES 对齐（去掉 "all"）；worker 不能依赖 api routes，故复制一份。
_APPEARANCE_VALID: frozenset[str] = frozenset(
    {
        "asian",
        "east_asian",
        "southeast_asian",
        "south_asian",
        "european",
        "latin",
        "middle_eastern",
        "african",
        "mixed",
        "other",
    }
)
_TAGGING_HTTP_TIMEOUT_S = 25.0
_PER_PROVIDER_RETRY_ATTEMPTS = 2
_PER_PROVIDER_RETRY_BACKOFF_S = 1.0
_TAGGING_TOTAL_TIMEOUT_S = 60.0
_DEFAULT_TAGGING_MODEL = "gpt-5.4-mini"


@dataclass(slots=True)
class AutoTagResult:
    """vision 自动识别结果。所有字段都允许缺省（未识别出来时给出保守默认）。"""

    image_id: str
    style_tags: list[str] = field(default_factory=list)
    appearance_direction: str | None = None
    age_segment: str | None = None
    gender: str | None = None
    notes: str | None = None


def _normalize_age_segment(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace(" ", "_")
    if key in _VALID_AGE_SEGMENTS:
        return key
    aliased = _AGE_ALIASES.get(key.replace("_", ""))
    if aliased is not None:
        return aliased
    aliased = _AGE_ALIASES.get(key)
    if aliased is not None:
        return aliased
    return None


def _normalize_gender(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _GENDER_ALIASES.get(value.strip().lower())


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
        # 显式 strip → 截 8 字：上游 prompt 偶尔输出带空格的标签（"  时尚  "），
        # 没先 strip 直接 [:8] 会把空格算进 8 字预算丢真实字符。
        tag = str(raw).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag.strip()[:8])
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


def _parse_tagging_payload(image_id: str, raw_text: str) -> AutoTagResult:
    """解析模型返回的 JSON。失败 graceful：尽力抓字段，再不济返回空字段。"""
    if not raw_text:
        return AutoTagResult(image_id=image_id)
    cleaned = _strip_markdown_fences(raw_text)
    payload: Any = None
    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        # JSON 失败：尝试用正则抓花括号内的 JSON object
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                payload = json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                payload = None
    if not isinstance(payload, dict):
        # 最后一线兜底：用 key: value 简单匹配几个常见字段
        return _regex_fallback(image_id, cleaned)
    appearance_raw = _clean_optional_text(
        payload.get("appearance_direction") or payload.get("appearanceDirection"),
        max_len=80,
    )
    appearance = (
        appearance_raw.strip().lower().replace(" ", "_") if appearance_raw else None
    )
    if appearance and appearance not in _APPEARANCE_VALID:
        appearance = None
    return AutoTagResult(
        image_id=image_id,
        style_tags=_clean_style_tags(
            payload.get("style_tags") or payload.get("tags") or payload.get("styleTags")
        ),
        appearance_direction=appearance,
        age_segment=_normalize_age_segment(
            payload.get("age_segment") or payload.get("ageSegment")
        ),
        gender=_normalize_gender(payload.get("gender")),
        notes=_clean_optional_text(payload.get("notes"), max_len=200),
    )


def _regex_fallback(image_id: str, text: str) -> AutoTagResult:
    """JSON parse 全都失败时，用正则抓常见字段。命中即填，不抛错。"""

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
    style_tags = _clean_style_tags(tags_match.group(1) if tags_match else None)
    appearance_raw = _clean_optional_text(_grab("appearance_direction"), max_len=80)
    appearance = (
        appearance_raw.strip().lower().replace(" ", "_") if appearance_raw else None
    )
    if appearance and appearance not in _APPEARANCE_VALID:
        appearance = None
    return AutoTagResult(
        image_id=image_id,
        style_tags=style_tags,
        appearance_direction=appearance,
        age_segment=_normalize_age_segment(_grab("age_segment")),
        gender=_normalize_gender(_grab("gender")),
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
            "model_library_tagging skipped: cannot read image id=%s key=%s err=%s",
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
            "purpose": "model_library_tagging",
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
                headers={**_auth_headers(api_key), "content-type": "application/json"},
            )
    except httpx.TimeoutException as exc:
        raise UpstreamError(
            "model library tagging upstream timeout",
            error_code=EC.UPSTREAM_TIMEOUT.value,
            status_code=None,
        ) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(
            f"model library tagging upstream network error: {exc}",
            error_code=EC.UPSTREAM_ERROR.value,
            status_code=None,
        ) from exc

    if resp.status_code >= 400:
        raise UpstreamError(
            f"model library tagging upstream http {resp.status_code}",
            error_code=EC.UPSTREAM_ERROR.value,
            status_code=resp.status_code,
        )

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpstreamError(
            "model library tagging upstream returned invalid json",
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
                        "model_library_tagging terminal upstream failure "
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
            "model_library_tagging upstream failed image_id=%s providers=%s err=%.300s",
            getattr(image_record, "id", None),
            ",".join(attempted_providers) or "<none>",
            str(last_exc),
        )
    return None


async def auto_tag_image_record(
    image_record: Any,
    *,
    model: str = _DEFAULT_TAGGING_MODEL,
) -> AutoTagResult:
    """对一张已加载的 Image ORM 行做 vision tagging。

    永不 raise expected upstream/parse 错误：返回字段缺省的 AutoTagResult。
    """
    image_id = str(getattr(image_record, "id", "") or "")
    if not image_id:
        return AutoTagResult(image_id="")
    image_url = await _image_data_url(image_record)
    if image_url is None:
        return AutoTagResult(image_id=image_id)
    try:
        async with asyncio.timeout(_TAGGING_TOTAL_TIMEOUT_S):
            raw = await _call_upstream(image_record, image_url, model=model)
    except (TimeoutError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "model_library_tagging failed image_id=%s err=%s",
            image_id,
            exc,
        )
        return AutoTagResult(image_id=image_id)
    if not raw:
        return AutoTagResult(image_id=image_id)
    return _parse_tagging_payload(image_id, raw)


async def auto_tag_model_image(
    session: Any,
    *,
    image_id: str,
    user_id: str,
    model: str = _DEFAULT_TAGGING_MODEL,
) -> AutoTagResult:
    """主入口：从 session 拉 image 行，调 vision，返回结构化字段。

    用户隔离：image_id 必须属于该 user_id，否则返回空结果（防止越权识别他人图）。
    """
    if not image_id or not user_id:
        return AutoTagResult(image_id=image_id or "")
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
        return AutoTagResult(image_id=image_id)
    return await auto_tag_image_record(record, model=model)


__all__ = [
    "AutoTagResult",
    "auto_tag_image_record",
    "auto_tag_model_image",
]
