"""Showcase scene interpretation, framing, and visibility policy."""

from __future__ import annotations

import re
from typing import Any, Iterable

from .showcase_runtime import runtime as _runtime


def _showcase_scene_label(value: Any) -> str:
    text = str(value or "").strip()
    labels = {
        "clean_ecommerce": "简洁电商棚拍",
        "premium_studio": "高级棚拍",
        "designed_lifestyle": "设计感生活场景",
        "urban_street": "城市街拍",
        "daily_life": "日常生活场景",
        "outdoor_daily": "户外日常场景",
        "phone_snapshot": "手机抓拍感",
        "social_seeding": "种草分享场景",
        "full_body": "全身入镜",
        "half_body": "半身入镜",
        "upper_body": "上半身近景",
        "front_full_body": "正面全身",
        "front_three_quarter": "三分之二正面",
        "side_or_back": "侧面或背面角度",
        "side_or_back_silhouette": "侧面或背面廓形",
        "upper_body_detail": "上半身细节",
        "detail_half_body": "半身细节",
        "eye_level": "平视",
        "high_angle": "轻微俯拍",
        "low_angle": "低角度",
        "slight_side": "轻微侧向",
        "natural_standard": "自然标准镜头",
        "handheld_standard": "手持标准镜头",
        "phone": "手机抓拍镜头",
        "vertical": "竖构图",
        "landscape": "横构图",
    }
    return labels.get(text, text.replace("_", " "))


def _showcase_scene_card_direction(scene_card: dict[str, Any] | None) -> str:
    runtime = _runtime()
    if not isinstance(scene_card, dict):
        return ""
    camera = runtime._dict_or_empty(scene_card.get("camera"))
    props = scene_card.get("props")
    prop_line = (
        "、".join(str(item).strip() for item in props if str(item).strip())[:120]
        if isinstance(props, list)
        else ""
    )
    negative = scene_card.get("negative")
    negative_line = (
        "；".join(str(item).strip() for item in negative if str(item).strip())[:220]
        if isinstance(negative, list)
        else ""
    )

    def kv(label: str, value: Any) -> str:
        text = str(value or "").strip()
        return f"{label}：{text}" if text else ""

    camera_line = "，".join(
        runtime._showcase_scene_label(item)
        for item in (
            camera.get("distance"),
            camera.get("angle"),
            camera.get("lens_feel"),
        )
        if str(item or "").strip()
    )
    detail_parts = [
        kv("环境层次", scene_card.get("environment_detail")),
        kv("光线细节", scene_card.get("lighting_detail")),
        kv("镜头细节", scene_card.get("camera_detail")),
        kv("构图细节", scene_card.get("composition_detail")),
        kv("摄影意图", scene_card.get("creative_intent")),
        kv("自然细节", scene_card.get("natural_detail")),
    ]
    parts = [
        kv(
            "场景风格",
            runtime._showcase_scene_label(scene_card.get("scene_family")),
        ),
        kv("拍摄地点", scene_card.get("location")),
        kv("生活事件", scene_card.get("micro_event")),
        kv("镜头机位", camera_line),
        kv("动作姿势", scene_card.get("pose")),
        kv("动态瞬间", scene_card.get("motion")),
        kv("搭配道具", prop_line),
        kv("光线", scene_card.get("lighting")),
        *detail_parts,
        kv("构图", scene_card.get("composition")),
        kv(
            "本张商品呈现",
            runtime._showcase_scene_label(scene_card.get("product_visibility")),
        ),
        kv("禁令", negative_line),
    ]
    return "；".join(str(part).strip() for part in parts if str(part).strip())


def _showcase_scene_card_scene_direction(scene_card: dict[str, Any] | None) -> str:
    if not isinstance(scene_card, dict):
        return ""
    parts = [
        str(scene_card.get("location") or "").strip(),
        str(scene_card.get("micro_event") or "").strip(),
        str(scene_card.get("environment_detail") or "").strip(),
    ]
    creative_intent = str(scene_card.get("creative_intent") or "").strip()
    if creative_intent:
        parts.append(f"摄影意图：{creative_intent}")
    props = scene_card.get("props")
    if isinstance(props, list):
        prop_line = "、".join(str(item).strip() for item in props if str(item).strip())
        if prop_line:
            parts.append(f"可出现低存在感搭配道具：{prop_line}")
    return "；".join(part for part in parts if part)


def _showcase_scene_card_action_direction(scene_card: dict[str, Any] | None) -> str:
    if not isinstance(scene_card, dict):
        return ""
    parts = [
        str(scene_card.get("pose") or "").strip(),
        str(scene_card.get("motion") or "").strip(),
        str(scene_card.get("natural_detail") or "").strip(),
    ]
    return "；".join(part for part in parts if part)


def _showcase_scene_card_camera_direction(scene_card: dict[str, Any] | None) -> str:
    runtime = _runtime()
    if not isinstance(scene_card, dict):
        return ""
    camera = runtime._dict_or_empty(scene_card.get("camera"))
    camera_parts = [
        runtime._showcase_scene_label(camera.get("distance")),
        runtime._showcase_scene_label(camera.get("angle")),
        runtime._showcase_scene_label(camera.get("lens_feel")),
    ]
    parts = [
        " / ".join(part for part in camera_parts if part),
        str(scene_card.get("camera_detail") or "").strip(),
    ]
    return "；".join(part for part in parts if part)


def _showcase_scene_card_text(scene_card: dict[str, Any] | None) -> str:
    if not isinstance(scene_card, dict):
        return ""
    values: list[str] = []
    for key in (
        "scene_family",
        "location",
        "micro_event",
        "pose",
        "motion",
        "lighting",
        "composition",
        "product_visibility",
        "environment_detail",
        "lighting_detail",
        "camera_detail",
        "composition_detail",
        "creative_intent",
        "natural_detail",
    ):
        value = scene_card.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    camera = scene_card.get("camera")
    if isinstance(camera, dict):
        values.extend(
            str(value).strip() for value in camera.values() if str(value).strip()
        )
    for key in ("props", "negative"):
        value = scene_card.get(key)
        if isinstance(value, list):
            values.extend(str(item).strip() for item in value if str(item).strip())
    return " ".join(values)


def _text_has_any(text: str, tokens: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(token.lower() in lowered for token in tokens)


def _is_child_showcase(age_segment: str | None, model_summary: str = "") -> bool:
    if age_segment in {"toddler", "child"}:
        return True
    lowered = (model_summary or "").lower()
    return any(
        token in lowered
        for token in ("儿童", "童装", "小朋友", "孩子", "女童", "男童", "kid", "child")
    )


def _showcase_scene_render_direction(
    scene_card: dict[str, Any] | None,
    *,
    age_segment: str | None,
    model_summary: str,
) -> str:
    runtime = _runtime()
    lighting = ""
    if isinstance(scene_card, dict):
        lighting = str(scene_card.get("lighting") or "").strip()
        lighting_detail = str(scene_card.get("lighting_detail") or "").strip()
    else:
        lighting_detail = ""
    light_part = (
        f"严格按本张光线执行（{lighting}）" if lighting else "严格按本张光线执行"
    )
    if lighting_detail:
        light_part = f"{light_part}；{lighting_detail}"
    if runtime._is_child_showcase(age_segment, model_summary):
        return (
            f"真实自然儿童摄影质感，{light_part}；儿童肤质自然，有轻微真实皮肤纹理、"
            "自然红润和碎发，不成人化、不厚重磨皮；衣服布料纹理、明线和贴布细节真实，"
            "不要塑料感、AI美颜脸或过度商业棚拍感"
        )
    return (
        f"真实摄影质感，{light_part}；皮肤保留真实毛孔细纹和自然光泽，"
        "衣服布料纹理、缝线和褶皱真实；不要塑料感、过度磨皮、AI美颜脸或全脸均匀照明"
    )


def _showcase_scene_framing_direction(
    scene_card: dict[str, Any] | None,
    fallback: str,
) -> str:
    runtime = _runtime()
    text = runtime._showcase_scene_card_text(scene_card)
    if not text:
        return fallback
    composition_detail = ""
    if isinstance(scene_card, dict):
        composition_detail = str(scene_card.get("composition_detail") or "").strip()
    wants_hem = runtime._text_has_any(
        text,
        ("裙摆", "衣摆", "下摆", "hem"),
    )
    if runtime._text_has_any(text, ("full_body", "全身", "head_to_toe")):
        base = "按本张方案做全身构图，头脚完整、透视自然，商品整体廓形和主要细节清楚"
        return f"{base}；{composition_detail}" if composition_detail else base
    if wants_hem:
        base = (
            "按本张方案做半身到大腿上方构图，画面必须包含手部动作和被展示的衣摆/裙摆区域，"
            "不要裁掉正在展示的商品细节"
        )
        return f"{base}；{composition_detail}" if composition_detail else base
    if runtime._text_has_any(
        text,
        ("upper_body", "half_body", "close", "胸", "半身", "近景"),
    ):
        base = (
            "按本张方案做上半身或半身近景，头顶和肩肘留边，胸前、领口、袖口、"
            "口袋、纽扣/扣饰和图案/贴布细节清楚"
        )
        return f"{base}；{composition_detail}" if composition_detail else base
    return fallback


def _showcase_visibility_policy(
    *,
    garment_lock: dict[str, Any] | None,
    product_preserve: str,
    scene_card: dict[str, Any] | None,
    shot_type: str,
) -> tuple[str, str]:
    runtime = _runtime()
    preserve_items: list[str] = []
    if isinstance(garment_lock, dict) and isinstance(
        garment_lock.get("must_preserve"), list
    ):
        preserve_items = [
            str(item).strip()
            for item in garment_lock.get("must_preserve", [])
            if str(item).strip()
        ]
    if not preserve_items:
        preserve_items = [
            item.strip() for item in product_preserve.split("、") if item.strip()
        ]

    text = runtime._showcase_scene_card_text(scene_card)
    is_back_or_side = shot_type == "side_or_back" or runtime._text_has_any(
        text, ("背后", "后背", "背面", "后片", "侧面", "side", "back")
    )
    wants_hem = runtime._text_has_any(
        text,
        ("裙摆", "衣摆", "下摆", "衣长", "hem"),
    )
    wants_full_body = shot_type in {
        "front_full_body",
        "side_or_back",
    } or runtime._text_has_any(text, ("full_body", "全身", "head_to_toe"))
    is_upper_or_detail = shot_type == "detail_half_body" or runtime._text_has_any(
        text, ("upper_body", "half_body", "close", "胸", "半身", "近景")
    )
    upper_tokens = (
        "胸",
        "领",
        "袖",
        "肩",
        "背带",
        "口袋",
        "纽扣",
        "扣",
        "刺绣",
        "图案",
        "logo",
        "贴布",
        "小熊",
        "上衣",
        "前",
        "纹理",
        "明线",
        "缝线",
    )
    lower_tokens = ("裙", "裙摆", "衣摆", "下摆", "裤", "衣长", "廓形")
    back_detail_tokens = ("背后", "后背", "背面", "后片", "交叉", "蝴蝶结")
    front_detail_tokens = ("前胸", "正面胸", "胸口", "前片", "正面", "胸袋")
    side_visible_tokens = (
        "上衣",
        "裙身",
        "裙",
        "衣摆",
        "裙摆",
        "下摆",
        "背带",
        "袖",
        "领",
        "廓形",
        "纹理",
        "明线",
        "缝线",
        "牛仔布",
    )

    visible: list[str] = []
    deferred: list[str] = []
    for item in preserve_items:
        item_is_back_detail = runtime._text_has_any(item, back_detail_tokens)
        item_is_front_detail = runtime._text_has_any(item, front_detail_tokens)
        if is_back_or_side:
            if item_is_back_detail:
                visible.append(item)
            elif item_is_front_detail:
                deferred.append(item)
            elif runtime._text_has_any(item, side_visible_tokens):
                visible.append(item)
            elif runtime._text_has_any(item, lower_tokens) and (
                wants_hem or wants_full_body
            ):
                visible.append(item)
            else:
                deferred.append(item)
            continue
        if is_upper_or_detail:
            if item_is_back_detail:
                deferred.append(item)
            elif runtime._text_has_any(item, lower_tokens) and not wants_hem:
                deferred.append(item)
            elif runtime._text_has_any(item, upper_tokens) or len(visible) < 3:
                visible.append(item)
            else:
                deferred.append(item)
            continue
        if item_is_back_detail:
            deferred.append(item)
        else:
            visible.append(item)

    if not visible:
        priority = (
            garment_lock.get("visibility_priority")
            if isinstance(garment_lock, dict)
            else None
        )
        if isinstance(priority, list):
            visible = [str(item).strip() for item in priority if str(item).strip()]
    if not visible:
        visible = ["本张入镜的商品主体、领口、袖口、口袋/扣饰和布料纹理"]
    visible_text = "、".join(
        runtime._truncate_prompt_text(item, 30) for item in visible[:5]
    )
    deferred_text = "、".join(
        runtime._truncate_prompt_text(item, 30)
        for item in deferred[:3]
        if item not in visible
    )
    return visible_text, deferred_text


def _truncate_prompt_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[: max(0, limit)]
    return text[: limit - 1] + "…"


def _join_lock_items(value: Any, *, max_items: int = 4, max_len: int = 28) -> str:
    if not isinstance(value, list):
        return ""
    runtime = _runtime()
    return "、".join(
        runtime._truncate_prompt_text(str(item).strip(), max_len)
        for item in value[:max_items]
        if str(item).strip()
    )


def _compact_lock_text(value: Any, *, max_items: int = 4, max_len: int = 28) -> str:
    runtime = _runtime()
    if isinstance(value, list):
        return runtime._join_lock_items(
            value,
            max_items=max_items,
            max_len=max_len,
        )
    if isinstance(value, str):
        parts = [
            part.strip() for part in re.split(r"[、,，;；]+", value) if part.strip()
        ]
        return runtime._join_lock_items(
            parts,
            max_items=max_items,
            max_len=max_len,
        )
    return ""


def _compact_product_identity(
    garment_lock: dict[str, Any] | None,
    product_preserve: str,
) -> str:
    runtime = _runtime()
    if isinstance(garment_lock, dict):
        category = str(garment_lock.get("category") or "").strip()
        if category and category != "服饰":
            return runtime._truncate_prompt_text(category, 48)
        core = str(garment_lock.get("core_identity") or "").strip()
        if core:
            return runtime._truncate_prompt_text(
                core.split("、")[0].strip() or core,
                48,
            )
    first = product_preserve.split("、")[0].strip() if product_preserve else ""
    return runtime._truncate_prompt_text(first or "这件服饰", 48)
