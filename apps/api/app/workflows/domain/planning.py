"""Reusable pure workflow planning helpers."""

from __future__ import annotations

from collections.abc import Iterable

from .models import AssetRequirement, CostEstimate, WorkflowPlan, WorkflowStepPlan


def build_linear_plan(
    steps: Iterable[tuple[str, str, bool]],
    *,
    required_assets: tuple[AssetRequirement, ...] = (),
    estimated_cost: CostEstimate | None = None,
) -> WorkflowPlan:
    return WorkflowPlan(
        steps=tuple(
            WorkflowStepPlan(key=key, title=title, requires_approval=requires_approval)
            for key, title, requires_approval in steps
        ),
        required_assets=required_assets,
        estimated_cost=estimated_cost or CostEstimate(),
    )


__all__ = ["build_linear_plan"]
