"""Response parsing and scene-card validation."""

from __future__ import annotations

import json
import re
from typing import Any

from ...domain.apparel_scene_fallbacks import (
    clean_text,
    coerce_string_list,
    dict_or_empty as _dict_or_empty,
    is_generic_scene_text as _is_generic_scene_text,
    product_visibility_for_shot as _product_visibility_for_shot,
    scene_fingerprint,
)

BACK_VIEW_TEXT_TOKENS = (
    "背影",
    "背向",
    "背对",
    "背面",
    "后背",
    "背后",
    "侧后",
    "from behind",
    "back view",
    "rear view",
    "back-facing",
)
SIDE_BACK_VIEW_TOKENS = (
    *BACK_VIEW_TEXT_TOKENS,
    "side_or_back",
    "side view",
    "profile view",
    "pure side",
    "纯侧面",
    "侧面轮廓",
    "侧背",
)
SIDE_BACK_CAMERA_ANGLE_TOKENS = (
    *SIDE_BACK_VIEW_TOKENS,
    "side profile",
    "side-profile",
    "side_profile",
    "profile",
    "side",
    "back",
    "rear",
    "behind",
)
FRONT_CAMERA_ANGLE_TOKENS = (
    "front",
    "front view",
    "front_view",
    "front three quarter",
    "front-three-quarter",
    "front_three_quarter",
    "three quarter front",
    "three-quarter-front",
    "three_quarter_front",
    "3/4 front",
    "eye level",
    "eye-level",
    "eye_level",
    "straight on",
    "straight-on",
    "straight_on",
    "正面",
    "三分之二正面",
    "四分之三正面",
    "平视",
)


def sanitize_shooting_brief(value: Any, *, max_len: int = 1800) -> str:
    text = clean_text(value, max_len=max_len)
    return (
        text.replace("SceneCard", "本张拍摄方案")
        .replace("scene_card", "拍摄方案")
        .replace("shot_plan", "拍摄计划")
        .replace("final_prompt", "拍摄方案")
    )


def coerce_candidate_briefs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    briefs: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = (
                item.get("shooting_brief")
                or item.get("brief")
                or item.get("description")
                or item.get("text")
            )
        else:
            text = item
        brief = sanitize_shooting_brief(text, max_len=900)
        if brief:
            briefs.append(brief)
        if len(briefs) >= 3:
            break
    return briefs


def coerce_selection_scores(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    numeric_keys = (
        "product_visibility",
        "naturalness",
        "photographic_quality",
        "variety",
        "risk_control",
        "total",
    )
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {
            "candidate": clean_text(
                item.get("candidate") or item.get("id") or index, max_len=30
            )
            or str(index)
        }
        for key in numeric_keys:
            if key not in item:
                continue
            score = item.get(key)
            if score is None:
                row[key] = clean_text(score, max_len=20)
                continue
            try:
                row[key] = round(float(score), 2)
            except (TypeError, ValueError):
                row[key] = clean_text(score, max_len=20)
        reason = clean_text(item.get("reason"), max_len=140)
        if reason:
            row["reason"] = reason
        out.append(row)
        if len(out) >= 3:
            break
    return out


def has_view_token(value: Any, tokens: tuple[str, ...]) -> bool:
    text = str(value or "").lower()
    return any(token.lower() in text for token in tokens)


def camera_angle_has_token(value: Any, tokens: tuple[str, ...]) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return False
    normalized = re.sub(r"[_-]+", " ", raw)
    for token in tokens:
        token_text = str(token or "").strip().lower()
        if not token_text:
            continue
        token_normalized = re.sub(r"[_-]+", " ", token_text)
        if re.search(
            rf"(?<![a-z0-9]){re.escape(token_normalized)}(?![a-z0-9])",
            normalized,
        ):
            return True
        if any("\u4e00" <= ch <= "\u9fff" for ch in token_text) and token_text in raw:
            return True
    return False


def required_gpt_scene_fields_missing(card: dict[str, Any]) -> list[str]:
    required = (
        "location",
        "micro_event",
        "pose",
        "motion",
        "lighting",
        "composition",
        "environment_detail",
        "lighting_detail",
        "camera_detail",
        "composition_detail",
        "creative_intent",
        "natural_detail",
        "shooting_brief",
    )
    missing = [key for key in required if not str(card.get(key) or "").strip()]
    camera = _dict_or_empty(card.get("camera"))
    for key in ("distance", "angle", "lens_feel", "orientation"):
        if not str(camera.get(key) or "").strip():
            missing.append(f"camera.{key}")
    return missing


def reject_side_back_for_non_side_card(
    card: dict[str, Any],
    shot_class: str,
) -> None:
    camera = _dict_or_empty(card.get("camera"))
    camera_angle = camera.get("angle")
    if shot_class == "side_or_back":
        if not (
            has_view_token(camera_angle, SIDE_BACK_VIEW_TOKENS)
            or camera_angle_has_token(camera_angle, SIDE_BACK_CAMERA_ANGLE_TOKENS)
        ):
            raise ValueError(
                "side_or_back GPT scene_card must use side/back camera angle"
            )
        return
    if camera_angle_has_token(camera_angle, SIDE_BACK_CAMERA_ANGLE_TOKENS):
        raise ValueError("non-side GPT scene_card uses back/side view camera angle")
    if shot_class in {
        "front_full_body",
        "natural_pose",
    } and not camera_angle_has_token(
        camera_angle,
        FRONT_CAMERA_ANGLE_TOKENS,
    ):
        raise ValueError(
            f"{shot_class} GPT scene_card camera.angle must stay front-facing"
        )
    checked = {
        "camera.angle": camera_angle,
        "product_visibility": card.get("product_visibility"),
        "micro_event": card.get("micro_event"),
        "pose": card.get("pose"),
        "motion": card.get("motion"),
        "composition": card.get("composition"),
        "camera_detail": card.get("camera_detail"),
        "composition_detail": card.get("composition_detail"),
        "creative_intent": card.get("creative_intent"),
        "natural_detail": card.get("natural_detail"),
        "shooting_brief": card.get("shooting_brief"),
    }
    offenders = [
        key
        for key, value in checked.items()
        if has_view_token(value, SIDE_BACK_VIEW_TOKENS)
    ]
    if offenders:
        raise ValueError(
            f"non-side GPT scene_card uses back/side view in {', '.join(offenders)}"
        )


def scene_card_match_index(
    raw: dict[str, Any],
    shot_picks: list[tuple[str, dict[str, Any]]],
    taken: list[bool],
) -> int | None:
    raw_id = clean_text(raw.get("id"), max_len=100).lower()
    exact_matches = [
        index
        for index, (shot_class, _variant) in enumerate(shot_picks)
        if not taken[index] and raw_id == f"{shot_class.lower()}-{index + 1}"
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    class_matches = [
        index
        for index, (shot_class, _variant) in enumerate(shot_picks)
        if not taken[index] and shot_class.lower() in raw_id
    ]
    if len(class_matches) == 1:
        return class_matches[0]

    visibility = clean_text(raw.get("product_visibility"), max_len=80)
    visibility_matches = [
        index
        for index, (shot_class, _variant) in enumerate(shot_picks)
        if (
            not taken[index]
            and visibility
            and visibility == _product_visibility_for_shot(shot_class)
        )
    ]
    return visibility_matches[0] if len(visibility_matches) == 1 else None


def align_scene_cards(
    raw_cards: Any,
    shot_picks: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any] | None]:
    cards = raw_cards if isinstance(raw_cards, list) else []
    aligned: list[dict[str, Any] | None] = [None] * len(shot_picks)
    taken = [False] * len(shot_picks)
    leftover: list[dict[str, Any]] = []
    for raw in cards:
        if not isinstance(raw, dict):
            continue
        matched_index = scene_card_match_index(raw, shot_picks, taken)
        if matched_index is None:
            leftover.append(raw)
            continue
        aligned[matched_index] = raw
        taken[matched_index] = True
    for index, card in enumerate(aligned):
        if card is None and leftover:
            aligned[index] = leftover.pop(0)
    return aligned


def validate_normalized_scene_card(
    card: dict[str, Any],
    *,
    index: int,
    shot_class: str,
    shot_label: str,
) -> dict[str, Any]:
    missing = required_gpt_scene_fields_missing(card)
    if missing:
        raise ValueError(
            f"incomplete GPT scene_card for shot {index + 1}: {', '.join(missing)}"
        )
    for field_name, label in (
        ("micro_event", "micro_event"),
        ("pose", "pose"),
        ("motion", "motion"),
    ):
        if _is_generic_scene_text(
            card.get(field_name),
            shot_class=shot_class,
            label=shot_label,
        ):
            raise ValueError(
                f"generic GPT {label} for shot {index + 1}: {card.get(field_name)}"
            )
    reject_side_back_for_non_side_card(card, shot_class)
    card["fingerprint"] = scene_fingerprint(card)
    return card


def normalize_scene_card(
    raw: dict[str, Any] | None,
    *,
    index: int,
    shot_picks: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"missing GPT scene_card for shot {index + 1}")
    shot_class = shot_picks[index][0]
    shot_label = clean_text(shot_picks[index][1].get("label"), max_len=160)
    camera = _dict_or_empty(raw.get("camera"))
    card = {
        "id": clean_text(raw.get("id"), max_len=80) or f"scene-{index + 1:02d}",
        "scene_family": clean_text(raw.get("scene_family"), max_len=60)
        or "gpt55_scene",
        "location": clean_text(raw.get("location"), max_len=120),
        "micro_event": clean_text(raw.get("micro_event"), max_len=160),
        "camera": {
            "distance": clean_text(camera.get("distance"), max_len=40),
            "angle": clean_text(camera.get("angle"), max_len=40),
            "lens_feel": clean_text(camera.get("lens_feel"), max_len=60),
            "orientation": clean_text(camera.get("orientation"), max_len=40),
        },
        "pose": clean_text(raw.get("pose"), max_len=160),
        "motion": clean_text(raw.get("motion"), max_len=160),
        "props": coerce_string_list(raw.get("props"), max_items=6, max_len=50),
        "lighting": clean_text(raw.get("lighting"), max_len=120),
        "composition": clean_text(raw.get("composition"), max_len=180),
        "product_visibility": clean_text(raw.get("product_visibility"), max_len=80)
        or _product_visibility_for_shot(shot_class),
        "environment_detail": clean_text(raw.get("environment_detail"), max_len=220),
        "lighting_detail": clean_text(raw.get("lighting_detail"), max_len=220),
        "camera_detail": clean_text(raw.get("camera_detail"), max_len=220),
        "composition_detail": clean_text(raw.get("composition_detail"), max_len=220),
        "creative_intent": clean_text(raw.get("creative_intent"), max_len=220),
        "natural_detail": clean_text(raw.get("natural_detail"), max_len=220),
        "shooting_brief": sanitize_shooting_brief(
            raw.get("shooting_brief") or raw.get("final_prompt"),
            max_len=900,
        ),
        "negative": coerce_string_list(raw.get("negative"), max_items=8, max_len=100),
        "source": "gpt55",
    }
    return validate_normalized_scene_card(
        card,
        index=index,
        shot_class=shot_class,
        shot_label=shot_label,
    )


def normalize_scene_cards(
    raw_cards: Any,
    shot_picks: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    aligned = align_scene_cards(raw_cards, shot_picks)
    normalized = [
        normalize_scene_card(raw, index=index, shot_picks=shot_picks)
        for index, raw in enumerate(aligned)
    ]
    return assert_unique_scene_fingerprints(normalized)


def assert_unique_scene_fingerprints(
    cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, card in enumerate(cards):
        fingerprint = scene_fingerprint(card)
        if fingerprint in seen:
            raise ValueError(f"duplicate GPT scene fingerprint at shot {index + 1}")
        seen.add(fingerprint)
        card["fingerprint"] = fingerprint
        out.append(card)
    return out


def unique_fingerprints(cards: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for card in cards:
        fingerprint = scene_fingerprint(card)
        if fingerprint and fingerprint not in seen:
            seen.add(fingerprint)
            out.append(fingerprint)
    return out


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be object")
    return data
