"""Worker-side billing hooks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import (
    AuditLog,
    BillingWindowUsageEvent,
    Completion,
    Generation,
    User,
    WalletTransaction,
)
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

_POST_COMMIT_BALANCE_CACHE_KEY = "lumen_post_commit_balance_cache"
_POST_COMMIT_WINDOW_CACHE_KEY = "lumen_post_commit_window_cache"


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


def _task_pricing_snapshot(task: Generation | Completion) -> dict[str, Any] | None:
    upstream_request = getattr(task, "upstream_request", None)
    if not isinstance(upstream_request, dict):
        return None
    snapshot = upstream_request.get("billing_pricing_snapshot")
    return snapshot if isinstance(snapshot, dict) else None


def _generation_snapshot_cost(
    generation: Generation,
    *,
    image_count: int,
) -> tuple[int, str] | None:
    snapshot = _task_pricing_snapshot(generation)
    if not snapshot or snapshot.get("kind") != "image":
        return None
    try:
        unit_price = int(snapshot.get("unit_price_micro") or 0)
    except (TypeError, ValueError):
        return None
    tier = snapshot.get("tier")
    if unit_price <= 0 or not isinstance(tier, str) or not tier:
        return None
    return unit_price * max(1, int(image_count)), tier


def _apply_rate_multiplier_micro(amount_micro: int, multiplier_x10000: int) -> int:
    amount = max(0, int(amount_micro or 0))
    multiplier = max(0, int(multiplier_x10000 or 0))
    if amount == 0 or multiplier == 0:
        return 0
    return max(1, (amount * multiplier) // 10_000)


def _generation_billing_ref_id(generation: Generation) -> str:
    return billing_core.generation_billing_ref_id(generation)


def _generation_billing_retry_count(generation: Generation) -> int:
    return billing_core.generation_billing_retry_count(generation)


def _completion_billing_ref_id(completion: Completion) -> str:
    return billing_core.completion_billing_ref_id(completion)


def completion_billing_ref_id(completion: Completion) -> str:
    return _completion_billing_ref_id(completion)


def _completion_billing_retry_count(completion: Completion) -> int:
    return billing_core.completion_billing_retry_count(completion)


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


def _snapshot_rate_multiplier_x10000(task: Generation | Completion) -> int | None:
    upstream_request = getattr(task, "upstream_request", None)
    if not isinstance(upstream_request, dict):
        return None
    raw = upstream_request.get("billing_rate_multiplier_x10000")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


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


async def _wallet_billing_applies(
    session: AsyncSession,
    *,
    user_id: str,
    ref_type: str,
    ref_id: str,
) -> bool:
    if await _account_mode(session, user_id) == "wallet":
        return True
    return (
        await billing_core._held_amount_for_ref(  # noqa: SLF001
            session,
            user_id,
            ref_type,
            ref_id,
        )
        > 0
    )


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


async def _ensure_completion_image_charge_fundable(
    session: AsyncSession,
    *,
    completion: Completion,
    billing_ref_id: str,
    image_output_cost_micro: int,
    rate_multiplier_x10000: int,
    allow_negative: bool,
) -> None:
    if allow_negative or not isinstance(session, AsyncSession):
        return
    image_cost = max(0, int(image_output_cost_micro or 0))
    if image_cost <= 0:
        return
    image_charge_micro = (
        image_cost * max(0, int(rate_multiplier_x10000 or 0))
    ) // 10_000
    if image_charge_micro <= 0:
        return

    wallet = await billing_core.get_wallet(
        session,
        completion.user_id,
        lock=True,
        create=False,
    )
    balance_micro = int(getattr(wallet, "balance_micro", 0) or 0) if wallet else 0
    held_micro = await billing_core._held_amount_for_ref(  # noqa: SLF001
        session,
        completion.user_id,
        "completion",
        billing_ref_id,
    )
    available_micro = balance_micro + int(held_micro or 0)
    if available_micro >= image_charge_micro:
        return
    raise billing_core.BillingError(
        "INSUFFICIENT_BALANCE",
        "insufficient wallet balance for completion image output",
        402,
    )


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


def _record_balance_cache_refresh(
    session: AsyncSession,
    *,
    user_id: str,
    balance_after: int,
) -> None:
    try:
        pending = session.info.setdefault(_POST_COMMIT_BALANCE_CACHE_KEY, {})
        pending[str(user_id)] = int(balance_after)
    except Exception:
        return


def _record_window_cache_increment(
    session: AsyncSession,
    *,
    key_id: str,
    micro: int,
    limits: dict[str, int],
) -> None:
    try:
        pending = session.info.setdefault(_POST_COMMIT_WINDOW_CACHE_KEY, [])
        pending.append((str(key_id), int(micro), dict(limits)))
    except Exception:
        return


async def _ensure_billing_window_usage_event(
    session: AsyncSession,
    *,
    tx: WalletTransaction,
    user_id: str,
    credential_id: str | None,
    amount_micro: int,
) -> bool:
    if (
        not credential_id
        or int(amount_micro) <= 0
        or getattr(tx, "kind", "settle") != "settle"
    ):
        return False
    existing = None
    get_fn = getattr(session, "get", None)
    if callable(get_fn):
        existing = await get_fn(BillingWindowUsageEvent, tx.id)
    if existing is not None:
        return False
    session.add(
        BillingWindowUsageEvent(
            wallet_transaction_id=tx.id,
            user_id=user_id,
            credential_id=credential_id,
            amount_micro=int(amount_micro),
        )
    )
    return True


async def flush_balance_cache_refreshes(session: AsyncSession) -> None:
    try:
        pending_balance = session.info.pop(_POST_COMMIT_BALANCE_CACHE_KEY, {})
    except Exception:
        pending_balance = {}
    try:
        pending_window = session.info.pop(_POST_COMMIT_WINDOW_CACHE_KEY, [])
    except Exception:
        pending_window = []
    cache = get_billing_cache()
    if cache is None:
        return
    if isinstance(pending_balance, dict):
        for user_id, balance_after in pending_balance.items():
            await cache.set_balance(str(user_id), int(balance_after))
    if isinstance(pending_window, list):
        for key_id, micro, limits in pending_window:
            increment_window_usage = getattr(cache, "increment_window_usage", None)
            if increment_window_usage is None:
                increment_window_usage = cache.queue_window_increment
            await increment_window_usage(
                str(key_id),
                int(micro),
                limits if isinstance(limits, dict) else None,
            )


async def settle_generation(
    session: AsyncSession,
    generation: Generation,
    *,
    width: int,
    height: int,
    image_count: int = 1,
) -> None:
    billing_ref_id = _generation_billing_ref_id(generation)
    if not await _wallet_billing_applies(
        session,
        user_id=generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
    ):
        return
    if not await _billing_enabled():
        return
    idempotency_key = f"settle:{billing_ref_id}"
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
    billable_image_count = max(1, int(image_count or 1))
    rate_multiplier = await generation_rate_multiplier_x10000(session, generation)
    snapshot_cost = _generation_snapshot_cost(
        generation,
        image_count=billable_image_count,
    )
    pricing_error: billing_core.BillingError | None = None
    if snapshot_cost is not None:
        base_cost, tier = snapshot_cost
        cost = _apply_rate_multiplier_micro(base_cost, rate_multiplier)
        tier_source = "task_snapshot"
    else:
        try:
            if requested_tier is not None:
                base_cost, tier = await billing_core.estimate_image_cost_for_tier(
                    session,
                    tier=requested_tier,
                    n=billable_image_count,
                )
                tier_source = "request"
            else:
                base_cost, tier = await billing_core.estimate_image_cost(
                    session,
                    size_px=max(0, int(width) * int(height)),
                    n=billable_image_count,
                    thresholds=await _thresholds(),
                )
                tier_source = "actual_pixels"
            cost = _apply_rate_multiplier_micro(base_cost, rate_multiplier)
        except billing_core.BillingError as exc:
            if exc.code != "PRICING_MISSING":
                raise
            pricing_error = exc
            cost = 0
            tier = requested_tier or "unknown"
            tier_source = "missing"
    zero_rate = rate_multiplier == 0 and pricing_error is None
    if cost <= 0 and not zero_rate:
        held = await held_amount_for_ref(
            session,
            generation.user_id,
            "generation",
            billing_ref_id,
        )
        if held <= 0:
            session.add(
                _audit(
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
            _audit(
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
    tx = await billing_core.settle(
        session,
        generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
        actual_micro=cost,
        idempotency_key=idempotency_key,
        allow_negative=await _allow_negative_balance(),
        record_zero=zero_rate,
        meta={
            "generation_id": generation.id,
            "tier": tier,
            "width": width,
            "height": height,
            "image_count": billable_image_count,
            "tier_source": tier_source,
            "model": generation.model,
            "retry_count": _generation_billing_retry_count(generation),
            "rate_multiplier_x10000": rate_multiplier,
        },
    )
    if tx is not None:
        _record_balance_cache_refresh(
            session,
            user_id=generation.user_id,
            balance_after=tx.balance_after,
        )
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
                    "image_count": billable_image_count,
                    "balance_after": tx.balance_after,
                    "hold_after": tx.hold_after,
                },
            )
        )
        if zero_rate:
            session.add(
                _audit(
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
    billing_ref_id = _generation_billing_ref_id(generation)
    if not await _wallet_billing_applies(
        session,
        user_id=generation.user_id,
        ref_type="generation",
        ref_id=billing_ref_id,
    ):
        return
    if not await _billing_enabled():
        return
    idempotency_key = f"release:{billing_ref_id}"
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
        ref_id=billing_ref_id,
        idempotency_key=idempotency_key,
        meta={
            "generation_id": generation.id,
            "reason": reason,
            "retry_count": _generation_billing_retry_count(generation),
        },
    )
    if tx is not None:
        _record_balance_cache_refresh(
            session,
            user_id=generation.user_id,
            balance_after=tx.balance_after,
        )
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


async def _completion_cost_breakdown(
    session: AsyncSession,
    completion: Completion,
    *,
    usage: UsageTokens,
    rate_multiplier: int,
    service_tier: str,
) -> CostBreakdown:
    snapshot = _task_pricing_snapshot(completion)
    if snapshot is not None:
        return billing_core.completion_breakdown_from_snapshot(
            snapshot,
            model=completion.model,
            tokens=usage,
            rate_multiplier_x10000=rate_multiplier,
            service_tier=service_tier,
        )
    if isinstance(session, AsyncSession):
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


def _completion_usage(completion: Completion, *, cache_aware: bool) -> UsageTokens:
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


def _held_amount_breakdown(
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


async def _resolve_completion_breakdown(
    session: AsyncSession,
    completion: Completion,
    *,
    billing_ref_id: str,
    usage: UsageTokens,
    rate_multiplier: int,
    service_tier: str,
) -> CostBreakdown | None:
    pricing_error: billing_core.BillingError | None = None
    try:
        breakdown = await _completion_cost_breakdown(
            session,
            completion,
            usage=usage,
            rate_multiplier=rate_multiplier,
            service_tier=service_tier,
        )
    except billing_core.BillingError as exc:
        if exc.code not in {"PRICING_MISSING", "PRICING_SNAPSHOT_INVALID"}:
            raise
        pricing_error = exc
        breakdown = None
    if breakdown is not None and (
        breakdown.actual_cost_micro > 0 or rate_multiplier == 0
    ):
        if breakdown.pricing_source == "fallback":
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
        return breakdown

    held = await held_amount_for_ref(
        session,
        completion.user_id,
        "completion",
        billing_ref_id,
    )
    if held <= 0:
        if pricing_error is not None:
            session.add(
                _audit(
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
        _audit(
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
    return _held_amount_breakdown(held, rate_multiplier=rate_multiplier)


async def _completion_request_fingerprint(
    session: AsyncSession,
    completion: Completion,
    *,
    idempotency_key: str,
    service_tier: str,
    usage: UsageTokens,
    breakdown: CostBreakdown,
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
    existing = await _existing_fingerprint_tx(
        session,
        completion.user_id,
        fingerprint,
    )
    if existing is None:
        return fingerprint, False
    _add_replay_audit(
        session,
        user_id=completion.user_id,
        tx=existing,
        replay_source="fingerprint",
    )
    return fingerprint, True


async def _audit_completion_window_limit(
    session: AsyncSession,
    completion: Completion,
    *,
    cache: Any,
    key_id: str | None,
    cost: int,
) -> None:
    if (
        cache is None
        or not isinstance(session, AsyncSession)
        or not key_id
        or not await _window_rate_limit_enabled()
    ):
        return
    allowed, window, window_usage = await cache.evaluate_rate_limits(
        session,
        key_id,
        cost,
    )
    if allowed:
        return
    billing_rate_limit_block_total.labels(window=window or "unknown").inc()
    session.add(
        _audit(
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
) -> tuple[str, str] | None:
    cache = get_billing_cache()
    key_id = getattr(completion, "user_api_credential_id", None)
    if (
        cache is None
        or not isinstance(session, AsyncSession)
        or not key_id
        or not await _window_rate_limit_enabled()
    ):
        return None
    projected_micro = await held_amount_for_ref(
        session,
        completion.user_id,
        "completion",
        _completion_billing_ref_id(completion),
    )
    if projected_micro <= 0:
        snapshot = _task_pricing_snapshot(completion)
        if snapshot is not None:
            try:
                preview = billing_core.completion_breakdown_from_snapshot(
                    snapshot,
                    model=completion.model,
                    tokens=UsageTokens(input_tokens=1, output_tokens=1),
                    rate_multiplier_x10000=(
                        await completion_rate_multiplier_x10000(session, completion)
                    ),
                    service_tier=_completion_service_tier(completion),
                )
                projected_micro = preview.actual_cost_micro
            except billing_core.BillingError:
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
    billing_rate_limit_block_total.labels(window=window or "unknown").inc()
    session.add(
        _audit(
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
    if tx is None:
        return
    window_event_added = await _ensure_billing_window_usage_event(
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
            billing_cost_micro_total.labels(kind=kind).inc(value)
    if cache is not None:
        _record_balance_cache_refresh(
            session,
            user_id=completion.user_id,
            balance_after=tx.balance_after,
        )
        if key_id and window_event_added:
            limits = await cache.credential_limits(session, key_id)
            _record_window_cache_increment(
                session,
                key_id=key_id,
                micro=max(0, cost),
                limits=limits,
            )
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
    if cost == 0 and breakdown.rate_multiplier_x10000 == 0:
        session.add(
            _audit(
                event_type="wallet.charge.zero_rate",
                user_id=completion.user_id,
                details={
                    "completion_id": completion.id,
                    "tx_id": tx.id,
                    "ref_type": "completion",
                    "ref_id": _completion_billing_ref_id(completion),
                    "rate_multiplier_x10000": 0,
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


async def charge_completion(session: AsyncSession, completion: Completion) -> None:
    billing_ref_id = _completion_billing_ref_id(completion)
    if not await _wallet_billing_applies(
        session,
        user_id=completion.user_id,
        ref_type="completion",
        ref_id=billing_ref_id,
    ):
        return
    if not await _billing_enabled():
        return
    idempotency_key = f"complete:{billing_ref_id}"
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
    usage = _completion_usage(completion, cache_aware=cache_aware)
    rate_multiplier = await completion_rate_multiplier_x10000(session, completion)
    service_tier = _completion_service_tier(completion)
    breakdown = await _resolve_completion_breakdown(
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
    billing_pricing_source_total.labels(source=breakdown.pricing_source).inc()
    fingerprint, replayed = await _completion_request_fingerprint(
        session,
        completion,
        idempotency_key=idempotency_key,
        service_tier=service_tier,
        usage=usage,
        breakdown=breakdown,
    )
    if replayed:
        return
    cache = get_billing_cache()
    key_id = getattr(completion, "user_api_credential_id", None)
    await _audit_completion_window_limit(
        session,
        completion,
        cache=cache,
        key_id=key_id,
        cost=cost,
    )
    allow_negative = await _allow_negative_balance()
    await _ensure_completion_image_charge_fundable(
        session,
        completion=completion,
        billing_ref_id=billing_ref_id,
        image_output_cost_micro=breakdown.image_output_cost_micro,
        rate_multiplier_x10000=rate_multiplier,
        allow_negative=allow_negative,
    )
    try:
        tx = await billing_core.settle(
            session,
            completion.user_id,
            ref_type="completion",
            ref_id=billing_ref_id,
            actual_micro=cost,
            idempotency_key=idempotency_key,
            allow_negative=allow_negative,
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
        wallet_charge_lost_total.inc()
        raise
    await _record_completion_settlement(
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
) -> None:
    billing_ref_id = _completion_billing_ref_id(completion)
    if not await _wallet_billing_applies(
        session,
        user_id=completion.user_id,
        ref_type="completion",
        ref_id=billing_ref_id,
    ):
        return
    if not await _billing_enabled():
        return
    idempotency_key = f"release:{billing_ref_id}"
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
        ref_id=billing_ref_id,
        idempotency_key=idempotency_key,
        meta={
            "completion_id": completion.id,
            "reason": reason,
            "billing_retry_count": _completion_billing_retry_count(completion),
        },
    )
    if tx is not None:
        _record_balance_cache_refresh(
            session,
            user_id=completion.user_id,
            balance_after=tx.balance_after,
        )
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
