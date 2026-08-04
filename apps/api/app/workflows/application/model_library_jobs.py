"""Pure model-library job and reference-image policies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..domain.apparel_library import (
    normalize_age_segment,
    normalize_model_gender,
)
from ..domain.library_contracts import model_library_download_filename


@dataclass(frozen=True, slots=True)
class ModelLibraryRunInputs:
    mode: str
    reference_image_id: str | None
    extracted_profile: Mapping[str, object] | None
    age_segment: str
    gender: str
    genders: tuple[str, ...]
    appearance_direction: str | None
    extra_requirements: str | None
    style_tags: tuple[str, ...]
    auto_tag: bool
    count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "reference_image_id": self.reference_image_id,
            "extracted_profile": (
                dict(self.extracted_profile) if self.extracted_profile else None
            ),
            "age_segment": self.age_segment,
            "gender": self.gender,
            "genders": list(self.genders),
            "appearance_direction": self.appearance_direction,
            "extra_requirements": self.extra_requirements,
            "style_tags": list(self.style_tags),
            "auto_tag": self.auto_tag,
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class ReferenceProfileValues:
    age_segment: str | None
    gender: str | None
    appearance_direction: str | None
    style_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedReferenceOverrides:
    age_segment: str
    gender: str
    genders: tuple[str, ...]
    appearance_direction: str | None
    style_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedModelLibraryJobItem:
    image_id: str
    image_url: str
    display_url: str | None
    thumb_url: str | None
    saved_item_id: str | None
    style_tags: tuple[str, ...]
    appearance_direction: str | None
    gender: str | None
    download_filename: str
    is_dual_race_bonus: bool
    billing_free: bool
    billing_label: str | None
    billing_exempt_reason: str | None


@dataclass(frozen=True, slots=True)
class ModelLibraryJobItemValues:
    image_id: str
    image_url: str
    display_url: str | None
    thumb_url: str | None
    mime: str | None
    saved_item_id: str | None
    age_segment: str | None
    gender: str | None
    style_tags: Iterable[object]
    appearance_direction: str | None
    image_meta: Mapping[str, object]
    image_is_dual_race_bonus: bool = False
    image_billing_free: bool = False
    image_billing_label: str | None = None
    image_billing_exempt_reason: str | None = None


def clean_optional_text(value: object, *, max_len: int = 120) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned[:max_len] if cleaned else None


def clean_style_tags(values: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag[:32])
        if len(out) >= 12:
            break
    return out


def dedupe_nonempty(values: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def model_library_generate_genders(
    raw_genders: Iterable[object],
    fallback_gender: object,
) -> list[str]:
    genders = [
        gender
        for gender in dedupe_nonempty(raw_genders)
        if gender in {"female", "male"}
    ]
    if not genders and isinstance(fallback_gender, str):
        fallback = fallback_gender.strip()
        if fallback in {"female", "male"}:
            genders = [fallback]
    return genders or ["female"]


def model_library_explicit_genders(
    raw_genders: Iterable[object],
    fallback_gender: object,
) -> list[str]:
    genders = [
        gender
        for gender in dedupe_nonempty(raw_genders)
        if gender in {"female", "male"}
    ]
    if not genders and isinstance(fallback_gender, str):
        fallback = fallback_gender.strip()
        if fallback in {"female", "male"}:
            genders = [fallback]
    return genders


def normalize_model_library_run_inputs(
    raw: Mapping[str, object],
    *,
    task_count: int,
) -> ModelLibraryRunInputs:
    genders = tuple(
        gender
        for gender in dedupe_nonempty(raw.get("genders") or [])
        if gender in {"female", "male"}
    )
    gender = (
        "/".join(genders)
        if len(genders) > 1
        else normalize_model_gender(genders[0] if genders else raw.get("gender"))
    )
    raw_count = raw.get("count")
    count = int(raw_count) if isinstance(raw_count, (int, str)) and raw_count else 0
    return ModelLibraryRunInputs(
        mode=str(raw.get("mode") or "text"),
        reference_image_id=clean_optional_text(
            raw.get("reference_image_id"),
            max_len=64,
        ),
        extracted_profile=(
            raw.get("extracted_profile")
            if isinstance(raw.get("extracted_profile"), Mapping)
            else None
        ),
        age_segment=normalize_age_segment(raw.get("age_segment")),
        gender=gender,
        genders=genders,
        appearance_direction=clean_optional_text(
            raw.get("appearance_direction"),
            max_len=80,
        ),
        extra_requirements=clean_optional_text(
            raw.get("extra_requirements"),
            max_len=400,
        ),
        style_tags=tuple(clean_style_tags(raw.get("style_tags") or [])),
        auto_tag=bool(raw.get("auto_tag", True)),
        count=count or task_count,
    )


def extract_bonus_image_ids(
    output_json: Mapping[str, object],
    image_ids: Iterable[str],
) -> list[str]:
    raw = output_json.get("dual_race_bonus_image_ids") or []
    if not isinstance(raw, list):
        return []
    seen = set(image_ids)
    return [
        image_id
        for image_id in raw
        if isinstance(image_id, str) and image_id not in seen
    ]


def reference_profile_has_required_fields(
    *,
    age_segment: str | None,
    explicit_genders: Sequence[str],
    profile: ReferenceProfileValues | None,
) -> bool:
    resolved_age = age_segment or (profile.age_segment if profile else None)
    resolved_genders = list(explicit_genders)
    if not resolved_genders and profile and profile.gender:
        resolved_genders = [profile.gender]
    return bool(resolved_age and resolved_genders)


def merge_reference_overrides(
    *,
    age_segment: str | None,
    explicit_genders: Sequence[str],
    appearance_direction: str | None,
    style_tags: Iterable[object],
    profile: ReferenceProfileValues | None,
) -> ResolvedReferenceOverrides:
    merged_tags = clean_style_tags(
        [
            *style_tags,
            *(profile.style_tags if profile else ()),
        ]
    )
    genders = list(explicit_genders)
    if not genders and profile and profile.gender in {"female", "male"}:
        genders = [profile.gender]
    if not genders:
        genders = ["female"]
    return ResolvedReferenceOverrides(
        age_segment=(
            age_segment or (profile.age_segment if profile else None) or "young_adult"
        ),
        gender=genders[0],
        genders=tuple(genders),
        appearance_direction=(
            appearance_direction or (profile.appearance_direction if profile else None)
        ),
        style_tags=tuple(merged_tags),
    )


def resolve_model_library_job_item(
    values: ModelLibraryJobItemValues,
) -> ResolvedModelLibraryJobItem:
    resolved_tags = clean_style_tags(
        [*(values.image_meta.get("style_tags") or []), *values.style_tags]
    )
    resolved_age = normalize_age_segment(
        values.image_meta.get("age_segment") or values.age_segment
    )
    if resolved_age == "user_favorites" and values.age_segment:
        resolved_age = normalize_age_segment(values.age_segment)
    resolved_gender = clean_optional_text(
        values.image_meta.get("gender") or values.gender,
        max_len=40,
    )
    resolved_appearance = clean_optional_text(
        values.image_meta.get("appearance_direction")
        or values.appearance_direction,
        max_len=80,
    )
    filename = clean_optional_text(
        values.image_meta.get("download_filename"),
        max_len=160,
    ) or model_library_download_filename(
        image_id=values.image_id,
        mime=values.mime
        or clean_optional_text(values.image_meta.get("mime"), max_len=80),
        age_segment=resolved_age,
        gender=resolved_gender,
        appearance_direction=resolved_appearance,
        style_tags=resolved_tags,
    )
    is_dual_race_bonus = bool(
        values.image_meta.get("is_dual_race_bonus")
        or values.image_is_dual_race_bonus
    )
    billing_label = clean_optional_text(
        values.image_meta.get("billing_label") or values.image_billing_label,
        max_len=32,
    )
    if "billing_free" in values.image_meta:
        billing_free = values.image_meta.get("billing_free") is True
    else:
        billing_free = bool(
            values.image_billing_free
            or billing_label == "free"
            or (is_dual_race_bonus and billing_label is None)
        )
    if billing_free and not billing_label:
        billing_label = "free"
    billing_exempt_reason = clean_optional_text(
        values.image_meta.get("billing_exempt_reason")
        or values.image_billing_exempt_reason,
        max_len=80,
    )
    return ResolvedModelLibraryJobItem(
        image_id=values.image_id,
        image_url=values.image_url,
        display_url=values.display_url,
        thumb_url=values.thumb_url,
        saved_item_id=values.saved_item_id,
        style_tags=tuple(resolved_tags),
        appearance_direction=resolved_appearance,
        gender=resolved_gender,
        download_filename=filename,
        is_dual_race_bonus=is_dual_race_bonus,
        billing_free=billing_free,
        billing_label=billing_label,
        billing_exempt_reason=billing_exempt_reason,
    )
