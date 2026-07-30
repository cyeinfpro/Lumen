"""Completion usage, pricing, fingerprint, and rate-limit helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Completion
from lumen_core.pricing import (
    CostBreakdown,
    UsageTokens,
    build_request_fingerprint,
)

from .contracts import CompletionPricingDependencies


async def completion_cost_breakdown(
    session: AsyncSession,
    completion: Completion,
    *,
    usage: UsageTokens,
    rate_multiplier: int,
    service_tier: str,
    billing_core: Any,
    async_session_type: type,
) -> CostBreakdown:
    upstream_request = getattr(completion, "upstream_request", None)
    snapshot = (
        upstream_request.get("billing_pricing_snapshot")
        if isinstance(upstream_request, dict)
        else None
    )
    if isinstance(snapshot, dict):
        return billing_core.completion_breakdown_from_snapshot(
            snapshot,
            model=completion.model,
            tokens=usage,
            rate_multiplier_x10000=rate_multiplier,
            service_tier=service_tier,
        )
    if isinstance(session, async_session_type):
        return await billing_core.estimate_completion_breakdown(
            session,
            model=completion.model,
            tokens=usage,
            rate_multiplier_x10000=rate_multiplier,
            service_tier=service_tier,
        )
    cost = await billing_core.estimate_completion_cost(
        session,
        model=completion.model,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
        cache_creation_5m_tokens=usage.cache_creation_5m_tokens,
        cache_creation_1h_tokens=usage.cache_creation_1h_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        image_output_tokens=usage.image_output_tokens,
        rate_multiplier_x10000=rate_multiplier,
        service_tier=service_tier,
    )
    return CostBreakdown(
        input_cost_micro=cost,
        output_cost_micro=0,
        cache_read_cost_micro=0,
        cache_creation_cost_micro=0,
        image_output_cost_micro=0,
        reasoning_cost_micro=0,
        long_context_applied=False,
        priority_tier_applied=service_tier.lower()
        in {"priority", "flex_priority", "premium"},
        rate_multiplier_x10000=rate_multiplier,
        total_cost_micro=cost,
        actual_cost_micro=cost,
        pricing_source="test",
    )


def completion_usage(completion: Completion, *, cache_aware: bool) -> UsageTokens:
    def token_attr(name: str) -> int:
        try:
            return max(0, int(getattr(completion, name, 0) or 0))
        except (TypeError, ValueError):
            return 0

    legacy_cache_input_tokens = (
        0
        if cache_aware
        else token_attr("cache_read_tokens") + token_attr("cache_creation_tokens")
    )
    return UsageTokens(
        input_tokens=token_attr("tokens_in") + legacy_cache_input_tokens,
        output_tokens=token_attr("tokens_out"),
        cache_read_tokens=token_attr("cache_read_tokens") if cache_aware else 0,
        cache_creation_tokens=token_attr("cache_creation_tokens") if cache_aware else 0,
        cache_creation_5m_tokens=(
            token_attr("cache_creation_5m_tokens") if cache_aware else 0
        ),
        cache_creation_1h_tokens=(
            token_attr("cache_creation_1h_tokens") if cache_aware else 0
        ),
        reasoning_tokens=token_attr("reasoning_tokens") if cache_aware else 0,
        image_output_tokens=token_attr("image_output_tokens") if cache_aware else 0,
    ).normalized()


def held_amount_breakdown(
    held: int,
    *,
    rate_multiplier: int,
) -> CostBreakdown:
    return CostBreakdown(
        input_cost_micro=held,
        output_cost_micro=0,
        cache_read_cost_micro=0,
        cache_creation_cost_micro=0,
        image_output_cost_micro=0,
        reasoning_cost_micro=0,
        long_context_applied=False,
        priority_tier_applied=False,
        rate_multiplier_x10000=rate_multiplier,
        total_cost_micro=held,
        actual_cost_micro=held,
        pricing_source="held_amount_fallback",
    )


async def resolve_completion_breakdown(
    session: AsyncSession,
    completion: Completion,
    *,
    billing_ref_id: str,
    usage: UsageTokens,
    rate_multiplier: int,
    service_tier: str,
    deps: CompletionPricingDependencies,
) -> CostBreakdown | None:
    pricing_error: Any | None = None
    try:
        breakdown = await deps.completion_cost_breakdown(
            session,
            completion,
            usage=usage,
            rate_multiplier=rate_multiplier,
            service_tier=service_tier,
        )
    except deps.billing_core.BillingError as exc:
        if exc.code not in {"PRICING_MISSING", "PRICING_SNAPSHOT_INVALID"}:
            raise
        pricing_error = exc
        breakdown = None
    if breakdown is not None and (
        breakdown.actual_cost_micro > 0 or rate_multiplier == 0
    ):
        if breakdown.pricing_source == "fallback":
            session.add(
                deps.audit(
                    event_type="billing.pricing.fallback_used",
                    user_id=completion.user_id,
                    details={
                        "model": completion.model,
                        "completion_id": completion.id,
                        "usage": usage.model_dump(),
                    },
                )
            )
        return breakdown

    held = await deps.held_amount_for_ref(
        session,
        completion.user_id,
        "completion",
        billing_ref_id,
    )
    if held <= 0:
        if pricing_error is not None:
            session.add(
                deps.audit(
                    event_type="billing.unresolved_after_upstream",
                    user_id=completion.user_id,
                    details={
                        "scope": "chat_model",
                        "model": completion.model,
                        "completion_id": completion.id,
                        "usage": usage.model_dump(),
                        "error": pricing_error.message,
                    },
                )
            )
        return None
    session.add(
        deps.audit(
            event_type="billing.pricing.hold_fallback_after_upstream",
            user_id=completion.user_id,
            details={
                "scope": "chat_model",
                "model": completion.model,
                "completion_id": completion.id,
                "usage": usage.model_dump(),
                "actual_micro": held,
                "error": pricing_error.message if pricing_error is not None else None,
            },
        )
    )
    return held_amount_breakdown(held, rate_multiplier=rate_multiplier)


async def completion_request_fingerprint(
    session: AsyncSession,
    completion: Completion,
    *,
    idempotency_key: str,
    service_tier: str,
    usage: UsageTokens,
    breakdown: CostBreakdown,
    deps: CompletionPricingDependencies,
) -> tuple[str, bool]:
    fingerprint = build_request_fingerprint(
        user_id=completion.user_id,
        account_type="user",
        api_key_id=getattr(completion, "user_api_credential_id", None),
        request_id=completion.id,
        idempotency_key=idempotency_key,
        model=completion.model,
        service_tier=service_tier,
        billing_type=0,
        tokens=usage,
        cost=breakdown,
    )
    existing = await deps.existing_fingerprint_tx(
        session,
        completion.user_id,
        fingerprint,
    )
    if existing is None:
        return fingerprint, False
    deps.add_replay_audit(
        session,
        user_id=completion.user_id,
        tx=existing,
        replay_source="fingerprint",
    )
    return fingerprint, True


async def audit_completion_window_limit(
    session: AsyncSession,
    completion: Completion,
    *,
    cache: Any,
    key_id: str | None,
    cost: int,
    deps: CompletionPricingDependencies,
) -> None:
    if (
        cache is None
        or not isinstance(session, deps.async_session_type)
        or not key_id
        or not await deps.window_rate_limit_enabled()
    ):
        return
    allowed, window, window_usage = await cache.evaluate_rate_limits(
        session,
        key_id,
        cost,
    )
    if allowed:
        return
    deps.billing_rate_limit_block_total.labels(window=window or "unknown").inc()
    session.add(
        deps.audit(
            event_type="billing.rate_limit.exceeded_after_upstream",
            user_id=completion.user_id,
            details={
                "completion_id": completion.id,
                "api_key_id": key_id,
                "window": window,
                "used_micro": window_usage.used_micro,
                "limit_micro": window_usage.limit_micro,
                "projected_micro": cost,
            },
        )
    )


async def completion_window_rate_limit_failure(
    session: AsyncSession,
    completion: Completion,
    *,
    deps: CompletionPricingDependencies,
) -> tuple[str, str] | None:
    cache = deps.get_billing_cache()
    key_id = getattr(completion, "user_api_credential_id", None)
    if (
        cache is None
        or not isinstance(session, deps.async_session_type)
        or not key_id
        or not await deps.window_rate_limit_enabled()
    ):
        return None
    projected_micro = await deps.held_amount_for_ref(
        session,
        completion.user_id,
        "completion",
        deps.completion_billing_ref_id(completion),
    )
    if projected_micro <= 0:
        snapshot = deps.task_pricing_snapshot(completion)
        if snapshot is not None:
            try:
                preview = deps.billing_core.completion_breakdown_from_snapshot(
                    snapshot,
                    model=completion.model,
                    tokens=UsageTokens(input_tokens=1, output_tokens=1),
                    rate_multiplier_x10000=(
                        await deps.completion_rate_multiplier_x10000(
                            session,
                            completion,
                        )
                    ),
                    service_tier=deps.completion_service_tier(completion),
                )
                projected_micro = preview.actual_cost_micro
            except deps.billing_core.BillingError:
                projected_micro = 0
    if projected_micro <= 0:
        return None
    allowed, window, window_usage = await cache.evaluate_rate_limits(
        session,
        key_id,
        projected_micro,
    )
    if allowed:
        return None
    deps.billing_rate_limit_block_total.labels(window=window or "unknown").inc()
    session.add(
        deps.audit(
            event_type="billing.rate_limit.preflight_blocked",
            user_id=completion.user_id,
            details={
                "completion_id": completion.id,
                "api_key_id": key_id,
                "window": window,
                "used_micro": window_usage.used_micro,
                "limit_micro": window_usage.limit_micro,
                "projected_micro": projected_micro,
                "resets_at": (
                    window_usage.resets_at.isoformat()
                    if window_usage.resets_at is not None
                    else None
                ),
            },
        )
    )
    return (
        "billing_window_rate_limit",
        f"{window or 'billing'} spending window limit exceeded",
    )
