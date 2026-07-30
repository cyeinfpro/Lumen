"""Image generation billing settlement and release services."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Generation

from .contracts import (
    GenerationDependencies,
    UnknownUpstreamDependencies,
    UnknownUpstreamSettlement,
)


async def settle_generation(
    session: AsyncSession,
    generation: Generation,
    *,
    width: int,
    height: int,
    image_count: int = 1,
    deps: GenerationDependencies,
) -> None:
    billing_ref_id = deps.generation_billing_ref_id(generation)
    if not await deps.wallet_billing_applies(
        session,
        user_id=generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
    ):
        return
    if not await deps.billing_enabled():
        return
    idempotency_key = f"settle:{billing_ref_id}"
    existing = await deps.existing_wallet_tx(
        session,
        generation.user_id,
        idempotency_key,
    )
    if existing is not None:
        deps.add_replay_audit(
            session,
            user_id=generation.user_id,
            tx=existing,
            replay_source="precheck",
        )
        return
    requested_tier = deps.generation_billing_tier(generation)
    billable_image_count = max(1, int(image_count or 1))
    rate_multiplier = await deps.generation_rate_multiplier_x10000(
        session,
        generation,
    )
    snapshot_cost = deps.generation_snapshot_cost(
        generation,
        image_count=billable_image_count,
    )
    pricing_error: Any | None = None
    if snapshot_cost is not None:
        base_cost, tier = snapshot_cost
        cost = deps.apply_rate_multiplier_micro(base_cost, rate_multiplier)
        tier_source = "task_snapshot"
    else:
        try:
            if requested_tier is not None:
                base_cost, tier = await deps.billing_core.estimate_image_cost_for_tier(
                    session,
                    tier=requested_tier,
                    n=billable_image_count,
                )
                tier_source = "request"
            else:
                base_cost, tier = await deps.billing_core.estimate_image_cost(
                    session,
                    size_px=max(0, int(width) * int(height)),
                    n=billable_image_count,
                    thresholds=await deps.thresholds(),
                )
                tier_source = "actual_pixels"
            cost = deps.apply_rate_multiplier_micro(base_cost, rate_multiplier)
        except deps.billing_core.BillingError as exc:
            if exc.code != "PRICING_MISSING":
                raise
            pricing_error = exc
            cost = 0
            tier = requested_tier or "unknown"
            tier_source = "missing"
    zero_rate = rate_multiplier == 0 and pricing_error is None
    if cost <= 0 and not zero_rate:
        held = await deps.held_amount_for_ref(
            session,
            generation.user_id,
            "generation",
            billing_ref_id,
        )
        if held <= 0:
            session.add(
                deps.audit(
                    event_type="billing.unresolved_after_upstream",
                    user_id=generation.user_id,
                    details={
                        "scope": "image_size",
                        "generation_id": generation.id,
                        "width": width,
                        "height": height,
                        "image_count": billable_image_count,
                        "error": pricing_error.message if pricing_error else None,
                    },
                )
            )
            return
        cost = held
        tier_source = "held_amount_fallback"
        session.add(
            deps.audit(
                event_type="billing.pricing.hold_fallback_after_upstream",
                user_id=generation.user_id,
                details={
                    "scope": "image_size",
                    "tier": tier,
                    "generation_id": generation.id,
                    "width": width,
                    "height": height,
                    "image_count": billable_image_count,
                    "actual_micro": cost,
                    "error": pricing_error.message if pricing_error else None,
                },
            )
        )
    tx = await deps.billing_core.settle(
        session,
        generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
        actual_micro=cost,
        idempotency_key=idempotency_key,
        allow_negative=await deps.allow_negative_balance(),
        record_zero=zero_rate,
        meta={
            "generation_id": generation.id,
            "tier": tier,
            "width": width,
            "height": height,
            "image_count": billable_image_count,
            "tier_source": tier_source,
            "model": generation.model,
            "retry_count": deps.generation_billing_retry_count(generation),
            "rate_multiplier_x10000": rate_multiplier,
            "provider": deps.generation_settle_provider(generation),
        },
    )
    if tx is None:
        return
    deps.record_balance_cache_refresh(
        session,
        user_id=generation.user_id,
        balance_after=tx.balance_after,
    )
    session.add(
        deps.audit(
            event_type="wallet.settle.image",
            user_id=generation.user_id,
            details={
                "generation_id": generation.id,
                "amount_micro": tx.amount_micro,
                "actual_micro": cost,
                "tier": tier,
                "tier_source": tier_source,
                "image_count": billable_image_count,
                "balance_after": tx.balance_after,
                "hold_after": tx.hold_after,
            },
        )
    )
    if zero_rate:
        session.add(
            deps.audit(
                event_type="wallet.charge.zero_rate",
                user_id=generation.user_id,
                details={
                    "generation_id": generation.id,
                    "tx_id": tx.id,
                    "ref_type": "generation",
                    "ref_id": billing_ref_id,
                    "rate_multiplier_x10000": rate_multiplier,
                },
            )
        )
    if int((tx.meta or {}).get("overdraw_micro") or 0) > 0:
        deps.wallet_overdrawn_total.labels(kind="settle").inc()
        session.add(
            deps.audit(
                event_type="wallet.overdrawn",
                user_id=generation.user_id,
                details={
                    "generation_id": generation.id,
                    "tx_id": tx.id,
                    "meta": tx.meta,
                },
            )
        )


async def release_generation(
    session: AsyncSession,
    generation: Generation,
    *,
    reason: str,
    deps: GenerationDependencies,
) -> None:
    billing_ref_id = deps.generation_billing_ref_id(generation)
    if not await deps.wallet_billing_applies(
        session,
        user_id=generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
    ):
        return
    if not await deps.billing_enabled():
        return
    idempotency_key = f"release:{billing_ref_id}"
    existing = await deps.existing_wallet_tx(
        session,
        generation.user_id,
        idempotency_key,
    )
    if existing is not None:
        deps.add_replay_audit(
            session,
            user_id=generation.user_id,
            tx=existing,
            replay_source="precheck",
        )
        return
    tx = await deps.billing_core.release(
        session,
        generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
        idempotency_key=idempotency_key,
        meta={
            "generation_id": generation.id,
            "reason": reason,
            "retry_count": deps.generation_billing_retry_count(generation),
        },
    )
    if tx is None:
        return
    deps.record_balance_cache_refresh(
        session,
        user_id=generation.user_id,
        balance_after=tx.balance_after,
    )
    session.add(
        deps.audit(
            event_type="wallet.release.image",
            user_id=generation.user_id,
            details={
                "generation_id": generation.id,
                "amount_micro": tx.amount_micro,
                "balance_after": tx.balance_after,
                "hold_after": tx.hold_after,
                "reason": reason,
            },
        )
    )


async def settle_unknown_upstream_hold(
    session: AsyncSession,
    user_id: str,
    *,
    settlement: UnknownUpstreamSettlement,
    deps: UnknownUpstreamDependencies,
) -> None:
    if not await deps.wallet_billing_applies(
        session,
        user_id=user_id,
        ref_type=settlement.ref_type,
        ref_id=settlement.ref_id,
    ):
        return
    if not await deps.billing_enabled():
        return
    idempotency_key = f"settle:{settlement.ref_id}"
    existing = await deps.existing_wallet_tx(session, user_id, idempotency_key)
    if existing is not None:
        deps.add_replay_audit(
            session,
            user_id=user_id,
            tx=existing,
            replay_source="precheck",
        )
        return
    held = await deps.held_amount_for_ref(
        session,
        user_id,
        settlement.ref_type,
        settlement.ref_id,
    )
    if held <= 0:
        session.add(
            deps.audit(
                event_type="billing.unresolved_after_upstream",
                user_id=user_id,
                details={
                    "scope": settlement.no_hold_scope,
                    "reason": settlement.reason,
                    "knowledge": settlement.knowledge,
                    **settlement.no_hold_extra,
                },
            )
        )
        return
    tx = await deps.billing_core.settle(
        session,
        user_id,
        ref_type=settlement.ref_type,
        ref_id=settlement.ref_id,
        actual_micro=held,
        idempotency_key=idempotency_key,
        allow_negative=await deps.allow_negative_balance(),
        meta={
            **settlement.settle_meta,
            "tier_source": "upstream_result_unknown",
            "upstream_cost_knowledge": settlement.knowledge,
        },
    )
    if tx is None:
        return
    deps.record_balance_cache_refresh(
        session,
        user_id=user_id,
        balance_after=tx.balance_after,
    )
    session.add(
        deps.audit(
            event_type=settlement.settle_event,
            user_id=user_id,
            details={
                "amount_micro": tx.amount_micro,
                "balance_after": tx.balance_after,
                "hold_after": tx.hold_after,
                "reason": settlement.reason,
                "knowledge": settlement.knowledge,
                **settlement.settle_audit_extra,
            },
        )
    )
    if int((tx.meta or {}).get("overdraw_micro") or 0) > 0:
        deps.wallet_overdrawn_total.labels(kind="settle").inc()
        session.add(
            deps.audit(
                event_type="wallet.overdrawn",
                user_id=user_id,
                details={
                    "ref_type": settlement.ref_type,
                    "ref_id": settlement.ref_id,
                    "tx_id": tx.id,
                    **settlement.overdraw_extra,
                },
            )
        )
