"""Portable model-image metadata helpers.

The DB remains the source of truth inside Lumen, but generated model images
also carry a small JSON payload in the file itself so downloaded images can be
re-uploaded and recognized without vision tagging.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from PIL import Image as PILImage
from PIL.PngImagePlugin import PngInfo

LUMEN_MODEL_METADATA_KEY = "lumen.model_library"
LUMEN_MODEL_METADATA_SCHEMA = "model_library.v1"
MODEL_FILENAME_PREFIX = "lumen-model"
MODEL_FILENAME_MAX_CHARS = 96

AGE_SLUG_BY_SEGMENT: dict[str, str] = {
    "toddler": "toddler",
    "child": "child",
    "teen": "teen",
    "young_adult": "young-adult",
    "adult": "mature",
    "middle_aged": "middle-aged",
    "senior": "senior",
}
AGE_SEGMENT_BY_SLUG = {value: key for key, value in AGE_SLUG_BY_SEGMENT.items()}

APPEARANCE_SLUG_BY_VALUE: dict[str, str] = {
    "asian": "asian",
    "east_asian": "east-asian",
    "southeast_asian": "southeast-asian",
    "south_asian": "south-asian",
    "european": "european",
    "latin": "latin",
    "middle_eastern": "middle-eastern",
    "african": "african",
    "mixed": "mixed",
    "other": "other",
}
APPEARANCE_BY_SLUG = {
    value: key for key, value in APPEARANCE_SLUG_BY_VALUE.items()
}

TAG_SLUG_BY_LABEL: dict[str, str] = {
    "温柔亲和": "gentle-friendly",
    "清冷高级": "cool-editorial",
    "甜美活力": "sweet-bright",
    "酷感街头": "cool-street",
    "知性通勤": "smart-commute",
    "极简中性": "minimal-neutral",
    "运动阳光": "sporty-sunny",
    "复古文艺": "retro-artistic",
    "成熟稳重": "mature-composed",
}
TAG_LABEL_BY_SLUG = {value: key for key, value in TAG_SLUG_BY_LABEL.items()}

_SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ModelImageMetadata:
    age_segment: str | None = None
    gender: str | None = None
    appearance_direction: str | None = None
    style_tags: list[str] = field(default_factory=list)
    source: str | None = None
    prompt_hint: str | None = None


def _clean_text(value: Any, *, max_len: int = 120) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def clean_style_tags(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = _clean_text(value, max_len=24)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= 12:
            break
    return out


def _slugify(value: str) -> str:
    slug = _SAFE_SLUG_RE.sub("-", value.lower()).strip("-")
    return slug or "tag"


def tag_slug(tag: str) -> str:
    return TAG_SLUG_BY_LABEL.get(tag, _slugify(tag))[:32].strip("-") or "tag"


def tag_from_slug(slug: str) -> str:
    return TAG_LABEL_BY_SLUG.get(slug, slug)


def build_model_image_metadata(
    *,
    age_segment: str | None,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
    source: str | None = None,
    prompt_hint: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "lumen_schema": LUMEN_MODEL_METADATA_SCHEMA,
        "age_segment": _clean_text(age_segment, max_len=32),
        "gender": _clean_text(gender, max_len=16),
        "appearance_direction": _clean_text(appearance_direction, max_len=80),
        "style_tags": clean_style_tags(style_tags),
        "source": _clean_text(source, max_len=80),
        "prompt_hint": _clean_text(prompt_hint, max_len=300),
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def parse_model_image_metadata(value: Any) -> ModelImageMetadata | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    if value.get("lumen_schema") != LUMEN_MODEL_METADATA_SCHEMA:
        return None
    return ModelImageMetadata(
        age_segment=_clean_text(value.get("age_segment"), max_len=32),
        gender=_clean_text(value.get("gender"), max_len=16),
        appearance_direction=_clean_text(value.get("appearance_direction"), max_len=80),
        style_tags=clean_style_tags(value.get("style_tags") or []),
        source=_clean_text(value.get("source"), max_len=80),
        prompt_hint=_clean_text(value.get("prompt_hint"), max_len=300),
    )


def read_model_image_metadata(image: PILImage.Image) -> ModelImageMetadata | None:
    value = image.info.get(LUMEN_MODEL_METADATA_KEY)
    return parse_model_image_metadata(value)


def pnginfo_for_model_metadata(payload: dict[str, Any]) -> PngInfo:
    info = PngInfo()
    info.add_itxt(
        LUMEN_MODEL_METADATA_KEY,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    return info


def save_image_with_model_metadata(
    image: PILImage.Image,
    fp: Any,
    *,
    fmt: str,
    metadata: dict[str, Any],
    **save_kwargs: Any,
) -> None:
    if fmt.upper() == "PNG" and metadata:
        image.save(fp, format=fmt, pnginfo=pnginfo_for_model_metadata(metadata), **save_kwargs)
        return
    image.save(fp, format=fmt, **save_kwargs)


def model_image_filename(
    *,
    image_id: str,
    ext: str,
    age_segment: str | None,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
) -> str:
    short_id = _slugify(image_id.replace("user:", ""))[:6] or "image"
    parts = [MODEL_FILENAME_PREFIX]
    if age_segment:
        parts.append(AGE_SLUG_BY_SEGMENT.get(age_segment, _slugify(age_segment)))
    if gender:
        parts.append(_slugify(gender))
    if appearance_direction:
        parts.append(
            APPEARANCE_SLUG_BY_VALUE.get(
                appearance_direction, _slugify(appearance_direction)
            )
        )
    parts.extend(tag_slug(tag) for tag in clean_style_tags(style_tags)[:2])
    parts.append(short_id)
    suffix = f".{ext.lstrip('.').lower() or 'png'}"
    while len("-".join(parts)) + len(suffix) > MODEL_FILENAME_MAX_CHARS and len(parts) > 5:
        parts.pop(-2)
    return f"{'-'.join(parts)}{suffix}"


def parse_model_image_filename(filename: str) -> ModelImageMetadata | None:
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    if not stem.startswith(f"{MODEL_FILENAME_PREFIX}-"):
        return None
    tail = stem[len(MODEL_FILENAME_PREFIX) + 1 :]
    parts = tail.split("-")
    joined = "-".join(parts)
    age_segment = next(
        (segment for slug, segment in AGE_SEGMENT_BY_SLUG.items() if joined.startswith(slug)),
        None,
    )
    tokens = parts
    if age_segment:
        age_slug = AGE_SLUG_BY_SEGMENT[age_segment].split("-")
        tokens = tokens[len(age_slug) :]
    gender = tokens[0] if tokens and tokens[0] in {"female", "male"} else None
    if gender:
        tokens = tokens[1:]
    appearance_direction = None
    for slug, value in sorted(APPEARANCE_BY_SLUG.items(), key=lambda item: -len(item[0])):
        slug_parts = slug.split("-")
        if tokens[: len(slug_parts)] == slug_parts:
            appearance_direction = value
            tokens = tokens[len(slug_parts) :]
            break
    tag_tokens = tokens[:-1] if len(tokens) > 1 else []
    tags: list[str] = []
    if tag_tokens:
        raw = "-".join(tag_tokens)
        known_tags = sorted(TAG_LABEL_BY_SLUG.items(), key=lambda item: -len(item[0]))
        while raw and len(tags) < 2:
            matched = False
            for slug, label in known_tags:
                if raw == slug or raw.startswith(f"{slug}-"):
                    tags.append(label)
                    raw = raw[len(slug) :].strip("-")
                    matched = True
                    break
            if not matched:
                break
    if not any([age_segment, gender, appearance_direction, tags]):
        return None
    return ModelImageMetadata(
        age_segment=age_segment,
        gender=gender,
        appearance_direction=appearance_direction,
        style_tags=tags,
        source="filename",
    )
