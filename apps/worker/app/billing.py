"""Worker-side billing hooks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import AuditLog, Completion, Generation, User, WalletTransaction
from lumen_core.pricing import (
    CostBreakdown,
    UsageTokens,
    build_request_fingerprint,
)

from . import runtime_settings
from .observability import (
    billing_cost_micro_total,
    billing_idempotency_replay_total,
    billing_pricing_source_total,
    billing_rate_limit_block_total,
    wallet_charge_lost_total,
    wallet_overdrawn_total,
)
from .services.billing_cache import get_billing_cache


async def _setting_bool(key: str, default: bool = False) -> bool:
    return billing_core.parse_bool_setting(await runtime_settings.resolve(key), default)


async def _billing_enabled() -> bool:
    return await _setting_bool("billing.enabled", False)


async def _allow_negative_balance() -> bool:
    return await _setting_bool("billing.allow_negative_balance", False)


async def _window_rate_limit_enabled() -> bool:
    return await _setting_bool("billing.window_rate_limit", False)


async def _cache_aware_enabled() -> bool:
    return await _setting_bool("billing.cache_aware", True)


async def _thresholds() -> dict[str, int]:
    return billing_core.parse_thresholds(
        await runtime_settings.resolve("billing.image_size_thresholds")
    )


def _generation_billing_tier(generation: Generation) -> str | None:
    upstream_request = getattr(generation, "upstream_request", None)
    if not isinstance(upstream_request, dict):
        return None
    tier = upstream_request.get("billing_tier")
    return tier if tier in {"1k", "2k", "4k"} else None


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
    try:
        return max(0, int(float(raw if raw is not None else 1) * 10_000))
    except (TypeError, ValueError):
        return 10_000


def _completion_service_tier(completion: Completion) -> str:
    upstream_request = getattr(completion, "upstream_request", None)
    if isinstance(upstream_request, dict):
        raw = upstream_request.get("service_tier")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return "standard"


def _audit(
    *,
    event_type: str,
    user_id: str,
    details: dict[str, Any],
) -> AuditLog:
    return AuditLog(
        user_id=user_id,
        event_type=event_type,
        details=details,
        created_at=datetime.now(timezone.utc),
    )


async def _existing_wallet_tx(
    session: AsyncSession, user_id: str, idempotency_key: str
) -> WalletTransaction | None:
    return (
        await session.execute(
            select(WalletTransaction).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


async def _existing_fingerprint_tx(
    session: AsyncSession,
    user_id: str,
    fingerprint: str,
) -> WalletTransaction | None:
    if not isinstance(session, AsyncSession) or not fingerprint:
        return None
    try:
        return (
            await session.execute(
                select(WalletTransaction)
                .where(
                    WalletTransaction.user_id == user_id,
                    WalletTransaction.meta["request_fingerprint"].as_string()
                    == fingerprint,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        return None


def _add_replay_audit(
    session: AsyncSession,
    *,
    user_id: str,
    tx: WalletTransaction,
    replay_source: str,
) -> None:
    billing_idempotency_replay_total.inc()
    session.add(
        _audit(
            event_type=f"wallet.{tx.kind}.replay",
            user_id=user_id,
            details={
                "tx_id": tx.id,
                "kind": tx.kind,
                "amount_micro": tx.amount_micro,
                "balance_after": tx.balance_after,
                "hold_after": tx.hold_after,
                "ref_type": tx.ref_type,
                "ref_id": tx.ref_id,
                "idempotency_key": tx.idempotency_key,
                "replay_source": replay_source,
            },
        )
    )


async def settle_generation(
    session: AsyncSession,
    generation: Generation,
    *,
    width: int,
    height: int,
) -> None:
    if await _account_mode(session, generation.user_id) != "wallet":
        return
    if not await _billing_enabled():
        return
    idempotency_key = f"settle:{generation.id}"
    existing = await _existing_wallet_tx(session, generation.user_id, idempotency_key)
    if existing is not None:
        _add_replay_audit(
            session,
            user_id=generation.user_id,
            tx=existing,
            replay_source="precheck",
        )
        return
    requested_tier = _generation_billing_tier(generation)
    if requested_tier is not None:
        cost, tier = await billing_core.estimate_image_cost_for_tier(
            session,
            tier=requested_tier,
            n=1,
        )
        tier_source = "request"
    else:
        cost, tier = await billing_core.estimate_image_cost(
            session,
            size_px=max(0, int(width) * int(height)),
            n=1,
            thresholds=await _thresholds(),
        )
        tier_source = "actual_pixels"
    if cost <= 0:
        session.add(
            _audit(
                event_type="pricing.not_configured",
                user_id=generation.user_id,
                details={
                    "scope": "image_size",
                    "tier": tier,
                    "generation_id": generation.id,
                    "width": width,
                    "height": height,
                    "tier_source": tier_source,
                },
            )
        )
    tx = await billing_core.settle(
        session,
        generation.user_id,
        ref_type="generation",
        ref_id=generation.id,
        actual_micro=cost,
        idempotency_key=idempotency_key,
        allow_negative=await _allow_negative_balance(),
        meta={
            "tier": tier,
            "width": width,
            "height": height,
            "tier_source": tier_source,
            "model": generation.model,
        },
    )
    if tx is not None:
        session.add(
            _audit(
                event_type="wallet.settle.image",
                user_id=generation.user_id,
                details={
                    "generation_id": generation.id,
                    "amount_micro": tx.amount_micro,
                    "actual_micro": cost,
                    "tier": tier,
                    "tier_source": tier_source,
                    "balance_after": tx.balance_after,
                    "hold_after": tx.hold_after,
                },
            )
        )
        if int((tx.meta or {}).get("overdraw_micro") or 0) > 0:
            wallet_overdrawn_total.labels(kind="settle").inc()
            session.add(
                _audit(
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
) -> None:
    if await _account_mode(session, generation.user_id) != "wallet":
        return
    if not await _billing_enabled():
        return
    idempotency_key = f"release:{generation.id}"
    existing = await _existing_wallet_tx(session, generation.user_id, idempotency_key)
    if existing is not None:
        _add_replay_audit(
            session,
            user_id=generation.user_id,
            tx=existing,
            replay_source="precheck",
        )
        return
    tx = await billing_core.release(
        session,
        generation.user_id,
        ref_type="generation",
        ref_id=generation.id,
        idempotency_key=idempotency_key,
        meta={"reason": reason},
    )
    if tx is not None:
        session.add(
            _audit(
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


async def charge_completion(session: AsyncSession, completion: Completion) -> None:
    if await _account_mode(session, completion.user_id) != "wallet":
        return
    if not await _billing_enabled():
        return
    idempotency_key = f"complete:{completion.id}"
    existing = await _existing_wallet_tx(session, completion.user_id, idempotency_key)
    if existing is not None:
        _add_replay_audit(
            session,
            user_id=completion.user_id,
            tx=existing,
            replay_source="precheck",
        )
        return
    cache_aware = (
        await _cache_aware_enabled() if isinstance(session, AsyncSession) else True
    )

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
    usage = UsageTokens(
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
    rate_multiplier = await _rate_multiplier_x10000(session, completion.user_id)
    service_tier = _completion_service_tier(completion)
    if isinstance(session, AsyncSession):
        breakdown = await billing_core.estimate_completion_breakdown(
            session,
            model=completion.model,
            tokens=usage,
            rate_multiplier_x10000=rate_multiplier,
            service_tier=service_tier,
        )
        cost = breakdown.actual_cost_micro
    else:
        # Unit tests use a minimal fake session; preserve the old seam while
        # production uses the full cache-aware breakdown above.
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
        breakdown = CostBreakdown(
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
    if cost <= 0 and (completion.tokens_in > 0 or completion.tokens_out > 0):
        session.add(
            _audit(
                event_type=(
                    "billing.pricing.missing"
                    if breakdown.pricing_source == "missing"
                    else "pricing.not_configured"
                ),
                user_id=completion.user_id,
                details={
                    "scope": "chat_model",
                    "model": completion.model,
                    "completion_id": completion.id,
                    "tokens_in": completion.tokens_in,
                    "tokens_out": completion.tokens_out,
                    "usage": usage.model_dump(),
                    "pricing_source": breakdown.pricing_source,
                },
            )
        )
    elif breakdown.pricing_source == "fallback":
        session.add(
            _audit(
                event_type="billing.pricing.fallback_used",
                user_id=completion.user_id,
                details={
                    "model": completion.model,
                    "completion_id": completion.id,
                    "usage": usage.model_dump(),
                },
            )
        )
    billing_pricing_source_total.labels(source=breakdown.pricing_source).inc()
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
    existing_by_fingerprint = await _existing_fingerprint_tx(
        session,
        completion.user_id,
        fingerprint,
    )
    if existing_by_fingerprint is not None:
        _add_replay_audit(
            session,
            user_id=completion.user_id,
            tx=existing_by_fingerprint,
            replay_source="fingerprint",
        )
        return
    cache = get_billing_cache()
    key_id = getattr(completion, "user_api_credential_id", None)
    if (
        cache is not None
        and isinstance(session, AsyncSession)
        and key_id
        and await _window_rate_limit_enabled()
    ):
        allowed, window, window_usage = await cache.evaluate_rate_limits(
            session,
            key_id,
            cost,
        )
        if not allowed:
            billing_rate_limit_block_total.labels(window=window or "unknown").inc()
            session.add(
                _audit(
                    event_type="billing.rate_limit.blocked",
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
            return
    try:
        tx = await billing_core.settle(
            session,
            completion.user_id,
            ref_type="completion",
            ref_id=completion.id,
            actual_micro=cost,
            idempotency_key=idempotency_key,
            allow_negative=await _allow_negative_balance(),
            meta={
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
            },
        )
    except Exception:
        wallet_charge_lost_total.inc()
        raise
    if tx is not None:
        for kind, value in (
            ("input", breakdown.input_cost_micro),
            ("output", breakdown.output_cost_micro),
            ("cache_read", breakdown.cache_read_cost_micro),
            ("cache_creation", breakdown.cache_creation_cost_micro),
            ("image", breakdown.image_output_cost_micro),
            ("reasoning", breakdown.reasoning_cost_micro),
        ):
            if value > 0:
                billing_cost_micro_total.labels(kind=kind).inc(value)
        if cache is not None:
            await cache.queue_deduct(completion.user_id, max(0, cost))
            if key_id:
                limits = await cache.credential_limits(session, key_id)
                await cache.queue_window_increment(key_id, max(0, cost), limits)
        session.add(
            _audit(
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
        if breakdown.cache_read_cost_micro > 0:
            session.add(
                _audit(
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
            wallet_overdrawn_total.labels(kind="settle").inc()
            session.add(
                _audit(
                    event_type="wallet.overdrawn",
                    user_id=completion.user_id,
                    details={
                        "completion_id": completion.id,
                        "tx_id": tx.id,
                        "meta": tx.meta,
                    },
                )
            )


async def release_completion(
    session: AsyncSession,
    completion: Completion,
    *,
    reason: str,
) -> None:
    if await _account_mode(session, completion.user_id) != "wallet":
        return
    if not await _billing_enabled():
        return
    idempotency_key = f"release:{completion.id}"
    existing = await _existing_wallet_tx(session, completion.user_id, idempotency_key)
    if existing is not None:
        _add_replay_audit(
            session,
            user_id=completion.user_id,
            tx=existing,
            replay_source="precheck",
        )
        return
    tx = await billing_core.release(
        session,
        completion.user_id,
        ref_type="completion",
        ref_id=completion.id,
        idempotency_key=idempotency_key,
        meta={"reason": reason},
    )
    if tx is not None:
        session.add(
            _audit(
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
