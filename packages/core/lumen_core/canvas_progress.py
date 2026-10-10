"""Bounded truthful progress snapshots for Canvas event coalescing."""

from typing import Any
from .canvas import canonical_hash


def canvas_progress_snapshot(owners: list[Any]) -> tuple[str, list[dict]]:
    items = []
    for owner in owners:
        identifier = getattr(owner, "id", None)
        if not isinstance(identifier, str) or not identifier:
            continue
        percent = getattr(owner, "progress_pct", None)
        stage = getattr(owner, "progress_stage", None)
        status = getattr(owner, "status", None)
        items.append(
            {
                "task_id": identifier,
                "status": status[:48] if isinstance(status, str) else None,
                "progress_stage": stage[:80] if isinstance(stage, str) else None,
                "progress_pct": percent
                if type(percent) is int and 0 <= percent <= 100
                else None,
                "cancel_requested": getattr(owner, "cancel_requested_at", None)
                is not None,
            }
        )
    items.sort(key=lambda item: item["task_id"])
    return canonical_hash(items), items
