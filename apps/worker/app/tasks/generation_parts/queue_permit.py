"""Weighted permit decisions used by generation queue admission."""

from __future__ import annotations

from typing import Any

from lumen_core.generation_resources import ResourceDemand

from .admission import (
    WeightedPermit,
    release_weighted_permit,
    reserve_weighted_permit,
)
from .services import RunGenerationDeps


def default_weighted_permit(
    *,
    task_id: str,
    dual_race: bool,
) -> WeightedPermit:
    return WeightedPermit(
        task_id=task_id,
        attempt=1,
        revision=0,
        demand=ResourceDemand(
            pixel_units=1,
            reference_units=0,
            postprocess_units=0,
            external_lane_units=2 if dual_race else 1,
            output_units=1,
        ),
        user_id="unknown",
    )


async def reserve_generation_permit(
    redis: Any,
    *,
    permit: WeightedPermit,
    owner: str,
    now: float,
    expiry: float,
    capacity: int,
    lock_key: str,
    services: RunGenerationDeps,
) -> bool:
    budget_fn = getattr(services.queue, "resource_budgets", None)
    budgets = (
        budget_fn()
        if callable(budget_fn)
        else (max(1, capacity * 4), max(1, capacity), max(1, capacity * 3))
    )
    return await reserve_weighted_permit(
        redis,
        permit=permit,
        owner=owner,
        now=now,
        expiry=expiry,
        global_budget=budgets[0],
        external_budget=budgets[1],
        user_budget=budgets[2],
        lock_key=lock_key,
    )


async def release_generation_permit(
    redis: Any,
    *,
    permit: WeightedPermit,
) -> bool:
    return await release_weighted_permit(redis, permit=permit)


__all__ = [
    "default_weighted_permit",
    "release_generation_permit",
    "reserve_generation_permit",
]
