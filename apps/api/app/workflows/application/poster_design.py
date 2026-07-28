"""Pure poster workflow policy and value transformations."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


POSTER_WORKFLOW_TYPE = "poster_design"
POSTER_WORKFLOW_STEPS = (
    "copy_input",
    "style_selection",
    "copy_analysis",
    "master_generation",
    "master_approval",
    "multi_size_generation",
    "delivery",
)
POSTER_DEFAULT_TARGET_ASPECTS: tuple[str, ...] = ("1:1", "9:16", "16:9", "3:4")
POSTER_MASTER_ASPECT = "1:1"


class PosterStyleView(Protocol):
    id: str
    title: str | None
    mood: str | None
    prompt_template: str | None
    palette: Sequence[object] | None
    recommended_aspects: Sequence[object] | None
    style_tags: Sequence[object] | None
    category: str | None


class PosterStringValues(tuple[str, ...]):
    """Immutable style values with value equality across list/tuple callers."""

    def __new__(cls, values: Iterable[str] = ()) -> PosterStringValues:
        return super().__new__(cls, values)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return NotImplemented

    __hash__ = tuple.__hash__


@dataclass(frozen=True, slots=True)
class PosterStyleSnapshot:
    id: str
    title: str
    mood: str
    prompt_template: str
    palette: PosterStringValues
    recommended_aspects: PosterStringValues
    style_tags: PosterStringValues
    category: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> PosterStyleSnapshot:
        return cls(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            mood=str(raw.get("mood") or ""),
            prompt_template=str(raw.get("prompt_template") or ""),
            palette=_string_tuple(raw.get("palette")),
            recommended_aspects=_string_tuple(raw.get("recommended_aspects")),
            style_tags=_string_tuple(raw.get("style_tags")),
            category=str(raw.get("category") or ""),
        )


@dataclass(frozen=True, slots=True)
class PosterStepSeed:
    step_key: str
    status: str
    input_json: Mapping[str, object]
    output_json: Mapping[str, object]


def _string_tuple(value: object) -> PosterStringValues:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return PosterStringValues()
    return PosterStringValues(
        cleaned for item in value if (cleaned := str(item).strip())
    )


def dedupe_nonempty(values: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def poster_style_summary(style: PosterStyleView) -> dict[str, object]:
    return {
        "style_id": str(style.id),
        "title": str(style.title or ""),
        "mood": str(style.mood or ""),
        "prompt_template": str(style.prompt_template or "").strip(),
        "palette": [str(value) for value in (style.palette or [])],
        "recommended_aspects": [
            str(value) for value in (style.recommended_aspects or [])
        ],
        "style_tags": [str(value) for value in (style.style_tags or [])],
        "category": str(style.category or ""),
    }


def poster_copy_analysis_prompt(copy_text: str) -> str:
    return (
        "你是海报文案结构化助手。请把下面一段海报营销文案切分成固定 JSON schema："
        "main_title（主标题，3-12 字）、subtitle（副标，可空）、selling_points（卖点数组，最多 4 条）、"
        "cta（行动号召，可空）、price（价格，可空）、tone（语气，1 句话）、"
        "info_density（信息密度，取值 high/medium/low）。"
        "必须只返回一个 JSON object，不要 Markdown、不要代码块、不要解释文字。"
        "如果某字段在原文里没有，填 null。info_density 的判定："
        "卖点+CTA+价格总条数 ≥ 4 → high；2-3 → medium；≤ 1 → low。"
        "保留原文措辞，不要改写或扩写。"
        f"\n\n原文案：\n{copy_text}"
    )


def poster_layout_safe_area(info_density: str) -> str:
    mapping = {
        "high": "下半区或左侧 1/3 区为主信息密集区，画面上半区留呼吸感",
        "medium": "中部水平带为主信息区，上下各留 25% 空间",
        "low": "中心 1/3 区为主信息区，四周大留白",
    }
    return mapping.get(info_density, mapping["medium"])


def poster_text_fields_block(copy_analysis: Mapping[str, object]) -> str:
    def value_for(key: str) -> str:
        value = copy_analysis.get(key)
        if value is None:
            return ""
        if isinstance(value, list):
            return "、".join(str(item).strip() for item in value if str(item).strip())
        return str(value).strip()

    fields = (
        ("main_title", value_for("main_title")),
        ("subtitle", value_for("subtitle")),
        ("selling_points", value_for("selling_points")),
        ("cta", value_for("cta")),
        ("price", value_for("price")),
    )
    lines = [f"- {key}: {value}" for key, value in fields if value]
    return "\n".join(lines) if lines else "- main_title: (无)"


def poster_brand_assets_block(brand_assets: Mapping[str, object]) -> str:
    primary_color = str(brand_assets.get("primary_color") or "").strip()
    font_family = str(brand_assets.get("font_family") or "").strip()
    bits: list[str] = []
    if primary_color:
        bits.append(f"primary brand color: {primary_color}")
    if font_family:
        bits.append(f"preferred font family: {font_family}")
    if brand_assets.get("logo_image_id"):
        bits.append(
            "a brand logo image is provided as reference; integrate it tastefully if appropriate"
        )
    if brand_assets.get("product_image_id"):
        bits.append(
            "a product image is provided as reference; place it as the visual focal point"
        )
    return "; ".join(bits) if bits else "no extra brand asset constraints"


def poster_brand_attachment_ids(
    metadata: Mapping[str, object],
    product_image_ids: Sequence[object],
) -> list[str]:
    raw_brand_assets = metadata.get("brand_assets")
    brand_assets = raw_brand_assets if isinstance(raw_brand_assets, Mapping) else {}
    image_ids = [
        brand_assets.get("logo_image_id"),
        brand_assets.get("product_image_id"),
        *product_image_ids,
    ]
    return dedupe_nonempty(image_ids)


def poster_master_prompt(
    *,
    style_summary: Mapping[str, object],
    copy_analysis: Mapping[str, object],
    brand_assets: Mapping[str, object],
    candidate_index: int,
) -> str:
    info_density = str(copy_analysis.get("info_density") or "medium")
    palette = style_summary.get("palette") or []
    palette_text = (
        ", ".join(str(value) for value in palette if str(value).strip())
        or "balanced palette"
    )
    style_prompt_template = str(style_summary.get("prompt_template") or "").strip()
    style_mood = str(style_summary.get("mood") or "").strip()
    safe_area = poster_layout_safe_area(info_density)
    text_block = poster_text_fields_block(copy_analysis)
    brand_block = poster_brand_assets_block(brand_assets)
    style_block = style_prompt_template or "clean modern poster design"
    return (
        "Create one high-quality marketing poster master, square 1:1 composition, "
        "print-ready visual.\n"
        "This is a master candidate used to confirm the visual style before "
        "rendering other aspect ratios; keep composition logic clean.\n"
        "Render the marketing text fields directly inside the image (do NOT leave "
        "them as placeholders): main_title is the largest, subtitle smaller below, "
        "selling_points as short bullets, cta as a small accent badge, price as a "
        "highlighted callout if present. Keep all text short, sharp, and legible.\n"
        f"Style direction: {style_block}.\n"
        f"Color palette priority: {palette_text}.\n"
        f"Mood: {style_mood or 'aligned with the style direction above'}.\n"
        f"Information density: {info_density}; layout safe area: {safe_area}.\n"
        f"Brand assets: {brand_block}.\n"
        "Avoid: watermark, signature, busy textures over text, unreadable glyphs, "
        "duplicated headlines, English filler text when source copy is Chinese.\n"
        f"Text fields to render:\n{text_block}\n"
        f"Candidate variation number: {candidate_index}."
    )


def poster_render_prompt(
    *,
    style_summary: Mapping[str, object],
    copy_analysis: Mapping[str, object],
    target_aspect: str,
    adjustments: str = "",
) -> str:
    palette = style_summary.get("palette") or []
    palette_text = (
        ", ".join(str(value) for value in palette if str(value).strip())
        or "balanced palette"
    )
    info_density = str(copy_analysis.get("info_density") or "medium")
    safe_area = poster_layout_safe_area(info_density)
    text_block = poster_text_fields_block(copy_analysis)
    extra = adjustments.strip()
    extra_line = f"\nAdditional direction: {extra}" if extra else ""
    return (
        f"Re-render the reference poster master into a {target_aspect} composition.\n"
        "Match the visual style, color palette, mood, decoration logic, and text "
        "rendering style of the reference image exactly.\n"
        "Adapt the composition naturally to the new aspect ratio without distortion; "
        "reposition text fields to keep them clearly legible in the new frame.\n"
        f"Reference palette: {palette_text}.\n"
        f"Information density: {info_density}; layout safe area: {safe_area}.\n"
        f"Text fields to keep visible:\n{text_block}\n"
        "Do not change the wording of any text field; only adjust position, size, "
        "and orientation to fit the new aspect ratio."
        f"{extra_line}"
    )


def poster_revision_prompt(
    *,
    style_summary: Mapping[str, object],
    copy_analysis: Mapping[str, object],
    target_aspect: str,
    instruction: str,
    scope: str,
) -> str:
    if scope == "style":
        return (
            f"{poster_render_prompt(style_summary=style_summary, copy_analysis=copy_analysis, target_aspect=target_aspect)}"
            f"\nUser revision (style change): {instruction.strip()}."
        )
    return (
        f"Revise this poster background while keeping the {target_aspect} composition.\n"
        "Preserve the visual style, color palette, mood, and decoration logic of the reference exactly.\n"
        "Do not change the wording of any text field; only adjust the background, "
        "layout, or composition based on the user's instruction.\n"
        f"Text fields to keep visible:\n{poster_text_fields_block(copy_analysis)}\n"
        f"User revision: {instruction.strip()}."
    )


def build_poster_step_seeds(
    *,
    user_prompt: str,
    metadata: Mapping[str, object],
) -> tuple[PosterStepSeed, ...]:
    seeds: list[PosterStepSeed] = []
    for key in POSTER_WORKFLOW_STEPS:
        status = "waiting_input"
        input_json: dict[str, object] = {}
        output_json: dict[str, object] = {}
        if key == "copy_input":
            status = "approved"
            input_json = {"copy_text": user_prompt}
            output_json = {"confirmed": True}
        elif key == "style_selection":
            status = "approved"
            input_json = {
                "style_id": metadata.get("style_id"),
                "target_aspects": metadata.get("target_aspects")
                or list(POSTER_DEFAULT_TARGET_ASPECTS),
            }
            output_json = {"confirmed": True}
        elif key == "copy_analysis":
            status = "running"
            input_json = {
                "copy_text": user_prompt,
                "prompt_contract": "extract poster copy into structured JSON",
            }
        seeds.append(
            PosterStepSeed(
                step_key=key,
                status=status,
                input_json=input_json,
                output_json=output_json,
            )
        )
    return tuple(seeds)


def _clean_string_list(
    values: Iterable[object],
    *,
    max_items: int,
    max_len: int,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item[:max_len])
        if len(out) >= max_items:
            break
    return out


def poster_parse_copy_analysis_text(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    parsed: object = None
    if raw:
        body = raw
        if body.startswith("```"):
            body = body.strip("`")
            body = body.removeprefix("json").strip()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            start = body.find("{")
            end = body.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(body[start : end + 1])
                except json.JSONDecodeError:
                    parsed = None
    values = parsed if isinstance(parsed, dict) else {}
    main_title = values.get("main_title")
    subtitle = values.get("subtitle")
    selling_points = values.get("selling_points")
    cta = values.get("cta")
    price = values.get("price")
    tone = values.get("tone")
    info_density = values.get("info_density")
    if info_density not in {"high", "medium", "low"}:
        info_density = "medium"
    return {
        "main_title": str(main_title).strip() if main_title else None,
        "subtitle": str(subtitle).strip() if subtitle else None,
        "selling_points": (
            _clean_string_list(
                (str(item) for item in selling_points),
                max_items=4,
                max_len=60,
            )
            if isinstance(selling_points, list)
            else []
        ),
        "cta": str(cta).strip() if cta else None,
        "price": str(price).strip() if price else None,
        "tone": str(tone).strip() if tone else None,
        "info_density": info_density,
        "raw_text": text or "",
    }


def merge_poster_copy_corrections(
    base: Mapping[str, object],
    corrections: Mapping[str, object],
    *,
    confirmed_at: datetime,
) -> dict[str, object]:
    final = dict(base)
    raw = dict(corrections)
    for key, value in raw.items():
        if value is not None:
            final[key] = value
    final["user_corrections"] = raw
    final["confirmed_at"] = confirmed_at.isoformat()
    return final


def pending_poster_aspects(
    requested: Iterable[str],
    existing: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    aspects = tuple(dict.fromkeys(requested))
    existing_set = set(existing)
    pending = tuple(aspect for aspect in aspects if aspect not in existing_set)
    return aspects, pending
