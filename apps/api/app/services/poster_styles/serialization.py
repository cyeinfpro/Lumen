"""Poster-style value shaping and pure formatting helpers.

The route module keeps compatibility wrappers for these functions because
several callers historically imported them from ``app.routes.poster_styles``.
The service itself only talks to the supplied runtime object and never imports
the route module.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from lumen_core.constants import POSTER_STYLE_MAX_SAMPLES
from lumen_core.models import PosterStyleItem
from lumen_core.schemas import (
    PosterStyleItemOut,
    PosterStyleSampleOut,
)


def safe_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def dedupe_nonempty(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def clean_string_list(
    values: Iterable[Any],
    *,
    max_items: int,
    max_len: int,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        if not isinstance(raw, (str, int, float)):
            continue
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value[:max_len])
        if len(result) >= max_items:
            break
    return result


def item_out_from_row(runtime: Any, row: PosterStyleItem) -> PosterStyleItemOut:
    """Shape a user-owned row into the public library representation."""

    cover_id = row.cover_image_id
    sample_ids = dedupe_nonempty([cover_id or "", *(row.sample_image_ids or [])])[
        : int(getattr(runtime, "POSTER_STYLE_MAX_SAMPLES", POSTER_STYLE_MAX_SAMPLES))
    ]
    if cover_id:
        cover_url = f"/api/images/{cover_id}/binary"
        display_url = f"/api/images/{cover_id}/variants/display2048"
        thumb_url = display_url
    else:
        cover_url = runtime._library_item_url(row.id, "binary")
        display_url = cover_url
        thumb_url = runtime._library_item_url(row.id, "thumb")
    samples_out = [
        PosterStyleSampleOut(
            index=index,
            image_id=image_id,
            image_url=f"/api/images/{image_id}/binary",
            display_url=f"/api/images/{image_id}/variants/display2048",
            thumb_url=f"/api/images/{image_id}/variants/display2048",
        )
        for index, image_id in enumerate(sample_ids)
    ]
    return PosterStyleItemOut(
        id=row.id,
        source=row.source,  # type: ignore[arg-type]
        visibility_scope="user_private",
        title=str(row.title or "").strip()[:120] or "未命名风格",
        category=runtime._normalize_category(row.category),  # type: ignore[arg-type]
        mood=runtime._clean_optional_text(row.mood, max_len=120),
        prompt_template=runtime._clean_optional_text(
            row.prompt_template,
            max_len=2000,
        ),
        palette=list(row.palette or []),
        recommended_aspects=list(row.recommended_aspects or []),
        style_tags=list(row.style_tags or []),
        cover_image_url=cover_url,
        display_url=display_url,
        thumb_url=thumb_url,
        cover_image_id=cover_id,
        sample_image_ids=sample_ids,
        samples=samples_out,
        preset_id=None,
        version=None,
        library_folder=runtime._clean_optional_text(
            row.library_folder,
            max_len=64,
        ),
        download_filename=None,
        auto_tagged_at=row.auto_tagged_at,
        auto_tag_notes=runtime._clean_optional_text(
            row.auto_tag_notes,
            max_len=400,
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def item_out_from_preset(
    runtime: Any,
    raw: dict[str, Any],
) -> PosterStyleItemOut:
    """Shape one JSON-index preset into the public library representation."""

    item_id = str(raw.get("id") or "")
    samples = raw.get("samples")
    if not isinstance(samples, list):
        samples = []
    max_samples = int(
        getattr(runtime, "POSTER_STYLE_MAX_SAMPLES", POSTER_STYLE_MAX_SAMPLES)
    )
    samples = samples[:max_samples]
    samples_out: list[PosterStyleSampleOut] = []
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            continue
        sample_url = runtime._library_sample_url(item_id, index)
        samples_out.append(
            PosterStyleSampleOut(
                index=index,
                image_id=None,
                image_url=sample_url,
                display_url=sample_url,
                thumb_url=sample_url,
            )
        )
    has_samples = bool(samples_out)
    cover_url = runtime._library_item_url(item_id, "binary") if has_samples else ""
    thumb_url = runtime._library_item_url(item_id, "thumb") if has_samples else None
    created_at = safe_datetime(raw.get("created_at")) or runtime._now()
    updated_at = safe_datetime(raw.get("updated_at"))
    return PosterStyleItemOut(
        id=item_id,
        source="preset",
        visibility_scope="global_preset",
        title=str(raw.get("title") or "").strip()[:120] or "未命名风格",
        category=runtime._normalize_category(raw.get("category")),  # type: ignore[arg-type]
        mood=runtime._clean_optional_text(raw.get("mood"), max_len=120),
        prompt_template=runtime._clean_optional_text(
            raw.get("prompt_template"),
            max_len=2000,
        ),
        palette=runtime._normalize_palette(raw.get("palette") or []),
        recommended_aspects=runtime._normalize_recommended_aspects(
            raw.get("recommended_aspects") or []
        ),
        style_tags=runtime._normalize_style_tags(raw.get("style_tags") or []),
        cover_image_url=cover_url,
        display_url=cover_url or None,
        thumb_url=thumb_url,
        cover_image_id=None,
        sample_image_ids=[],
        samples=samples_out,
        preset_id=runtime._clean_optional_text(raw.get("preset_id"), max_len=120),
        version=int(raw.get("version") or 1),
        library_folder=runtime._clean_optional_text(
            raw.get("library_folder")
            or runtime._poster_style_folder_for_category(raw.get("category")),
            max_len=64,
        ),
        download_filename=None,
        auto_tagged_at=None,
        auto_tag_notes=None,
        created_at=created_at,
        updated_at=updated_at,
    )


def filter_preset_items(
    runtime: Any,
    items: Iterable[dict[str, Any]],
    *,
    category: str,
    q: str,
    tags: Iterable[str],
) -> list[dict[str, Any]]:
    query = q.strip().lower()
    tag_filter = {
        tag.strip().lower() for tag in tags if isinstance(tag, str) and tag.strip()
    }
    result: list[dict[str, Any]] = []
    for item in items:
        item_category = runtime._normalize_category(item.get("category"))
        if category != "all" and item_category != category:
            continue
        item_tags = {
            str(tag).strip().lower()
            for tag in (item.get("style_tags") or [])
            if isinstance(tag, (str, int, float))
        }
        if tag_filter and not tag_filter.intersection(item_tags):
            continue
        if query:
            haystack = " ".join(
                [
                    str(item.get("title") or ""),
                    str(item.get("mood") or ""),
                    str(item.get("prompt_template") or ""),
                    " ".join(item.get("style_tags") or []),
                ]
            ).lower()
            if query not in haystack:
                continue
        result.append(item)
    category_rank = {
        name: index
        for index, name in enumerate(
            [
                "illustration",
                "3d",
                "minimal",
                "retro",
                "traditional",
                "photo",
                "other",
                "user_favorites",
            ]
        )
    }
    return sorted(
        result,
        key=lambda item: (
            category_rank.get(runtime._normalize_category(item.get("category")), 9),
            str(item.get("preset_id") or ""),
            int(item.get("version") or 0),
        ),
    )


def generate_prompt(body: Any, *, candidate_index: int) -> str:
    """Build a cache-friendly generation prompt for one candidate."""

    extras: list[str] = []
    if body.prompt_template:
        extras.append(body.prompt_template.strip())
    palette_text = ", ".join(body.palette[:6]) if body.palette else ""
    mood_text = (body.mood or "").strip()
    tag_text = ", ".join(body.style_tags[:6]) if body.style_tags else ""
    return " ".join(
        part
        for part in [
            "Create one stylish poster sample illustrating a single visual style.",
            "The poster should be a self-contained composition representative of the style,",
            "no real product mockups required.",
            "Use plain, generic placeholder shapes or motifs to demonstrate the style,",
            "not specific brand names or logos.",
            f"Style direction: {extras[0]}" if extras else "",
            f"Palette: {palette_text}." if palette_text else "",
            f"Mood: {mood_text}." if mood_text else "",
            f"Style tags: {tag_text}." if tag_text else "",
            f"Variation index: {candidate_index}.",
            f"User intent: {body.prompt.strip()}",
        ]
        if part
    ).strip()


def parse_tagging_text(text: str) -> dict[str, Any]:
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        newline = cleaned.find("\n")
        if newline != -1:
            cleaned = cleaned[newline + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
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
    return payload if isinstance(payload, dict) else {}
