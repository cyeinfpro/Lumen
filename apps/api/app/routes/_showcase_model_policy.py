from __future__ import annotations

import re
from typing import Any


def _infer_age(text: str) -> int | None:
    lowered = (text or "").lower()
    age_match = re.search(r"(\d{1,2})\s*(?:岁|-year-old|year old|yo)", lowered)
    if age_match:
        try:
            return int(age_match.group(1))
        except ValueError:
            return None
    return None


def _infer_model_height_cm(text: str) -> int:
    age = _infer_age(text)
    if age is None:
        lowered = (text or "").lower()
        if any(
            word in lowered
            for word in ("儿童", "童装", "小朋友", "孩子", "kid", "kids", "child")
        ):
            return 128
        return 168
    if age <= 2:
        return 90
    if age <= 12:
        return 80 + age * 6
    if age <= 17:
        return min(168, 128 + (age - 12) * 7)
    return 168


def _height_requirement(text: str) -> str:
    height_cm = _infer_model_height_cm(text)
    return (
        f"Keep the model's perceived height around {height_cm}cm, with consistent "
        "head-to-body ratio, limb length, and scale across all reference views and later images."
    )


def _age_direction(text: str) -> str:
    lowered = (text or "").lower()
    age = _infer_age(text)
    if (
        age is not None
        and age <= 12
        or any(
            word in lowered
            for word in ("儿童", "童装", "小朋友", "孩子", "kid", "kids", "child")
        )
    ):
        age_text = f"around {age} years old" if age is not None else "child age range"
        return (
            f"The model should be {age_text}, with age-appropriate body proportion, face, "
            "posture, expression, and styling. Keep the look natural and non-adultized."
        )
    if age is not None and age < 18:
        return (
            f"The model should be around {age} years old, with teen-appropriate proportions, "
            "expression, pose, and styling."
        )
    if age is not None:
        return (
            f"The model should look around {age} years old, with expression, pose, styling, "
            "and scene adjusted to that age."
        )
    return (
        "Follow the user's requested model type, age impression, expression, pose, and styling; "
        "if unspecified, choose a natural ecommerce model that fits the garment."
    )


def _accessory_age_direction(text: str) -> str:
    lowered = (text or "").lower()
    age = _infer_age(text)
    if (
        age is not None
        and age <= 12
        or any(
            word in lowered
            for word in ("儿童", "童装", "小朋友", "孩子", "kid", "kids", "child")
        )
    ):
        return (
            "Accessory styling must be child-appropriate: simple, safe-looking, playful but restrained, "
            "with no adult jewelry styling, glamour accessories, mature handbags, heels, or adult fashion cues."
        )
    if (
        age is not None
        and age < 18
        or any(word in lowered for word in ("青少年", "teen", "teenager"))
    ):
        return "Accessory styling must fit a teenager: casual, age-appropriate, not childish, and not adult glamour."
    if age is not None:
        return (
            f"Accessory styling must match an adult around {age} years old: commercially polished, natural, "
            "and appropriate for that age and product category."
        )
    return (
        "Infer the target age from the user's direction and product context, then choose accessories that match "
        "that age group instead of assuming the model is always a child."
    )


def _accessory_strength_direction(strength: str) -> str:
    if strength == "strong":
        return "更明显但仍克制，必须服务整体造型，不要压过模特身份和后续服装主体"
    if strength == "medium":
        return "中等存在感，清楚可见但不要主导画面"
    return "低存在感，近看可辨认，远看不抢主体"


_FACE_ARCHETYPES_FEMALE: tuple[str, ...] = (
    "oval face, almond eyes, straight nose, full lips, long straight hair, slim build",
    "round face, narrow long eyes, small upturned nose, subtle lips, short bob, soft standard build",
    "heart-shaped face, wide round eyes, petite nose, plump lips, medium wavy hair, tall slim build",
    "long oval face, sharp upturned eyes, high-bridge nose, balanced lips, long straight hair with highlights, tall lean build",
    "square face with strong jaw, deep-set eyes, straight nose, fuller lips, short curly hair, athletic build",
    "soft round face, relaxed almond eyes, rounded nose, subtle lips, long straight side-part hair, softly curvy build",
    "diamond face, monolid sharp eyes, narrow nose, thin lips, low ponytail, lean dancer-like build",
    "oblong face, double-eyelid almond eyes, medium straight nose, natural lips, shoulder-length wavy hair, willowy build",
)


_FACE_ARCHETYPES_MALE: tuple[str, ...] = (
    "oval face, calm double-eyelid eyes, straight nose, balanced lips, short side-part hair, lean tall build",
    "square face with strong jaw, sharp focused eyes, high-bridge nose, firm lips, short crew cut, broad athletic build",
    "long oval face, deep-set eyes, slim straight nose, neutral lips, medium-length wavy hair, tall slender build",
    "round face, friendly bright eyes, rounded nose, fuller lips, short messy textured hair, standard build",
    "diamond face, monolid eyes, narrow nose, thin lips, slicked-back hair, lean editorial build",
    "rectangular face, focused upturned eyes, defined nose, balanced lips, short undercut, fit toned build",
    "heart-shaped face, almond eyes, petite nose, soft lips, ear-length tousled hair, slim build",
    "oblong face, deep almond eyes, medium straight nose, natural lips, short side-part hair, tall lean build",
)


def _infer_candidate_gender(
    style_prompt: str,
    product_analysis: dict[str, Any],
) -> str:
    """从风格描述 + 商品分类粗判性别；找不到信号就默认 female。

    英文只匹配独立词，避免 female 之类的词误触发 male。
    """
    text = " ".join(
        [style_prompt or "", str(product_analysis.get("category") or "")]
    ).lower()
    if any(token in text for token in ("女装", "女性", "女士", "女生", "女童")) or any(
        re.search(pattern, text)
        for pattern in (
            r"\bfemale\b",
            r"\bwomen\b",
            r"\bwoman\b",
            r"\bgirl\b",
            r"\bwomenswear\b",
        )
    ):
        return "female"
    if any(token in text for token in ("男装", "男性", "男士", "男生", "男童")) or any(
        re.search(pattern, text)
        for pattern in (
            r"\bmale\b",
            r"\bmen\b",
            r"\bman\b",
            r"\bboy\b",
            r"\bmenswear\b",
        )
    ):
        return "male"
    return "female"


def _model_diversity_anchor(
    *,
    candidate_index: int,
    gender: str | None,
    age_segment: str | None = None,
) -> str:
    """按 candidate_index 取一组差异化外貌锚点，避免多张候选收敛到同一张 AI 通用脸。

    toddler / child 用引导句，避免成人 archetype（如 ponytail、dancer build）套小孩的违和感。
    """
    if age_segment in {"toddler", "child"}:
        return (
            "Make this candidate visibly different from other candidates "
            "in face shape, hair length, and body type."
        )
    pool = (
        _FACE_ARCHETYPES_MALE
        if (gender or "").lower() == "male"
        else _FACE_ARCHETYPES_FEMALE
    )
    archetype = pool[(max(candidate_index, 1) - 1) % len(pool)]
    return (
        f"Look anchor for this candidate: {archetype}. "
        "Stay visibly distinct from other candidates; "
        "hair color and skin tone should follow the appearance direction."
    )


def _style_region_from_text(text: str) -> str:
    if any(token in text for token in ("东亚", "亚洲", "日系", "韩系", "中式")):
        return "亚洲"
    for region in ("欧美", "亚洲", "拉美", "中东", "非洲"):
        if region in text:
            return region
    return "自然商业摄影"


def _compact_showcase_user_direction(text: str, style_region: str) -> str:
    direction = (text or "").strip()
    if not direction:
        return ""
    for token in (
        f"外貌方向：{style_region}",
        style_region,
        "模特姿势生动活泼有活力",
        "姿势生动活泼有活力",
        "生动活泼有活力",
        "全身照",
        "自然走动回头",
        "走动回头",
        "自然走动",
        "走动",
        "回头",
    ):
        direction = direction.replace(token, "")
    direction = re.sub(r"[，,、；;\s]+", "，", direction).strip("，,、；; ")
    return direction[:60]
