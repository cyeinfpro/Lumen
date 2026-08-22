"""Agent text hold settlement using existing wallet and pricing primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.model_entities import AgentRun, AuditLog
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


def _usage_within_reservation(
    snapshot: dict[str, Any],
    tokens: UsageTokens,
) -> bool:
    reserved_input = snapshot.get("reserved_input_tokens")
    reserved_output = snapshot.get("reserved_output_tokens")
    if not isinstance(reserved_input, int) or not isinstance(reserved_output, int):
        return False
    input_total = (
        tokens.input_tokens
        + tokens.cache_read_tokens
        + tokens.cache_creation_tokens
    )
    return (
        input_total <= max(0, reserved_input)
        and tokens.output_tokens <= max(0, reserved_output)
        and tokens.reasoning_tokens <= tokens.output_tokens
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
    pricing = snapshot.get("pricing_snapshot")
    multiplier = snapshot.get("rate_multiplier_x10000")
    if not isinstance(pricing, dict) or not isinstance(multiplier, int):
        return await settle_agent_text_unknown(
            db,
            run=run,
            reason="pricing_snapshot_missing",
        )
    tokens = agent_usage_tokens(usage)
    if not _usage_within_reservation(snapshot, tokens):
        return await settle_agent_text_unknown(
            db,
            run=run,
            reason="provider_usage_exceeds_reservation",
        )
    try:
        breakdown = billing_core.completion_breakdown_from_snapshot(
            pricing,
            model=run.model or "",
            tokens=tokens,
            rate_multiplier_x10000=multiplier,
        )
    except billing_core.BillingError:
        return await settle_agent_text_unknown(
            db,
            run=run,
            reason="pricing_snapshot_invalid",
        )
    actual = max(0, int(breakdown.actual_cost_micro or 0))
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
            "cost_breakdown": breakdown.model_dump(),
            "rate_multiplier_x10000": multiplier,
            "upstream_cost_knowledge": "actual",
        },
    )
    _record_state(
        run,
        state="settled",
        knowledge="actual",
        actual_micro=actual,
        breakdown=breakdown.model_dump(),
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
    actual = held if held > 0 else max(0, int(run.text_hold_micro or 0))
    if actual <= 0:
        db.add(
            AuditLog(
                user_id=run.user_id,
                event_type="billing.unresolved_after_upstream",
                details={
                    "scope": "agent_result_unknown",
                    "agent_run_id": run.id,
                    "reason": reason,
                },
            )
        )
        _record_state(
            run,
            state="unresolved",
            knowledge="unknown",
            actual_micro=0,
        )
        return AgentBillingResult("not_applicable", 0)
    tx = await billing_core.settle(
        db,
        run.user_id,
        ref_type="agent_run",
        ref_id=run.id,
        actual_micro=actual,
        idempotency_key=f"settle:{run.id}",
        allow_negative=await worker_billing.allow_negative_balance(),
        meta={
            "agent_run_id": run.id,
            "model": run.model,
            "provider": run.provider_name,
            "turn_count": run.turn_count,
            "tool_call_count": run.tool_call_count,
            "tier_source": "upstream_result_unknown",
            "upstream_cost_knowledge": "unknown",
            "reason": reason,
        },
    )
    _record_state(
        run,
        state="settled_unknown",
        knowledge="unknown",
        actual_micro=actual,
    )
    return _record_transaction(
        db,
        run=run,
        tx=tx,
        event_type="wallet.settle.agent_result_unknown",
        knowledge="unknown",
        actual_micro=actual,
    )


__all__ = [
    "AgentBillingResult",
    "agent_usage_tokens",
    "release_agent_text_hold",
    "settle_agent_text_actual",
    "settle_agent_text_unknown",
]
