"""Completion billing settlement, audit, and release services."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    Completion,
    WalletTransaction,
)
from lumen_core.pricing import CostBreakdown, UsageTokens

from .contracts import CompletionDependencies


async def record_completion_settlement(
    session: AsyncSession,
    completion: Completion,
    *,
    tx: WalletTransaction | None,
    cache: Any,
    key_id: str | None,
    cost: int,
    usage: UsageTokens,
    breakdown: CostBreakdown,
    fingerprint: str,
    service_tier: str,
    deps: CompletionDependencies,
) -> None:
    if tx is None:
        return
    window_event_added = await deps.ensure_billing_window_usage_event(
        session,
        tx=tx,
        user_id=completion.user_id,
        credential_id=key_id,
        amount_micro=cost,
    )
    for kind, value in (
        ("input", breakdown.input_cost_micro),
        ("output", breakdown.output_cost_micro),
        ("cache_read", breakdown.cache_read_cost_micro),
        ("cache_creation", breakdown.cache_creation_cost_micro),
        ("image", breakdown.image_output_cost_micro),
        ("reasoning", breakdown.reasoning_cost_micro),
    ):
        if value > 0:
            deps.billing_cost_micro_total.labels(kind=kind).inc(value)
    if cache is not None:
        deps.record_balance_cache_refresh(
            session,
            user_id=completion.user_id,
            balance_after=tx.balance_after,
        )
        if key_id and window_event_added:
            limits = await cache.credential_limits(session, key_id)
            deps.record_window_cache_increment(
                session,
                key_id=key_id,
                micro=max(0, cost),
                limits=limits,
            )
    session.add(
        deps.audit(
            event_type="wallet.charge.completion",
            user_id=completion.user_id,
            details={
                "completion_id": completion.id,
                "cost_micro": cost,
                "usage": usage.model_dump(),
                "cost_breakdown": breakdown.model_dump(),
                "request_fingerprint": fingerprint,
                "service_tier": service_tier,
                "amount_micro": tx.amount_micro,
                "balance_after": tx.balance_after,
            },
        )
    )
    if cost == 0 and breakdown.rate_multiplier_x10000 == 0:
        session.add(
            deps.audit(
                event_type="wallet.charge.zero_rate",
                user_id=completion.user_id,
                details={
                    "completion_id": completion.id,
                    "tx_id": tx.id,
                    "ref_type": "completion",
                    "ref_id": deps.completion_billing_ref_id(completion),
                    "rate_multiplier_x10000": 0,
                },
            )
        )
    if breakdown.cache_read_cost_micro > 0:
        session.add(
            deps.audit(
                event_type="wallet.charge.completion.cache_read",
                user_id=completion.user_id,
                details={
                    "completion_id": completion.id,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_read_cost_micro": breakdown.cache_read_cost_micro,
                },
            )
        )
    if int((tx.meta or {}).get("overdraw_micro") or 0) > 0:
        deps.wallet_overdrawn_total.labels(kind="settle").inc()
        session.add(
            deps.audit(
                event_type="wallet.overdrawn",
                user_id=completion.user_id,
                details={
                    "completion_id": completion.id,
                    "tx_id": tx.id,
                    "meta": tx.meta,
                },
            )
        )


async def charge_completion(
    session: AsyncSession,
    completion: Completion,
    *,
    deps: CompletionDependencies,
    billing_enabled: bool | None = None,
    cache_aware: bool | None = None,
    allow_negative: bool | None = None,
    window_rate_limit: bool | None = None,
) -> None:
    billing_ref_id = deps.completion_billing_ref_id(completion)
    if not await deps.wallet_billing_applies(
        session,
        user_id=completion.user_id,
        ref_type="completion",
        ref_id=billing_ref_id,
    ):
        return
    enabled = (
        await deps.billing_enabled()
        if billing_enabled is None
        else bool(billing_enabled)
    )
    if not enabled:
        return
    idempotency_key = f"complete:{billing_ref_id}"
    existing = await deps.existing_wallet_tx(
        session,
        completion.user_id,
        idempotency_key,
    )
    if existing is not None:
        deps.add_replay_audit(
            session,
            user_id=completion.user_id,
            tx=existing,
            replay_source="precheck",
        )
        return
    resolved_cache_aware = (
        (
            await deps.cache_aware_enabled()
            if isinstance(session, deps.async_session_type)
            else True
        )
        if cache_aware is None
        else bool(cache_aware)
    )
    usage = deps.completion_usage(completion, cache_aware=resolved_cache_aware)
    rate_multiplier = await deps.completion_rate_multiplier_x10000(session, completion)
    service_tier = deps.completion_service_tier(completion)
    breakdown = await deps.resolve_completion_breakdown(
        session,
        completion,
        billing_ref_id=billing_ref_id,
        usage=usage,
        rate_multiplier=rate_multiplier,
        service_tier=service_tier,
    )
    if breakdown is None:
        return
    cost = breakdown.actual_cost_micro
    deps.billing_pricing_source_total.labels(source=breakdown.pricing_source).inc()
    fingerprint, replayed = await deps.completion_request_fingerprint(
        session,
        completion,
        idempotency_key=idempotency_key,
        service_tier=service_tier,
        usage=usage,
        breakdown=breakdown,
    )
    if replayed:
        return
    cache = deps.get_billing_cache()
    key_id = getattr(completion, "user_api_credential_id", None)
    await deps.audit_completion_window_limit(
        session,
        completion,
        cache=cache,
        key_id=key_id,
        cost=cost,
        window_rate_limit=window_rate_limit,
    )
    resolved_allow_negative = (
        await deps.allow_negative_balance()
        if allow_negative is None
        else bool(allow_negative)
    )
    # No balance re-check here: the upstream cost has already been incurred and
    # delivered, so settlement must record it in full (pure pass-through).
    # billing_core.settle never rejects on insufficient balance — the deficit
    # lands in overdraw_micro as a collectible debt marker. Wallet gating for
    # image tool output happens only before dispatch
    # (_ensure_completion_tool_image_wallet_budget), while the cost is still
    # avoidable; an INSUFFICIENT_BALANCE raise at this stage would leave the
    # user unbilled for a provider charge the platform already paid.
    try:
        tx = await deps.billing_core.settle(
            session,
            completion.user_id,
            ref_type="completion",
            ref_id=billing_ref_id,
            actual_micro=cost,
            idempotency_key=idempotency_key,
            allow_negative=resolved_allow_negative,
            record_zero=cost == 0 and rate_multiplier == 0,
            meta={
                "completion_id": completion.id,
                "model": completion.model,
                "tokens_in": usage.input_tokens,
                "tokens_out": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_creation_tokens": usage.cache_creation_tokens,
                "cache_creation_5m_tokens": usage.cache_creation_5m_tokens,
                "cache_creation_1h_tokens": usage.cache_creation_1h_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "image_output_tokens": usage.image_output_tokens,
                "cost_breakdown": breakdown.model_dump(),
                "request_fingerprint": fingerprint,
                "rate_multiplier_x10000": rate_multiplier,
                "service_tier": service_tier,
                "api_key_id": key_id,
            },
        )
    except Exception:
        deps.wallet_charge_lost_total.inc()
        raise
    await deps.record_completion_settlement(
        session,
        completion,
        tx=tx,
        cache=cache,
        key_id=key_id,
        cost=cost,
        usage=usage,
        breakdown=breakdown,
        fingerprint=fingerprint,
        service_tier=service_tier,
    )


async def release_completion(
    session: AsyncSession,
    completion: Completion,
    *,
    reason: str,
    deps: CompletionDependencies,
) -> None:
    billing_ref_id = deps.completion_billing_ref_id(completion)
    if not await deps.wallet_billing_applies(
        session,
        user_id=completion.user_id,
        ref_type="completion",
        ref_id=billing_ref_id,
    ):
        return
    if not await deps.billing_enabled():
        return
    idempotency_key = f"release:{billing_ref_id}"
    existing = await deps.existing_wallet_tx(
        session,
        completion.user_id,
        idempotency_key,
    )
    if existing is not None:
        deps.add_replay_audit(
            session,
            user_id=completion.user_id,
            tx=existing,
            replay_source="precheck",
        )
        return
    tx = await deps.billing_core.release(
        session,
        completion.user_id,
        ref_type="completion",
        ref_id=billing_ref_id,
        idempotency_key=idempotency_key,
        meta={
            "completion_id": completion.id,
            "reason": reason,
            "billing_retry_count": deps.completion_billing_retry_count(completion),
        },
    )
    if tx is None:
        return
    deps.record_balance_cache_refresh(
        session,
        user_id=completion.user_id,
        balance_after=tx.balance_after,
    )
    session.add(
        deps.audit(
            event_type="wallet.release.completion",
            user_id=completion.user_id,
            details={
                "completion_id": completion.id,
                "amount_micro": tx.amount_micro,
                "balance_after": tx.balance_after,
                "hold_after": tx.hold_after,
                "reason": reason,
            },
        )
    )
