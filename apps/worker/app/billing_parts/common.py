"""Shared worker billing persistence and cache helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import (
    AuditLog,
    BillingWindowUsageEvent,
    Completion,
    WalletTransaction,
)

from .contracts import CommonDependencies

POST_COMMIT_BALANCE_CACHE_KEY = "lumen_post_commit_balance_cache"
POST_COMMIT_WINDOW_CACHE_KEY = "lumen_post_commit_window_cache"


def audit(
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


async def existing_wallet_tx(
    session: AsyncSession,
    user_id: str,
    idempotency_key: str,
) -> WalletTransaction | None:
    return (
        await session.execute(
            select(WalletTransaction).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


async def wallet_billing_applies(
    session: AsyncSession,
    *,
    user_id: str,
    ref_type: str,
    ref_id: str,
    account_mode: Any,
    billing_core: Any,
) -> bool:
    if await account_mode(session, user_id) == "wallet":
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


async def existing_fingerprint_tx(
    session: AsyncSession,
    user_id: str,
    fingerprint: str,
    *,
    async_session_type: type,
) -> WalletTransaction | None:
    if not isinstance(session, async_session_type) or not fingerprint:
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


async def ensure_completion_image_charge_fundable(
    session: AsyncSession,
    *,
    completion: Completion,
    billing_ref_id: str,
    image_output_cost_micro: int,
    rate_multiplier_x10000: int,
    allow_negative: bool,
    deps: CommonDependencies,
) -> None:
    if allow_negative or not isinstance(session, deps.async_session_type):
        return
    image_cost = max(0, int(image_output_cost_micro or 0))
    if image_cost <= 0:
        return
    image_charge_micro = (
        image_cost * max(0, int(rate_multiplier_x10000 or 0))
    ) // 10_000
    if image_charge_micro <= 0:
        return

    wallet = await deps.billing_core.get_wallet(
        session,
        completion.user_id,
        lock=True,
        create=False,
    )
    balance_micro = int(getattr(wallet, "balance_micro", 0) or 0) if wallet else 0
    held_micro = await deps.billing_core._held_amount_for_ref(  # noqa: SLF001
        session,
        completion.user_id,
        "completion",
        billing_ref_id,
    )
    if balance_micro + int(held_micro or 0) >= image_charge_micro:
        return
    raise deps.billing_core.BillingError(
        "INSUFFICIENT_BALANCE",
        "insufficient wallet balance for completion image output",
        402,
    )


def add_replay_audit(
    session: AsyncSession,
    *,
    user_id: str,
    tx: WalletTransaction,
    replay_source: str,
    deps: CommonDependencies,
) -> None:
    deps.billing_idempotency_replay_total.inc()
    session.add(
        deps.audit(
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


def record_balance_cache_refresh(
    session: AsyncSession,
    *,
    user_id: str,
    balance_after: int,
) -> None:
    try:
        pending = session.info.setdefault(POST_COMMIT_BALANCE_CACHE_KEY, {})
        pending[str(user_id)] = int(balance_after)
    except Exception:
        return


def record_window_cache_increment(
    session: AsyncSession,
    *,
    key_id: str,
    micro: int,
    limits: dict[str, int],
) -> None:
    try:
        pending = session.info.setdefault(POST_COMMIT_WINDOW_CACHE_KEY, [])
        pending.append((str(key_id), int(micro), dict(limits)))
    except Exception:
        return


async def ensure_billing_window_usage_event(
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


async def flush_balance_cache_refreshes(
    session: AsyncSession,
    *,
    deps: CommonDependencies,
) -> None:
    try:
        pending_balance = session.info.pop(POST_COMMIT_BALANCE_CACHE_KEY, {})
    except Exception:
        pending_balance = {}
    try:
        pending_window = session.info.pop(POST_COMMIT_WINDOW_CACHE_KEY, [])
    except Exception:
        pending_window = []
    cache = deps.get_billing_cache()
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
