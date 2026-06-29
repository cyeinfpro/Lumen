"""提示词增强（Prompt Enhancement）。

POST /prompts/enhance — 流式返回 AI 优化后的图像生成提示词。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import secrets
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import Image, Video, new_uuid7
from lumen_core.pricing import UsageTokens, parse_usage
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    build_effective_provider_config,
    endpoint_kind_allowed,
    provider_supports_route,
    resolve_provider_proxy_url,
    weighted_priority_order,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import VideoReferenceMediaIn
from lumen_core.vision_tagging import image_record_to_data_url

from ..billing_cache_state import invalidate_balance_cache
from ..config import settings
from ..db import SessionLocal, get_db
from ..deps import CurrentUser, verify_csrf
from ..audit import hash_email, write_audit
from ..public_urls import resolve_public_base_url
from ..ratelimit import RateLimiter
from ..redis_client import get_redis
from ..runtime_settings import get_setting

logger = logging.getLogger(__name__)

_VIDEO_REFERENCE_ACCESS_TOKEN_TTL = timedelta(hours=24)

router = APIRouter(
    prefix="/prompts",
    tags=["prompts"],
    dependencies=[Depends(verify_csrf)],
)

ENHANCE_SYSTEM_PROMPT = """\
You are an expert prompt engineer for AI image generation.
Your task is to enhance the user's image prompt to produce more vivid, detailed results.

Rules:
- Maintain the user's original intent and subject matter exactly
- Add rich details: lighting, atmosphere, composition, texture, color palette, style
- Keep the output concise — one paragraph, under 200 words
- Write in the same language as the input
- Do NOT add negative prompts, technical parameters, or meta-instructions
- Do NOT wrap in quotes or add any prefix/suffix like "Enhanced prompt:"
- Output ONLY the enhanced prompt text, nothing else\
"""
VIDEO_ENHANCE_SYSTEM_PROMPT = """\
You are an expert prompt engineer for AI video generation.
Your task is to enhance the user's video prompt for a text-to-video, image-to-video, or reference-guided video model.
The result must be motion/camera-first: improve what moves, how it moves, how the camera moves with it, and how the shot evolves over time.
Optimize for Volcano/Seedance-style video generation prompts: clear subject, scene, action timeline, camera movement, visual style, duration-aware pacing, and reference consistency.
Also apply Vibe Creating when appropriate: preserve the user's creative intent while strengthening story, emotion, memory, atmosphere, imagery, and subjective experience.

Rules:
- Before writing, silently judge whether the real scene fits Vibe Creating, whether the current expression is already usable, and whether information is sufficient; never expose internal labels or classifications
- If the prompt is already vivid, coherent, and generation-ready, lightly refine it or leave it essentially intact instead of over-expanding it
- If the prompt lacks a visual anchor, main action/state, local mood, or video theme/style, ask 1-3 concise clarification questions instead of inventing unsupported characters, relationships, plot twists, scenes, or emotional turns
- If the request is a UI tutorial, functional demo, strict step-by-step script, or exact dialogue-sync scene, preserve the original workflow and do not force Vibe Creating; at most provide an optional VC version when useful
- Preserve the user's original intent and subject matter exactly
- Preserve exact dialogue, voiceover, music, sound effects, lyrics, required structure, and explicitly requested parameters verbatim
- Use supplied reference images, first frames, posters, and video URLs as visual constraints for identity, styling, and composition continuity
- Preserve exact reference anchors such as [ref:image:1] or [ref:video:1] when they appear; never replace an anchor with a plain description only
- If multiple same-kind references are supplied and the user says this image, that image, left image, or another ambiguous phrase without an anchor, ask a concise clarification question instead of guessing
- For image-to-video or first-frame tasks, keep identity, outfit/product details, layout, lighting, and viewpoint stable; describe only the intended motion after the reference frame
- For reference-video tasks, extract reusable motion rhythm, camera path, and continuity cues without copying unrelated subjects or scenes
- Do NOT repeat or inventory existing subjects, clothing, props, backgrounds, or other static elements already present unless a detail is needed for continuity
- Convert vague input into one compact shot plan: subject action, motion trajectory, pose changes, camera movement, lens/framing, beginning-to-end temporal progression, rhythm, motion continuity, and reference-material consistency
- Use one primary subject action and one primary camera move unless the user explicitly requests a multi-shot sequence; avoid conflicting camera directions, impossible physics, and overcrowded scene changes
- De-emphasize low-value technical camera controls such as focal length, aperture, exposure, ISO, device jargon, and pure editing commands unless the user explicitly asks to keep them; translate useful camera intent into audience-facing visual feeling
- Multi-shot inputs may remain as natural paragraphs when they serve one unified experience; keep numbering only when the user explicitly requests numbered structure
- Respect supplied model, duration, resolution, aspect ratio, and audio intent when they are present
- Do not invent subtitles, captions, interface text, logos, watermarks, or on-screen typography unless the user explicitly requests them
- Keep the output concise - one paragraph, under 220 words
- Write in the same language as the user's prompt; if no prompt is provided, write in Chinese
- Do NOT add negative prompts, seed values, command flags, JSON, technical parameters, markdown, explanations, or labels
- Do NOT wrap in quotes or add any prefix/suffix like "Enhanced prompt:"
- Output ONLY the enhanced video prompt text, nothing else\
"""
VIDEO_ENHANCE_VARIANT_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert prompt engineer for AI video generation.
Your task is to enhance the user's video prompt for a text-to-video, image-to-video, or reference-guided video model.
The result must be motion/camera-first: improve what moves, how it moves, how the camera moves with it, and how the shot evolves over time.
Optimize for Volcano/Seedance-style video generation prompts: clear subject, scene, action timeline, camera movement, visual style, duration-aware pacing, and reference consistency.
Also apply Vibe Creating when appropriate: preserve the user's creative intent while strengthening story, emotion, memory, atmosphere, imagery, and subjective experience.

Rules:
- Before writing, silently judge whether the real scene fits Vibe Creating, whether the current expression is already usable, and whether information is sufficient; never expose internal labels or classifications
- If the prompt is already vivid, coherent, and generation-ready, lightly refine it or leave it essentially intact instead of over-expanding it
- If the prompt lacks a visual anchor, main action/state, local mood, or video theme/style, ask 1-3 concise clarification questions instead of inventing unsupported characters, relationships, plot twists, scenes, or emotional turns
- If the request is a UI tutorial, functional demo, strict step-by-step script, or exact dialogue-sync scene, preserve the original workflow and do not force Vibe Creating; at most provide an optional VC version when useful
- Preserve the user's original intent and subject matter exactly
- Preserve exact dialogue, voiceover, music, sound effects, lyrics, required structure, and explicitly requested parameters verbatim
- Use supplied reference images, first frames, posters, and video URLs as visual constraints for identity, styling, and composition continuity
- Preserve exact reference anchors such as [ref:image:1] or [ref:video:1] when they appear; never replace an anchor with a plain description only
- If multiple same-kind references are supplied and the user says this image, that image, left image, or another ambiguous phrase without an anchor, ask a concise clarification question instead of guessing
- For image-to-video or first-frame tasks, keep identity, outfit/product details, layout, lighting, and viewpoint stable; describe only the intended motion after the reference frame
- For reference-video tasks, extract reusable motion rhythm, camera path, and continuity cues without copying unrelated subjects or scenes
- Do NOT repeat or inventory existing subjects, clothing, props, backgrounds, or other static elements already present unless a detail is needed for continuity
- Convert vague input into one compact shot plan: subject action, motion trajectory, pose changes, camera movement, lens/framing, beginning-to-end temporal progression, rhythm, motion continuity, and reference-material consistency
- Use one primary subject action and one primary camera move unless the user explicitly requests a multi-shot sequence; avoid conflicting camera directions, impossible physics, and overcrowded scene changes
- De-emphasize low-value technical camera controls such as focal length, aperture, exposure, ISO, device jargon, and pure editing commands unless the user explicitly asks to keep them; translate useful camera intent into audience-facing visual feeling
- Multi-shot inputs may remain as natural paragraphs when they serve one unified experience; keep numbering only when the user explicitly requests numbered structure
- Respect supplied model, duration, resolution, aspect ratio, and audio intent when they are present
- Do not invent subtitles, captions, interface text, logos, watermarks, or on-screen typography unless the user explicitly requests them
- Keep each variant concise - one paragraph, under 220 words
- Write in the same language as the user's prompt; if no prompt is provided, write in Chinese
- Do NOT add negative prompts, seed values, command flags, JSON, technical parameters, markdown, explanations, or commentary
- Output exactly {variant_count} variants and nothing else
- Use this strict XML-like format for every candidate: <variant action="direct_rewrite" title="short unique title">enhanced video prompt text</variant>
- Allowed action values: direct_pass, light_refine, direct_rewrite, ask_first, keep_original, optional_vc
- If information is insufficient, output only one ask_first variant containing the minimum 1-3 questions needed; do not pad to {variant_count} variants
- If the scene is low-fit for Vibe Creating, output one keep_original variant or one optional_vc variant instead of forcing a rewrite
- The first <variant> must be the recommended best option
- Each variant must emphasize a distinct generation strategy: subject action trajectory, camera movement/framing, or rhythm/continuity/reference consistency
- Do NOT add numbering, markdown fences, bullet lists, labels, or any text before, between, or after the variant blocks\
"""

_PROVIDER_RR_COUNTERS: dict[int, int] = {}
_PROVIDER_RR_LOCK = asyncio.Lock()
_RETRYABLE_HTTP_STATUS = {408, 409, 425, 429}
_FALLBACK_400_MARKERS = (
    "model",
    "service_tier",
    "tier",
    "reasoning",
    "unsupported",
    "not_found",
    "not found",
)
PROMPTS_ENHANCE_LIMITER = RateLimiter(capacity=20, refill_per_sec=20 / 60)
_PROMPT_ENHANCE_MEDIA_MAX_BYTES = 18 * 1024 * 1024
_PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES = 24 * 1024 * 1024
_PROMPT_ENHANCE_KEEPALIVE_SECONDS = 10.0
_PROMPT_ENHANCE_KEEPALIVE_CHUNK = ": keep-alive\n\n"
_PROMPT_ENHANCE_CONNECT_TIMEOUT_SECONDS = 10.0
_PROMPT_ENHANCE_READ_TIMEOUT_SECONDS = 25.0
_PROMPT_ENHANCE_WRITE_TIMEOUT_SECONDS = 10.0
_PROMPT_ENHANCE_POOL_TIMEOUT_SECONDS = 10.0
_PROMPT_ENHANCE_RELEASE_TASKS: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class _EnhanceAttempt:
    name: str
    model: str
    reasoning_effort: str | None = "low"
    service_tier: str | None = "priority"


@dataclass
class _EnhanceBillingContext:
    db: AsyncSession
    user_id: str
    user_email: str | None
    request_id: str
    rate_multiplier_x10000: int
    cache_aware: bool
    allow_negative: bool
    hold_amount_micro: int = 0


@dataclass
class _EnhanceUsageCapture:
    provider_name: str | None = None
    model: str | None = None
    service_tier: str = "standard"
    response_id: str | None = None
    usage: dict[str, Any] | None = None


_ENHANCE_ATTEMPTS = (
    _EnhanceAttempt(name="primary", model="gpt-5.5", reasoning_effort="low"),
    _EnhanceAttempt(
        name="fallback-gpt-5.4-low", model="gpt-5.4", reasoning_effort="low"
    ),
    _EnhanceAttempt(
        name="fallback-gpt-5.4-low-standard",
        model="gpt-5.4",
        reasoning_effort="low",
        service_tier=None,
    ),
)


class EnhanceIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class VideoEnhanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=10000)
    action: str = Field(default="t2v", max_length=32)
    model: str = Field(default="", max_length=128)
    duration_s: int | None = Field(default=None, ge=-1, le=60)
    resolution: str | None = Field(default=None, max_length=32)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    generate_audio: bool | None = None
    input_image_id: str | None = Field(default=None, max_length=36)
    variant_count: int = Field(default=1, ge=1, le=3)
    reference_media: list[VideoReferenceMediaIn] = Field(
        default_factory=list,
        max_length=12,
    )

    @model_validator(mode="after")
    def require_prompt_or_reference(self) -> "VideoEnhanceIn":
        if (
            not self.text.strip()
            and not (self.input_image_id or "").strip()
            and not self.reference_media
        ):
            raise ValueError("text or reference media is required")
        return self


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


def _responses_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"


def _video_enhance_system_prompt(variant_count: int) -> str:
    if variant_count <= 1:
        return VIDEO_ENHANCE_SYSTEM_PROMPT
    return VIDEO_ENHANCE_VARIANT_SYSTEM_PROMPT_TEMPLATE.format(
        variant_count=variant_count
    )


def _provider_allows_prompt_enhance(provider: ProviderDefinition) -> bool:
    return (
        "chat" in provider.purposes
        and endpoint_kind_allowed(provider, "responses")
        and provider_supports_route(
            provider,
            route="text",
            endpoint_kind="responses",
        )
    )


async def _resolve_provider_order(db: AsyncSession) -> list[ProviderDefinition]:
    """Read Provider Pool, with legacy UPSTREAM_* env fallback only if absent."""
    spec_providers = get_spec("providers")
    raw_providers = await get_setting(db, spec_providers) if spec_providers else None
    providers, _proxies, errors = build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    for err in errors:
        logger.warning("%s", err)
    providers = [p for p in providers if _provider_allows_prompt_enhance(p)]
    async with _PROVIDER_RR_LOCK:
        return weighted_priority_order(providers, _PROVIDER_RR_COUNTERS)


class _EnhanceProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _build_enhance_body(
    text: str,
    attempt: _EnhanceAttempt,
    *,
    system_prompt: str = ENHANCE_SYSTEM_PROMPT,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": attempt.model,
        "instructions": system_prompt,
        "input": [
            {
                "role": "user",
                "content": content
                if content is not None
                else [{"type": "input_text", "text": text}],
            }
        ],
        "stream": True,
    }
    if metadata:
        body["metadata"] = metadata
    if attempt.reasoning_effort:
        body["reasoning"] = {"effort": attempt.reasoning_effort}
    if attempt.service_tier:
        body["service_tier"] = attempt.service_tier
    return body


def _storage_path(storage_key: str) -> Path:
    root = Path(settings.storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise _http("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise _http("invalid_path", "absolute storage paths are not allowed", 400)
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise _http("invalid_path", "storage path escapes root", 400) from None
    return path


async def _owned_image(db: AsyncSession, *, user_id: str, image_id: str) -> Image:
    image = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if image is None:
        raise _http("image_not_found", "image not found", 404)
    return image


async def _owned_video(db: AsyncSession, *, user_id: str, video_id: str) -> Video:
    video = (
        await db.execute(
            select(Video).where(
                Video.id == video_id,
                Video.user_id == user_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise _http("video_not_found", "video not found", 404)
    return video


async def _image_data_url(image: Image) -> str | None:
    if image.size_bytes and image.size_bytes > _PROMPT_ENHANCE_MEDIA_MAX_BYTES:
        return None
    try:
        raw = await asyncio.to_thread(_storage_path(image.storage_key).read_bytes)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "prompt enhance read image failed image_id=%s key=%s err=%s",
            image.id,
            image.storage_key,
            exc,
        )
        return None
    if len(raw) > _PROMPT_ENHANCE_MEDIA_MAX_BYTES:
        return None
    return image_record_to_data_url(image, raw)


async def _video_poster_data_url(video: Video) -> str | None:
    key = (video.poster_storage_key or "").strip()
    if not key:
        return None
    try:
        raw = await asyncio.to_thread(_storage_path(key).read_bytes)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "prompt enhance read video poster failed video_id=%s key=%s err=%s",
            video.id,
            key,
            exc,
        )
        return None
    if not raw or len(raw) > _PROMPT_ENHANCE_MEDIA_MAX_BYTES:
        return None
    mime, _encoding = mimetypes.guess_type(key)
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _append_input_image_with_budget(
    content: list[dict[str, Any]],
    image_url: str,
    *,
    media_payload_bytes: int,
) -> tuple[bool, int]:
    next_payload_bytes = media_payload_bytes
    if image_url.startswith("data:image/"):
        payload_bytes = len(image_url.encode("utf-8"))
        if media_payload_bytes + payload_bytes > _PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES:
            return False, media_payload_bytes
        next_payload_bytes += payload_bytes
    content.append({"type": "input_image", "image_url": image_url})
    return True, next_payload_bytes


def _external_image_url_for_input(url: str | None) -> str | None:
    value = (url or "").strip()
    if value.startswith("data:image/"):
        return value
    if value.startswith(("http://", "https://")):
        return value
    return None


def _append_video_context_line(lines: list[str], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        clean = value.strip()
        if not clean:
            return
        lines.append(f"{key}: {clean}")
        return
    lines.append(f"{key}: {value}")


def _reference_anchor(ref_id: str | None, kind: str, index: int) -> str:
    clean = (ref_id or "").strip().lower()
    parts = clean.split(":")
    if (
        len(parts) == 3
        and parts[0] == "ref"
        and parts[1] == kind
        and parts[2].isdigit()
        and int(parts[2]) > 0
    ):
        return f"[{clean}]"
    return f"[ref:{kind}:{index}]"


def _video_reference_public_url(video: Video, public_base_url: str) -> tuple[str, bool]:
    metadata = dict(video.metadata_jsonb or {})
    token = metadata.get("reference_access_token")
    expires_raw = metadata.get("reference_access_token_expires_at")
    expires_at = None
    if isinstance(expires_raw, str) and expires_raw.strip():
        with suppress(ValueError):
            expires_at = datetime.fromisoformat(expires_raw)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            else:
                expires_at = expires_at.astimezone(timezone.utc)
    changed = False
    if (
        not isinstance(token, str)
        or not token
        or expires_at is None
        or expires_at <= datetime.now(timezone.utc)
    ):
        token = secrets.token_urlsafe(32)
        metadata["reference_access_token"] = token
        changed = True
    metadata["reference_access_token_expires_at"] = (
        datetime.now(timezone.utc) + _VIDEO_REFERENCE_ACCESS_TOKEN_TTL
    ).isoformat()
    video.metadata_jsonb = metadata
    changed = True
    query = urlencode({"token": token})
    return (
        f"{public_base_url.rstrip('/')}/api/videos/reference/{video.id}/binary?{query}",
        changed,
    )


async def _resolve_optional_public_base_url(
    request: Request,
    db: AsyncSession,
) -> str | None:
    try:
        return await resolve_public_base_url(request, db)
    except Exception as exc:  # noqa: BLE001
        logger.info("prompt enhance public base unavailable: %s", exc)
        return None


async def _build_video_enhance_content(
    body: VideoEnhanceIn,
    *,
    request: Request,
    db: AsyncSession,
    user_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    lines = [
        "任务：将下面的输入优化成可直接提交给视频生成模型的一段提示词。",
        (
            "优化重点：按火山/Seedance 视频提示词结构补齐主体动作、"
            "运动轨迹、运镜、首尾时间推进、视觉风格和参考一致性；"
            "不要生成字幕、水印、UI 文案、seed 或命令参数。"
        ),
        (
            "Vibe Creating 判断：先判断真实场景是否适合故事、情绪、记忆、"
            "氛围、意象或主观体验表达；再判断原文应直接放行、轻度提纯、"
            "直接改写、先补问、原样保留，还是只给可选 VC 版。"
        ),
        (
            "信息密度：若缺少视觉锚点、行为/状态、局部调性或视频主题/风格，"
            '请先输出 action="ask_first" 的 1-3 个必要问题，不要硬猜。'
        ),
        (
            "参考素材锚点合同：参考图/视频都有唯一锚点，例如 [ref:image:1]。"
            "优化后必须保留用户实际引用到的锚点；如果同类素材有多张而用户只说"
            "这张图、那张图、左图、右图等模糊指代，请输出 ask_first 补问，"
            "不要自行选择。"
        ),
        (
            "约束优先：用户明确写出的台词、旁白、音乐、音效、歌词、"
            "镜头结构、参数保留要求和交付格式必须保留；未要求保留的焦段、"
            "光圈、曝光、ISO、设备和纯剪辑参数可转译为自然观看感受。"
        ),
        f"原始描述：{body.text.strip() or '（未填写，请主要根据参考素材生成）'}",
    ]
    _append_video_context_line(lines, "生成模式", body.action)
    _append_video_context_line(lines, "模型", body.model)
    _append_video_context_line(lines, "时长", body.duration_s)
    _append_video_context_line(lines, "分辨率", body.resolution)
    _append_video_context_line(lines, "画幅", body.aspect_ratio)
    if body.generate_audio is not None:
        lines.append(f"音频：{'需要' if body.generate_audio else '不需要'}")
    if body.variant_count > 1:
        lines.append(
            f"候选方案数量：{body.variant_count}；必须按 "
            '<variant action="direct_rewrite" title="...">...</variant> 输出，'
            "第一项为推荐最佳；action 只能是 direct_pass、light_refine、"
            "direct_rewrite、ask_first、keep_original、optional_vc；"
            "信息不足或低适配时可只输出 1 个补问/保留候选，不要为了凑数硬改写；"
            "正常改写时每个方案应有不同侧重，分别强化动作轨迹、运镜/镜头语言、"
            "时间推进/节奏/连续性/参考素材一致性。"
        )

    content: list[dict[str, Any]] = [{"type": "input_text", "text": "\n".join(lines)}]
    token_changed = False
    media_payload_bytes = 0

    if body.input_image_id:
        image = await _owned_image(db, user_id=user_id, image_id=body.input_image_id)
        content.append({"type": "input_text", "text": "首帧参考图："})
        image_url = await _image_data_url(image)
        if image_url:
            appended, media_payload_bytes = _append_input_image_with_budget(
                content,
                image_url,
                media_payload_bytes=media_payload_bytes,
            )
            if not appended:
                content.append(
                    {
                        "type": "input_text",
                        "text": f"首帧参考图 image_id={image.id}，但参考素材总体过大，已降级为文字引用。",
                    }
                )
        else:
            content.append(
                {
                    "type": "input_text",
                    "text": f"首帧参考图 image_id={image.id}，但图片过大或暂不可读取。",
                }
            )

    public_base_url: str | None = None
    for index, item in enumerate(body.reference_media, start=1):
        noun = "图片" if item.kind == "image" else "音频" if item.kind == "audio" else "视频"
        label = (item.label or "").strip() or f"参考{noun} {index}"
        same_kind_index = sum(
            1 for prior in body.reference_media[:index] if prior.kind == item.kind
        )
        anchor = _reference_anchor(item.ref_id, item.kind, same_kind_index)
        if item.kind == "image":
            if item.image_id:
                image = await _owned_image(db, user_id=user_id, image_id=item.image_id)
                content.append(
                    {
                        "type": "input_text",
                        "text": f"{label} 锚点 {anchor}；优化输出引用该素材时必须保留此锚点：",
                    }
                )
                image_url = await _image_data_url(image)
                if image_url:
                    appended, media_payload_bytes = _append_input_image_with_budget(
                        content,
                        image_url,
                        media_payload_bytes=media_payload_bytes,
                    )
                    if not appended:
                        content.append(
                            {
                                "type": "input_text",
                                "text": f"{label} image_id={image.id}，但参考素材总体过大，已降级为文字引用。",
                            }
                        )
                else:
                    content.append(
                        {
                            "type": "input_text",
                            "text": f"{label} image_id={image.id}，但图片过大或暂不可读取。",
                        }
                    )
            elif image_url := _external_image_url_for_input(item.url):
                is_data_image = image_url.startswith("data:image/")
                content.append(
                    {
                        "type": "input_text",
                        "text": (
                            f"{label} 锚点 {anchor}；外部图片数据 URL："
                            if is_data_image
                            else f"{label} 锚点 {anchor}；外部图片 URL：{image_url}"
                        ),
                    }
                )
                appended, media_payload_bytes = _append_input_image_with_budget(
                    content,
                    image_url,
                    media_payload_bytes=media_payload_bytes,
                )
                if not appended:
                    content.append(
                        {
                            "type": "input_text",
                            "text": (
                                f"{label} 外部图片数据 URL 过大，已忽略图片内容，仅保留标签。"
                                if is_data_image
                                else f"{label} 外部图片过大，已降级为 URL 文字引用。"
                            ),
                        }
                    )
            else:
                content.append(
                    {
                        "type": "input_text",
                        "text": f"{label} 锚点 {anchor}；外部图片引用：{(item.url or '').strip()}",
                    }
                )
            continue

        if item.kind == "audio":
            content.append(
                {
                    "type": "input_text",
                    "text": f"{label} 锚点 {anchor}；外部参考音频 URL：{(item.url or '').strip()}",
                }
            )
            continue

        if item.video_id:
            video = await _owned_video(db, user_id=user_id, video_id=item.video_id)
            if public_base_url is None:
                public_base_url = await _resolve_optional_public_base_url(request, db)
            video_url = ""
            if public_base_url:
                video_url, changed = _video_reference_public_url(video, public_base_url)
                token_changed = token_changed or changed
            details = [
                f"{label}：参考视频",
                f"anchor={anchor}",
                f"video_id={video.id}",
                f"mime={video.mime}",
                f"duration_ms={video.duration_ms}",
                f"size_bytes={video.size_bytes}",
            ]
            if video_url:
                details.append(f"url={video_url}")
            content.append({"type": "input_text", "text": "；".join(details)})
            poster_url = await _video_poster_data_url(video)
            if poster_url:
                content.append(
                    {
                        "type": "input_text",
                        "text": f"{label} 的 poster / 首帧视觉参考：",
                    }
                )
                appended, media_payload_bytes = _append_input_image_with_budget(
                    content,
                    poster_url,
                    media_payload_bytes=media_payload_bytes,
                )
                if not appended:
                    content.append(
                        {
                            "type": "input_text",
                            "text": f"{label} 的 poster 过大，已降级为文字引用。",
                        }
                    )
        else:
            content.append(
                {
                    "type": "input_text",
                    "text": f"{label} 锚点 {anchor}；外部参考视频 URL：{(item.url or '').strip()}",
                }
            )

    return content, token_changed


async def _setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    try:
        return await get_setting(db, spec)
    except (AssertionError, IndexError):
        if key.startswith("billing."):
            return None
        raise


async def _billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.enabled"),
        False,
    )


async def _billing_cache_aware(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.cache_aware"),
        True,
    )


async def _billing_allow_negative(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.allow_negative_balance"),
        False,
    )


def _rate_multiplier_x10000(user: Any) -> int:
    raw = getattr(user, "billing_rate_multiplier", 1)
    try:
        return max(0, int(float(raw if raw is not None else 1) * 10_000))
    except (TypeError, ValueError):
        return 10_000


async def _prepare_prompt_enhance_billing(
    db: AsyncSession,
    user: Any,
) -> _EnhanceBillingContext | None:
    if getattr(user, "account_mode", "wallet") != "wallet":
        return None
    if not await _billing_enabled(db):
        return None

    request_id = new_uuid7()
    rate_multiplier_x10000 = _rate_multiplier_x10000(user)
    cache_aware = await _billing_cache_aware(db)
    allow_negative = await _billing_allow_negative(db)
    preview = await billing_core.estimate_completion_cost(
        db,
        model=_ENHANCE_ATTEMPTS[0].model,
        tokens_in=1,
        tokens_out=1,
        rate_multiplier_x10000=rate_multiplier_x10000,
        service_tier=_ENHANCE_ATTEMPTS[0].service_tier or "standard",
    )
    if preview <= 0:
        await write_audit(
            db,
            event_type="pricing.not_configured",
            user_id=user.id,
            actor_email_hash=hash_email(getattr(user, "email", None)),
            details={
                "scope": "chat_model",
                "model": _ENHANCE_ATTEMPTS[0].model,
                "route": "prompts.enhance.preflight",
            },
            autocommit=False,
        )
    hold_amount = max(10_000, int(preview or 0))
    try:
        await billing_core.hold(
            db,
            user.id,
            hold_amount,
            ref_type="prompt_enhance",
            ref_id=request_id,
            idempotency_key=f"prompt_enhance:hold:{request_id}",
            allow_negative=allow_negative,
            meta={
                "route": "prompts.enhance",
                "model": _ENHANCE_ATTEMPTS[0].model,
                "service_tier": _ENHANCE_ATTEMPTS[0].service_tier or "standard",
                "estimated_cost_micro": preview,
                "preauth_micro": hold_amount,
            },
        )
    except billing_core.BillingError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": {"code": exc.code, "message": exc.message}},
        ) from exc
    await db.commit()
    await invalidate_balance_cache(user.id)
    return _EnhanceBillingContext(
        db=db,
        user_id=user.id,
        user_email=getattr(user, "email", None),
        request_id=request_id,
        rate_multiplier_x10000=rate_multiplier_x10000,
        cache_aware=cache_aware,
        allow_negative=allow_negative,
        hold_amount_micro=hold_amount,
    )


def _capture_enhance_usage(
    capture: _EnhanceUsageCapture | None,
    event: dict[str, Any],
    *,
    provider: ProviderDefinition,
    attempt: _EnhanceAttempt,
) -> None:
    if capture is None:
        return
    response = event.get("response")
    response_obj = response if isinstance(response, dict) else {}
    usage = event.get("usage")
    if not isinstance(usage, dict):
        usage = response_obj.get("usage")
    if not isinstance(usage, dict):
        return

    response_id = response_obj.get("id") or event.get("response_id")
    model = response_obj.get("model") or event.get("model") or attempt.model
    capture.provider_name = provider.name
    capture.model = model if isinstance(model, str) and model.strip() else attempt.model
    capture.service_tier = attempt.service_tier or "standard"
    capture.response_id = (
        response_id if isinstance(response_id, str) and response_id.strip() else None
    )
    capture.usage = usage


def _normalize_usage_for_billing(
    usage: UsageTokens,
    *,
    cache_aware: bool,
) -> UsageTokens:
    if cache_aware:
        return usage.normalized()
    legacy_cache_input_tokens = usage.cache_read_tokens + usage.cache_creation_tokens
    return UsageTokens(
        input_tokens=usage.input_tokens + legacy_cache_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        image_output_tokens=usage.image_output_tokens,
    ).normalized()


async def _charge_prompt_enhance(
    billing: _EnhanceBillingContext,
    capture: _EnhanceUsageCapture,
) -> None:
    if not capture.usage:
        await _release_prompt_enhance_hold(billing, reason="missing_usage")
        return
    model = capture.model or _ENHANCE_ATTEMPTS[0].model
    usage = _normalize_usage_for_billing(
        parse_usage(model, capture.usage),
        cache_aware=billing.cache_aware,
    )
    if (
        usage.input_tokens <= 0
        and usage.output_tokens <= 0
        and usage.cache_read_tokens <= 0
        and usage.cache_creation_tokens <= 0
        and usage.cache_creation_5m_tokens <= 0
        and usage.cache_creation_1h_tokens <= 0
        and usage.reasoning_tokens <= 0
        and usage.image_output_tokens <= 0
    ):
        await _release_prompt_enhance_hold(billing, reason="zero_usage")
        return

    breakdown = await billing_core.estimate_completion_breakdown(
        billing.db,
        model=model,
        tokens=usage,
        rate_multiplier_x10000=billing.rate_multiplier_x10000,
        service_tier=capture.service_tier,
    )
    cost = breakdown.actual_cost_micro
    response_id = capture.response_id or billing.request_id
    ref_id = billing.request_id if billing.hold_amount_micro > 0 else response_id
    if cost <= 0 and (usage.input_tokens > 0 or usage.output_tokens > 0):
        await write_audit(
            billing.db,
            event_type=(
                "billing.pricing.missing"
                if breakdown.pricing_source == "missing"
                else "pricing.not_configured"
            ),
            user_id=billing.user_id,
            actor_email_hash=hash_email(billing.user_email),
            details={
                "scope": "chat_model",
                "model": model,
                "prompt_enhance_id": ref_id,
                "usage": usage.model_dump(),
                "pricing_source": breakdown.pricing_source,
            },
            autocommit=False,
        )
    elif breakdown.pricing_source == "fallback":
        await write_audit(
            billing.db,
            event_type="billing.pricing.fallback_used",
            user_id=billing.user_id,
            actor_email_hash=hash_email(billing.user_email),
            details={
                "model": model,
                "prompt_enhance_id": ref_id,
                "usage": usage.model_dump(),
            },
            autocommit=False,
        )

    tx_meta = {
        "route": "prompts.enhance",
        "model": model,
        "provider": capture.provider_name,
        "response_id": response_id,
        "tokens_in": usage.input_tokens,
        "tokens_out": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_creation_5m_tokens": usage.cache_creation_5m_tokens,
        "cache_creation_1h_tokens": usage.cache_creation_1h_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "image_output_tokens": usage.image_output_tokens,
        "cost_breakdown": breakdown.model_dump(),
        "rate_multiplier_x10000": billing.rate_multiplier_x10000,
        "service_tier": capture.service_tier,
    }
    if billing.hold_amount_micro > 0:
        tx = await billing_core.settle(
            billing.db,
            billing.user_id,
            ref_type="prompt_enhance",
            ref_id=ref_id,
            actual_micro=cost,
            idempotency_key=f"prompt_enhance:settle:{ref_id}",
            allow_negative=billing.allow_negative,
            meta={**tx_meta, "preauth_micro": billing.hold_amount_micro},
        )
    else:
        tx = await billing_core.charge(
            billing.db,
            billing.user_id,
            cost,
            ref_type="prompt_enhance",
            ref_id=ref_id,
            idempotency_key=f"prompt_enhance:{ref_id}",
            allow_negative=billing.allow_negative,
            record_zero=True,
            kind="charge_completion",
            meta=tx_meta,
        )
    if tx is not None:
        await write_audit(
            billing.db,
            event_type="wallet.charge.completion",
            user_id=billing.user_id,
            actor_email_hash=hash_email(billing.user_email),
            details={
                "completion_id": ref_id,
                "prompt_enhance_id": ref_id,
                "response_id": response_id,
                "route": "prompts.enhance",
                "cost_micro": cost,
                "usage": usage.model_dump(),
                "cost_breakdown": breakdown.model_dump(),
                "service_tier": capture.service_tier,
                "amount_micro": tx.amount_micro,
                "balance_after": tx.balance_after,
            },
            autocommit=False,
        )
    await billing.db.commit()
    if tx is not None:
        await invalidate_balance_cache(billing.user_id)


async def _release_prompt_enhance_hold(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> None:
    if billing is None or billing.hold_amount_micro <= 0:
        return
    try:
        await billing_core.release(
            billing.db,
            billing.user_id,
            ref_type="prompt_enhance",
            ref_id=billing.request_id,
            idempotency_key=f"prompt_enhance:release:{billing.request_id}:{reason}",
            meta={"route": "prompts.enhance", "reason": reason},
        )
        await billing.db.commit()
        await invalidate_balance_cache(billing.user_id)
    except Exception:
        logger.exception("prompt enhance billing hold release failed")


def _track_prompt_enhance_release_task(task: asyncio.Task[None]) -> None:
    _PROMPT_ENHANCE_RELEASE_TASKS.add(task)

    def _done(completed: asyncio.Task[None]) -> None:
        _PROMPT_ENHANCE_RELEASE_TASKS.discard(completed)
        with suppress(asyncio.CancelledError):
            exc = completed.exception()
            if exc is not None:
                logger.error(
                    "prompt enhance detached hold release failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    task.add_done_callback(_done)


async def _release_prompt_enhance_hold_detached(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> None:
    if billing is None or billing.hold_amount_micro <= 0:
        return
    async with SessionLocal() as db:
        detached = replace(billing, db=db)
        await _release_prompt_enhance_hold(detached, reason=reason)


def _schedule_prompt_enhance_hold_release(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> asyncio.Task[None] | None:
    if billing is None or billing.hold_amount_micro <= 0:
        return None
    task = asyncio.create_task(
        _release_prompt_enhance_hold_detached(billing, reason=reason)
    )
    _track_prompt_enhance_release_task(task)
    return task


async def _release_prompt_enhance_hold_after_cancel(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> None:
    task = _schedule_prompt_enhance_hold_release(billing, reason=reason)
    if task is None:
        return
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        logger.info(
            "prompt enhance hold release continues after stream cancellation "
            "request_id=%s reason=%s",
            billing.request_id if billing is not None else None,
            reason,
        )
        raise


def _is_retryable_upstream_error(status_code: int, raw: bytes) -> bool:
    if status_code in _RETRYABLE_HTTP_STATUS or status_code >= 500:
        return True
    if status_code not in {400, 404}:
        return False
    text = raw[:2000].decode("utf-8", errors="ignore").lower()
    return any(marker in text for marker in _FALLBACK_400_MARKERS)


def _extract_error_message(evt: dict[str, Any]) -> str:
    err = evt.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or err.get("type")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if isinstance(err, str) and err.strip():
        return err.strip()
    msg = evt.get("message")
    return msg.strip() if isinstance(msg, str) and msg.strip() else "response_failed"


def _extract_response_text(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    chunks: list[str] = []
    output = obj.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        chunks.append(text)
    text = obj.get("output_text") or obj.get("text")
    if isinstance(text, str) and text:
        chunks.append(text)
    return "".join(chunks)


def _iter_sse_payloads_from_buffer(buffer: str) -> tuple[list[str], str]:
    buffer = buffer.replace("\r\n", "\n")
    payloads: list[str] = []
    while "\n\n" in buffer:
        raw_event, buffer = buffer.split("\n\n", 1)
        data_lines: list[str] = []
        for line in raw_event.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            payloads.append("\n".join(data_lines))
    return payloads, buffer


async def _stream_enhance_one(
    text: str,
    provider: ProviderDefinition,
    attempt: _EnhanceAttempt,
    capture: _EnhanceUsageCapture | None = None,
    *,
    system_prompt: str = ENHANCE_SYSTEM_PROMPT,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    url = _responses_url(provider.base_url)

    body = _build_enhance_body(
        text,
        attempt,
        system_prompt=system_prompt,
        content=content,
        metadata=metadata,
    )

    try:
        proxy_url = await resolve_provider_proxy_url(provider.proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=_PROMPT_ENHANCE_CONNECT_TIMEOUT_SECONDS,
                read=_PROMPT_ENHANCE_READ_TIMEOUT_SECONDS,
                write=_PROMPT_ENHANCE_WRITE_TIMEOUT_SECONDS,
                pool=_PROMPT_ENHANCE_POOL_TIMEOUT_SECONDS,
            ),
            proxy=proxy_url,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {provider.api_key}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status_code != 200:
                    raw = await resp.aread()
                    logger.warning(
                        "enhance upstream error provider=%s attempt=%s status=%s: %s",
                        provider.name,
                        attempt.name,
                        resp.status_code,
                        raw[:500],
                    )
                    raise _EnhanceProviderError(
                        f"upstream http {resp.status_code}",
                        retryable=_is_retryable_upstream_error(resp.status_code, raw),
                    )

                buf = ""
                emitted = False
                async for chunk in resp.aiter_text():
                    buf += chunk
                    payloads, buf = _iter_sse_payloads_from_buffer(buf)
                    for payload in payloads:
                        if payload == "[DONE]":
                            return
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        _capture_enhance_usage(
                            capture,
                            evt,
                            provider=provider,
                            attempt=attempt,
                        )
                        evt_type = evt.get("type", "")
                        if evt_type == "response.output_text.delta":
                            delta = evt.get("delta", "")
                            if delta:
                                emitted = True
                                yield f"data: {json.dumps({'text': delta})}\n\n"
                        elif evt_type == "response.output_text.done":
                            text_done = evt.get("text")
                            if not emitted and isinstance(text_done, str) and text_done:
                                emitted = True
                                yield f"data: {json.dumps({'text': text_done})}\n\n"
                            elif not emitted:
                                raise _EnhanceProviderError(
                                    "empty_response",
                                    retryable=True,
                                )
                        elif evt_type == "response.completed":
                            if not emitted:
                                completed_text = _extract_response_text(
                                    evt.get("response") or evt
                                )
                                if completed_text:
                                    emitted = True
                                    yield f"data: {json.dumps({'text': completed_text})}\n\n"
                                else:
                                    raise _EnhanceProviderError(
                                        "empty_response",
                                        retryable=True,
                                    )
                            return
                        elif evt_type in {
                            "response.failed",
                            "response.incomplete",
                            "error",
                        }:
                            raise _EnhanceProviderError(
                                _extract_error_message(evt),
                                retryable=not emitted,
                            )

                if emitted:
                    return
                raise _EnhanceProviderError("empty_response", retryable=True)

    except _EnhanceProviderError:
        raise
    except httpx.TimeoutException:
        logger.warning(
            "enhance upstream timeout provider=%s attempt=%s read_timeout_s=%s",
            provider.name,
            attempt.name,
            _PROMPT_ENHANCE_READ_TIMEOUT_SECONDS,
        )
        raise _EnhanceProviderError("timeout", retryable=True) from None
    except httpx.HTTPError as exc:
        raise _EnhanceProviderError(type(exc).__name__, retryable=True) from exc


async def _stream_enhance(
    text: str,
    providers: list[ProviderDefinition],
    billing: _EnhanceBillingContext | None = None,
    *,
    system_prompt: str = ENHANCE_SYSTEM_PROMPT,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    last_error = "upstream_error"
    total_attempts = len(_ENHANCE_ATTEMPTS) * len(providers)
    seen_attempts = 0
    settled = False
    stream_kwargs: dict[str, Any] = {}
    if system_prompt != ENHANCE_SYSTEM_PROMPT:
        stream_kwargs["system_prompt"] = system_prompt
    if content is not None:
        stream_kwargs["content"] = content
    if metadata is not None:
        stream_kwargs["metadata"] = metadata
    try:
        for attempt in _ENHANCE_ATTEMPTS:
            for provider in providers:
                seen_attempts += 1
                emitted = False
                capture = _EnhanceUsageCapture()
                try:
                    async for chunk in _stream_enhance_one(
                        text,
                        provider,
                        attempt,
                        capture,
                        **stream_kwargs,
                    ):
                        emitted = True
                        yield chunk
                    if billing is not None:
                        try:
                            await _charge_prompt_enhance(billing, capture)
                            settled = True
                        except Exception:
                            logger.exception("prompt enhance billing charge failed")
                            await _release_prompt_enhance_hold(
                                billing,
                                reason="charge_failed",
                            )
                            settled = True
                            yield f"data: {json.dumps({'error': 'billing_failed'})}\n\n"
                            return
                    yield "data: [DONE]\n\n"
                    return
                except _EnhanceProviderError as exc:
                    last_error = (
                        "timeout" if str(exc) == "timeout" else "upstream_error"
                    )
                    logger.warning(
                        (
                            "enhance provider failed provider=%s attempt=%s "
                            "remaining=%d retryable=%s err=%s"
                        ),
                        provider.name,
                        attempt.name,
                        total_attempts - seen_attempts,
                        exc.retryable,
                        exc,
                    )
                    if emitted or not exc.retryable:
                        await _release_prompt_enhance_hold(
                            billing,
                            reason="provider_error_after_emit"
                            if emitted
                            else "provider_error",
                        )
                        settled = True
                        yield f"data: {json.dumps({'error': last_error})}\n\n"
                        return
                except GeneratorExit:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "enhance provider exception provider=%s attempt=%s",
                        provider.name,
                        attempt.name,
                    )
                    last_error = "internal"
                    if emitted:
                        await _release_prompt_enhance_hold(
                            billing,
                            reason="internal_error_after_emit",
                        )
                        settled = True
                        yield f"data: {json.dumps({'error': last_error})}\n\n"
                        return
        await _release_prompt_enhance_hold(billing, reason="no_success")
        settled = True
        yield f"data: {json.dumps({'error': last_error})}\n\n"
    except asyncio.CancelledError:
        if not settled:
            await _release_prompt_enhance_hold_after_cancel(
                billing,
                reason="stream_cancelled",
            )
        raise
    except GeneratorExit:
        if not settled:
            await _release_prompt_enhance_hold(billing, reason="stream_cancelled")
        raise


async def _stream_with_keepalive(
    source: AsyncIterator[str],
    *,
    interval_seconds: float = _PROMPT_ENHANCE_KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[tuple[str, str | BaseException | None]] = asyncio.Queue()

    async def _pump() -> None:
        try:
            async for chunk in source:
                await queue.put(("chunk", chunk))
            await queue.put(("done", None))
        except BaseException as exc:  # noqa: BLE001
            await queue.put(("error", exc))

    pump_task = asyncio.create_task(_pump())

    async def _cancel_pump() -> None:
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("prompt enhance keepalive pump failed during cancellation")

    try:
        yield _PROMPT_ENHANCE_KEEPALIVE_CHUNK
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(),
                    timeout=interval_seconds,
                )
            except asyncio.TimeoutError:
                yield _PROMPT_ENHANCE_KEEPALIVE_CHUNK
                continue

            if kind == "chunk":
                if not isinstance(payload, str):
                    raise RuntimeError("prompt enhance stream emitted non-text chunk")
                yield payload
                continue
            if kind == "done":
                return
            if isinstance(payload, BaseException):
                raise payload
            return
    except (asyncio.CancelledError, GeneratorExit):
        await _cancel_pump()
        raise
    finally:
        if not pump_task.done():
            await _cancel_pump()


@router.post("/enhance")
async def enhance_prompt(
    body: EnhanceIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    await PROMPTS_ENHANCE_LIMITER.check(get_redis(), f"rl:prompt_enhance:{user.id}")
    providers = [p for p in await _resolve_provider_order(db) if p.api_key.strip()]
    if not providers:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "not_configured",
                    "message": "upstream API key not set",
                },
            },
        )
    billing = await _prepare_prompt_enhance_billing(db, user)

    return StreamingResponse(
        _stream_with_keepalive(_stream_enhance(body.text, providers, billing)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/video/enhance")
async def enhance_video_prompt(
    body: VideoEnhanceIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    await PROMPTS_ENHANCE_LIMITER.check(get_redis(), f"rl:prompt_enhance:{user.id}")
    providers = [p for p in await _resolve_provider_order(db) if p.api_key.strip()]
    if not providers:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "not_configured",
                    "message": "upstream API key not set",
                },
            },
        )

    content, token_changed = await _build_video_enhance_content(
        body,
        request=request,
        db=db,
        user_id=user.id,
    )
    if token_changed:
        await db.commit()
    billing = await _prepare_prompt_enhance_billing(db, user)

    return StreamingResponse(
        _stream_with_keepalive(
            _stream_enhance(
                body.text,
                providers,
                billing,
                system_prompt=_video_enhance_system_prompt(body.variant_count),
                content=content,
            )
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
