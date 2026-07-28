"""Deterministic showcase shot variant selection."""

from __future__ import annotations

from typing import cast

from .showcase_shot_pool import (
    SHOT_CLASS_ORDER,
    ShotClass,
    ShotVariant,
    Template,
    resolve_pool_band as _resolve_pool_band,
    select_variants as _select_shot_variants,
    shot_class_distribution as _shot_class_distribution,
)
from .showcase_shot_pool_adult import ADULT_POOL
from .showcase_shot_pool_kids import CHILD_POOL, TODDLER_POOL
from .immutables import freeze_mapping


SHOT_POOL_BY_BAND = freeze_mapping(
    {
        "young_adult": ADULT_POOL,
        "child": CHILD_POOL,
        "toddler": TODDLER_POOL,
    }
)


def _showcase_default_variant(
    template: str,
    shot_type: str,
    age_segment: str | None,
) -> ShotVariant | None:
    band = _resolve_pool_band(age_segment)
    pool = SHOT_POOL_BY_BAND.get(band, ADULT_POOL)
    template_key = cast(Template, template)
    template_pool = pool.get(template_key) or ADULT_POOL.get(template_key)
    if not template_pool:
        return None
    shot_key = cast(ShotClass, shot_type)
    variants = template_pool.get(shot_key) or template_pool.get(SHOT_CLASS_ORDER[0])
    if not variants:
        return None
    for variant in variants:
        if variant["framing"] == "product_first":
            return variant
    return variants[0]


def _showcase_pick_shot_variants(
    *,
    template: str,
    age_segment: str | None,
    output_count: int,
    seed_key: str,
) -> list[tuple[ShotClass, ShotVariant]]:
    band = _resolve_pool_band(age_segment)
    pool = SHOT_POOL_BY_BAND.get(band, ADULT_POOL)
    template_key = cast(Template, template)
    template_pool = pool.get(template_key) or ADULT_POOL.get(template_key) or {}
    plan = _shot_class_distribution(output_count)
    variants = _select_shot_variants(
        pool=template_pool,
        plan=plan,
        seed_key=seed_key,
        min_product_first=(
            output_count if output_count <= 4 else 6 if output_count <= 8 else 12
        ),
    )
    return list(zip(plan, variants))


# Public workflow contracts.
showcase_default_variant = _showcase_default_variant
showcase_pick_shot_variants = _showcase_pick_shot_variants
