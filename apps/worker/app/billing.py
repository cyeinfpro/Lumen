"""Worker-side billing hooks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import AuditLog, Completion, Generation, User, WalletTransaction

from . import runtime_settings
from .observability import wallet_charge_lost_total, wallet_overdrawn_total


async def _setting_bool(key: str, default: bool = False) -> bool:
    return billing_core.parse_bool_setting(await runtime_settings.resolve(key), default)


async def _billing_enabled() -> bool:
    return await _setting_bool("billing.enabled", False)


async def _allow_negative_balance() -> bool:
    return await _setting_bool("billing.allow_negative_balance", False)


async def _thresholds() -> dict[str, int]:
    return billing_core.parse_thresholds(
        await runtime_settings.resolve("billing.image_size_thresholds")
    )


async def _account_mode(session: AsyncSession, user_id: str) -> str:
    return (
        await session.execute(select(User.account_mode).where(User.id == user_id))
    ).scalar_one_or_none() or "wallet"


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


def _add_replay_audit(
    session: AsyncSession,
    *,
    user_id: str,
    tx: WalletTransaction,
    replay_source: str,
) -> None:
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
    cost, tier = await billing_core.estimate_image_cost(
        session,
        size_px=max(0, int(width) * int(height)),
        n=1,
        thresholds=await _thresholds(),
    )
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
    cost = await billing_core.estimate_completion_cost(
        session,
        model=completion.model,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )
    if cost <= 0 and (completion.tokens_in > 0 or completion.tokens_out > 0):
        session.add(
            _audit(
                event_type="pricing.not_configured",
                user_id=completion.user_id,
                details={
                    "scope": "chat_model",
                    "model": completion.model,
                    "completion_id": completion.id,
                    "tokens_in": completion.tokens_in,
                    "tokens_out": completion.tokens_out,
                },
            )
        )
    try:
        tx = await billing_core.charge(
            session,
            completion.user_id,
            cost,
            ref_type="completion",
            ref_id=completion.id,
            idempotency_key=idempotency_key,
            allow_negative=await _allow_negative_balance(),
            meta={
                "model": completion.model,
                "tokens_in": completion.tokens_in,
                "tokens_out": completion.tokens_out,
            },
        )
    except Exception:
        wallet_charge_lost_total.inc()
        raise
    if tx is not None:
        session.add(
            _audit(
                event_type="wallet.charge.completion",
                user_id=completion.user_id,
                details={
                    "completion_id": completion.id,
                    "cost_micro": cost,
                    "amount_micro": tx.amount_micro,
                    "balance_after": tx.balance_after,
                },
            )
        )
        if int((tx.meta or {}).get("overdraw_micro") or 0) > 0:
            wallet_overdrawn_total.labels(kind="charge").inc()
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
