"""Verify admitted task snapshots before their transaction can publish."""

from .billing_projection import task_estimate
from .errors import canvas_http


def verify_plan_admission(prepared, owners):
    if prepared.run.kind == "single":
        return
    limit = (prepared.execution.pricing_snapshot_jsonb or {}).get(
        "estimated_cost_micro"
    )
    costs = [task_estimate(owner) for owner in owners]
    if (
        type(limit) is not int
        or any(cost is None for cost in costs)
        or sum(costs) > limit
    ):
        raise canvas_http(
            "canvas_plan_price_changed",
            "task estimate changed; preview a new run before admission",
            409,
        )
