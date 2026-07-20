"""Poster-style vision tagging services."""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    build_effective_provider_config,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
    weighted_priority_order,
)
from lumen_core.schemas import PosterStyleAutoTagOut

from .serialization import parse_tagging_text


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


async def _request_provider(
    provider: Any,
    *,
    request_body: dict[str, Any],
) -> tuple[str | None, str | None]:
    try:
        proxy_url = await resolve_provider_proxy_url(provider.proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=25.0,
                write=25.0,
                pool=10.0,
            ),
            proxy=proxy_url,
        ) as client:
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
) -> dict[str, Any]:
    image = (
        await db.execute(
            select(runtime.Image).where(
                runtime.Image.id == image_id,
                runtime.Image.user_id == user_id,
                runtime.Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if image is None:
        return {}
    storage_key = (image.storage_key or "").strip()
    if not storage_key:
        return {}
    try:
        raw = runtime._storage_path(storage_key).read_bytes()
    except Exception as exc:  # noqa: BLE001
        runtime.logger.info(
            "poster_style auto_tag api: read image failed key=%s err=%s",
            storage_key,
            exc,
        )
        return {}
    if not raw:
        return {}
    mime = (
        image.mime
        if isinstance(image.mime, str) and image.mime.startswith("image/")
        else "image/png"
    )
    image_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

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
    request_body = _tagging_request_body(
        image_id=image_id,
        image_url=image_url,
        instructions=instructions,
    )
    last_error: str | None = None
    for provider in ordered:
        text, error = await _request_provider(
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


async def auto_tag_item(
    runtime: Any,
    *,
    db: AsyncSession,
    user_id: str,
    item_id: str,
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

    async with runtime._poster_style_auto_tag_semaphore():
        raw_payload = await runtime._api_call_poster_style_tagging_upstream(
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
    user_id: str,
    item_id: str,
) -> None:
    try:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            await runtime._auto_tag_poster_style_item(
                db=session,
                user_id=user_id,
                item_id=item_id,
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
