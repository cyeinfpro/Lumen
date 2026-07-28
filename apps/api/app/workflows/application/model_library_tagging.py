"""Model-library auto-tagging use case and normalization policy."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..domain.apparel_library import MODEL_LIBRARY_AGE_SEGMENTS, normalize_age_segment
from ..ports.model_library_tagging import (
    ModelLibraryTagUpdate,
    ModelLibraryTaggingPort,
)
from .model_library_jobs import clean_optional_text, clean_style_tags


AGE_ALIASES_API: Mapping[str, str] = MappingProxyType(
    {
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
)

_GENDER_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "female": "female",
        "woman": "female",
        "girl": "female",
        "f": "female",
        "male": "male",
        "man": "male",
        "boy": "male",
        "m": "male",
    }
)


class ModelLibraryTaggingError(RuntimeError):
    pass


class ModelLibraryTagItemNotFound(ModelLibraryTaggingError):
    pass


class ModelLibraryTagItemMissingImage(ModelLibraryTaggingError):
    pass


@dataclass(frozen=True, slots=True)
class ModelLibraryAutoTagResult:
    item_id: str
    style_tags: tuple[str, ...]
    appearance_direction: str | None
    age_segment: str | None
    gender: str | None
    notes: str | None


def normalize_tagged_age(
    value: Any,
    *,
    age_segments: Collection[str] = MODEL_LIBRARY_AGE_SEGMENTS,
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


def _raw_style_tags(payload: Mapping[str, object]) -> list[str]:
    raw = (
        payload.get("style_tags")
        or payload.get("tags")
        or payload.get("styleTags")
        or []
    )
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(tag) for tag in raw if isinstance(tag, (str, int, float))]
    return []


async def auto_tag_model_library_item(
    *,
    user_id: str,
    item_id: str,
    age_segments: Collection[str],
    port: ModelLibraryTaggingPort,
) -> ModelLibraryAutoTagResult:
    migrated_legacy = await port.ensure_legacy_migrated(user_id=user_id)
    item = await port.load_item(user_id=user_id, item_id=item_id)
    if item is None:
        raise ModelLibraryTagItemNotFound(item_id)
    image_id = item.image_id.strip()
    if not image_id:
        raise ModelLibraryTagItemMissingImage(item_id)

    payload = await port.fetch_tags(user_id=user_id, image_id=image_id)
    style_tags = clean_style_tags(_raw_style_tags(payload))
    appearance_direction = clean_optional_text(
        payload.get("appearance_direction") or payload.get("appearanceDirection"),
        max_len=80,
    )
    age_segment = normalize_tagged_age(
        payload.get("age_segment") or payload.get("ageSegment"),
        age_segments=age_segments,
    )
    gender = normalize_tagged_gender(payload.get("gender"))
    notes = clean_optional_text(payload.get("notes"), max_len=200)

    upstream_signal = bool(
        payload
        and (style_tags or appearance_direction or age_segment or gender or notes)
    )
    if upstream_signal:
        merged_tags = (
            tuple(clean_style_tags([*item.style_tags, *style_tags]))
            if style_tags
            else None
        )
        update = ModelLibraryTagUpdate(
            style_tags=merged_tags,
            appearance_direction=(
                appearance_direction
                if appearance_direction and not item.appearance_direction
                else None
            ),
            age_segment=(
                age_segment
                if age_segment
                and normalize_age_segment(item.age_segment) == "user_favorites"
                else None
            ),
            gender=gender if gender and not item.gender else None,
            notes=notes,
        )
        await port.save_update(
            user_id=user_id,
            item_id=item_id,
            update=update,
        )
    elif migrated_legacy:
        await port.commit_migration()

    return ModelLibraryAutoTagResult(
        item_id=item_id,
        style_tags=tuple(style_tags),
        appearance_direction=appearance_direction,
        age_segment=age_segment,
        gender=gender,
        notes=notes,
    )
