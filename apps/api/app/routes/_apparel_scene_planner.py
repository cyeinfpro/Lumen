"""GPT-5.5 preflight planning for apparel showcase generation.

This module intentionally keeps the GPT-facing director/composer/reviewer
contract separate from ``workflows.py``. The workflow route owns persistence and
task creation; this module owns structured scene planning and safe fallbacks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Iterable, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import MAX_PROMPT_CHARS
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    build_effective_provider_config,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
    weighted_priority_order,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.vision_tagging import extract_response_text, responses_url

from ..runtime_settings import get_setting

logger = logging.getLogger(__name__)

SceneStrategy = Literal["balanced", "natural_series", "editorial_campaign"]
SceneVariety = Literal["safe", "rich", "wild"]
ScenePlannerMode = Literal["gpt55_preflight", "gpt55_batch_only", "rules_fallback"]
ContinuityAnchor = Literal["none", "accessory", "pet", "location_series"]

_PROVIDER_RR_COUNTERS: dict[int, int] = {}
_PROVIDER_RR_LOCK = asyncio.Lock()
_DIRECTOR_MODEL = "gpt-5.5"
_FALLBACK_MODEL = "gpt-5.4"
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

_SHOT_CAMERA = {
    "front_full_body": {
        "distance": "full_body",
        "angle": "eye_level",
        "lens_feel": "natural_standard",
        "orientation": "vertical",
    },
    "natural_pose": {
        "distance": "full_body",
        "angle": "slight_side",
        "lens_feel": "handheld_standard",
        "orientation": "vertical",
    },
    "detail_half_body": {
        "distance": "half_body",
        "angle": "eye_level",
        "lens_feel": "natural_standard",
        "orientation": "vertical",
    },
    "side_or_back": {
        "distance": "full_body",
        "angle": "side_or_back",
        "lens_feel": "natural_standard",
        "orientation": "vertical",
    },
}

_TEMPLATE_FAMILY = {
    "white_ecommerce": "clean_ecommerce",
    "premium_studio": "premium_studio",
    "urban_commute": "urban_street",
    "lifestyle": "designed_lifestyle",
    "daily_snapshot": "daily_life",
    "natural_phone_snapshot": "phone_snapshot",
    "social_seed": "social_seeding",
}


def clean_text(value: Any, *, max_len: int = 160) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len]


def clean_string_list(
    values: Iterable[Any], *, max_items: int, max_len: int
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean_text(value, max_len=max_len)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def coerce_string_list(value: Any, *, max_items: int = 8, max_len: int = 80) -> list[str]:
    if isinstance(value, list):
        return clean_string_list(value, max_items=max_items, max_len=max_len)
    if isinstance(value, str) and value.strip():
        parts = re.split(r"[、,，;；\n]+", value)
        return clean_string_list(parts, max_items=max_items, max_len=max_len)
    return []


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "是", "需要"}:
            return True
        if normalized in {"false", "0", "no", "n", "否", "不需要", ""}:
            return False
    if isinstance(value, (int, float)):
        return value != 0
    return False


def build_garment_lock(product_analysis: dict[str, Any]) -> dict[str, Any]:
    category = clean_text(product_analysis.get("category"), max_len=80) or "服饰"
    must_preserve = coerce_string_list(product_analysis.get("must_preserve"))
    if not must_preserve:
        must_preserve = [
            "颜色",
            "版型",
            "领口",
            "袖型",
            "衣长",
            "图案/logo",
            "纽扣/拉链/口袋/缝线",
        ]
    key_details = coerce_string_list(product_analysis.get("key_details"), max_items=6)
    core_identity = "、".join([category, *must_preserve[:3]])[:140]
    visibility_priority = _visibility_priority(must_preserve, category)
    return {
        "category": category,
        "core_identity": core_identity,
        "must_preserve": must_preserve[:8],
        "key_details": key_details,
        "visibility_priority": visibility_priority,
        "occlusion_policy": (
            "手、头发、包带、宠物、饮料杯、手机和前景物不得遮挡商品主体；"
            "胸前、领口、袖口、口袋、纽扣和图案/logo必须清楚可见。"
        ),
        "mutation_bans": [
            "改颜色",
            "改廓形",
            "改领口",
            "改袖型",
            "改衣长",
            "新增图案/logo",
            "新增口袋",
            "改纽扣/拉链/缝线",
        ],
        "risks": coerce_string_list(product_analysis.get("risks"), max_items=6),
    }


def _visibility_priority(must_preserve: list[str], category: str) -> list[str]:
    text = " ".join([category, *must_preserve])
    priority: list[str] = []
    for keyword, label in (
        ("胸", "正面胸口"),
        ("领", "领口"),
        ("口袋", "口袋"),
        ("袋", "口袋"),
        ("袖", "袖口和袖型"),
        ("纽扣", "前襟纽扣"),
        ("拉链", "拉链"),
        ("logo", "图案/logo"),
        ("图案", "图案/logo"),
        ("印花", "图案/logo"),
        ("格纹", "纹理/图案"),
        ("条纹", "纹理/图案"),
        ("版型", "整体廓形"),
        ("衣长", "衣长"),
    ):
        if keyword in text and label not in priority:
            priority.append(label)
    for fallback in ("正面主体", "整体廓形", "领口", "袖口"):
        if fallback not in priority:
            priority.append(fallback)
        if len(priority) >= 6:
            break
    return priority[:6]


def fallback_scene_cards_from_pool(
    *,
    product_analysis: dict[str, Any],
    template: str,
    scene_environment: str,
    shot_picks: list[tuple[str, dict[str, Any]]],
    aspect_ratio: str,
    user_prompt: str,
    accessory_plan: dict[str, Any],
    allow_pet: bool,
    continuity_anchor: str,
) -> list[dict[str, Any]]:
    category = clean_text(product_analysis.get("category"), max_len=80) or "服饰"
    family = _TEMPLATE_FAMILY.get(template, template)
    if scene_environment == "outdoor" and family in {"daily_life", "phone_snapshot"}:
        family = "outdoor_daily"
    accessories = coerce_string_list(accessory_plan.get("items"), max_items=4)
    cards: list[dict[str, Any]] = []
    for index, (shot_class, variant) in enumerate(shot_picks, start=1):
        label = clean_text(variant.get("label"), max_len=140) or shot_class
        camera = dict(_SHOT_CAMERA.get(shot_class, _SHOT_CAMERA["natural_pose"]))
        if aspect_ratio in {"16:9", "21:9", "4:3", "3:2"}:
            camera["orientation"] = "landscape"
        props = list(accessories)
        if allow_pet and continuity_anchor == "pet" and family not in {
            "clean_ecommerce",
            "premium_studio",
        }:
            props.append("低存在感宠物")
        card = {
            "id": f"fallback-{index:02d}-{shot_class}",
            "scene_family": family,
            "location": _fallback_location(template, scene_environment, category),
            "micro_event": label,
            "camera": camera,
            "pose": label,
            "motion": _motion_for_shot(shot_class),
            "props": clean_string_list(props, max_items=5, max_len=40),
            "lighting": _fallback_lighting(template, scene_environment),
            "composition": _composition_for_shot(shot_class),
            "product_visibility": _product_visibility_for_shot(shot_class),
            "negative": [
                "不要改变商品颜色、版型、图案/logo、纽扣、口袋或缝线",
                "不要让手、包带、头发、宠物或道具遮挡商品主体",
                "不要夸张摆拍或让场景抢主体",
            ],
            "source": "rules_fallback",
            "user_direction": clean_text(user_prompt, max_len=120),
        }
        card["fingerprint"] = scene_fingerprint(card)
        cards.append(card)
    return cards


def _fallback_location(template: str, scene_environment: str, category: str) -> str:
    if template == "white_ecommerce":
        return "白底或近白底商业摄影空间"
    if template == "premium_studio":
        return "高级摄影棚或干净灰底空间"
    if template == "urban_commute":
        return f"与{category}风格匹配的城市街角"
    if scene_environment == "outdoor":
        return f"与{category}风格匹配的户外日常场景"
    if template in {"daily_snapshot", "natural_phone_snapshot"}:
        return f"与{category}风格匹配的真实生活空间"
    if template == "social_seed":
        return "自然穿搭分享场景"
    return f"与{category}风格匹配的精品空间"


def _fallback_lighting(template: str, scene_environment: str) -> str:
    if template in {"white_ecommerce", "premium_studio"}:
        return "柔和可控的商业摄影光，服装细节清楚"
    if scene_environment == "outdoor" or template == "urban_commute":
        return "户外自然侧光或斜上光，真实阴影和空间深度"
    return "自然窗光或柔和室内暖光，方向明确不过曝"


def _motion_for_shot(shot_class: str) -> str:
    if shot_class == "detail_half_body":
        return "小幅整理领口或袖口，服装细节清楚"
    if shot_class == "side_or_back":
        return "轻微转身或回头，身体重心稳定"
    if shot_class == "natural_pose":
        return "小步停下或轻整理衣摆，自然抓拍感"
    return "自然站定或小步向前，商品主体完整"


def _composition_for_shot(shot_class: str) -> str:
    if shot_class == "detail_half_body":
        return "上半身为主，胸前、领口、袖口和面料纹理清楚"
    if shot_class == "side_or_back":
        return "侧面或背面廓形清楚，人物完整不切断"
    return "人物完整入镜，商品主体占画面主要面积，背景只作氛围"


def _product_visibility_for_shot(shot_class: str) -> str:
    if shot_class == "detail_half_body":
        return "upper_body_detail"
    if shot_class == "side_or_back":
        return "side_or_back_silhouette"
    return "front_full_body"


def scene_fingerprint(card: dict[str, Any]) -> str:
    camera = card.get("camera") if isinstance(card.get("camera"), dict) else {}
    parts = [
        card.get("scene_family"),
        card.get("location"),
        camera.get("angle"),
        camera.get("distance"),
        card.get("micro_event"),
        (card.get("props") or [""])[0] if isinstance(card.get("props"), list) else "",
        card.get("lighting"),
    ]
    normalized = [
        re.sub(r"\s+", " ", str(part or "").strip().lower())[:80] for part in parts
    ]
    return "|".join(normalized)


async def plan_scene_cards_with_gpt55(
    db: AsyncSession,
    *,
    product_analysis: dict[str, Any],
    garment_lock: dict[str, Any],
    model_summary: str,
    template: str,
    scene_environment: str,
    shot_picks: list[tuple[str, dict[str, Any]]],
    aspect_ratio: str,
    output_count: int,
    user_prompt: str,
    accessory_plan: dict[str, Any],
    scene_strategy: str,
    scene_variety: str,
    continuity_anchor: str,
    allow_pet: bool,
    allow_background_people: bool,
    provider_order: list[ProviderDefinition] | None = None,
) -> dict[str, Any]:
    fallback_cards = fallback_scene_cards_from_pool(
        product_analysis=product_analysis,
        template=template,
        scene_environment=scene_environment,
        shot_picks=shot_picks,
        aspect_ratio=aspect_ratio,
        user_prompt=user_prompt,
        accessory_plan=accessory_plan,
        allow_pet=allow_pet,
        continuity_anchor=continuity_anchor,
    )
    payload = {
        "product": {
            "analysis": product_analysis,
            "garment_lock": garment_lock,
        },
        "model": {"summary": model_summary},
        "request": {
            "count": output_count,
            "template": template,
            "scene_environment": scene_environment,
            "aspect_ratio": aspect_ratio,
            "strategy": scene_strategy,
            "variety": scene_variety,
            "continuity_anchor": continuity_anchor,
            "allow_pet": allow_pet,
            "allow_background_people": allow_background_people,
            "user_direction": user_prompt,
        },
        "shot_plan": [
            {
                "shot_class": shot_class,
                "variant_label": clean_text(variant.get("label"), max_len=140),
                "framing": variant.get("framing"),
            }
            for shot_class, variant in shot_picks
        ],
        "fallback_scene_pool": fallback_cards,
    }
    instructions = _director_instructions(output_count)
    try:
        raw = await _call_gpt55_json(
            db,
            purpose="apparel_scene_director",
            instructions=instructions,
            payload=payload,
            max_output_tokens=3600 if output_count <= 8 else 6000,
            provider_order=provider_order,
        )
        cards = _normalize_scene_cards(
            raw.get("scene_cards"), fallback_cards, shot_picks
        )
        if len(cards) != output_count:
            raise ValueError("scene card count mismatch")
        fingerprints = _unique_fingerprints(cards)
        return {
            "planner": "gpt55_preflight",
            "planner_status": "ok",
            "series_concept": clean_text(raw.get("series_concept"), max_len=160)
            or "自然服饰展示拍摄",
            "continuity_anchors": coerce_string_list(
                raw.get("continuity_anchors"), max_items=6
            ),
            "scene_cards": cards,
            "scene_fingerprints": fingerprints,
            "risk_notes": coerce_string_list(raw.get("risk_notes"), max_items=8),
            "fallback_reason": None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("apparel scene director fallback: %s", exc)
        return _fallback_planning_result(fallback_cards, reason=str(exc))


def rules_fallback_planning(
    *,
    product_analysis: dict[str, Any],
    template: str,
    scene_environment: str,
    shot_picks: list[tuple[str, dict[str, Any]]],
    aspect_ratio: str,
    user_prompt: str,
    accessory_plan: dict[str, Any],
    allow_pet: bool,
    continuity_anchor: str,
) -> dict[str, Any]:
    cards = fallback_scene_cards_from_pool(
        product_analysis=product_analysis,
        template=template,
        scene_environment=scene_environment,
        shot_picks=shot_picks,
        aspect_ratio=aspect_ratio,
        user_prompt=user_prompt,
        accessory_plan=accessory_plan,
        allow_pet=allow_pet,
        continuity_anchor=continuity_anchor,
    )
    return _fallback_planning_result(cards, reason="rules_fallback_requested")


def _fallback_planning_result(
    cards: list[dict[str, Any]], *, reason: str
) -> dict[str, Any]:
    return {
        "planner": "rules_fallback",
        "planner_status": "fallback",
        "series_concept": "规则兜底自然服饰展示",
        "continuity_anchors": [],
        "scene_cards": cards,
        "scene_fingerprints": _unique_fingerprints(cards),
        "risk_notes": [reason[:200]] if reason else [],
        "fallback_reason": reason[:500] if reason else None,
    }


def _director_instructions(output_count: int) -> str:
    return (
        "你是服饰电商真人模特图的拍摄导演。你要为整批图片生成自然、不重复、"
        "像真实拍摄分镜的 SceneCards。必须只输出 JSON 对象，不要 Markdown。\n"
        f"scene_cards 必须正好 {output_count} 条，且第 i 条必须严格对应 "
        "shot_plan[i]，id 用 shot_plan[i].shot_class 加 '-' 加索引，例如 "
        "detail_half_body-3。禁止重排 shot_plan 顺序。\n"
        "字段：series_concept, continuity_anchors, scene_cards, risk_notes。\n"
        "每个 scene_card 字段必须有 id, scene_family, location, micro_event, camera, "
        "pose, motion, props, lighting, composition, product_visibility, negative。\n"
        "camera 必须有 distance, angle, lens_feel, orientation。\n"
        "最高优先级：商品还原，不能改颜色、版型、领口、袖型、衣长、图案/logo、"
        "纽扣、口袋、缝线。动作和道具不得遮挡商品主体。"
        "每张 micro_event 必须不同，camera angle/distance 要有变化。"
        "可以有连续元素，但不能让宠物、包、饮料、手机抢主体。"
        "童装/儿童必须年龄合适，不能成人化。"
    )


async def compose_image_prompt_with_gpt55(
    db: AsyncSession,
    *,
    base_prompt: str,
    product_analysis: dict[str, Any],
    garment_lock: dict[str, Any],
    model_summary: str,
    scene_card: dict[str, Any],
    shot_class: str,
    template: str,
    aspect_ratio: str,
    final_quality: str,
    rewrite_instruction: str | None = None,
    provider_order: list[ProviderDefinition] | None = None,
) -> dict[str, Any]:
    payload = {
        "base_prompt": base_prompt,
        "product_analysis": product_analysis,
        "garment_lock": garment_lock,
        "model_summary": model_summary,
        "scene_card": scene_card,
        "shot_class": shot_class,
        "template": template,
        "aspect_ratio": aspect_ratio,
        "final_quality": final_quality,
        "rewrite_instruction": rewrite_instruction or "",
    }
    instructions = (
        "你是服饰图像生成 prompt 编排师。请把 SceneCard 编排成单张图片模型"
        "可执行的中文 final_prompt。必须只输出 JSON 对象，不要 Markdown。\n"
        "字段：final_prompt, product_visibility_checklist, negative_prompt_notes, regenerate_if。\n"
        "final_prompt 必须自然、有具体拍摄事件和镜头，但商品还原优先级最高。"
        "不要引入没有要求的新服装图案、logo、口袋、腰带或遮挡道具。"
        "如果有 rewrite_instruction，必须按它降低风险。"
    )
    try:
        raw = await _call_gpt55_json(
            db,
            purpose="apparel_prompt_composer",
            instructions=instructions,
            payload=payload,
            max_output_tokens=2600,
            provider_order=provider_order,
        )
        final_prompt = clean_text(raw.get("final_prompt"), max_len=MAX_PROMPT_CHARS)
        if len(final_prompt) < 80:
            raise ValueError("final prompt too short")
        return {
            "scene_card_id": clean_text(scene_card.get("id"), max_len=80),
            "status": "ok",
            "final_prompt": final_prompt,
            "product_visibility_checklist": coerce_string_list(
                raw.get("product_visibility_checklist"), max_items=8, max_len=100
            ),
            "negative_prompt_notes": coerce_string_list(
                raw.get("negative_prompt_notes"), max_items=8, max_len=100
            ),
            "regenerate_if": coerce_string_list(
                raw.get("regenerate_if"), max_items=8, max_len=120
            ),
            "fallback_reason": None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("apparel prompt composer fallback: %s", exc)
        return fallback_prompt_composition(
            base_prompt=base_prompt,
            scene_card=scene_card,
            reason=str(exc),
        )


def fallback_prompt_composition(
    *,
    base_prompt: str,
    scene_card: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "scene_card_id": clean_text(scene_card.get("id"), max_len=80),
        "status": "fallback",
        "final_prompt": base_prompt[:MAX_PROMPT_CHARS],
        "product_visibility_checklist": [],
        "negative_prompt_notes": coerce_string_list(
            scene_card.get("negative"), max_items=8, max_len=100
        ),
        "regenerate_if": [],
        "fallback_reason": reason[:500],
    }


async def review_prompt_risk_with_gpt55(
    db: AsyncSession,
    *,
    final_prompt: str,
    garment_lock: dict[str, Any],
    scene_card: dict[str, Any],
    batch_context: dict[str, Any],
    provider_order: list[ProviderDefinition] | None = None,
) -> dict[str, Any]:
    payload = {
        "final_prompt": final_prompt,
        "garment_lock": garment_lock,
        "scene_card": scene_card,
        "batch_context": batch_context,
    }
    instructions = (
        "你是服饰电商图片生成前的风险审稿员。只检查 prompt，不看图片。"
        "必须只输出 JSON 对象，不要 Markdown。字段：risk_level, risks, "
        "must_rewrite, rewrite_instruction。risk_level 只能 low/medium/high。"
        "若 prompt 可能改商品、遮挡商品主体、动作过复杂、和批次重复、或宠物/道具抢主体，"
        "必须标记风险并给出简短 rewrite_instruction。"
    )
    try:
        raw = await _call_gpt55_json(
            db,
            purpose="apparel_prompt_risk_review",
            instructions=instructions,
            payload=payload,
            max_output_tokens=900,
            provider_order=provider_order,
        )
        risk_level = clean_text(raw.get("risk_level"), max_len=20).lower()
        if risk_level not in {"low", "medium", "high"}:
            risk_level = "medium"
        risks = coerce_string_list(raw.get("risks"), max_items=8, max_len=120)
        must_rewrite = coerce_bool(raw.get("must_rewrite")) or risk_level == "high"
        return {
            "scene_card_id": clean_text(scene_card.get("id"), max_len=80),
            "status": "ok",
            "risk_level": risk_level,
            "risks": risks,
            "must_rewrite": must_rewrite,
            "rewrite_instruction": clean_text(
                raw.get("rewrite_instruction"), max_len=240
            ),
            "fallback_reason": None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("apparel risk review fallback: %s", exc)
        return fallback_risk_review(scene_card=scene_card, reason=str(exc))


def fallback_risk_review(
    *, scene_card: dict[str, Any], reason: str | None = None
) -> dict[str, Any]:
    return {
        "scene_card_id": clean_text(scene_card.get("id"), max_len=80),
        "status": "fallback",
        "risk_level": "medium",
        "risks": [reason[:200]] if reason else [],
        "must_rewrite": False,
        "rewrite_instruction": "",
        "fallback_reason": reason[:500] if reason else None,
    }


def _normalize_scene_cards(
    raw_cards: Any,
    fallback_cards: list[dict[str, Any]],
    shot_picks: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    cards = raw_cards if isinstance(raw_cards, list) else []
    aligned: list[dict[str, Any] | None] = [None] * len(shot_picks)
    taken = [False] * len(shot_picks)
    leftover: list[dict[str, Any]] = []
    for raw in cards:
        if not isinstance(raw, dict):
            continue
        raw_id = clean_text(raw.get("id"), max_len=100).lower()
        vis = clean_text(raw.get("product_visibility"), max_len=80)
        matched_index: int | None = None
        for index, (shot_class, _variant) in enumerate(shot_picks):
            if not taken[index] and shot_class.lower() in raw_id:
                matched_index = index
                break
        if matched_index is None and vis:
            for index, (shot_class, _variant) in enumerate(shot_picks):
                if taken[index]:
                    continue
                if vis == _product_visibility_for_shot(shot_class):
                    matched_index = index
                    break
        if matched_index is None:
            leftover.append(raw)
            continue
        aligned[matched_index] = raw
        taken[matched_index] = True
    for index in range(len(aligned)):
        if aligned[index] is None and leftover:
            aligned[index] = leftover.pop(0)

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(aligned):
        fallback = fallback_cards[index] if index < len(fallback_cards) else {}
        if not isinstance(raw, dict):
            raw = {}
        camera = raw.get("camera") if isinstance(raw.get("camera"), dict) else {}
        card = {
            "id": clean_text(raw.get("id"), max_len=80)
            or fallback.get("id")
            or f"scene-{index + 1:02d}",
            "scene_family": clean_text(raw.get("scene_family"), max_len=60)
            or fallback.get("scene_family")
            or "daily_life",
            "location": clean_text(raw.get("location"), max_len=120)
            or fallback.get("location")
            or "真实生活场景",
            "micro_event": clean_text(raw.get("micro_event"), max_len=160)
            or fallback.get("micro_event")
            or "自然穿搭抓拍",
            "camera": {
                "distance": clean_text(camera.get("distance"), max_len=40)
                or (fallback.get("camera") or {}).get("distance")
                or "full_body",
                "angle": clean_text(camera.get("angle"), max_len=40)
                or (fallback.get("camera") or {}).get("angle")
                or "eye_level",
                "lens_feel": clean_text(camera.get("lens_feel"), max_len=60)
                or (fallback.get("camera") or {}).get("lens_feel")
                or "natural_standard",
                "orientation": clean_text(camera.get("orientation"), max_len=40)
                or (fallback.get("camera") or {}).get("orientation")
                or "vertical",
            },
            "pose": clean_text(raw.get("pose"), max_len=160)
            or fallback.get("pose")
            or "自然站姿",
            "motion": clean_text(raw.get("motion"), max_len=160)
            or fallback.get("motion")
            or "小幅自然动作",
            "props": coerce_string_list(raw.get("props"), max_items=6, max_len=50)
            or list(fallback.get("props") or []),
            "lighting": clean_text(raw.get("lighting"), max_len=120)
            or fallback.get("lighting")
            or "自然光",
            "composition": clean_text(raw.get("composition"), max_len=180)
            or fallback.get("composition")
            or "商品主体清晰",
            "product_visibility": clean_text(
                raw.get("product_visibility"), max_len=80
            )
            or fallback.get("product_visibility")
            or "front_full_body",
            "negative": coerce_string_list(
                raw.get("negative"), max_items=8, max_len=100
            )
            or list(fallback.get("negative") or []),
            "source": "gpt55",
        }
        card["fingerprint"] = scene_fingerprint(card)
        normalized.append(card)
    return _dedupe_scene_cards(normalized, fallback_cards)


def _dedupe_scene_cards(
    cards: list[dict[str, Any]], fallback_cards: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, card in enumerate(cards):
        fingerprint = scene_fingerprint(card)
        if fingerprint in seen:
            if index < len(fallback_cards):
                card = dict(fallback_cards[index])
                card["source"] = "rules_fallback_dedupe"
                fingerprint = scene_fingerprint(card)
                if fingerprint in seen:
                    card["micro_event"] = (
                        f"{card.get('micro_event') or '自然穿搭抓拍'}（变体 {index + 1}）"
                    )[:160]
                    fingerprint = scene_fingerprint(card)
        seen.add(fingerprint)
        card["fingerprint"] = fingerprint
        out.append(card)
    return out


def _unique_fingerprints(cards: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for card in cards:
        fingerprint = scene_fingerprint(card)
        if fingerprint and fingerprint not in seen:
            seen.add(fingerprint)
            out.append(fingerprint)
    return out


async def resolve_scene_provider_order(db: AsyncSession) -> list[ProviderDefinition]:
    spec_providers = get_spec("providers")
    raw_providers = (
        await get_setting(db, spec_providers) if spec_providers else None
    )
    providers, _proxies, errors = build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    for err in errors:
        logger.warning("%s", err)
    providers = [p for p in providers if endpoint_kind_allowed(p, "responses")]
    async with _PROVIDER_RR_LOCK:
        return weighted_priority_order(providers, _PROVIDER_RR_COUNTERS)


async def _call_gpt55_json(
    db: AsyncSession,
    *,
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
    provider_order: list[ProviderDefinition] | None = None,
) -> dict[str, Any]:
    providers = (
        list(provider_order)
        if provider_order is not None
        else await resolve_scene_provider_order(db)
    )
    if not providers:
        raise RuntimeError("no responses provider available")
    primary_effort = "medium" if purpose == "apparel_scene_director" else "low"
    attempts = (
        {
            "name": "gpt55-priority",
            "model": _DIRECTOR_MODEL,
            "reasoning": {"effort": primary_effort},
            "service_tier": "priority",
        },
        {
            "name": "gpt55-standard",
            "model": _DIRECTOR_MODEL,
            "reasoning": {"effort": "low"},
            "service_tier": None,
        },
        {
            "name": "gpt54-standard-fallback",
            "model": _FALLBACK_MODEL,
            "reasoning": {"effort": "low"},
            "service_tier": None,
        },
    )
    last_error = "unknown"
    for provider in providers:
        provider_fatal = False
        for attempt in attempts:
            try:
                text = await _call_responses_text(
                    provider=provider,
                    attempt=attempt,
                    purpose=purpose,
                    instructions=instructions,
                    payload=payload,
                    max_output_tokens=max_output_tokens,
                )
                data = _extract_json_object(text)
                if isinstance(data, dict):
                    return data
                raise ValueError("json root is not object")
            except Exception as exc:  # noqa: BLE001
                last_error = f"{provider.name}/{attempt['name']}: {exc}"
                logger.info("gpt55 json attempt failed: %s", last_error)
                if not _should_try_next_attempt(exc):
                    provider_fatal = True
                    break
        if provider_fatal:
            continue
    raise RuntimeError(last_error)


async def _call_responses_text(
    *,
    provider: ProviderDefinition,
    attempt: dict[str, Any],
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
) -> str:
    body: dict[str, Any] = {
        "model": attempt["model"],
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(payload, ensure_ascii=False),
                    }
                ],
            }
        ],
        "stream": False,
        "store": False,
        "max_output_tokens": max_output_tokens,
        "metadata": {"purpose": purpose},
    }
    if attempt.get("reasoning"):
        body["reasoning"] = attempt["reasoning"]
    if attempt.get("service_tier"):
        body["service_tier"] = attempt["service_tier"]

    proxy_url = await resolve_provider_proxy_url(provider.proxy)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=8.0, read=25.0, write=25.0, pool=8.0),
        proxy=proxy_url,
    ) as client:
        resp = await client.post(
            responses_url(provider.base_url),
            json=body,
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code >= 400:
        detail = resp.text[:500]
        raise _UpstreamHTTPError(resp.status_code, detail)
    try:
        response_payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("upstream returned invalid JSON") from exc
    text = extract_response_text(response_payload)
    if not text:
        raise ValueError("upstream returned empty text")
    return text


class _UpstreamHTTPError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"http {status_code}: {detail}")
        self.status_code = status_code


def _should_try_next_attempt(exc: Exception) -> bool:
    if isinstance(exc, _UpstreamHTTPError):
        if exc.status_code in _RETRYABLE_STATUS:
            return True
        if 400 <= exc.status_code < 500:
            return False
        return True
    return True


def _extract_json_object(text: str) -> dict[str, Any]:
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


__all__ = [
    "ContinuityAnchor",
    "ScenePlannerMode",
    "SceneStrategy",
    "SceneVariety",
    "build_garment_lock",
    "coerce_bool",
    "clean_text",
    "compose_image_prompt_with_gpt55",
    "fallback_prompt_composition",
    "fallback_risk_review",
    "fallback_scene_cards_from_pool",
    "plan_scene_cards_with_gpt55",
    "review_prompt_risk_with_gpt55",
    "resolve_scene_provider_order",
    "rules_fallback_planning",
    "scene_fingerprint",
]
