"""Shared vision tagging helpers for library image analysis.

This module is intentionally app-agnostic: it owns prompt constants, JSON
parsing, result dataclasses, and the single-provider Responses call. API and
worker processes provide their own storage reads and provider selection.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from .constants import GenerationErrorCode as EC
from .providers import ProviderProxyDefinition, resolve_provider_proxy_url


VALID_MODEL_LIBRARY_AGE_SEGMENTS: frozenset[str] = frozenset(
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

MODEL_LIBRARY_TAGGING_INSTRUCTIONS = (
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
    "只输出 JSON 对象，不要 Markdown / 代码块 / 解释。字段必须用上述英文 key。"
)
MODEL_LIBRARY_APPEARANCE_VALID: frozenset[str] = frozenset(
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

POSTER_STYLE_TAGGING_INSTRUCTIONS = (
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
_VALID_POSTER_CATEGORIES: frozenset[str] = frozenset(
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
_POSTER_CATEGORY_ALIASES: dict[str, str] = {
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

TAGGING_HTTP_TIMEOUT_S = 25.0
TAGGING_TOTAL_TIMEOUT_S = 60.0
PER_PROVIDER_RETRY_ATTEMPTS = 2
PER_PROVIDER_RETRY_BACKOFF_S = 1.0
DEFAULT_TAGGING_MODEL = "gpt-5.4-mini"


class VisionTaggingUpstreamError(Exception):
    """Small upstream error type shared by API/worker tagging wrappers."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


@dataclass(slots=True)
class AutoTagResult:
    """Model-library vision result. Empty fields mean "not recognized"."""

    image_id: str
    style_tags: list[str] = field(default_factory=list)
    appearance_direction: str | None = None
    age_segment: str | None = None
    gender: str | None = None
    notes: str | None = None

    def __bool__(self) -> bool:
        return bool(
            self.style_tags
            or self.appearance_direction
            or self.age_segment
            or self.gender
            or self.notes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "age_segment": self.age_segment,
            "gender": self.gender,
            "appearance_direction": self.appearance_direction,
            "style_tags": list(self.style_tags or []),
            "notes": self.notes,
        }


@dataclass(slots=True)
class PosterStyleAutoTagResult:
    """Poster-style vision result. Empty fields mean "not recognized"."""

    image_id: str
    category: str | None = None
    style_tags: list[str] = field(default_factory=list)
    mood: str | None = None
    palette: list[str] = field(default_factory=list)
    notes: str | None = None

    def __bool__(self) -> bool:
        return bool(
            self.category or self.style_tags or self.mood or self.palette or self.notes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "style_tags": list(self.style_tags or []),
            "mood": self.mood,
            "palette": list(self.palette or []),
            "notes": self.notes,
        }


def _normalize_age_segment(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace(" ", "_")
    if key in VALID_MODEL_LIBRARY_AGE_SEGMENTS:
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


def _normalize_poster_category(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s\-]+", "_", value.strip().lower()).strip("_")
    if not key:
        return None
    if key in _VALID_POSTER_CATEGORIES:
        return key
    return _POSTER_CATEGORY_ALIASES.get(key)


def _clean_style_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [part for part in re.split(r"[,，;；、\s]+", value) if part]
    elif isinstance(value, list):
        candidates = [
            str(part) for part in value if isinstance(part, (str, int, float))
        ]
    else:
        candidates = []
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        tag = str(raw).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag[:8])
        if len(out) >= 6:
            break
    return out


def _clean_palette(value: Any) -> list[str]:
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
        normalized = (
            hex_value.upper() if hex_value.startswith("#") else f"#{hex_value.upper()}"
        )
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized[:9])
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
    stripped = text.strip()
    fence_matches = re.findall(
        r"(?m)^[ \t]*```[ \t]*(?:[A-Za-z0-9_-]+)?[ \t]*\n([\s\S]*?)\n^[ \t]*```[ \t]*(?=\n|$)",
        stripped,
    )
    if fence_matches:
        for candidate in fence_matches:
            body = candidate.strip()
            if re.search(r"\{[\s\S]*\}", body):
                return body
        stripped = fence_matches[0]
    return stripped.strip()


def parse_model_library_tagging_payload(image_id: str, raw_text: str) -> AutoTagResult:
    if not raw_text:
        return AutoTagResult(image_id=image_id)
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
        return _model_regex_fallback(image_id, cleaned)
    appearance_raw = _clean_optional_text(
        payload.get("appearance_direction") or payload.get("appearanceDirection"),
        max_len=80,
    )
    appearance = (
        appearance_raw.strip().lower().replace(" ", "_") if appearance_raw else None
    )
    if appearance and appearance not in MODEL_LIBRARY_APPEARANCE_VALID:
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


def _model_regex_fallback(image_id: str, text: str) -> AutoTagResult:
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
    appearance_raw = _clean_optional_text(_grab("appearance_direction"), max_len=80)
    appearance = (
        appearance_raw.strip().lower().replace(" ", "_") if appearance_raw else None
    )
    if appearance and appearance not in MODEL_LIBRARY_APPEARANCE_VALID:
        appearance = None
    return AutoTagResult(
        image_id=image_id,
        style_tags=_clean_style_tags(tags_match.group(1) if tags_match else None),
        appearance_direction=appearance,
        age_segment=_normalize_age_segment(_grab("age_segment")),
        gender=_normalize_gender(_grab("gender")),
        notes=_clean_optional_text(_grab("notes"), max_len=200),
    )


def parse_poster_style_tagging_payload(
    image_id: str, raw_text: str
) -> PosterStyleAutoTagResult:
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
        return _poster_regex_fallback(image_id, cleaned)
    return PosterStyleAutoTagResult(
        image_id=image_id,
        category=_normalize_poster_category(payload.get("category")),
        style_tags=_clean_style_tags(payload.get("style_tags") or payload.get("tags")),
        mood=_clean_optional_text(payload.get("mood"), max_len=120),
        palette=_clean_palette(payload.get("palette")),
        notes=_clean_optional_text(payload.get("notes"), max_len=200),
    )


def _poster_regex_fallback(image_id: str, text: str) -> PosterStyleAutoTagResult:
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
        category=_normalize_poster_category(_grab("category")),
        style_tags=_clean_style_tags(tags_match.group(1) if tags_match else None),
        mood=_clean_optional_text(_grab("mood"), max_len=120),
        palette=_clean_palette(palette_match.group(1) if palette_match else None),
        notes=_clean_optional_text(_grab("notes"), max_len=200),
    )


def image_record_to_data_url(image_record: Any, raw: bytes) -> str | None:
    if not raw:
        return None
    mime = getattr(image_record, "mime", None)
    if not isinstance(mime, str) or not mime.startswith("image/"):
        mime = "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def responses_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return f"{base}/responses"


def extract_response_text(payload: Any) -> str:
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


async def call_vision_tagging_upstream_one(
    *,
    image_id: str,
    image_url: str,
    model: str,
    base_url: str,
    api_key: str,
    purpose: str,
    instructions: str,
    proxy: ProviderProxyDefinition | None = None,
    auth_headers: Mapping[str, str] | None = None,
    read_timeout_s: float = TAGGING_HTTP_TIMEOUT_S,
) -> str:
    body = {
        "model": model,
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": instructions},
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        ],
        "metadata": {"image_id": image_id, "purpose": purpose},
        "stream": False,
        "store": False,
        "max_output_tokens": 600,
    }
    headers = {
        "authorization": f"Bearer {api_key}",
        **dict(auth_headers or {}),
        "content-type": "application/json",
    }
    try:
        proxy_url = await resolve_provider_proxy_url(proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=read_timeout_s,
                write=read_timeout_s,
                pool=10.0,
            ),
            proxy=proxy_url,
        ) as client:
            resp = await client.post(
                responses_url(base_url),
                json=body,
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        raise VisionTaggingUpstreamError(
            "vision tagging upstream timeout",
            error_code=EC.UPSTREAM_TIMEOUT.value,
            status_code=None,
        ) from exc
    except httpx.HTTPError as exc:
        raise VisionTaggingUpstreamError(
            f"vision tagging upstream network error: {exc}",
            error_code=EC.UPSTREAM_ERROR.value,
            status_code=None,
        ) from exc

    if resp.status_code >= 400:
        raise VisionTaggingUpstreamError(
            f"vision tagging upstream http {resp.status_code}",
            error_code=EC.UPSTREAM_ERROR.value,
            status_code=resp.status_code,
        )

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise VisionTaggingUpstreamError(
            "vision tagging upstream returned invalid json",
            error_code=EC.BAD_RESPONSE.value,
            status_code=resp.status_code,
        ) from exc
    return extract_response_text(payload)


__all__ = [
    "AutoTagResult",
    "DEFAULT_TAGGING_MODEL",
    "MODEL_LIBRARY_TAGGING_INSTRUCTIONS",
    "PER_PROVIDER_RETRY_ATTEMPTS",
    "PER_PROVIDER_RETRY_BACKOFF_S",
    "POSTER_STYLE_TAGGING_INSTRUCTIONS",
    "PosterStyleAutoTagResult",
    "TAGGING_HTTP_TIMEOUT_S",
    "TAGGING_TOTAL_TIMEOUT_S",
    "VisionTaggingUpstreamError",
    "_clean_style_tags",
    "_normalize_age_segment",
    "_normalize_gender",
    "_strip_markdown_fences",
    "call_vision_tagging_upstream_one",
    "extract_response_text",
    "image_record_to_data_url",
    "parse_model_library_tagging_payload",
    "parse_poster_style_tagging_payload",
    "responses_url",
]
