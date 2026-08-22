"""Billing usage metadata and classification helpers."""

from __future__ import annotations

from typing import Any

from lumen_core.models import WalletTransaction
from lumen_core.schemas import BillingUsageByKindOut


CHARGE_KINDS = ("charge", "charge_completion")
_CHARGE_KINDS = CHARGE_KINDS


def meta_int(mapping: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(mapping.get(key) or 0))
    except (TypeError, ValueError):
        return 0


_meta_int = meta_int


def _scaled_meta_cost(mapping: dict[str, Any], key: str) -> int:
    value = _meta_int(mapping, key)
    raw_multiplier = mapping.get("rate_multiplier_x10000")
    multiplier = (
        _meta_int(mapping, "rate_multiplier_x10000")
        if raw_multiplier is not None
        else 10_000
    )
    return (value * multiplier) // 10_000


def _usage_by_kind(rows: list[WalletTransaction]) -> BillingUsageByKindOut:
    totals = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "image": 0,
        "reasoning": 0,
        "agent_text": 0,
        "agent_text_to_image": 0,
        "agent_image_to_image": 0,
    }
    for row in rows:
        meta = row.meta or {}
        breakdown = meta.get("cost_breakdown")
        if isinstance(breakdown, dict):
            row_totals = {
                "input": _scaled_meta_cost(breakdown, "input_cost_micro"),
                "output": _scaled_meta_cost(breakdown, "output_cost_micro"),
                "cache_read": _scaled_meta_cost(breakdown, "cache_read_cost_micro"),
                "cache_creation": _scaled_meta_cost(
                    breakdown, "cache_creation_cost_micro"
                ),
                "image": _scaled_meta_cost(breakdown, "image_output_cost_micro"),
                "reasoning": _scaled_meta_cost(breakdown, "reasoning_cost_micro"),
            }
            if sum(row_totals.values()) > 0:
                for key, value in row_totals.items():
                    totals[key] += value
                if row.ref_type == "agent_run":
                    totals["agent_text"] += sum(row_totals.values())
                elif row.ref_type == "generation" and meta.get("source") == "agent":
                    if meta.get("agent_image_mode") == "image_to_image":
                        totals["agent_image_to_image"] += sum(row_totals.values())
                    else:
                        totals["agent_text_to_image"] += sum(row_totals.values())
                continue

        fallback = (
            _meta_int(meta, "actual_micro")
            or _meta_int(meta, "cost_micro")
            or abs(int(row.amount_micro))
        )
        if row.ref_type in {"generation", "video_generation"}:
            totals["image"] += fallback
            if meta.get("source") == "agent":
                if meta.get("agent_image_mode") == "image_to_image":
                    totals["agent_image_to_image"] += fallback
                else:
                    totals["agent_text_to_image"] += fallback
        elif row.ref_type == "agent_run":
            totals["output"] += fallback
            totals["agent_text"] += fallback
        elif row.kind in (*_CHARGE_KINDS, "settle"):
            totals["output"] += fallback
    return BillingUsageByKindOut(**totals)


def _usage_total(usage: BillingUsageByKindOut) -> int:
    return (
        usage.input
        + usage.output
        + usage.cache_read
        + usage.cache_creation
        + usage.image
        + usage.reasoning
    )


meta_int = _meta_int
scaled_meta_cost = _scaled_meta_cost
usage_by_kind = _usage_by_kind
usage_total = _usage_total

__all__ = [
    "CHARGE_KINDS",
    "meta_int",
    "meta_int",
    "scaled_meta_cost",
    "usage_by_kind",
    "usage_total",
]
