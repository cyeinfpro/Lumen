"""Pure, deterministic scene-planning fallbacks and normalization helpers."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable


__all__ = [
    "_SHOT_CAMERA",
    "_TEMPLATE_FAMILY",
    "_FALLBACK_FAMILY_POOLS",
    "_FALLBACK_LOCATION_POOLS",
    "_FALLBACK_ENVIRONMENT_DETAILS",
    "_FALLBACK_EVENTS_BY_SHOT",
    "_FALLBACK_POSES_BY_SHOT",
    "_FALLBACK_MOTIONS_BY_SHOT",
    "_GENERIC_SCENE_TEXT",
    "_is_generic_scene_text",
    "clean_text",
    "clean_string_list",
    "coerce_string_list",
    "coerce_bool",
    "_dict_or_empty",
    "_GENERIC_PRODUCT_KEYWORDS",
    "_append_product_keyword",
    "compact_product_context_for_gpt55",
    "build_garment_lock",
    "_visibility_priority",
    "fallback_scene_cards_from_pool",
    "_stable_cycle",
    "_fallback_family",
    "_fallback_location",
    "_fallback_micro_event",
    "_fallback_pose",
    "_fallback_lighting",
    "_fallback_environment_detail",
    "_fallback_lighting_detail",
    "_camera_detail_for_shot",
    "_composition_detail_for_shot",
    "_creative_intent_for_shot",
    "_natural_detail_for_shot",
    "_motion_for_shot",
    "_composition_for_shot",
    "_fallback_scene_card_shooting_brief",
    "_product_visibility_for_shot",
    "scene_fingerprint",
    "_sanitize_shooting_brief",
]


def _sanitize_shooting_brief(value: Any, *, max_len: int = 1800) -> str:
    text = clean_text(value, max_len=max_len)
    return (
        text.replace("SceneCard", "本张拍摄方案")
        .replace("scene_card", "拍摄方案")
        .replace("shot_plan", "拍摄计划")
        .replace("final_prompt", "拍摄方案")
    )


_SHOT_CAMERA = {
    "front_full_body": {
        "distance": "full_body",
        "angle": "eye_level",
        "lens_feel": "natural_standard",
        "orientation": "vertical",
    },
    "natural_pose": {
        "distance": "full_body",
        "angle": "front_three_quarter",
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

_FALLBACK_ENVIRONMENT_DETAILS: dict[str, tuple[str, ...]] = {
    "clean_ecommerce": (
        "背景只保留浅色墙面和地面交界线，空间干净但不死白",
        "地面有轻微真实阴影，服装边缘和背景分离清楚",
        "画面没有多余陈列，留出呼吸感让商品成为第一视觉",
    ),
    "premium_studio": (
        "灰白墙面、木地板和远处柔和墙角形成真实空间深度",
        "背景有极简家具或墙面转角的低存在感层次，不抢服装",
        "地板反光很弱，人物脚下有自然落影，棚拍空间不空洞",
    ),
    "urban_street": (
        "远处店招、橱窗或街沿虚化成城市层次，主体周围保持干净",
        "地面斑马线、路缘或玻璃反光提供生活感，但不贴近衣服主体",
        "背景路人只在远处虚化出现，画面重点仍在模特和服装",
    ),
    "outdoor_daily": (
        "树影、步道和远处建筑形成轻微纵深，背景不过度杂乱",
        "脚下地面和身后绿植有真实距离，人物不会贴在背景上",
        "远景保留日常环境线索，前景不遮挡服装主体",
    ),
    "designed_lifestyle": (
        "室内软装、墙面和地面材质形成生活质感，布置克制干净",
        "背景只保留一两个低存在感物件，空间真实但不喧宾夺主",
        "人物离背景有一小段距离，形成自然景深和空间层次",
    ),
    "daily_life": (
        "生活物件保持在远处或边缘，像真实家居随手拍但不凌乱",
        "窗边、地面和家具边线形成真实室内空间，不做空白棚拍",
        "背景有轻微日常痕迹，所有道具都避开服装主体",
    ),
    "phone_snapshot": (
        "背景有轻微手机抓拍的不完美边缘和真实空间线索",
        "远处环境自然虚化，主体周围不堆道具",
        "画面像朋友随手记录，保留真实距离和轻微生活感",
    ),
    "social_seeding": (
        "背景有精品店或咖啡店的低存在感氛围，主体周围干净",
        "玻璃、绿植或门框只作为边缘层次，不遮挡衣服",
        "空间有轻微打卡感，但动作保持日常自然",
    ),
}

_FALLBACK_EVENTS_BY_SHOT: dict[str, tuple[str, ...]] = {
    "front_full_body": (
        "从画面外小步走进光线里，刚被镜头叫住",
        "顺着场景向前走近两步，在脚步落地瞬间抬眼",
        "从门口轻快走出，身体还带着向前的惯性",
        "穿过光影区域时回头看向镜头，脚步还没完全停稳",
        "沿着背景线条小跑后放慢，手臂自然摆动但不遮挡衣服",
    ),
    "natural_pose": (
        "向镜头走近时突然被叫住，眼神刚转回来",
        "绕过场景边缘半步转身，衣摆跟着轻轻摆动",
        "和镜头外的人回应后笑着继续往前走",
        "一只手在身体侧边带起衣摆外缘，避开商品主体",
        "沿着场景向前走时被高速快门自然抓拍",
    ),
    "detail_half_body": (
        "抬手轻整理袖口，胸前和领口保持清楚",
        "手指从衣摆外缘掠过展示面料垂感",
        "肩颈放松地看向一侧，衣领细节清楚",
        "低头检查纽扣或拉链，手不压住主体",
        "自然抬臂把发丝拨到肩后，手臂避开胸前图案",
    ),
    "side_or_back": (
        "侧身迈上一步时自然回头，脚步仍在移动",
        "从座位旁起身转向镜头，衣摆有轻微摆动",
        "看向橱窗时半转身体，侧面轮廓被光线勾出",
        "转过街角前被叫住，身体还保留转身惯性",
        "背向前走半步后自然回望，后脚刚离开地面",
    ),
}

_FALLBACK_POSES_BY_SHOT: dict[str, tuple[str, ...]] = {
    "front_full_body": (
        "一脚刚落在前方的全身动态姿态，肩颈放松",
        "身体三分之二正面向镜头走近，双手在低位自然摆动",
        "重心正从后脚转到前脚，前脚轻点地面",
        "步伐刚停下但身体仍有前进惯性的全身姿态",
    ),
    "natural_pose": (
        "身体三分之二正面移动中回头，头部自然看向镜头附近",
        "轻快前行动作，手部保持低位并避开商品主体",
        "上半身放松向前带动，视线偏离镜头一点",
        "正面微侧的小幅移动姿态，脚步方向和视线形成张力",
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
        "脚步落地的瞬间被凝住，衣摆有清楚的自然摆动",
        "身体向镜头前进的惯性仍在，重心真实可见",
        "从移动到停下的半拍，动作幅度中等但商品主体清楚",
        "手臂随步伐自然摆到身体两侧，避开胸前和图案区域",
    ),
    "natural_pose": (
        "小步前行中的自然定格，脚尖和衣摆都带出方向感",
        "正面微侧移动带出衣服褶皱和身体节奏",
        "手部低位随步伐摆动，主体不被遮挡",
        "视线和身体方向不同步的高速抓拍感",
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


def _dict_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


_GENERIC_PRODUCT_KEYWORDS = {
    "unknown",
    "需人工复核",
    "服饰",
    "颜色",
    "版型",
    "款式",
    "廓形",
    "领口",
    "袖型",
    "衣长",
    "面料观感",
    "图案/logo",
    "纽扣/拉链/口袋/缝线",
    "可见商品细节",
}


def _append_product_keyword(out: list[str], value: Any, *, max_len: int = 50) -> None:
    text = clean_text(value, max_len=max_len)
    if not text or text.lower() == "unknown" or text in _GENERIC_PRODUCT_KEYWORDS:
        return
    if text not in out:
        out.append(text)


def compact_product_context_for_gpt55(
    product_analysis: dict[str, Any],
    garment_lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Small product hint for GPT-5.5 director/composer prompts.

    Full product fidelity constraints are appended later by the deterministic
    workflow prompt layer. GPT-5.5 only needs enough product context to choose a
    compatible scene and avoid blocking the visible garment area.
    """

    garment_lock = garment_lock or {}
    category = (
        clean_text(product_analysis.get("category"), max_len=80)
        or clean_text(garment_lock.get("category"), max_len=80)
        or clean_text(garment_lock.get("core_identity"), max_len=80)
        or "服饰"
    )
    keywords: list[str] = []
    for key in ("color", "material_guess", "silhouette"):
        _append_product_keyword(keywords, product_analysis.get(key))
    for key in ("key_details", "must_preserve"):
        for item in coerce_string_list(
            product_analysis.get(key), max_items=6, max_len=50
        ):
            _append_product_keyword(keywords, item)
            if len(keywords) >= 5:
                break
        if len(keywords) >= 5:
            break
    if not keywords:
        for item in coerce_string_list(
            garment_lock.get("must_preserve"), max_items=5, max_len=50
        ):
            _append_product_keyword(keywords, item)
            if len(keywords) >= 5:
                break
    return {
        "category": category,
        "visual_keywords": keywords[:5],
        "scene_fit_hint": clean_text(
            product_analysis.get("background_recommendation"), max_len=140
        ),
        "role": "只作为服装风格和可见区域提示；不要输出商品还原条款或细节清单。",
    }


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
            "environment_detail": _fallback_environment_detail(family, index=index),
            "lighting_detail": _fallback_lighting_detail(
                template, scene_environment, family=family, index=index
            ),
            "camera_detail": _camera_detail_for_shot(shot_class, camera, index=index),
            "composition_detail": _composition_detail_for_shot(
                shot_class, aspect_ratio=aspect_ratio, index=index
            ),
            "creative_intent": _creative_intent_for_shot(
                shot_class, family=family, index=index
            ),
            "natural_detail": _natural_detail_for_shot(shot_class, index=index),
            "negative": [
                "不要改变商品颜色、版型、图案/logo、纽扣、口袋或缝线",
                "不要让手、包带、头发、宠物或道具遮挡商品主体",
                "不要夸张摆拍或让场景抢主体",
            ],
            "source": "rules_fallback",
            "user_direction": clean_text(user_prompt, max_len=120),
        }
        card["shooting_brief"] = _fallback_scene_card_shooting_brief(
            card,
            shot_class=shot_class,
        )
        card["fingerprint"] = scene_fingerprint(card)
        cards.append(card)
    return cards


def _stable_cycle(options: tuple[str, ...], *, index: int, seed: str) -> str:
    if not options:
        return ""
    digest = hashlib.sha1(
        seed.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
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


def _fallback_environment_detail(family: str, *, index: int) -> str:
    options = (
        _FALLBACK_ENVIRONMENT_DETAILS.get(family)
        or _FALLBACK_ENVIRONMENT_DETAILS["designed_lifestyle"]
    )
    return _stable_cycle(options, index=index, seed=f"{family}|environment")


def _fallback_lighting_detail(
    template: str,
    scene_environment: str,
    *,
    family: str,
    index: int,
) -> str:
    if template in {"white_ecommerce", "premium_studio"} or family in {
        "clean_ecommerce",
        "premium_studio",
    }:
        options = (
            "主光从左前方或侧窗方向落下，脸部和衣服有柔和明暗层次",
            "柔光不过曝，肩线、裙摆和地面落影都能看出真实方向",
            "光线干净但不全脸均匀，衣服纹理和缝线有轻微高光",
        )
    elif scene_environment == "outdoor" or family in {"urban_street", "outdoor_daily"}:
        options = (
            "自然侧光穿过建筑或树影，脸部一侧略亮一侧略暗",
            "地面和背景有真实投影，衣服边缘被自然光勾出轮廓",
            "日光方向明确但不过曝，皮肤和布料都保留真实明暗变化",
        )
    else:
        options = (
            "窗边柔光从斜前方进入，墙面和地面有轻微渐变阴影",
            "室内暖光和自然光混合但不偏色，衣服颜色保持准确",
            "背景略暗一档，主体服装细节保持清楚",
        )
    return _stable_cycle(options, index=index, seed=f"{template}|{family}|lighting")


def _camera_detail_for_shot(
    shot_class: str, camera: dict[str, Any], *, index: int
) -> str:
    angle = clean_text(camera.get("angle"), max_len=40)
    distance = clean_text(camera.get("distance"), max_len=40)
    if shot_class == "detail_half_body":
        options = (
            "镜头靠近到半身距离，视角稳定不过度广角，手部动作和商品细节同框",
            "平视或轻微胸口高度机位，背景轻微虚化，服装细节优先清晰",
            "近距离手机抓拍感但透视自然，肩膀和手肘不顶边",
        )
    elif shot_class == "side_or_back":
        options = (
            "全身侧后角度，镜头与人物保持真实距离，头脚完整不切断",
            "平视全身机位，身体转动方向清楚，背面结构和侧面廓形都能读出来",
            "镜头略偏人物行进方向，保留回望空间和脚下落点",
        )
    elif shot_class == "natural_pose":
        options = (
            "手持标准镜头距离，人物占画面主要高度，动作像刚被定格",
            "镜头略跟随人物移动，保留一点环境但不压过衣服",
            "平视三分之二正面机位，身体重心和步伐方向可信",
        )
    else:
        options = (
            "全身标准距离，头脚完整，镜头不拉长头身比",
            "平视机位，人物竖向落在画面中轴附近，服装整体清楚",
            "自然标准镜头，背景留白适度，主体比例真实",
        )
    detail = _stable_cycle(options, index=index, seed=f"{shot_class}|camera")
    if angle or distance:
        return f"{detail}；基础机位为 {distance or '自然距离'}、{angle or '自然视角'}"
    return detail


def _composition_detail_for_shot(
    shot_class: str, *, aspect_ratio: str, index: int
) -> str:
    if shot_class == "detail_half_body":
        options = (
            "画面从头顶到大腿上方留边，胸前、领口、袖口和手部动作都在清晰区域",
            "模特略落在一侧三分线，另一侧留出干净背景呼吸感",
            "前景最多只有轻微虚化边缘，不压住商品主体",
        )
    elif shot_class == "side_or_back":
        options = (
            "人物完整落在画面中部，转身方向一侧留出空间，背面结构不被头发遮挡",
            "侧后廓形和脚下落点同时可见，背景线条不要穿过头部或衣服主体",
            "全身构图稳定，肩背、腰线、裙摆或裤脚都有清楚边界",
        )
    elif shot_class == "natural_pose":
        options = (
            "人物占画面约六成高度，环境只提供生活线索，服装始终是视觉中心",
            "动作方向前方留白，手臂和道具避开胸前主体",
            "轻微抓拍偏移但不歪斜，头顶和脚下都有自然边距",
        )
    else:
        options = (
            "竖构图头脚完整，肩部和脚下都不贴边，商品主体占画面主要面积",
            "人物轻微偏离中线形成自然商业摄影构图，对侧保留呼吸感",
            "背景线条简洁，视线先落在服装颜色、版型和主要细节上",
        )
    detail = _stable_cycle(options, index=index, seed=f"{shot_class}|composition")
    return f"{detail}；适配 {aspect_ratio} 画幅"


def _creative_intent_for_shot(shot_class: str, *, family: str, index: int) -> str:
    options_by_shot = {
        "front_full_body": (
            "用克制的环境肖像感呈现服装，像真实生活里被光线和空间自然托住的一瞬",
            "把完整廓形放进有呼吸感的空间关系里，让商品准确同时画面有安静张力",
            "用决定性停步瞬间代替摆拍，让人物、光线和服装线条形成自然平衡",
        ),
        "natural_pose": (
            "抓住动作半拍之间的真实停顿，画面像生活纪实而不是模特指令",
            "用身体重心和环境留白制造轻微叙事感，让服装成为生活片段的一部分",
            "让人物和背景形成自然关系，保留一点不完美的抓拍边缘来增加真实感",
        ),
        "detail_half_body": (
            "用近距离观察感呈现衣料和手部微动作，像摄影师捕捉到的安静细节",
            "把商品细节放在真实光线和皮肤质感里，避免硬说明式特写",
            "让手指、布料受力和表情构成一个小叙事，而不是单纯展示局部",
        ),
        "side_or_back": (
            "用转身回望的瞬间制造侧背面廓形张力，像被自然叫住的真实片刻",
            "把背面结构和空间方向放在同一条视觉动线上，画面有作品感但不夸张",
            "用肩背线条、脚步落点和光影边缘建立安静的摄影叙事",
        ),
    }
    options = options_by_shot.get(shot_class) or options_by_shot["natural_pose"]
    intent = _stable_cycle(options, index=index, seed=f"{shot_class}|creative")
    if family in {"urban_street", "outdoor_daily", "phone_snapshot"}:
        return f"{intent}，保留街头或户外的偶然性和真实空气感"
    if family in {"premium_studio", "clean_ecommerce"}:
        return f"{intent}，构图和光线要像高级摄影作品但仍然服务商品"
    return f"{intent}，场景质感要丰富但不抢衣服主体"


def _natural_detail_for_shot(shot_class: str, *, index: int) -> str:
    options_by_shot = {
        "front_full_body": (
            "表情像刚停下来被叫住，嘴角和眼神放松，不做夸张营业笑",
            "手指自然弯曲，肩颈放松，衣摆和袖口有真实垂坠褶皱",
            "站姿有轻微重心差，脚尖方向和身体朝向不完全一致",
        ),
        "natural_pose": (
            "动作停在半拍之间，眼神没有刻意盯镜头，像真实生活抓拍",
            "手部保持低位或轻触边缘，身体有小幅移动带来的自然褶皱",
            "头发和衣服边缘有细小飞散感，但不遮挡商品关键区域",
        ),
        "detail_half_body": (
            "手指只是轻轻整理，不按压、不挡住胸前主体，表情专注但自然",
            "近景保留儿童或真人的自然皮肤和碎发，服装细节不被美颜抹平",
            "肩颈和手腕有真实微动作，布料受力和褶皱方向可信",
        ),
        "side_or_back": (
            "回望幅度很小，像走动中自然被叫住，不做舞台式扭身",
            "头发避开背部关键结构，肩背和背带/后片轮廓清楚",
            "脚步有前后落差，衣摆随转身产生轻微自然摆动",
        ),
    }
    options = options_by_shot.get(shot_class) or options_by_shot["natural_pose"]
    return _stable_cycle(options, index=index, seed=f"{shot_class}|natural")


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


def _fallback_scene_card_shooting_brief(
    card: dict[str, Any],
    *,
    shot_class: str,
) -> str:
    camera = _dict_or_empty(card.get("camera"))
    camera_seed = "，".join(
        clean_text(item, max_len=50)
        for item in (
            camera.get("distance"),
            camera.get("angle"),
            camera.get("lens_feel"),
        )
        if str(item or "").strip()
    )
    view_rule = (
        "保持正面或三分之二正面，脸和当前角度商品主体清楚"
        if shot_class != "side_or_back"
        else "侧面或背面廓形清楚，人物完整不切断"
    )
    location = clean_text(card.get("location"), max_len=90)
    micro_event = clean_text(card.get("micro_event"), max_len=120)
    pose = clean_text(card.get("pose"), max_len=110)
    motion = clean_text(card.get("motion"), max_len=110)
    camera_detail = clean_text(card.get("camera_detail"), max_len=160)
    natural_detail = clean_text(card.get("natural_detail"), max_len=160)
    camera_items = [
        item for item in (camera_seed or "自然标准镜头", camera_detail) if item
    ]
    natural_items = [item for item in (natural_detail, view_rule) if item]
    parts = [
        f"{'，'.join(item for item in (location, micro_event) if item)}。",
        f"{'，'.join(item for item in (pose, motion) if item)}。",
        f"{'；'.join(camera_items)}。",
        f"{clean_text(card.get('lighting_detail'), max_len=160)}",
        f"{clean_text(card.get('composition_detail'), max_len=160)}",
        f"{clean_text(card.get('creative_intent'), max_len=160)}。",
        f"{'；'.join(natural_items)}。",
    ]
    return _sanitize_shooting_brief(
        "".join(part for part in parts if part.strip("，。；")),
        max_len=900,
    )


def _product_visibility_for_shot(shot_class: str) -> str:
    if shot_class == "detail_half_body":
        return "upper_body_detail"
    if shot_class == "side_or_back":
        return "side_or_back_silhouette"
    return "front_full_body"


def scene_fingerprint(card: dict[str, Any]) -> str:
    camera = _dict_or_empty(card.get("camera"))
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
