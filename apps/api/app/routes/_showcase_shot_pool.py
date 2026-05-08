"""Shared types and helpers for the showcase shot variant pools.

Pool files (`_showcase_shot_pool_adult.py`, `_showcase_shot_pool_kids.py`) provide
the actual variant lists. This module owns the data structure, age-segment
routing, and the deterministic random selector.
"""

from __future__ import annotations

import hashlib
import random
from typing import Literal, TypedDict


ShotFraming = Literal["product_first", "tone_first"]


class ShotVariant(TypedDict):
    label: str
    framing: ShotFraming


ShotClass = Literal[
    "front_full_body",
    "natural_pose",
    "detail_half_body",
    "side_or_back",
]

Template = Literal[
    "white_ecommerce",
    "premium_studio",
    "urban_commute",
    "lifestyle",
    "daily_snapshot",
    "natural_phone_snapshot",
    "social_seed",
]

# 模特库年龄段（packages/core/lumen_core/schemas.py:462 ModelAgeSegment）
AgeSegment = Literal[
    "toddler",
    "child",
    "teen",
    "young_adult",
    "adult",
    "middle_aged",
    "senior",
]

# 池子按 3 段组织。teen/adult/middle_aged/senior 全部派生自 young_adult。
PoolBand = Literal["young_adult", "child", "toddler"]

ShotPool = dict[Template, dict[ShotClass, list[ShotVariant]]]


SHOT_CLASS_ORDER: tuple[ShotClass, ...] = (
    "front_full_body",
    "natural_pose",
    "detail_half_body",
    "side_or_back",
)


def resolve_pool_band(age_segment: AgeSegment | str | None) -> PoolBand:
    """模特库 7 段 → 3 个池子段。"""
    if age_segment == "toddler":
        return "toddler"
    if age_segment == "child":
        return "child"
    return "young_adult"


def age_soft_constraint(age_segment: AgeSegment | str | None) -> str:
    """teen/adult/middle_aged/senior 共享 young_adult 池，叠加这里的姿态约束。"""
    if age_segment == "teen":
        return "姿态略青春不要成熟感"
    if age_segment == "adult":
        return "姿态成熟稳重"
    if age_segment == "middle_aged":
        return "姿态从容不夸张"
    if age_segment == "senior":
        return "姿态温和稳重，避免戏剧化大动作"
    return ""


def shot_class_distribution(output_count: int) -> list[ShotClass]:
    """决定 N 张图分配到 4 类机位。

    1: 1 张正面全身商品展示
    2: 1 正面 + 1 动作
    4: 4 类各 1
    8: 4 类各 2
    16: 4 类各 4（用尽池子）
    其他: 按 4 类轮询补齐
    """
    if output_count <= 1:
        return ["front_full_body"]
    if output_count == 2:
        return ["front_full_body", "natural_pose"]
    if output_count == 4:
        return list(SHOT_CLASS_ORDER)
    if output_count == 8:
        return [c for c in SHOT_CLASS_ORDER for _ in range(2)]
    if output_count == 16:
        return [c for c in SHOT_CLASS_ORDER for _ in range(4)]
    plan: list[ShotClass] = []
    while len(plan) < output_count:
        plan.extend(SHOT_CLASS_ORDER)
    return plan[:output_count]


def _seed_from(*parts: str | int) -> int:
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def select_variants(
    *,
    pool: dict[ShotClass, list[ShotVariant]],
    plan: list[ShotClass],
    seed_key: str,
    min_product_first: int = 2,
) -> list[ShotVariant]:
    """按机位计划从池子里抽变体。

    - 池子小于 indices 时循环复用变体（保证返回长度等于 plan 长度）
    - 满足 min_product_first 张数（仅 N >= min_product_first 时强制）
    - 用 seed_key 做确定性随机，retry 同 task 同 shot 得到同结果
    """
    rng = random.Random(_seed_from(seed_key, "primary"))
    plan_count = len(plan)
    target_product = min(min_product_first, plan_count) if plan_count else 0

    grouped: dict[ShotClass, list[int]] = {}
    for idx, shot_class in enumerate(plan):
        grouped.setdefault(shot_class, []).append(idx)

    selections: list[ShotVariant | None] = [None] * plan_count
    product_used = 0

    for shot_class, indices in grouped.items():
        variants = list(pool.get(shot_class) or [])
        if not variants:
            continue
        product_pool = [v for v in variants if v["framing"] == "product_first"]
        tone_pool = [v for v in variants if v["framing"] == "tone_first"]
        rng.shuffle(product_pool)
        rng.shuffle(tone_pool)

        # 默认按池子原配比抽：product 概率 = len(product_pool) / len(variants)
        all_pool = product_pool + tone_pool
        rng.shuffle(all_pool)
        # 池子不够时按 cycle 循环复用，每 cycle 重新 shuffle 避免相邻重复
        filled: list[ShotVariant] = []
        while len(filled) < len(indices):
            cycle_pool = list(all_pool)
            rng.shuffle(cycle_pool)
            filled.extend(cycle_pool)
        for slot, var in zip(indices, filled):
            selections[slot] = var
            if var["framing"] == "product_first":
                product_used += 1

    if target_product and product_used < target_product:
        # 有机会就把 tone 替换成 product，从池子里挑没用过的
        used_labels = {s["label"] for s in selections if s is not None}
        for idx, current in enumerate(selections):
            if product_used >= target_product:
                break
            if current is None or current["framing"] == "product_first":
                continue
            shot_class = plan[idx]
            candidates = [
                v
                for v in (pool.get(shot_class) or [])
                if v["framing"] == "product_first" and v["label"] not in used_labels
            ]
            if not candidates:
                continue
            picked = rng.choice(candidates)
            used_labels.discard(current["label"])
            used_labels.add(picked["label"])
            selections[idx] = picked
            product_used += 1

    # tone-only 池兜底：保留 None 槽位无法填的不太可能（每模板每类 >=1 条）
    return [s for s in selections if s is not None]


__all__ = [
    "AgeSegment",
    "PoolBand",
    "SHOT_CLASS_ORDER",
    "ShotClass",
    "ShotFraming",
    "ShotPool",
    "ShotVariant",
    "Template",
    "age_soft_constraint",
    "resolve_pool_band",
    "select_variants",
    "shot_class_distribution",
]
