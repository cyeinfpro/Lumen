"""Worker-side billing facade and explicit domain dependency assembly."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import Completion, Generation, User, WalletTransaction
from lumen_core.pricing import CostBreakdown, UsageTokens

from .billing_parts import common as common_service
from .billing_parts import completion as completion_service
from .billing_parts import completion_pricing
from .billing_parts import generation as generation_service
from .billing_parts.helpers import (
    allow_negative_balance as _allow_negative_balance,
    apply_rate_multiplier_micro as _apply_rate_multiplier_micro,
    billing_enabled as _billing_enabled,
    cache_aware_enabled as _cache_aware_enabled,
    completion_billing_ref_id as _completion_billing_ref_id,
    completion_billing_retry_count as _completion_billing_retry_count,
    completion_service_tier as _completion_service_tier,
    generation_billing_ref_id as _generation_billing_ref_id,
    generation_billing_retry_count as _generation_billing_retry_count,
    generation_billing_tier as _generation_billing_tier,
    generation_settle_provider as _generation_settle_provider,
    generation_snapshot_cost as _generation_snapshot_cost,
    snapshot_rate_multiplier_x10000 as _snapshot_rate_multiplier_x10000,
    task_pricing_snapshot as _task_pricing_snapshot,
    thresholds as _thresholds,
    window_rate_limit_enabled as _window_rate_limit_enabled,
)
from .billing_parts.contracts import (
    CommonDependencies,
    CompletionDependencies,
    CompletionPricingDependencies,
    GenerationDependencies,
    UnknownUpstreamDependencies,
    UnknownUpstreamSettlement,
)
from .observability import (
    billing_cost_micro_total,
    billing_idempotency_replay_total,
    billing_pricing_source_total,
    billing_rate_limit_block_total,
    wallet_charge_lost_total,
    wallet_overdrawn_total,
)
from .services.billing_cache import get_billing_cache

_POST_COMMIT_BALANCE_CACHE_KEY = "lumen_post_commit_balance_cache"
_POST_COMMIT_WINDOW_CACHE_KEY = "lumen_post_commit_window_cache"

_audit = common_service.audit
_existing_wallet_tx = common_service.existing_wallet_tx
_record_balance_cache_refresh = common_service.record_balance_cache_refresh
_record_window_cache_increment = common_service.record_window_cache_increment
_ensure_billing_window_usage_event = common_service.ensure_billing_window_usage_event


def completion_billing_ref_id(completion: Completion) -> str:
    return _completion_billing_ref_id(completion)


async def _account_mode(session: AsyncSession, user_id: str) -> str:
    return (
        await session.execute(select(User.account_mode).where(User.id == user_id))
    ).scalar_one_or_none() or "wallet"


async def billing_enabled() -> bool:
    return await _billing_enabled()


async def allow_negative_balance() -> bool:
    return await _allow_negative_balance()


async def account_mode(session: AsyncSession, user_id: str) -> str:
    return await _account_mode(session, user_id)


async def held_amount_for_ref(
    session: AsyncSession,
    user_id: str,
    ref_type: str,
    ref_id: str,
) -> int:
    return await billing_core._held_amount_for_ref(  # noqa: SLF001
        session, user_id, ref_type, ref_id
    )


async def _rate_multiplier_x10000(session: AsyncSession, user_id: str) -> int:
    if not isinstance(session, AsyncSession):
        return 10_000
    raw = (
        await session.execute(
            select(User.billing_rate_multiplier).where(User.id == user_id)
        )
    ).scalar_one_or_none()
    return billing_core.parse_rate_multiplier_x10000(raw)


async def generation_rate_multiplier_x10000(
    session: AsyncSession,
    generation: Generation,
) -> int:
    snapshot = _snapshot_rate_multiplier_x10000(generation)
    if snapshot is not None:
        return snapshot
    return await _rate_multiplier_x10000(session, generation.user_id)


async def completion_rate_multiplier_x10000(
    session: AsyncSession,
    completion: Completion,
) -> int:
    snapshot = _snapshot_rate_multiplier_x10000(completion)
    if snapshot is not None:
        return snapshot
    return await _rate_multiplier_x10000(session, completion.user_id)


def _common_dependencies() -> CommonDependencies:
    return CommonDependencies(
        billing_core=billing_core,
        async_session_type=AsyncSession,
        get_billing_cache=get_billing_cache,
        audit=_audit,
        billing_idempotency_replay_total=billing_idempotency_replay_total,
    )


def _generation_dependencies() -> GenerationDependencies:
    return GenerationDependencies(
        billing_core=billing_core,
        wallet_billing_applies=_wallet_billing_applies,
        billing_enabled=_billing_enabled,
        existing_wallet_tx=_existing_wallet_tx,
        add_replay_audit=_add_replay_audit,
        generation_billing_ref_id=_generation_billing_ref_id,
        generation_billing_retry_count=_generation_billing_retry_count,
        generation_billing_tier=_generation_billing_tier,
        generation_snapshot_cost=_generation_snapshot_cost,
        generation_rate_multiplier_x10000=generation_rate_multiplier_x10000,
        apply_rate_multiplier_micro=_apply_rate_multiplier_micro,
        thresholds=_thresholds,
        held_amount_for_ref=held_amount_for_ref,
        allow_negative_balance=_allow_negative_balance,
        record_balance_cache_refresh=_record_balance_cache_refresh,
        audit=_audit,
        generation_settle_provider=_generation_settle_provider,
        wallet_overdrawn_total=wallet_overdrawn_total,
    )


def _unknown_upstream_dependencies() -> UnknownUpstreamDependencies:
    return UnknownUpstreamDependencies(
        billing_core=billing_core,
        wallet_billing_applies=_wallet_billing_applies,
        billing_enabled=_billing_enabled,
        existing_wallet_tx=_existing_wallet_tx,
        add_replay_audit=_add_replay_audit,
        held_amount_for_ref=held_amount_for_ref,
        allow_negative_balance=_allow_negative_balance,
        record_balance_cache_refresh=_record_balance_cache_refresh,
        audit=_audit,
        wallet_overdrawn_total=wallet_overdrawn_total,
    )


def _completion_pricing_dependencies() -> CompletionPricingDependencies:
    return CompletionPricingDependencies(
        billing_core=billing_core,
        async_session_type=AsyncSession,
        task_pricing_snapshot=_task_pricing_snapshot,
        existing_fingerprint_tx=_existing_fingerprint_tx,
        add_replay_audit=_add_replay_audit,
        held_amount_for_ref=held_amount_for_ref,
        completion_cost_breakdown=_completion_cost_breakdown,
        completion_rate_multiplier_x10000=completion_rate_multiplier_x10000,
        completion_service_tier=_completion_service_tier,
        completion_billing_ref_id=_completion_billing_ref_id,
        window_rate_limit_enabled=_window_rate_limit_enabled,
        get_billing_cache=get_billing_cache,
        audit=_audit,
        billing_rate_limit_block_total=billing_rate_limit_block_total,
    )


def _completion_dependencies() -> CompletionDependencies:
    return CompletionDependencies(
        billing_core=billing_core,
        async_session_type=AsyncSession,
        wallet_billing_applies=_wallet_billing_applies,
        billing_enabled=_billing_enabled,
        cache_aware_enabled=_cache_aware_enabled,
        existing_wallet_tx=_existing_wallet_tx,
        add_replay_audit=_add_replay_audit,
        completion_billing_ref_id=_completion_billing_ref_id,
        completion_billing_retry_count=_completion_billing_retry_count,
        completion_usage=_completion_usage,
        completion_rate_multiplier_x10000=completion_rate_multiplier_x10000,
        completion_service_tier=_completion_service_tier,
        resolve_completion_breakdown=_resolve_completion_breakdown,
        completion_request_fingerprint=_completion_request_fingerprint,
        audit_completion_window_limit=_audit_completion_window_limit,
        ensure_completion_image_charge_fundable=_ensure_completion_image_charge_fundable,
        allow_negative_balance=_allow_negative_balance,
        record_completion_settlement=_record_completion_settlement,
        record_balance_cache_refresh=_record_balance_cache_refresh,
        record_window_cache_increment=_record_window_cache_increment,
        ensure_billing_window_usage_event=_ensure_billing_window_usage_event,
        get_billing_cache=get_billing_cache,
        audit=_audit,
        billing_cost_micro_total=billing_cost_micro_total,
        billing_pricing_source_total=billing_pricing_source_total,
        wallet_charge_lost_total=wallet_charge_lost_total,
        wallet_overdrawn_total=wallet_overdrawn_total,
    )


async def _wallet_billing_applies(
    session: AsyncSession,
    *,
    user_id: str,
    ref_type: str,
    ref_id: str,
) -> bool:
    return await common_service.wallet_billing_applies(
        session,
        user_id=user_id,
        ref_type=ref_type,
        ref_id=ref_id,
        account_mode=_account_mode,
        billing_core=billing_core,
    )


async def _existing_fingerprint_tx(
    session: AsyncSession,
    user_id: str,
    fingerprint: str,
) -> WalletTransaction | None:
    return await common_service.existing_fingerprint_tx(
        session,
        user_id,
        fingerprint,
        async_session_type=AsyncSession,
    )


async def _ensure_completion_image_charge_fundable(
    session: AsyncSession,
    *,
    completion: Completion,
    billing_ref_id: str,
    image_output_cost_micro: int,
    rate_multiplier_x10000: int,
    allow_negative: bool,
) -> None:
    await common_service.ensure_completion_image_charge_fundable(
        session,
        completion=completion,
        billing_ref_id=billing_ref_id,
        image_output_cost_micro=image_output_cost_micro,
        rate_multiplier_x10000=rate_multiplier_x10000,
        allow_negative=allow_negative,
        deps=_common_dependencies(),
    )


def _add_replay_audit(
    session: AsyncSession,
    *,
    user_id: str,
    tx: WalletTransaction,
    replay_source: str,
) -> None:
    common_service.add_replay_audit(
        session,
        user_id=user_id,
        tx=tx,
        replay_source=replay_source,
        deps=_common_dependencies(),
    )


async def flush_balance_cache_refreshes(session: AsyncSession) -> None:
    await common_service.flush_balance_cache_refreshes(
        session,
        deps=_common_dependencies(),
    )


async def settle_generation(
    session: AsyncSession,
    generation: Generation,
    *,
    width: int,
    height: int,
    image_count: int = 1,
) -> None:
    await generation_service.settle_generation(
        session,
        generation,
        width=width,
        height=height,
        image_count=image_count,
        deps=_generation_dependencies(),
    )


async def release_generation(
    session: AsyncSession,
    generation: Generation,
    *,
    reason: str,
) -> None:
    await generation_service.release_generation(
        session,
        generation,
        reason=reason,
        deps=_generation_dependencies(),
    )


async def _settle_unknown_upstream_hold(
    session: AsyncSession,
    user_id: str,
    *,
    ref_type: str,
    ref_id: str,
    no_hold_scope: str,
    no_hold_extra: dict,
    settle_meta: dict,
    settle_event: str,
    settle_audit_extra: dict,
    overdraw_extra: dict,
    reason: str,
    knowledge: str,
) -> None:
    await generation_service.settle_unknown_upstream_hold(
        session,
        user_id,
        settlement=UnknownUpstreamSettlement(
            ref_type=ref_type,
            ref_id=ref_id,
            no_hold_scope=no_hold_scope,
            no_hold_extra=no_hold_extra,
            settle_meta=settle_meta,
            settle_event=settle_event,
            settle_audit_extra=settle_audit_extra,
            overdraw_extra=overdraw_extra,
            reason=reason,
            knowledge=knowledge,
        ),
        deps=_unknown_upstream_dependencies(),
    )


async def settle_generation_unknown_upstream(
    session: AsyncSession,
    generation: Generation,
    *,
    reason: str,
    knowledge: str,
) -> None:
    billing_ref_id = _generation_billing_ref_id(generation)
    await _settle_unknown_upstream_hold(
        session,
        generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
        no_hold_scope="image_result_unknown",
        no_hold_extra={"generation_id": generation.id},
        settle_meta={
            "generation_id": generation.id,
            "model": generation.model,
            "retry_count": _generation_billing_retry_count(generation),
            "provider": _generation_settle_provider(generation),
        },
        settle_event="wallet.settle.image_result_unknown",
        settle_audit_extra={"generation_id": generation.id},
        overdraw_extra={"generation_id": generation.id},
        reason=reason,
        knowledge=knowledge,
    )


async def settle_completion_unknown_upstream(
    session: AsyncSession,
    completion: Completion,
    *,
    reason: str,
    knowledge: str,
) -> None:
    billing_ref_id = _completion_billing_ref_id(completion)
    await _settle_unknown_upstream_hold(
        session,
        completion.user_id,
        ref_type="completion",
        ref_id=billing_ref_id,
        no_hold_scope="completion_result_unknown",
        no_hold_extra={"completion_id": completion.id},
        settle_meta={
            "completion_id": completion.id,
            "model": completion.model,
            "billing_retry_count": _completion_billing_retry_count(completion),
            "provider": getattr(completion, "upstream_supplier_id", None),
        },
        settle_event="wallet.settle.completion_result_unknown",
        settle_audit_extra={"completion_id": completion.id},
        overdraw_extra={"completion_id": completion.id},
        reason=reason,
        knowledge=knowledge,
    )


async def _completion_cost_breakdown(
    session: AsyncSession,
    completion: Completion,
    *,
    usage: UsageTokens,
    rate_multiplier: int,
    service_tier: str,
) -> CostBreakdown:
    return await completion_pricing.completion_cost_breakdown(
        session,
        completion,
        usage=usage,
        rate_multiplier=rate_multiplier,
        service_tier=service_tier,
        billing_core=billing_core,
        async_session_type=AsyncSession,
    )


def _completion_usage(completion: Completion, *, cache_aware: bool) -> UsageTokens:
    return completion_pricing.completion_usage(completion, cache_aware=cache_aware)


def _held_amount_breakdown(
    held: int,
    *,
    rate_multiplier: int,
) -> CostBreakdown:
    return completion_pricing.held_amount_breakdown(
        held,
        rate_multiplier=rate_multiplier,
    )


async def _resolve_completion_breakdown(
    session: AsyncSession,
    completion: Completion,
    *,
    billing_ref_id: str,
    usage: UsageTokens,
    rate_multiplier: int,
    service_tier: str,
) -> CostBreakdown | None:
    return await completion_pricing.resolve_completion_breakdown(
        session,
        completion,
        billing_ref_id=billing_ref_id,
        usage=usage,
        rate_multiplier=rate_multiplier,
        service_tier=service_tier,
        deps=_completion_pricing_dependencies(),
    )


async def _completion_request_fingerprint(
    session: AsyncSession,
    completion: Completion,
    *,
    idempotency_key: str,
    service_tier: str,
    usage: UsageTokens,
    breakdown: CostBreakdown,
) -> tuple[str, bool]:
    return await completion_pricing.completion_request_fingerprint(
        session,
        completion,
        idempotency_key=idempotency_key,
        service_tier=service_tier,
        usage=usage,
        breakdown=breakdown,
        deps=_completion_pricing_dependencies(),
    )


async def _audit_completion_window_limit(
    session: AsyncSession,
    completion: Completion,
    *,
    cache: Any,
    key_id: str | None,
    cost: int,
) -> None:
    await completion_pricing.audit_completion_window_limit(
        session,
        completion,
        cache=cache,
        key_id=key_id,
        cost=cost,
        deps=_completion_pricing_dependencies(),
    )


async def completion_window_rate_limit_failure(
    session: AsyncSession,
    completion: Completion,
) -> tuple[str, str] | None:
    return await completion_pricing.completion_window_rate_limit_failure(
        session,
        completion,
        deps=_completion_pricing_dependencies(),
    )


async def _record_completion_settlement(
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
) -> None:
    await completion_service.record_completion_settlement(
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
        deps=_completion_dependencies(),
    )


async def charge_completion(session: AsyncSession, completion: Completion) -> None:
    await completion_service.charge_completion(
        session,
        completion,
        deps=_completion_dependencies(),
    )


async def release_completion(
    session: AsyncSession,
    completion: Completion,
    *,
    reason: str,
) -> None:
    await completion_service.release_completion(
        session,
        completion,
        reason=reason,
        deps=_completion_dependencies(),
    )
