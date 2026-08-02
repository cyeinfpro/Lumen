"""Billing lifecycle for prompt enhancement streams."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.pricing import UsageTokens, parse_usage

from ...task_billing import EnhanceBillingContext, EnhanceUsageCapture


@dataclass(frozen=True)
class BillingRuntime:
    attempts: Sequence[Any]
    billing_enabled: Callable[[AsyncSession], Awaitable[bool]]
    billing_cache_aware: Callable[[AsyncSession], Awaitable[bool]]
    billing_allow_negative: Callable[[AsyncSession], Awaitable[bool]]
    new_id: Callable[[], str]
    rate_multiplier_x10000: Callable[[Any], int]
    pricing_snapshot_key: Callable[[str, str], str]
    invalidate_balance_cache: Callable[[str], Awaitable[None]]
    write_audit: Callable[..., Awaitable[Any]]
    hash_email: Callable[[str | None], str | None]
    logger: Any


async def prepare_prompt_enhance_billing(
    db: AsyncSession,
    user: Any,
    *,
    runtime: BillingRuntime,
) -> EnhanceBillingContext | None:
    if getattr(user, "account_mode", "wallet") != "wallet":
        return None
    if not await runtime.billing_enabled(db):
        return None

    request_id = runtime.new_id()
    rate_multiplier = runtime.rate_multiplier_x10000(user)
    cache_aware = await runtime.billing_cache_aware(db)
    allow_negative = await runtime.billing_allow_negative(db)
    pricing_snapshots: dict[str, dict[str, Any]] = {}
    preview = 0
    for attempt in runtime.attempts:
        service_tier = attempt.service_tier or "standard"
        snapshot_key = runtime.pricing_snapshot_key(attempt.model, service_tier)
        if snapshot_key in pricing_snapshots:
            continue
        try:
            snapshot = await billing_core.completion_pricing_snapshot(
                db,
                model=attempt.model,
                service_tier=service_tier,
            )
            attempt_preview = billing_core.completion_breakdown_from_snapshot(
                snapshot,
                model=attempt.model,
                tokens=billing_core.UsageTokens(input_tokens=1, output_tokens=1),
                rate_multiplier_x10000=rate_multiplier,
                service_tier=service_tier,
            ).actual_cost_micro
        except billing_core.BillingError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": {"code": exc.code, "message": exc.message}},
            ) from exc
        pricing_snapshots[snapshot_key] = snapshot
        preview = max(preview, int(attempt_preview))
    if preview <= 0 and rate_multiplier > 0:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "PRICING_MISSING",
                    "message": (
                        "missing enabled chat pricing rule for "
                        f"{runtime.attempts[0].model}"
                    ),
                }
            },
        )
    hold_amount = 0 if rate_multiplier == 0 else max(10_000, int(preview or 0))
    if hold_amount > 0:
        try:
            await billing_core.hold(
                db,
                user.id,
                hold_amount,
                ref_type="prompt_enhance",
                ref_id=request_id,
                idempotency_key=f"prompt_enhance:hold:{request_id}",
                allow_negative=allow_negative,
                meta={
                    "route": "prompts.enhance",
                    "model": runtime.attempts[0].model,
                    "service_tier": (
                        runtime.attempts[0].service_tier or "standard"
                    ),
                    "estimated_cost_micro": preview,
                    "preauth_micro": hold_amount,
                    "pricing_snapshots": pricing_snapshots,
                    "rate_multiplier_x10000": rate_multiplier,
                },
            )
        except billing_core.BillingError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": {"code": exc.code, "message": exc.message}},
            ) from exc
        await db.commit()
        await runtime.invalidate_balance_cache(user.id)
    return EnhanceBillingContext(
        db=db,
        user_id=user.id,
        user_email=getattr(user, "email", None),
        request_id=request_id,
        rate_multiplier_x10000=rate_multiplier,
        cache_aware=cache_aware,
        allow_negative=allow_negative,
        hold_amount_micro=hold_amount,
        pricing_snapshots=pricing_snapshots,
    )


def capture_enhance_usage(
    capture: EnhanceUsageCapture | None,
    event: dict[str, Any],
    *,
    provider: Any,
    attempt: Any,
    pricing_snapshot_key: Callable[[str, str], str],
) -> None:
    if capture is None:
        return
    response = event.get("response")
    response_obj = response if isinstance(response, dict) else {}
    usage = event.get("usage")
    if not isinstance(usage, dict):
        usage = response_obj.get("usage")
    if not isinstance(usage, dict):
        return

    response_id = response_obj.get("id") or event.get("response_id")
    model = response_obj.get("model") or event.get("model") or attempt.model
    capture.provider_name = provider.name
    capture.model = model if isinstance(model, str) and model.strip() else attempt.model
    capture.service_tier = attempt.service_tier or "standard"
    capture.pricing_snapshot_key = pricing_snapshot_key(
        attempt.model,
        capture.service_tier,
    )
    capture.response_id = (
        response_id if isinstance(response_id, str) and response_id.strip() else None
    )
    capture.usage = usage


def _normalize_usage_for_billing(
    usage: UsageTokens,
    *,
    cache_aware: bool,
) -> UsageTokens:
    if cache_aware:
        return usage.normalized()
    legacy_cache_input_tokens = usage.cache_read_tokens + usage.cache_creation_tokens
    return UsageTokens(
        input_tokens=usage.input_tokens + legacy_cache_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        image_output_tokens=usage.image_output_tokens,
    ).normalized()


def _usage_is_empty(usage: UsageTokens) -> bool:
    return all(
        value <= 0
        for value in (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_creation_tokens,
            usage.cache_creation_5m_tokens,
            usage.cache_creation_1h_tokens,
            usage.reasoning_tokens,
            usage.image_output_tokens,
        )
    )


def _held_amount_breakdown(
    billing: EnhanceBillingContext,
    *,
    cost: int | None = None,
) -> billing_core.CostBreakdown:
    actual_cost = billing.hold_amount_micro if cost is None else cost
    return billing_core.CostBreakdown(
        input_cost_micro=actual_cost,
        output_cost_micro=0,
        cache_read_cost_micro=0,
        cache_creation_cost_micro=0,
        image_output_cost_micro=0,
        reasoning_cost_micro=0,
        long_context_applied=False,
        priority_tier_applied=False,
        rate_multiplier_x10000=billing.rate_multiplier_x10000,
        total_cost_micro=actual_cost,
        actual_cost_micro=actual_cost,
        pricing_source="held_amount_fallback",
    )


async def _audit_held_amount_fallback(
    billing: EnhanceBillingContext,
    *,
    model: str,
    usage: UsageTokens,
    error: str,
    runtime: BillingRuntime,
) -> None:
    await runtime.write_audit(
        billing.db,
        event_type="billing.pricing.hold_fallback_after_upstream",
        user_id=billing.user_id,
        actor_email_hash=runtime.hash_email(billing.user_email),
        details={
            "scope": "chat_model",
            "model": model,
            "prompt_enhance_id": billing.request_id,
            "usage": usage.model_dump(),
            "actual_micro": billing.hold_amount_micro,
            "error": error,
        },
        autocommit=False,
    )


async def _resolve_breakdown(
    billing: EnhanceBillingContext,
    capture: EnhanceUsageCapture,
    *,
    model: str,
    usage: UsageTokens,
    runtime: BillingRuntime,
) -> billing_core.CostBreakdown:
    try:
        snapshot = billing.pricing_snapshots.get(
            capture.pricing_snapshot_key
            or runtime.pricing_snapshot_key(model, capture.service_tier)
        )
        if snapshot is not None:
            return billing_core.completion_breakdown_from_snapshot(
                snapshot,
                model=model,
                tokens=usage,
                rate_multiplier_x10000=billing.rate_multiplier_x10000,
                service_tier=capture.service_tier,
            )
        return await billing_core.estimate_completion_breakdown(
            billing.db,
            model=model,
            tokens=usage,
            rate_multiplier_x10000=billing.rate_multiplier_x10000,
            service_tier=capture.service_tier,
        )
    except billing_core.BillingError as exc:
        if (
            exc.code not in {"PRICING_MISSING", "PRICING_SNAPSHOT_INVALID"}
            or billing.hold_amount_micro <= 0
        ):
            raise
        await _audit_held_amount_fallback(
            billing,
            model=model,
            usage=usage,
            error=exc.message,
            runtime=runtime,
        )
        return _held_amount_breakdown(billing)


def _effective_cost(
    billing: EnhanceBillingContext,
    breakdown: billing_core.CostBreakdown,
) -> tuple[int, billing_core.CostBreakdown]:
    cost = breakdown.actual_cost_micro
    if cost > 0 or billing.hold_amount_micro <= 0:
        return cost, breakdown
    cost = billing.hold_amount_micro
    return cost, _held_amount_breakdown(billing, cost=cost)


async def _audit_fallback_pricing(
    billing: EnhanceBillingContext,
    *,
    breakdown: billing_core.CostBreakdown,
    model: str,
    ref_id: str,
    usage: UsageTokens,
    runtime: BillingRuntime,
) -> None:
    if breakdown.pricing_source != "fallback":
        return
    await runtime.write_audit(
        billing.db,
        event_type="billing.pricing.fallback_used",
        user_id=billing.user_id,
        actor_email_hash=runtime.hash_email(billing.user_email),
        details={
            "model": model,
            "prompt_enhance_id": ref_id,
            "usage": usage.model_dump(),
        },
        autocommit=False,
    )


def _transaction_meta(
    billing: EnhanceBillingContext,
    capture: EnhanceUsageCapture,
    *,
    breakdown: billing_core.CostBreakdown,
    model: str,
    response_id: str,
    usage: UsageTokens,
) -> dict[str, Any]:
    return {
        "route": "prompts.enhance",
        "model": model,
        "provider": capture.provider_name,
        "response_id": response_id,
        "tokens_in": usage.input_tokens,
        "tokens_out": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_creation_5m_tokens": usage.cache_creation_5m_tokens,
        "cache_creation_1h_tokens": usage.cache_creation_1h_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "image_output_tokens": usage.image_output_tokens,
        "cost_breakdown": breakdown.model_dump(),
        "rate_multiplier_x10000": billing.rate_multiplier_x10000,
        "service_tier": capture.service_tier,
    }


async def _settle_or_charge(
    billing: EnhanceBillingContext,
    *,
    cost: int,
    ref_id: str,
    transaction_meta: dict[str, Any],
) -> Any:
    if billing.hold_amount_micro > 0:
        return await billing_core.settle(
            billing.db,
            billing.user_id,
            ref_type="prompt_enhance",
            ref_id=ref_id,
            actual_micro=cost,
            idempotency_key=f"prompt_enhance:settle:{ref_id}",
            allow_negative=billing.allow_negative,
            meta={**transaction_meta, "preauth_micro": billing.hold_amount_micro},
        )
    return await billing_core.charge(
        billing.db,
        billing.user_id,
        cost,
        ref_type="prompt_enhance",
        ref_id=ref_id,
        idempotency_key=f"prompt_enhance:{ref_id}",
        allow_negative=billing.allow_negative,
        record_zero=True,
        kind="charge_completion",
        meta=transaction_meta,
    )


async def _audit_charge(
    billing: EnhanceBillingContext,
    capture: EnhanceUsageCapture,
    transaction: Any,
    *,
    breakdown: billing_core.CostBreakdown,
    cost: int,
    ref_id: str,
    response_id: str,
    usage: UsageTokens,
    runtime: BillingRuntime,
) -> None:
    if transaction is None:
        return
    await runtime.write_audit(
        billing.db,
        event_type="wallet.charge.completion",
        user_id=billing.user_id,
        actor_email_hash=runtime.hash_email(billing.user_email),
        details={
            "completion_id": ref_id,
            "prompt_enhance_id": ref_id,
            "response_id": response_id,
            "route": "prompts.enhance",
            "cost_micro": cost,
            "usage": usage.model_dump(),
            "cost_breakdown": breakdown.model_dump(),
            "service_tier": capture.service_tier,
            "amount_micro": transaction.amount_micro,
            "balance_after": transaction.balance_after,
        },
        autocommit=False,
    )
    if cost != 0 or billing.rate_multiplier_x10000 != 0:
        return
    await runtime.write_audit(
        billing.db,
        event_type="wallet.charge.zero_rate",
        user_id=billing.user_id,
        actor_email_hash=runtime.hash_email(billing.user_email),
        details={
            "prompt_enhance_id": ref_id,
            "response_id": response_id,
            "tx_id": transaction.id,
            "ref_type": "prompt_enhance",
            "ref_id": ref_id,
            "rate_multiplier_x10000": 0,
        },
        autocommit=False,
    )


async def charge_prompt_enhance(
    billing: EnhanceBillingContext,
    capture: EnhanceUsageCapture,
    *,
    runtime: BillingRuntime,
) -> None:
    if not capture.usage:
        # 内容已完整交付、上游必然已计费,只是拿不到用量:不得 fail-open 释放,
        # 按 hold 默认金额结算(纯转嫁:上游扣费用户必付)。
        await settle_prompt_enhance_default_hold(
            billing,
            reason="missing_usage",
            runtime=runtime,
        )
        return
    model = capture.model or runtime.attempts[0].model
    usage = _normalize_usage_for_billing(
        parse_usage(model, capture.usage),
        cache_aware=billing.cache_aware,
    )
    if _usage_is_empty(usage):
        await settle_prompt_enhance_default_hold(
            billing,
            reason="zero_usage",
            runtime=runtime,
        )
        return
    breakdown = await _resolve_breakdown(
        billing,
        capture,
        model=model,
        usage=usage,
        runtime=runtime,
    )
    response_id = capture.response_id or billing.request_id
    ref_id = billing.request_id if billing.hold_amount_micro > 0 else response_id
    cost, breakdown = _effective_cost(billing, breakdown)
    await _audit_fallback_pricing(
        billing,
        breakdown=breakdown,
        model=model,
        ref_id=ref_id,
        usage=usage,
        runtime=runtime,
    )
    transaction_meta = _transaction_meta(
        billing,
        capture,
        breakdown=breakdown,
        model=model,
        response_id=response_id,
        usage=usage,
    )
    transaction = await _settle_or_charge(
        billing,
        cost=cost,
        ref_id=ref_id,
        transaction_meta=transaction_meta,
    )
    await _audit_charge(
        billing,
        capture,
        transaction,
        breakdown=breakdown,
        cost=cost,
        ref_id=ref_id,
        response_id=response_id,
        usage=usage,
        runtime=runtime,
    )
    await billing.db.commit()
    if transaction is not None:
        await runtime.invalidate_balance_cache(billing.user_id)


async def settle_prompt_enhance_default_hold(
    billing: EnhanceBillingContext | None,
    *,
    reason: str,
    runtime: BillingRuntime,
) -> None:
    """按 hold 默认金额结算「上游成本已发生但真实用量不可知」的路径。

    纯转嫁铁律与 lumen_core.upstream_billing 决策表(仅 PROVEN_ABSENT 才
    RELEASE):已产出内容、读超时、上游处理中失败、客户端断流、计费失败、
    用量缺失等一律按默认金额(hold)结算,不得 fail-open 释放 hold 让平台
    吸收上游成本;真实成本差额交给对账。hold <= 0(零费率)时无成本可记。
    """
    if billing is None or billing.hold_amount_micro <= 0:
        return
    # 结算尝试标记:链外孤儿兜底释放据此跳过(见 EnhanceSettleOutcome)。
    billing.settle_outcome.attempted = True
    try:
        model = runtime.attempts[0].model if runtime.attempts else None
        await _settle_or_charge(
            billing,
            cost=billing.hold_amount_micro,
            ref_id=billing.request_id,
            transaction_meta={
                "route": "prompts.enhance",
                "model": model,
                "reason": reason,
                "settle_source": "unknown_upstream_hold",
            },
        )
        await billing.db.commit()
        await runtime.invalidate_balance_cache(billing.user_id)
    except Exception:
        # 结算落库失败:attempted 已置位,兜底不会 fail-open 释放,hold 保留在
        # 钱包中由管理端孤儿扫描对账;日志带上 request_id 便于人工追回。
        runtime.logger.exception(
            "prompt enhance billing default settle failed request_id=%s hold_micro=%d",
            billing.request_id,
            billing.hold_amount_micro,
        )


async def release_prompt_enhance_hold(
    billing: EnhanceBillingContext | None,
    *,
    reason: str,
    runtime: BillingRuntime,
) -> None:
    if billing is None or billing.hold_amount_micro <= 0:
        return
    try:
        await billing_core.release(
            billing.db,
            billing.user_id,
            ref_type="prompt_enhance",
            ref_id=billing.request_id,
            idempotency_key=f"prompt_enhance:release:{billing.request_id}:{reason}",
            meta={"route": "prompts.enhance", "reason": reason},
        )
        await billing.db.commit()
        await runtime.invalidate_balance_cache(billing.user_id)
    except Exception:
        runtime.logger.exception("prompt enhance billing hold release failed")
