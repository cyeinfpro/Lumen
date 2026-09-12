"""Pure selectors for replaying requested images, never billing-only extras."""
from __future__ import annotations
from typing import Any, Iterable
from lumen_core.image_models import MAX_IMAGE_COUNT

_BONUS_POLICIES = frozenset({
    "dual_race_loser_settled_separately", "batch_extra_settled_separately",
})

def primary_generations(rows: Iterable[Any]) -> list[Any]:
    result = []
    for row in rows:
        raw = getattr(row, "upstream_request", None)
        request = raw if isinstance(raw, dict) else {}
        if (request.get("is_dual_race_bonus") is True
            or request.get("bonus_billing_obligation") is True
            or request.get("billing_policy") in _BONUS_POLICIES
            or request.get("batch_parent_generation_id")):
            continue
        result.append(row)
    return result

def requested_generation_count(rows: list[Any]) -> int:
    primary = primary_generations(rows)
    counts: set[int] = set()
    for row in primary:
        request = row.upstream_request if isinstance(row.upstream_request, dict) else {}
        for key in ("requested_image_count", "batch_task_count"):
            raw = request.get(key)
            if raw is None:
                continue
            if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_IMAGE_COUNT:
                raise ValueError("invalid historical requested image count")
            counts.add(raw)
    if len(counts) > 1:
        raise ValueError("conflicting historical batch counts")
    count = next(iter(counts)) if counts else len(primary)
    if not 1 <= count <= MAX_IMAGE_COUNT:
        raise ValueError("historical primary batch is empty or exceeds the request limit")
    return count
