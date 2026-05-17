"""GPT-5.5 preflight planning for apparel showcase generation.

This module intentionally keeps the GPT-facing director/composer/reviewer
contract separate from ``workflows.py``. The workflow route owns persistence and
task creation; this module owns structured scene planning and safe fallbacks.
"""

from __future__ import annotations

import asyncio
import hashlib
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

_FALLBACK_FAMILY_POOLS = {
    "indoor_rich": (
        "premium_studio",
        "designed_lifestyle",
        "daily_life",
        "phone_snapshot",
    ),
    "outdoor_rich": (
        "urban_street",
        "outdoor_daily",
        "social_seeding",
        "phone_snapshot",
    ),
    "editorial": (
        "premium_studio",
        "designed_lifestyle",
        "urban_street",
        "social_seeding",
    ),
}

_FALLBACK_LOCATION_POOLS: dict[str, tuple[str, ...]] = {
    "clean_ecommerce": (
        "近白底自然光摄影区",
        "浅灰背景的极简电商棚",
        "米白墙面和浅色地面的干净拍摄区",
        "无缝白背景前的自然站位区",
    ),
    "premium_studio": (
        "带侧窗光的极简摄影棚角落",
        "灰白墙面和木地板的高级棚拍空间",
        "画廊式浅色走廊",
        "柔光灯下的干净布景区",
    ),
    "urban_street": (
        "咖啡店门口的人行道",
        "城市斑马线旁的街角",
        "玻璃橱窗外的街边",
        "树影落下的社区街道",
        "地铁口外的开阔人行区",
    ),
    "outdoor_daily": (
        "公园步道边",
        "小区楼下的绿化步道",
        "便利店外的街边台阶",
        "河边栏杆旁的步行道",
        "阳光下的校园式步道",
    ),
    "designed_lifestyle": (
        "自然采光的客厅一角",
        "书店过道旁",
        "木质长桌边的生活空间",
        "酒店大堂边缘的安静区域",
        "浅色楼梯转角",
    ),
    "daily_life": (
        "窗边玄关",
        "家中餐桌旁",
        "开放式厨房边缘",
        "阳台门口的自然光区域",
        "衣帽架旁的生活角落",
    ),
    "phone_snapshot": (
        "朋友视角的街边随手拍位置",
        "窗边自然光下的手机抓拍位置",
        "店外台阶旁的手机竖拍位置",
        "走廊尽头的自然手机视角",
        "公园座椅旁的随手拍位置",
    ),
    "social_seeding": (
        "精品店门口的种草街拍位",
        "咖啡店外的小桌旁",
        "展览空间外的自然打卡位",
        "街边绿植和玻璃窗之间",
        "生活方式店的入口旁",
    ),
}

_FALLBACK_EVENTS_BY_SHOT: dict[str, tuple[str, ...]] = {
    "front_full_body": (
        "刚走到地点中央时短暂停步看向镜头",
        "等人时自然站定，身体重心落在一侧",
        "从门口走出后停下整理步伐",
        "穿过光影区域时回头确认镜头",
        "在路边停住，手臂自然垂落不遮挡衣服",
    ),
    "natural_pose": (
        "走了两步后自然放慢脚步",
        "低头看了一眼手机又抬眼",
        "和镜头外的人轻声回应",
        "一只手轻扶衣摆边缘但不遮挡主体",
        "转身准备离开时被自然抓拍",
    ),
    "detail_half_body": (
        "抬手轻整理袖口，胸前和领口保持清楚",
        "手指轻触衣摆边缘展示面料垂感",
        "肩颈放松地看向一侧，衣领细节清楚",
        "低头检查纽扣或拉链，手不压住主体",
        "自然抬臂调整发丝，手臂避开胸前图案",
    ),
    "side_or_back": (
        "侧身迈上一步时回头",
        "从座位旁起身转向镜头",
        "看向橱窗时身体保持侧面轮廓",
        "转过街角前短暂停住",
        "背向前走半步后自然回望",
    ),
}

_FALLBACK_POSES_BY_SHOT: dict[str, tuple[str, ...]] = {
    "front_full_body": (
        "一脚在前的自然全身站姿，肩颈放松",
        "身体微微侧向镜头，双手自然垂落",
        "重心落在后脚，前脚轻点地面",
        "步伐刚停下的全身姿态",
    ),
    "natural_pose": (
        "身体三分之二侧向，头部自然转回",
        "轻微前行动作，手部保持低位",
        "上半身放松，视线偏离镜头一点",
        "自然转身中的松弛姿态",
    ),
    "detail_half_body": (
        "半身微侧，手部动作避开胸前主体",
        "肩线自然，手指只触碰袖口或衣摆边缘",
        "头部微低，上半身保持服装细节清楚",
        "手臂打开一点，领口和胸前完整可见",
    ),
    "side_or_back": (
        "侧身站位，肩背轮廓完整",
        "回头看向镜头，身体保持侧后角度",
        "小步转身，背部或侧面廓形清楚",
        "一肩靠近镜头，另一侧自然后退",
    ),
}

_FALLBACK_MOTIONS_BY_SHOT: dict[str, tuple[str, ...]] = {
    "front_full_body": (
        "刚停住的轻微惯性，衣摆有自然垂坠",
        "低幅度呼吸感，身体重心真实",
        "脚步从移动到停下，动作幅度很小",
        "手臂自然摆动到身体两侧",
    ),
    "natural_pose": (
        "小步前行中的自然定格",
        "轻微转身带出衣服褶皱",
        "手部低位小动作，主体不被遮挡",
        "视线和身体方向不同步的抓拍感",
    ),
    "detail_half_body": (
        "手指轻整理细节，衣服纹理和结构清楚",
        "肩颈有轻微动作，胸前保持无遮挡",
        "袖口或领口被轻轻调整但不改变款式",
        "近距离自然呼吸感，面料褶皱可信",
    ),
    "side_or_back": (
        "轻微转身带出侧面或背面廓形",
        "一步未落稳的自然抓拍",
        "回头动作很小，身体比例稳定",
        "衣摆随转身轻微移动",
    ),
}

_GENERIC_SCENE_TEXT = {
    "正面全身",
    "自然动作",
    "半身细节",
    "上身细节",
    "侧面背面",
    "自然站姿",
    "自然站定",
    "自然穿搭抓拍",
}


def _is_generic_scene_text(value: Any, *, shot_class: str, label: str) -> bool:
    text = clean_text(value, max_len=160)
    if not text:
        return True
    normalized = text.lower()
    if normalized in {shot_class.lower(), clean_text(label, max_len=160).lower()}:
        return True
    return text in _GENERIC_SCENE_TEXT


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


def coerce_string_list(
    value: Any, *, max_items: int = 8, max_len: int = 80
) -> list[str]:
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
    scene_strategy: str = "natural_series",
    scene_variety: str = "rich",
) -> list[dict[str, Any]]:
    category = clean_text(product_analysis.get("category"), max_len=80) or "服饰"
    base_family = _TEMPLATE_FAMILY.get(template, template)
    if scene_environment == "outdoor" and base_family in {
        "daily_life",
        "phone_snapshot",
    }:
        base_family = "outdoor_daily"
    accessories = coerce_string_list(accessory_plan.get("items"), max_items=4)
    cards: list[dict[str, Any]] = []
    for index, (shot_class, variant) in enumerate(shot_picks, start=1):
        label = clean_text(variant.get("label"), max_len=140) or shot_class
        family = _fallback_family(
            base_family=base_family,
            template=template,
            scene_environment=scene_environment,
            scene_strategy=scene_strategy,
            scene_variety=scene_variety,
            continuity_anchor=continuity_anchor,
            index=index,
            shot_class=shot_class,
            user_prompt=user_prompt,
        )
        camera = dict(_SHOT_CAMERA.get(shot_class, _SHOT_CAMERA["natural_pose"]))
        if aspect_ratio in {"16:9", "21:9", "4:3", "3:2"}:
            camera["orientation"] = "landscape"
        props = list(accessories)
        if (
            allow_pet
            and continuity_anchor == "pet"
            and family
            not in {
                "clean_ecommerce",
                "premium_studio",
            }
        ):
            props.append("低存在感宠物")
        card = {
            "id": f"fallback-{index:02d}-{shot_class}",
            "scene_family": family,
            "location": _fallback_location(
                template, scene_environment, category, family=family, index=index
            ),
            "micro_event": _fallback_micro_event(
                shot_class, label, family=family, category=category, index=index
            ),
            "camera": camera,
            "pose": _fallback_pose(shot_class, label, index=index),
            "motion": _motion_for_shot(shot_class, index=index),
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


def _stable_cycle(options: tuple[str, ...], *, index: int, seed: str) -> str:
    if not options:
        return ""
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    offset = int(digest[:8], 16) % len(options)
    return options[(offset + index - 1) % len(options)]


def _fallback_family(
    *,
    base_family: str,
    template: str,
    scene_environment: str,
    scene_strategy: str,
    scene_variety: str,
    continuity_anchor: str,
    index: int,
    shot_class: str,
    user_prompt: str,
) -> str:
    if template == "white_ecommerce" and scene_variety == "safe":
        return base_family
    if continuity_anchor == "location_series" and scene_variety == "safe":
        return base_family
    if scene_strategy == "editorial_campaign":
        pool = _FALLBACK_FAMILY_POOLS["editorial"]
    elif scene_environment == "outdoor":
        pool = _FALLBACK_FAMILY_POOLS["outdoor_rich"]
    elif scene_variety in {"rich", "wild"} and template != "white_ecommerce":
        pool = _FALLBACK_FAMILY_POOLS["indoor_rich"]
    else:
        return base_family
    return _stable_cycle(
        pool,
        index=index,
        seed=f"{template}|{scene_environment}|{scene_strategy}|{scene_variety}|{shot_class}|{user_prompt}",
    )


def _fallback_location(
    template: str,
    scene_environment: str,
    category: str,
    *,
    family: str | None = None,
    index: int = 1,
) -> str:
    family_key = family or _TEMPLATE_FAMILY.get(template, template)
    locations = _FALLBACK_LOCATION_POOLS.get(family_key)
    if locations:
        return _stable_cycle(
            locations,
            index=index,
            seed=f"{template}|{scene_environment}|{category}|{family_key}",
        )
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


def _fallback_micro_event(
    shot_class: str,
    label: str,
    *,
    family: str,
    category: str,
    index: int,
) -> str:
    events = _FALLBACK_EVENTS_BY_SHOT.get(shot_class, ())
    event = _stable_cycle(
        events,
        index=index,
        seed=f"{family}|{category}|{shot_class}|event",
    )
    return event or label


def _fallback_pose(shot_class: str, label: str, *, index: int) -> str:
    poses = _FALLBACK_POSES_BY_SHOT.get(shot_class, ())
    pose = _stable_cycle(poses, index=index, seed=f"{shot_class}|pose")
    return pose or label


def _fallback_lighting(template: str, scene_environment: str) -> str:
    if template in {"white_ecommerce", "premium_studio"}:
        return "柔和可控的商业摄影光，服装细节清楚"
    if scene_environment == "outdoor" or template == "urban_commute":
        return "户外自然侧光或斜上光，真实阴影和空间深度"
    return "自然窗光或柔和室内暖光，方向明确不过曝"


def _motion_for_shot(shot_class: str, *, index: int = 1) -> str:
    motions = _FALLBACK_MOTIONS_BY_SHOT.get(shot_class, ())
    motion = _stable_cycle(motions, index=index, seed=f"{shot_class}|motion")
    if motion:
        return motion
    return "小幅自然动作，商品主体完整"


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
        scene_strategy=scene_strategy,
        scene_variety=scene_variety,
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
        "fallback_guardrails": {
            "do_not_copy": "不要照抄模板 shot label；你需要重新导演每张图的真实地点、事件、动作和机位。",
            "safe_if_needed": "如果上游失败，本地规则才会兜底；正常情况下以你的 SceneCard 为准。",
        },
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
    scene_strategy: str = "natural_series",
    scene_variety: str = "rich",
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
        scene_strategy=scene_strategy,
        scene_variety=scene_variety,
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
        "像真实拍摄分镜的 SceneCards。场景、姿势、微动作、镜头全部由你决定，"
        "不要照抄 shot_plan 的标签或 fallback 文案。必须只输出 JSON 对象，不要 Markdown。\n"
        f"scene_cards 必须正好 {output_count} 条，且第 i 条必须严格对应 "
        "shot_plan[i]，id 用 shot_plan[i].shot_class 加 '-' 加索引，例如 "
        "detail_half_body-3。禁止重排 shot_plan 顺序。\n"
        "字段：series_concept, continuity_anchors, scene_cards, risk_notes。\n"
        "每个 scene_card 字段必须有 id, scene_family, location, micro_event, camera, "
        "pose, motion, props, lighting, composition, product_visibility, negative。\n"
        "camera 必须有 distance, angle, lens_feel, orientation。\n"
        "最高优先级：商品还原，不能改颜色、版型、领口、袖型、衣长、图案/logo、"
        "纽扣、口袋、缝线。动作和道具不得遮挡商品主体。"
        "每张 micro_event 必须是具体生活事件，不能直接复制 variant_label 或写成"
        "正面全身/自然动作/自然站姿。camera angle/distance、地点、身体重心、"
        "手部动作至少两项要变化，禁止整批退回普通棚拍站姿。"
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
        "必须保留 scene_card 的 location、micro_event、pose、motion、camera，不得简化成普通站姿。"
        "SceneCard 是本张唯一场景来源：不要把 base_prompt、template 或 shot label 里的其它地点、"
        "花坛、街边、棚拍、户外/室内光线等混进 final_prompt，除非它们已经出现在 scene_card。"
        "本张只要求清楚呈现当前镜头能看到的商品细节；半身/上身近景不要强求背后、裙摆、"
        "全身廓形等不可见细节。"
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
        shot_class = shot_picks[index][0] if index < len(shot_picks) else ""
        shot_label = (
            clean_text(shot_picks[index][1].get("label"), max_len=160)
            if index < len(shot_picks) and isinstance(shot_picks[index][1], dict)
            else ""
        )
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
            "product_visibility": clean_text(raw.get("product_visibility"), max_len=80)
            or fallback.get("product_visibility")
            or "front_full_body",
            "negative": coerce_string_list(
                raw.get("negative"), max_items=8, max_len=100
            )
            or list(fallback.get("negative") or []),
            "source": "gpt55",
        }
        if _is_generic_scene_text(
            card.get("micro_event"), shot_class=shot_class, label=shot_label
        ):
            card["micro_event"] = fallback.get("micro_event") or card["micro_event"]
        if _is_generic_scene_text(
            card.get("pose"), shot_class=shot_class, label=shot_label
        ):
            card["pose"] = fallback.get("pose") or card["pose"]
        if _is_generic_scene_text(
            card.get("motion"), shot_class=shot_class, label=shot_label
        ):
            card["motion"] = fallback.get("motion") or card["motion"]
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
    raw_providers = await get_setting(db, spec_providers) if spec_providers else None
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
        timeout=httpx.Timeout(connect=8.0, read=70.0, write=30.0, pool=8.0),
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
