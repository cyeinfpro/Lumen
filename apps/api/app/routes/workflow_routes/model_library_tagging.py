"""Auto-tag persistence helpers for model-library workflow routes."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import ModelLibraryItem
from lumen_core.schemas import ApparelModelLibraryAutoTagOut


AGE_ALIASES_API: dict[str, str] = {
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


@dataclass(frozen=True)
class AutoTagHooks:
    ensure_legacy_user_library_migrated: Callable[..., Awaitable[bool]]
    api_call_tagging_upstream: Callable[..., Awaitable[dict[str, Any]]]
    http_error: Callable[..., Exception]
    clean_style_tags: Callable[..., list[str]]
    clean_optional_text: Callable[..., str | None]
    normalize_tagged_age: Callable[[Any], str | None]
    normalize_tagged_gender: Callable[[Any], str | None]
    normalize_age_segment: Callable[[Any], str]
    model_library_folder_for_age: Callable[..., str]
    now: Callable[[], Any]


def normalize_tagged_age(
    value: Any,
    *,
    age_segments: Collection[str],
) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace(" ", "_")
    if key in age_segments and key != "all":
        return key
    return AGE_ALIASES_API.get(key.replace("_", "")) or AGE_ALIASES_API.get(key)


def normalize_tagged_gender(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _GENDER_ALIASES.get(value.strip().lower())


async def auto_tag_library_item(
    *,
    db: AsyncSession,
    user_id: str,
    item_id: str,
    hooks: AutoTagHooks,
) -> ApparelModelLibraryAutoTagOut:
    """Tag one model-library row without overwriting user-authored metadata."""
    migrated_legacy = await hooks.ensure_legacy_user_library_migrated(db, user_id)
    row = (
        await db.execute(
            select(ModelLibraryItem).where(
                ModelLibraryItem.id == item_id,
                ModelLibraryItem.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise hooks.http_error("not_found", "model library item not found", 404)
    image_id = (row.image_id or "").strip()
    if not image_id:
        raise hooks.http_error(
            "invalid_item",
            "library item has no backing image",
            422,
        )
    raw_payload = await hooks.api_call_tagging_upstream(
        db,
        image_id=image_id,
        user_id=user_id,
    )
    raw_tags_value = (
        raw_payload.get("style_tags")
        or raw_payload.get("tags")
        or raw_payload.get("styleTags")
        or []
    )
    if isinstance(raw_tags_value, str):
        raw_tags_iterable: list[str] = [raw_tags_value]
    elif isinstance(raw_tags_value, list):
        raw_tags_iterable = [
            str(tag) for tag in raw_tags_value if isinstance(tag, (str, int, float))
        ]
    else:
        raw_tags_iterable = []
    style_tags = hooks.clean_style_tags(raw_tags_iterable)
    appearance_direction = hooks.clean_optional_text(
        raw_payload.get("appearance_direction")
        or raw_payload.get("appearanceDirection"),
        max_len=80,
    )
    age_segment = hooks.normalize_tagged_age(
        raw_payload.get("age_segment") or raw_payload.get("ageSegment")
    )
    gender = hooks.normalize_tagged_gender(raw_payload.get("gender"))
    notes = hooks.clean_optional_text(raw_payload.get("notes"), max_len=200)

    upstream_signal = bool(
        raw_payload
        and (style_tags or appearance_direction or age_segment or gender or notes)
    )
    if upstream_signal:
        if style_tags:
            row.style_tags = hooks.clean_style_tags(
                [*(row.style_tags or []), *style_tags]
            )
        if appearance_direction and not row.appearance_direction:
            row.appearance_direction = appearance_direction
        if (
            age_segment
            and hooks.normalize_age_segment(row.age_segment) == "user_favorites"
        ):
            row.age_segment = age_segment
            row.library_folder = hooks.model_library_folder_for_age(
                age_segment,
                row.gender,
            )
        if gender and not row.gender:
            row.gender = gender
            row.library_folder = hooks.model_library_folder_for_age(
                hooks.normalize_age_segment(row.age_segment),
                gender,
            )
        if notes:
            row.auto_tag_notes = notes
        row.auto_tagged_at = hooks.now()
        await db.commit()
        await db.refresh(row)
    elif migrated_legacy:
        await db.commit()
    return ApparelModelLibraryAutoTagOut(
        item_id=item_id,
        style_tags=style_tags,
        appearance_direction=appearance_direction,
        age_segment=age_segment,  # type: ignore[arg-type]
        gender=gender,
        notes=notes,
    )
