"""Prompt enhance 的媒体内容构建助手(图片/视频引用 → 输入内容)。

从 routes/prompts.py 拆出,保持路由文件在 route/controller 行数上限内。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import secrets
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Image, Video
from lumen_core.vision_tagging import image_record_to_data_url

from ...config import settings
from ...public_urls import resolve_public_base_url
from . import content as _prompt_content

logger = logging.getLogger(__name__)

_PROMPT_ENHANCE_MEDIA_MAX_BYTES = 18 * 1024 * 1024
PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES = 24 * 1024 * 1024
_VIDEO_REFERENCE_ACCESS_TOKEN_TTL = timedelta(hours=24)


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


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


def append_input_image_with_budget(
    content: list[dict[str, Any]],
    image_url: str,
    *,
    media_payload_bytes: int,
) -> tuple[bool, int]:
    return _prompt_content.append_input_image_with_budget(
        content,
        image_url,
        media_payload_bytes=media_payload_bytes,
        media_total_max_bytes=PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES,
    )


def _external_image_url_for_input(url: str | None) -> str | None:
    return _prompt_content.external_image_url_for_input(url)


def _append_video_context_line(lines: list[str], key: str, value: Any) -> None:
    _prompt_content.append_video_context_line(lines, key, value)


def _reference_anchor(ref_id: str | None, kind: str, index: int) -> str:
    return _prompt_content.reference_anchor(ref_id, kind, index)


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


async def build_video_enhance_content(
    body: _prompt_content.VideoEnhanceIn,
    *,
    request: Request,
    db: AsyncSession,
    user_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    runtime = _prompt_content.ContentRuntime(
        owned_image=_owned_image,
        owned_video=_owned_video,
        image_data_url=_image_data_url,
        video_poster_data_url=_video_poster_data_url,
        resolve_public_base_url=_resolve_optional_public_base_url,
        video_reference_public_url=_video_reference_public_url,
    )
    return await _prompt_content.build_video_enhance_content(
        body,
        request=request,
        db=db,
        user_id=user_id,
        runtime=runtime,
        media_total_max_bytes=PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES,
    )
