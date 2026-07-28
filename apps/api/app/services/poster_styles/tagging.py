"""Poster-style vision tagging services."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncContextManager, AsyncIterator, Awaitable, Callable

import httpx
from fastapi import HTTPException
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, ImageVariant
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    build_effective_provider_config,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
    weighted_priority_order,
)
from lumen_core.schemas import PosterStyleAutoTagOut

from .serialization import parse_tagging_text
from .tagging_runtime import PosterTaggingRuntime


POSTER_TAGGING_PREVIEW_MAX_SIDE = 1536
POSTER_TAGGING_PREVIEW_MAX_BYTES = 2 * 1024 * 1024
POSTER_TAGGING_SOURCE_MAX_BYTES = 64 * 1024 * 1024
POSTER_TAGGING_MAX_PIXELS = 64_000_000
POSTER_TAGGING_REQUEST_MAX_BYTES = 3 * 1024 * 1024


class PosterTaggingPreviewError(ValueError):
    pass


class PosterTaggingRequestTooLarge(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PosterTaggingPreview:
    content: bytes
    mime: str
    width: int
    height: int
    source_kind: str

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.content).decode('ascii')}"


def auto_tag_concurrency() -> int:
    try:
        return max(
            1,
            min(
                4,
                int(os.environ.get("POSTER_STYLE_AUTO_TAG_CONCURRENCY", "2") or "2"),
            ),
        )
    except (TypeError, ValueError):
        return 2


def _tagging_instructions() -> str:
    return (
        "你是海报风格库自动打标签助手。仔细分析这张海报样图的视觉风格，输出严格 JSON。\n\n"
        "字段（全部必填，无法判断填空串/空数组）：\n"
        "- category：英文小写之一：illustration / 3d / minimal / retro / traditional / photo / other。\n"
        "- style_tags：3-6 个中文短词，每个 ≤ 8 字，聚焦视觉风格特征。\n"
        "    禁止描述具体商品 / 模特 / 文字内容；禁止英文。\n"
        "- mood：≤ 20 字中文，整体情绪关键词。\n"
        "- palette：3-6 个 #RRGGBB 十六进制色彩值。\n"
        "- notes：≤ 60 字中文一句话点评。\n\n"
        "只输出 JSON 对象，不要 Markdown / 代码块 / 解释。字段必须用上述英文 key。"
    )


def _tagging_request_body(
    *,
    image_id: str,
    image_url: str,
    instructions: str,
) -> dict[str, Any]:
    return {
        "model": "gpt-5.4-mini",
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
        "metadata": {"image_id": image_id, "purpose": "poster_style_tagging"},
        "stream": False,
        "store": False,
        "max_output_tokens": 600,
    }


def checked_tagging_request_body(
    *,
    image_id: str,
    image_url: str,
    instructions: str,
    max_bytes: int = POSTER_TAGGING_REQUEST_MAX_BYTES,
) -> dict[str, Any]:
    body = _tagging_request_body(
        image_id=image_id,
        image_url=image_url,
        instructions=instructions,
    )
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise PosterTaggingRequestTooLarge(
            f"poster tagging request exceeds {max_bytes} bytes"
        )
    return body


def _response_text(payload: Any) -> str:
    chunks: list[str] = []
    output = payload.get("output") if isinstance(payload, dict) else None
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
                text = part.get("text") or part.get("output_text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    output_text = payload.get("output_text") if isinstance(payload, dict) else None
    if isinstance(output_text, str) and output_text:
        chunks.append(output_text)
    return "".join(chunks).strip()


def _flatten_for_webp(image: PILImage.Image) -> PILImage.Image:
    if "A" not in image.getbands() and "transparency" not in image.info:
        return image.convert("RGB")
    with image.convert("RGBA") as rgba:
        background = PILImage.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")


def _validate_source_dimensions(image: PILImage.Image) -> None:
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > POSTER_TAGGING_MAX_PIXELS:
        raise PosterTaggingPreviewError("poster tagging source exceeds pixel limit")


def _encode_bounded_preview(image: PILImage.Image) -> tuple[bytes, tuple[int, int]]:
    oriented = ImageOps.exif_transpose(image)
    try:
        _validate_source_dimensions(oriented)
        oriented.thumbnail(
            (POSTER_TAGGING_PREVIEW_MAX_SIDE, POSTER_TAGGING_PREVIEW_MAX_SIDE),
            PILImage.Resampling.LANCZOS,
        )
        with _flatten_for_webp(oriented) as rgb:
            for max_side in (1536, 1280, 1024, 768):
                candidate = rgb.copy()
                try:
                    candidate.thumbnail(
                        (max_side, max_side),
                        PILImage.Resampling.LANCZOS,
                    )
                    for quality in (86, 76, 64, 52):
                        output = io.BytesIO()
                        candidate.save(
                            output,
                            format="WEBP",
                            quality=quality,
                            method=4,
                        )
                        content = output.getvalue()
                        if len(content) <= POSTER_TAGGING_PREVIEW_MAX_BYTES:
                            return content, candidate.size
                finally:
                    candidate.close()
    finally:
        if oriented is not image:
            oriented.close()
    raise PosterTaggingPreviewError("poster tagging preview exceeds byte limit")


def _load_preview_file(
    path: Path,
    *,
    source_kind: str,
) -> PosterTaggingPreview:
    size = path.stat().st_size
    limit = (
        POSTER_TAGGING_PREVIEW_MAX_BYTES
        if source_kind == "preview1024"
        else POSTER_TAGGING_SOURCE_MAX_BYTES
    )
    if size <= 0 or size > limit:
        raise PosterTaggingPreviewError(
            f"poster tagging {source_kind} exceeds source byte limit"
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(path) as image:
                _validate_source_dimensions(image)
                image.draft(
                    "RGB",
                    (POSTER_TAGGING_PREVIEW_MAX_SIDE, POSTER_TAGGING_PREVIEW_MAX_SIDE),
                )
                image.load()
                content, dimensions = _encode_bounded_preview(image)
    except PosterTaggingPreviewError:
        raise
    except (
        PILImage.DecompressionBombError,
        PILImage.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise PosterTaggingPreviewError("poster tagging preview is unreadable") from exc
    return PosterTaggingPreview(
        content=content,
        mime="image/webp",
        width=dimensions[0],
        height=dimensions[1],
        source_kind=source_kind,
    )


async def load_tagging_preview(
    runtime: Any,
    db: AsyncSession,
    *,
    image_id: str,
    user_id: str,
) -> PosterTaggingPreview | None:
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
        return None
    variant = (
        await db.execute(
            select(ImageVariant).where(
                ImageVariant.image_id == image_id,
                ImageVariant.kind == "preview1024",
            )
        )
    ).scalar_one_or_none()
    source_kind = "preview1024" if variant is not None else "bounded_preview"
    storage_key = (
        str(getattr(variant, "storage_key", "") or "")
        if variant is not None
        else str(getattr(image, "storage_key", "") or "")
    )
    if not storage_key:
        return None
    try:
        return await asyncio.to_thread(
            _load_preview_file,
            runtime._storage_path(storage_key),
            source_kind=source_kind,
        )
    except (OSError, PosterTaggingPreviewError) as exc:
        runtime.logger.info(
            "poster_tagging_failure reason=preview_unavailable image_id=%s "
            "source_kind=%s err=%s",
            image_id,
            source_kind,
            exc,
        )
        return None


async def _request_provider(
    tagging_runtime: PosterTaggingRuntime,
    provider: Any,
    *,
    request_body: dict[str, Any],
) -> tuple[str | None, str | None]:
    try:
        proxy_url = await resolve_provider_proxy_url(provider.proxy)
        client = await tagging_runtime.http_clients.client_for(proxy_url)
        base = provider.base_url.rstrip("/")
        url = (
            f"{base}/v1/responses"
            if not base.endswith("/v1")
            else f"{base}/responses"
        )
        response = await client.post(
            url,
            json=request_body,
            headers={
                "authorization": f"Bearer {provider.api_key}",
                "content-type": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        return None, f"network: {exc}"
    if response.status_code >= 400:
        return None, f"http {response.status_code}"
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return None, "bad_json"
    return _response_text(payload), None


async def call_tagging_upstream(
    runtime: Any,
    db: AsyncSession,
    *,
    image_id: str,
    user_id: str,
    tagging_runtime: PosterTaggingRuntime,
) -> dict[str, Any]:
    preview = await load_tagging_preview(
        runtime,
        db,
        image_id=image_id,
        user_id=user_id,
    )
    if preview is None:
        return {}
    image_url = preview.data_url()

    providers_spec = runtime.get_spec("providers")
    raw_providers = (
        await runtime.get_setting(db, providers_spec)
        if providers_spec is not None
        else None
    )
    providers, _proxies, _errors = build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    ordered = weighted_priority_order(
        [
            provider
            for provider in providers
            if endpoint_kind_allowed(provider, "responses")
        ],
        {},
    )
    if not ordered:
        return {}

    instructions = _tagging_instructions()
    try:
        request_body = checked_tagging_request_body(
            image_id=image_id,
            image_url=image_url,
            instructions=instructions,
        )
    except PosterTaggingRequestTooLarge as exc:
        runtime.logger.info(
            "poster_tagging_failure reason=request_too_large image_id=%s "
            "preview_bytes=%s err=%s",
            image_id,
            len(preview.content),
            exc,
        )
        return {}
    runtime.logger.info(
        "poster_tagging_request image_id=%s source=%s preview_bytes=%s "
        "request_bytes=%s",
        image_id,
        preview.source_kind,
        len(preview.content),
        len(
            json.dumps(
                request_body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
    )
    last_error: str | None = None
    for provider in ordered:
        text, error = await _request_provider(
            tagging_runtime,
            provider,
            request_body=request_body,
        )
        if error is not None:
            last_error = error
            continue
        return parse_tagging_text(text or "")
    if last_error is not None:
        runtime.logger.info(
            "poster_style auto_tag api: all providers failed err=%s",
            last_error,
        )
    return {}


@asynccontextmanager
async def _unlimited_capacity() -> AsyncIterator[None]:
    yield


async def auto_tag_item(
    runtime: Any,
    *,
    db: AsyncSession,
    user_id: str,
    item_id: str,
    capacity: AsyncContextManager[None] | None = None,
    upstream: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> PosterStyleAutoTagOut:
    row = await runtime._find_user_item(db, user_id=user_id, item_id=item_id)
    if row is None:
        raise runtime._http("not_found", "poster style item not found", 404)
    cover_id = (row.cover_image_id or "").strip()
    if not cover_id:
        raise runtime._http(
            "invalid_item",
            "poster style item has no cover image",
            422,
        )

    upstream_call = upstream or runtime._api_call_poster_style_tagging_upstream
    async with capacity or _unlimited_capacity():
        raw_payload = await upstream_call(
            db,
            image_id=cover_id,
            user_id=user_id,
        )

    raw_tags = (
        raw_payload.get("style_tags")
        or raw_payload.get("tags")
        or raw_payload.get("styleTags")
        or []
    )
    if isinstance(raw_tags, str):
        tag_values = [raw_tags]
    elif isinstance(raw_tags, list):
        tag_values = [
            str(value) for value in raw_tags if isinstance(value, (str, int, float))
        ]
    else:
        tag_values = []
    style_tags = runtime._normalize_style_tags(tag_values)
    category_raw = raw_payload.get("category")
    category = (
        runtime._normalize_category(category_raw)
        if isinstance(category_raw, str)
        else "user_favorites"
    )
    mood = runtime._clean_optional_text(raw_payload.get("mood"), max_len=120)
    palette = runtime._normalize_palette(raw_payload.get("palette") or [])
    notes = runtime._clean_optional_text(raw_payload.get("notes"), max_len=400)
    upstream_signal = bool(
        raw_payload
        and (
            style_tags
            or mood
            or palette
            or notes
            or (category and category != "user_favorites")
        )
    )
    if upstream_signal:
        if style_tags:
            row.style_tags = runtime._normalize_style_tags(
                [*(row.style_tags or []), *style_tags]
            )
        if mood and not row.mood:
            row.mood = mood
        if palette and not (row.palette or []):
            row.palette = palette
        if (
            category
            and category != "user_favorites"
            and runtime._normalize_category(row.category) == "user_favorites"
        ):
            row.category = category
            row.library_folder = runtime._poster_style_folder_for_category(category)
        if notes:
            row.auto_tag_notes = notes
        row.auto_tagged_at = runtime._now()
        await db.commit()
        await db.refresh(row)
    return PosterStyleAutoTagOut(
        item_id=item_id,
        style_tags=style_tags,
        category=category if category != "user_favorites" else None,  # type: ignore[arg-type]
        mood=mood,
        palette=palette,
        notes=notes,
    )


async def run_auto_tag_in_background(
    runtime: Any,
    tagging_runtime: PosterTaggingRuntime,
    user_id: str,
    item_id: str,
) -> None:
    try:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            async def upstream(
                db: AsyncSession,
                *,
                image_id: str,
                user_id: str,
            ) -> dict[str, Any]:
                return await call_tagging_upstream(
                    runtime,
                    db,
                    image_id=image_id,
                    user_id=user_id,
                    tagging_runtime=tagging_runtime,
                )

            await auto_tag_item(
                runtime,
                db=session,
                user_id=user_id,
                item_id=item_id,
                capacity=tagging_runtime.capacity.hold(),
                upstream=upstream,
            )
    except HTTPException as exc:
        runtime.logger.info(
            "poster_style auto_tag background skipped user=%s item=%s status=%s",
            user_id,
            item_id,
            exc.status_code,
        )
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning(
            "poster_style auto_tag background failed user=%s item=%s err=%s",
            user_id,
            item_id,
            exc,
        )


__all__ = [
    "POSTER_TAGGING_PREVIEW_MAX_BYTES",
    "POSTER_TAGGING_PREVIEW_MAX_SIDE",
    "POSTER_TAGGING_REQUEST_MAX_BYTES",
    "PosterTaggingPreview",
    "PosterTaggingPreviewError",
    "PosterTaggingRequestTooLarge",
    "auto_tag_concurrency",
    "auto_tag_item",
    "call_tagging_upstream",
    "checked_tagging_request_body",
    "load_tagging_preview",
]
