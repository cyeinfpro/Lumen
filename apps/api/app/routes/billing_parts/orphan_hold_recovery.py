"""Shared recovery primitives for orphan wallet holds."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.billing_operations import AuditLog, WalletTransaction

from ...audit import AuditPersistenceError, hash_email

OrphanHoldRecoveryAction = Literal["release", "settle_default", "manual_review"]


@dataclass(frozen=True, slots=True)
class HoldGroup:
    tx_ids: list[str]
    aggregate_held_micro: int

    @property
    def count(self) -> int:
        return len(self.tx_ids)


def recovery_action(ref_type: str | None) -> OrphanHoldRecoveryAction:
    if ref_type in {"generation", "completion", "video_generation"}:
        return "release"
    if ref_type == "prompt_enhance":
        return "settle_default"
    return "manual_review"


async def load_hold_group(
    db: AsyncSession,
    hold: WalletTransaction,
) -> HoldGroup:
    rows = (
        await db.execute(
            select(
                WalletTransaction.id,
                WalletTransaction.amount_micro,
            )
            .where(
                WalletTransaction.user_id == hold.user_id,
                WalletTransaction.kind == "hold",
                WalletTransaction.ref_type == hold.ref_type,
                WalletTransaction.ref_id == hold.ref_id,
            )
            .order_by(
                WalletTransaction.created_at.asc(),
                WalletTransaction.id.asc(),
            )
        )
    ).all()
    return HoldGroup(
        tx_ids=[str(row.id) for row in rows],
        aggregate_held_micro=max(
            0,
            -sum(int(row.amount_micro or 0) for row in rows),
        ),
    )


def replay_hold_group(
    transaction: WalletTransaction,
    fallback_hold: WalletTransaction,
) -> HoldGroup:
    meta = transaction.meta if isinstance(transaction.meta, Mapping) else {}
    raw_ids = meta.get("hold_tx_ids")
    tx_ids = (
        [str(value) for value in raw_ids if isinstance(value, str) and value]
        if isinstance(raw_ids, list)
        else []
    )
    if not tx_ids:
        tx_ids = [str(meta.get("hold_tx_id") or fallback_hold.id)]
    try:
        aggregate = max(0, int(meta.get("aggregate_held_micro") or 0))
    except (TypeError, ValueError):
        aggregate = 0
    if aggregate <= 0:
        aggregate = max(0, -int(fallback_hold.amount_micro or 0))
    return HoldGroup(tx_ids=tx_ids, aggregate_held_micro=aggregate)


async def ensure_admin_recovery_audit(
    db: AsyncSession,
    *,
    commands: Any,
    http: Any,
    request: Any,
    admin: Any,
    target_user_id: str,
    event_type: str,
    transaction: WalletTransaction,
    transaction_detail_key: str,
    details: dict[str, Any],
    already_committed: bool,
) -> bool:
    existing_audit = (
        await db.execute(
            select(AuditLog.id)
            .where(
                AuditLog.event_type == event_type,
                AuditLog.target_user_id == target_user_id,
                AuditLog.details[transaction_detail_key].as_string()
                == str(transaction.id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_audit is not None:
        return False
    try:
        written = await commands.write_audit(
            db,
            event_type=event_type,
            user_id=admin.id,
            actor_email_hash=hash_email(admin.email),
            actor_ip_hash=commands.request_ip_hash(request),
            target_user_id=target_user_id,
            details={
                **details,
                transaction_detail_key: transaction.id,
                "audit_recovery": already_committed,
                "original_created_by_admin": transaction.created_by_admin,
            },
            autocommit=False,
        )
    except AuditPersistenceError:
        await db.rollback()
        message = (
            "wallet recovery already exists, but its missing audit record "
            "could not be repaired"
            if already_committed
            else "wallet recovery was rolled back because its audit record "
            "could not be written"
        )
        raise http("AUDIT_WRITE_FAILED", message, 503) from None
    if written is not True:
        await db.rollback()
        message = (
            "wallet recovery already exists, but its missing audit record "
            "could not be repaired"
            if already_committed
            else "wallet recovery was rolled back because its audit record "
            "could not be written"
        )
        raise http("AUDIT_WRITE_FAILED", message, 503)
    return True
