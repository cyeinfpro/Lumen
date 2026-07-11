"""Deterministic showcase shot variant selection."""

from __future__ import annotations

from typing import cast

from ..routes._showcase_shot_pool import ShotClass, ShotVariant, Template
from .showcase_runtime import runtime as _runtime


def _showcase_default_variant(
    template: str,
    shot_type: str,
    age_segment: str | None,
) -> ShotVariant | None:
    runtime = _runtime()
    band = runtime._resolve_pool_band(age_segment)
    pool = runtime.SHOT_POOL_BY_BAND.get(band, runtime.ADULT_POOL)
    template_key = cast(Template, template)
    template_pool = pool.get(template_key) or runtime.ADULT_POOL.get(template_key)
    if not template_pool:
        return None
    shot_key = cast(ShotClass, shot_type)
    variants = template_pool.get(shot_key) or template_pool.get(
        runtime.SHOT_CLASS_ORDER[0]
    )
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
    runtime = _runtime()
    band = runtime._resolve_pool_band(age_segment)
    pool = runtime.SHOT_POOL_BY_BAND.get(band, runtime.ADULT_POOL)
    template_key = cast(Template, template)
    template_pool = pool.get(template_key) or runtime.ADULT_POOL.get(template_key) or {}
    plan = runtime._shot_class_distribution(output_count)
    variants = runtime._select_shot_variants(
        pool=template_pool,
        plan=plan,
        seed_key=seed_key,
        min_product_first=(
            output_count if output_count <= 4 else 6 if output_count <= 8 else 12
        ),
    )
    return list(zip(plan, variants))
