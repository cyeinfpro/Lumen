"""Append-only remediation for historical Agent unknown-result overcharges."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lumen_core import billing as billing_core
from lumen_core.model_entities import AgentRun, AuditLog, WalletTransaction

from .agent_billing import evidenced_agent_cost
from .billing_parts.common import record_balance_cache_refresh
from .db import SessionLocal


@dataclass(frozen=True, slots=True)
class AgentBillingCorrection:
    run_id: str
    charged_micro: int
    evidenced_micro: int
    credit_micro: int
    applied: bool


async def correct_unknown_agent_charge(
    db: AsyncSession,
    *,
    run_id: str,
    dry_run: bool = True,
) -> AgentBillingCorrection | None:
    run = (
        await db.execute(
            select(AgentRun).where(AgentRun.id == run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    settlement = (
        await db.execute(
            select(WalletTransaction)
            .where(
                WalletTransaction.user_id == run.user_id,
                WalletTransaction.ref_type == "agent_run",
                WalletTransaction.ref_id == run.id,
                WalletTransaction.kind == "settle",
            )
            .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if settlement is None or not isinstance(settlement.meta, dict):
        return None
    if (
        settlement.meta.get("upstream_cost_knowledge") != "unknown"
        or settlement.meta.get("tier_source") != "upstream_result_unknown"
    ):
        return None
    key = f"agent-unknown-correction:{run.id}:v1"
    existing = (
        await db.execute(
            select(WalletTransaction).where(
                WalletTransaction.user_id == run.user_id,
                WalletTransaction.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    charged = max(0, int(settlement.meta.get("actual_micro") or 0))
    evidenced, _breakdown, _tokens = evidenced_agent_cost(
        run,
        run.usage_jsonb if isinstance(run.usage_jsonb, dict) else {},
    )
    credit = max(0, charged - evidenced)
    correction = AgentBillingCorrection(
        run_id=run.id,
        charged_micro=charged,
        evidenced_micro=evidenced,
        credit_micro=credit,
        applied=False,
    )
    if dry_run:
        return correction
    correction_meta = {
        "agent_run_id": run.id,
        "source_settlement_id": settlement.id,
        "charged_micro": charged,
        "evidenced_micro": evidenced,
        "policy_version": 1,
    }
    tx = (
        await billing_core.credit_correction(
            db,
            run.user_id,
            credit,
            ref_type="agent_run_correction",
            ref_id=run.id,
            idempotency_key=key,
            reason="historical_agent_unknown_result_overcharge",
            meta=correction_meta,
        )
        if credit > 0
        else await billing_core.charge(
            db,
            run.user_id,
            0,
            ref_type="agent_run_correction",
            ref_id=run.id,
            idempotency_key=key,
            record_zero=True,
            kind="correction_credit",
            meta={**correction_meta, "zero_credit_marker": True},
        )
    )
    billing = dict(run.billing_jsonb) if isinstance(run.billing_jsonb, dict) else {}
    billing["unknown_result_correction"] = {
        "version": 1,
        "transaction_id": tx.id if tx is not None else None,
        "charged_micro": charged,
        "evidenced_micro": evidenced,
        "credited_micro": credit,
    }
    run.billing_jsonb = billing
    if tx is not None:
        record_balance_cache_refresh(
            db,
            user_id=run.user_id,
            balance_after=int(tx.balance_after),
        )
        db.add(
            AuditLog(
                user_id=run.user_id,
                event_type="billing.agent_unknown_result_corrected",
                details={
                    "agent_run_id": run.id,
                    "source_settlement_id": settlement.id,
                    "correction_transaction_id": tx.id,
                    "charged_micro": charged,
                    "evidenced_micro": evidenced,
                    "credited_micro": credit,
                    "policy_version": 1,
                },
            )
        )
    return AgentBillingCorrection(
        run_id=run.id,
        charged_micro=charged,
        evidenced_micro=evidenced,
        credit_micro=credit,
        applied=tx is not None,
    )


async def _legacy_unknown_page(
    *,
    after_transaction_id: str | None,
    limit: int,
) -> list[tuple[str, str]]:
    settlement = aliased(WalletTransaction)
    correction = aliased(WalletTransaction)
    correction_key = (
        literal("agent-unknown-correction:") + settlement.ref_id + literal(":v1")
    )
    statement = (
        select(settlement.id, settlement.ref_id)
        .outerjoin(
            correction,
            and_(
                correction.user_id == settlement.user_id,
                correction.idempotency_key == correction_key,
            ),
        )
        .where(
            settlement.kind == "settle",
            settlement.ref_type == "agent_run",
            settlement.ref_id.is_not(None),
            settlement.meta["upstream_cost_knowledge"].as_string() == "unknown",
            settlement.meta["tier_source"].as_string() == "upstream_result_unknown",
            correction.id.is_(None),
        )
    )
    if after_transaction_id is not None:
        statement = statement.where(settlement.id > after_transaction_id)
    async with SessionLocal() as db:
        rows = (
            await db.execute(statement.order_by(settlement.id.asc()).limit(limit))
        ).all()
    return [
        (transaction_id, run_id)
        for transaction_id, run_id in rows
        if isinstance(transaction_id, str) and isinstance(run_id, str)
    ]


async def run_agent_unknown_charge_backfill(
    *,
    dry_run: bool,
    batch_size: int = 100,
) -> dict[str, Any]:
    maximum = max(1, min(int(batch_size), 1000))
    cursor: str | None = None
    scanned = 0
    candidates = 0
    applied = 0
    credited_micro = 0
    results: list[dict[str, Any]] = []
    while True:
        page = await _legacy_unknown_page(
            after_transaction_id=cursor,
            limit=maximum,
        )
        if not page:
            break
        for transaction_id, run_id in page:
            cursor = transaction_id
            scanned += 1
            async with SessionLocal() as db:
                async with db.begin():
                    correction = await correct_unknown_agent_charge(
                        db,
                        run_id=run_id,
                        dry_run=dry_run,
                    )
            if correction is None or correction.credit_micro <= 0:
                continue
            candidates += 1
            applied += int(correction.applied)
            credited_micro += correction.credit_micro if correction.applied else 0
            results.append(
                {
                    "run_id": correction.run_id,
                    "charged_micro": correction.charged_micro,
                    "evidenced_micro": correction.evidenced_micro,
                    "credit_micro": correction.credit_micro,
                    "applied": correction.applied,
                }
            )
        if len(page) < maximum:
            break
    return {
        "dry_run": dry_run,
        "scanned": scanned,
        "candidates": candidates,
        "applied": applied,
        "credited_micro": credited_micro,
        "last_transaction_id": cursor,
        "items": results,
    }


async def correct_agent_unknown_charges(
    _ctx: dict[str, Any],
    *,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    return await run_agent_unknown_charge_backfill(
        dry_run=dry_run,
        batch_size=limit,
    )


__all__ = [
    "AgentBillingCorrection",
    "correct_agent_unknown_charges",
    "correct_unknown_agent_charge",
    "run_agent_unknown_charge_backfill",
]
