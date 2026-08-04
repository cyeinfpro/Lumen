"""Image generation billing settlement and release services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Generation

from .common import (
    billing_obligation_is_unsettleable,
    mark_billing_obligation_unsettleable,
    terminal_billing_applies,
)
from .contracts import (
    GenerationDependencies,
    UnknownUpstreamDependencies,
    UnknownUpstreamSettlement,
)
from .helpers import task_pricing_snapshot

BONUS_BILLING_OBLIGATION_KEY = "bonus_billing_obligation"


def _generation_billing_obligation(generation: Generation) -> bool:
    request = getattr(generation, "upstream_request", None)
    return bool(
        task_pricing_snapshot(generation) is not None
        or (
            isinstance(request, dict)
            and request.get(BONUS_BILLING_OBLIGATION_KEY) is True
            and request.get("billing_free") is not True
        )
    )


@dataclass(frozen=True)
class _GenerationCost:
    """settle_generation 的成本解析结果(定价/档位/回退来源)。"""

    cost: int
    tier: str | None
    tier_source: str
    zero_rate: bool
    pricing_error: Any | None
    billable_image_count: int
    rate_multiplier: int
    billing_ref_id: str


async def _resolve_generation_cost(
    session: AsyncSession,
    generation: Generation,
    *,
    width: int,
    height: int,
    image_count: int,
    billing_ref_id: str,
    deps: GenerationDependencies,
) -> _GenerationCost:
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
            # 与 settle_unknown_upstream_hold 同一语义:被先前 release 消费的
            # hold 不是真实结算,定价缺失时按被退回的金额补记,不能吞掉已发生
            # 的上游成本;从未建 hold 或已有真实 settle 则保留对账记录。
            consumed = await deps.existing_ref_consumption_tx(
                session,
                generation.user_id,
                "generation",
                billing_ref_id,
            )
            released = (
                max(0, int(getattr(consumed, "amount_micro", 0) or 0))
                if consumed is not None and consumed.kind == "release"
                else 0
            )
            if released <= 0:
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
                            "error": (
                                pricing_error.message if pricing_error else None
                            ),
                        },
                    )
                )
                return _GenerationCost(
                    cost=0,
                    tier=tier,
                    tier_source="unresolved",
                    zero_rate=zero_rate,
                    pricing_error=pricing_error,
                    billable_image_count=billable_image_count,
                    rate_multiplier=rate_multiplier,
                    billing_ref_id=billing_ref_id,
                )
            cost = released
            tier_source = "released_hold_fallback"
            session.add(
                deps.audit(
                    event_type=(
                        "billing.pricing.released_hold_fallback_after_upstream"
                    ),
                    user_id=generation.user_id,
                    details={
                        "scope": "image_size",
                        "tier": tier,
                        "generation_id": generation.id,
                        "width": width,
                        "height": height,
                        "image_count": billable_image_count,
                        "actual_micro": cost,
                        "error": (
                            pricing_error.message if pricing_error else None
                        ),
                    },
                )
            )
        else:
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
                        "error": (
                            pricing_error.message if pricing_error else None
                        ),
                    },
                )
            )
    return _GenerationCost(
        cost=cost,
        tier=tier,
        tier_source=tier_source,
        zero_rate=zero_rate,
        pricing_error=pricing_error,
        billable_image_count=billable_image_count,
        rate_multiplier=rate_multiplier,
        billing_ref_id=billing_ref_id,
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
    request = getattr(generation, "upstream_request", None)
    if isinstance(request, dict) and request.get("billing_free") is True:
        return
    if billing_obligation_is_unsettleable(generation):
        return
    billing_obligation = _generation_billing_obligation(generation)
    if not await deps.wallet_billing_applies(
        session,
        user_id=generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
    ):
        return
    if not await terminal_billing_applies(
        session,
        user_id=generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
        billing_enabled=deps.billing_enabled,
        billing_obligation=billing_obligation,
        billing_core=deps.billing_core,
    ):
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
    resolved = await _resolve_generation_cost(
        session,
        generation,
        width=width,
        height=height,
        image_count=image_count,
        billing_ref_id=billing_ref_id,
        deps=deps,
    )
    if resolved.tier_source == "unresolved":
        if billing_obligation:
            mark_billing_obligation_unsettleable(
                generation,
                reason=(
                    "pricing_missing_without_hold"
                    if resolved.pricing_error is not None
                    else "non_positive_pricing_without_hold"
                ),
            )
        return
    cost = resolved.cost
    tier = resolved.tier
    tier_source = resolved.tier_source
    zero_rate = resolved.zero_rate
    billable_image_count = resolved.billable_image_count
    rate_multiplier = resolved.rate_multiplier
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
    if not await terminal_billing_applies(
        session,
        user_id=generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
        billing_enabled=deps.billing_enabled,
        billing_obligation=task_pricing_snapshot(generation) is not None,
        billing_core=deps.billing_core,
    ):
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
    if not await terminal_billing_applies(
        session,
        user_id=user_id,
        ref_type=settlement.ref_type,
        ref_id=settlement.ref_id,
        billing_enabled=deps.billing_enabled,
        billing_obligation=settlement.billing_obligation,
        billing_core=deps.billing_core,
    ):
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
        # 核心 settle 的 held=0 直扣语义:先 release 只是退回 hold、不算真实
        # 结算(见 billing_core.billing.settle 的注释)。若该 ref 的 hold 已被
        # 先前的 release 消费,而此后上游确认/可能已扣费,必须按被退回的金额
        # 补记,否则上游成本由平台全额吸收。已存在真实 settle 或从未建过 hold
        # 时没有可补记的金额,保留对账记录。
        consumed = await deps.existing_ref_consumption_tx(
            session,
            user_id,
            settlement.ref_type,
            settlement.ref_id,
        )
        released = (
            max(0, int(getattr(consumed, "amount_micro", 0) or 0))
            if consumed is not None and consumed.kind == "release"
            else 0
        )
        if released <= 0:
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
        held = released
        session.add(
            deps.audit(
                event_type="billing.pricing.released_hold_fallback_after_upstream",
                user_id=user_id,
                details={
                    "scope": settlement.no_hold_scope,
                    "reason": settlement.reason,
                    "knowledge": settlement.knowledge,
                    "actual_micro": held,
                    **settlement.no_hold_extra,
                },
            )
        )
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
