"""Agent text hold settlement using existing wallet and pricing primitives."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.model_entities import AgentProviderCall, AgentRun, AuditLog
from lumen_core.pricing import UsageTokens

from . import billing as worker_billing
from .billing_parts.common import audit, record_balance_cache_refresh


BillingKnowledge = Literal["actual", "proven_absent", "unknown"]


@dataclass(frozen=True, slots=True)
class AgentBillingResult:
    action: Literal["settled", "released", "not_applicable", "replayed"]
    actual_micro: int
    balance_after: int | None = None


def agent_usage_tokens(usage: dict[str, Any]) -> UsageTokens:
    def value(key: str) -> int:
        raw = usage.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return max(0, raw)
        return 0

    cache_write = value("cache_write_tokens")
    cache_write_1h = min(cache_write, value("cache_write_1h_tokens"))
    return UsageTokens(
        input_tokens=value("input_tokens"),
        output_tokens=value("output_tokens"),
        cache_read_tokens=value("cache_read_tokens"),
        cache_creation_tokens=cache_write,
        cache_creation_5m_tokens=max(0, cache_write - cache_write_1h),
        cache_creation_1h_tokens=cache_write_1h,
        reasoning_tokens=value("reasoning_tokens"),
    )


def _billing_snapshot(run: AgentRun) -> dict[str, Any]:
    return dict(run.billing_jsonb) if isinstance(run.billing_jsonb, dict) else {}


def _applies(run: AgentRun) -> bool:
    return run.account_mode_snapshot == "wallet" and int(run.text_hold_micro or 0) > 0


def _record_state(
    run: AgentRun,
    *,
    state: str,
    knowledge: BillingKnowledge,
    actual_micro: int,
    breakdown: dict[str, Any] | None = None,
) -> None:
    snapshot = _billing_snapshot(run)
    snapshot.update(
        {
            "state": state,
            "knowledge": knowledge,
            "actual_micro": max(0, int(actual_micro)),
        }
    )
    if breakdown is not None:
        snapshot["cost_breakdown"] = breakdown
    run.billing_jsonb = snapshot


def evidenced_agent_cost(
    run: AgentRun,
    usage: dict[str, Any],
) -> tuple[int, dict[str, Any] | None, UsageTokens]:
    snapshot = _billing_snapshot(run)
    pricing = snapshot.get("pricing_snapshot")
    multiplier = snapshot.get("rate_multiplier_x10000")
    tokens = agent_usage_tokens(usage)
    if not isinstance(pricing, dict) or not isinstance(multiplier, int):
        return 0, None, tokens
    try:
        breakdown = billing_core.completion_breakdown_from_snapshot(
            pricing,
            model=run.model or "",
            tokens=tokens,
            rate_multiplier_x10000=multiplier,
        )
    except billing_core.BillingError:
        return 0, None, tokens
    return (
        max(0, int(breakdown.actual_cost_micro or 0)),
        breakdown.model_dump(),
        tokens,
    )


def _uncertain_dispatch_ordinals(
    call_rows: list[Any],
    dispatch: dict[str, Any],
) -> list[int]:
    if call_rows:
        return [
            int(row.dispatch_ordinal)
            for row in call_rows
            if row.result_state not in {"exact", "missing"}
        ][:128]
    authorized = max(
        int(dispatch.get("provider_dispatch_authorized_count") or 0),
        int(dispatch.get("provider_dispatch_count") or 0),
    )
    completed = max(0, int(dispatch.get("provider_completed_count") or 0))
    return list(range(completed + 1, authorized + 1))[:128]


def _record_transaction(
    db: AsyncSession,
    *,
    run: AgentRun,
    tx: Any,
    event_type: str,
    knowledge: BillingKnowledge,
    actual_micro: int,
) -> AgentBillingResult:
    if tx is None:
        return AgentBillingResult("replayed", max(0, actual_micro))
    record_balance_cache_refresh(
        db,
        user_id=run.user_id,
        balance_after=int(tx.balance_after),
    )
    db.add(
        audit(
            event_type=event_type,
            user_id=run.user_id,
            details={
                "agent_run_id": run.id,
                "amount_micro": int(tx.amount_micro),
                "actual_micro": max(0, actual_micro),
                "balance_after": int(tx.balance_after),
                "hold_after": int(tx.hold_after),
                "knowledge": knowledge,
                "provider": run.provider_name,
                "model": run.model,
                "turn_count": run.turn_count,
                "tool_call_count": run.tool_call_count,
            },
        )
    )
    return AgentBillingResult(
        "released" if tx.kind == "release" else "settled",
        max(0, actual_micro),
        int(tx.balance_after),
    )


async def settle_agent_text_actual(
    db: AsyncSession,
    *,
    run: AgentRun,
    usage: dict[str, Any],
) -> AgentBillingResult:
    if not _applies(run):
        _record_state(
            run,
            state="not_applicable",
            knowledge="actual",
            actual_micro=0,
        )
        return AgentBillingResult("not_applicable", 0)
    snapshot = _billing_snapshot(run)
    multiplier = snapshot.get("rate_multiplier_x10000")
    actual, breakdown, tokens = evidenced_agent_cost(run, usage)
    if breakdown is None or not isinstance(multiplier, int):
        return await settle_agent_text_unknown(
            db,
            run=run,
            reason="pricing_evidence_unavailable",
        )
    tx = await billing_core.settle(
        db,
        run.user_id,
        ref_type="agent_run",
        ref_id=run.id,
        actual_micro=actual,
        idempotency_key=f"settle:{run.id}",
        allow_negative=await worker_billing.allow_negative_balance(),
        record_zero=actual == 0,
        meta={
            "agent_run_id": run.id,
            "model": run.model,
            "provider": run.provider_name,
            "turn_count": run.turn_count,
            "tool_call_count": run.tool_call_count,
            "tokens_in": tokens.input_tokens,
            "tokens_out": tokens.output_tokens,
            "cache_read_tokens": tokens.cache_read_tokens,
            "cache_creation_tokens": tokens.cache_creation_tokens,
            "cache_creation_5m_tokens": tokens.cache_creation_5m_tokens,
            "cache_creation_1h_tokens": tokens.cache_creation_1h_tokens,
            "reasoning_tokens": tokens.reasoning_tokens,
            "cost_breakdown": breakdown,
            "rate_multiplier_x10000": multiplier,
            "upstream_cost_knowledge": "actual",
        },
    )
    _record_state(
        run,
        state="settled",
        knowledge="actual",
        actual_micro=actual,
        breakdown=breakdown,
    )
    return _record_transaction(
        db,
        run=run,
        tx=tx,
        event_type="wallet.settle.agent",
        knowledge="actual",
        actual_micro=actual,
    )


async def release_agent_text_hold(
    db: AsyncSession,
    *,
    run: AgentRun,
    reason: str,
) -> AgentBillingResult:
    if not _applies(run):
        _record_state(
            run,
            state="not_applicable",
            knowledge="proven_absent",
            actual_micro=0,
        )
        return AgentBillingResult("not_applicable", 0)
    tx = await billing_core.release(
        db,
        run.user_id,
        ref_type="agent_run",
        ref_id=run.id,
        idempotency_key=f"release:{run.id}:{reason}",
        meta={"agent_run_id": run.id, "reason": reason},
    )
    _record_state(
        run,
        state="released",
        knowledge="proven_absent",
        actual_micro=0,
    )
    return _record_transaction(
        db,
        run=run,
        tx=tx,
        event_type="wallet.release.agent",
        knowledge="proven_absent",
        actual_micro=0,
    )


async def settle_agent_text_unknown(
    db: AsyncSession,
    *,
    run: AgentRun,
    reason: str,
) -> AgentBillingResult:
    if not _applies(run):
        _record_state(
            run,
            state="not_applicable",
            knowledge="unknown",
            actual_micro=0,
        )
        return AgentBillingResult("not_applicable", 0)
    held = await worker_billing.held_amount_for_ref(
        db,
        run.user_id,
        "agent_run",
        run.id,
    )
    usage = getattr(run, "usage_jsonb", {})
    actual, breakdown, tokens = evidenced_agent_cost(
        run,
        usage if isinstance(usage, dict) else {},
    )
    raw_dispatch = getattr(run, "dispatch_jsonb", {})
    dispatch = raw_dispatch if isinstance(raw_dispatch, dict) else {}
    execution_epoch = getattr(run, "execution_epoch", None)
    call_rows = (
        list(
            (
                await db.execute(
                    select(AgentProviderCall)
                    .where(
                        AgentProviderCall.agent_run_id == run.id,
                        AgentProviderCall.execution_epoch == execution_epoch,
                    )
                    .order_by(AgentProviderCall.dispatch_ordinal.asc())
                )
            )
            .scalars()
            .all()
        )
        if isinstance(execution_epoch, int)
        else []
    )
    uncertain_ordinals = _uncertain_dispatch_ordinals(call_rows, dispatch)
    evidence = {
        "version": 1,
        "reason": reason,
        "uncertain_dispatch_ordinals": uncertain_ordinals,
        "maximum_uncertain_exposure_micro": (held if uncertain_ordinals else 0),
        "evidenced_actual_micro": actual,
        "usage": {
            "input_tokens": tokens.input_tokens,
            "output_tokens": tokens.output_tokens,
            "cache_read_tokens": tokens.cache_read_tokens,
            "cache_creation_tokens": tokens.cache_creation_tokens,
            "reasoning_tokens": tokens.reasoning_tokens,
        },
    }
    evidence_hash = hashlib.sha256(
        json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    evidence["hash"] = evidence_hash
    tx = await billing_core.settle(
        db,
        run.user_id,
        ref_type="agent_run",
        ref_id=run.id,
        actual_micro=actual,
        idempotency_key=f"settle:{run.id}",
        allow_negative=await worker_billing.allow_negative_balance(),
        record_zero=actual == 0,
        meta={
            "agent_run_id": run.id,
            "model": run.model,
            "provider": run.provider_name,
            "turn_count": run.turn_count,
            "tool_call_count": run.tool_call_count,
            "tier_source": "evidenced_usage_only",
            "upstream_cost_knowledge": "unknown",
            "unresolved_liability": evidence,
        },
    )
    _record_state(
        run,
        state="settled_evidenced_unknown",
        knowledge="unknown",
        actual_micro=actual,
        breakdown=breakdown,
    )
    snapshot = _billing_snapshot(run)
    snapshot["unresolved_liability"] = evidence
    run.billing_jsonb = snapshot
    db.add(
        AuditLog(
            user_id=run.user_id,
            event_type="billing.agent_upstream_liability_unknown",
            details={
                "agent_run_id": run.id,
                "evidenced_actual_micro": actual,
                "maximum_uncertain_exposure_micro": evidence[
                    "maximum_uncertain_exposure_micro"
                ],
                "uncertain_dispatch_ordinals": uncertain_ordinals,
                "evidence_hash": evidence_hash,
                "reason": reason,
            },
        )
    )
    return _record_transaction(
        db,
        run=run,
        tx=tx,
        event_type="wallet.settle.agent_evidenced_unknown",
        knowledge="unknown",
        actual_micro=actual,
    )


__all__ = [
    "AgentBillingResult",
    "_uncertain_dispatch_ordinals",
    "agent_usage_tokens",
    "evidenced_agent_cost",
    "release_agent_text_hold",
    "settle_agent_text_actual",
    "settle_agent_text_unknown",
]
