"""Read-only Canvas costs from durable task snapshots and wallet ledger.

No reserves, settlements, refunds, or pricing guesses are made in this module.
Missing facts stay null; hold refunds are not mistaken for negative service cost.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.billing_values import generation_billing_ref_id
from lumen_core.model_entities import WalletTransaction


def _amount(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def task_estimate(owner: Any | None) -> int | None:
    if owner is None:
        return None
    estimate = _amount(getattr(owner, "est_cost_micro", None))
    if estimate is not None:
        return estimate
    request = getattr(owner, "upstream_request", None)
    request = request if isinstance(request, dict) else {}
    snapshot = request.get("billing_pricing_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    if snapshot.get("kind") != "image":
        return None
    unit = _amount(snapshot.get("unit_price_micro"))
    rate = _amount(request.get("billing_rate_multiplier_x10000"))
    if unit is None or rate is None:
        return None
    return max(1, unit * rate // 10_000) if unit and rate else 0


def ledger_summary(rows: list[Any], *, estimate: int | None) -> dict[str, Any]:
    seen: set[str] = set()
    held = 0
    charged = 0
    settled = False
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        meta = row.meta if isinstance(row.meta, dict) else {}
        delta = meta.get("hold_delta")
        if type(delta) is int:
            held += delta
        elif row.kind == "hold":
            held += max(0, -row.amount_micro)
        elif row.kind == "release":
            held -= max(0, row.amount_micro)
        actual = None
        if row.kind == "settle":
            actual = _amount(meta.get("actual_micro"))
        elif row.kind in {"charge", "consume"}:
            actual = _amount(meta.get("cost_micro"))
        if actual is not None:
            settled = True
            charged += actual
    return {
        "currency": "CNY",
        "estimated_cost_micro": estimate,
        "reserved_micro": max(0, held) if rows else None,
        "actual_cost_micro": charged if settled else None,
        "source": "wallet_ledger"
        if rows
        else "task_snapshot"
        if estimate is not None
        else "unknown",
    }


async def task_billing_details(
    db: AsyncSession,
    *,
    tasks: list[Any],
    owners: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    references: dict[str, set[tuple[str, str, str]]] = {}
    for task in tasks:
        owner = owners.get(task.id)
        if owner is None:
            continue
        ref_type = task.task_kind
        if ref_type not in {"generation", "video_generation"}:
            continue
        keys = {(owner.user_id, ref_type, owner.id)}
        if task.task_kind == "generation":
            keys.add((owner.user_id, "generation", generation_billing_ref_id(owner)))
        references[task.id] = keys
    keys = set().union(*references.values()) if references else set()
    transactions = []
    if keys:
        # Bound each owner and reference pair together, including repair attempts.
        transactions = list(
            (
                await db.execute(
                    select(WalletTransaction).where(
                        or_(
                            *[
                                (WalletTransaction.user_id == user_id)
                                & tuple_(
                                    WalletTransaction.ref_type, WalletTransaction.ref_id
                                ).in_(
                                    [
                                        (kind, ref_id)
                                        for uid, kind, ref_id in keys
                                        if uid == user_id
                                    ]
                                )
                                for user_id in sorted({uid for uid, _, _ in keys})
                            ]
                        )
                    )
                )
            ).scalars()
        )
    by_reference: dict[tuple[str, str, str], list[Any]] = {}
    for tx in transactions:
        by_reference.setdefault((tx.user_id, tx.ref_type, tx.ref_id), []).append(tx)
    return {
        task.id: ledger_summary(
            [
                tx
                for key in references.get(task.id, set())
                for tx in by_reference.get(key, [])
            ],
            estimate=task_estimate(owners.get(task.id)),
        )
        for task in tasks
    }


def aggregate_task_billing(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Only declare a total when every task contributes a known amount."""
    unique = {task["id"]: task.get("billing", {}) for task in tasks}
    fields = ("estimated_cost_micro", "reserved_micro", "actual_cost_micro")
    result: dict[str, Any] = {"currency": "CNY", "task_count": len(unique)}
    for field in fields:
        values = [billing.get(field) for billing in unique.values()]
        known = [_amount(value) for value in values]
        complete = bool(known) and all(value is not None for value in known)
        result[field] = sum(known) if complete else None
        result[f"known_{field}"] = sum(value for value in known if value is not None)
    result["source"] = "task_ledger_aggregate"
    return result
