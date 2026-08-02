"""Administrative default settlement for orphan prompt-enhancement holds."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.model_entities.billing_operations import WalletTransaction
from lumen_core.schema_models.billing import WalletTransactionOut

from ...db import get_db
from ...deps import AdminUser, verify_csrf
from .composition import build_billing_services
from .orphan_hold_recovery import (
    ensure_admin_recovery_audit,
    load_hold_group,
    replay_hold_group,
)

router = APIRouter()


@router.post(
    "/admin/billing/holds/{tx_id}:settle-default",
    response_model=WalletTransactionOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_settle_orphan_prompt_hold(
    tx_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WalletTransactionOut:
    services = build_billing_services()
    queries = services.queries
    commands = services.commands
    hold = await db.get(WalletTransaction, tx_id)
    if hold is None or hold.kind != "hold":
        raise queries.http("not_found", "hold transaction not found", 404)
    if hold.ref_type != "prompt_enhance" or not hold.ref_id:
        raise queries.http(
            "INVALID_HOLD_RECOVERY",
            "default settlement is only available for prompt enhancement holds",
            409,
        )
    target_user_id = str(hold.user_id)
    settle_key = f"admin_settle_hold:{tx_id}"
    existing_settle = (
        await db.execute(
            select(WalletTransaction)
            .where(
                WalletTransaction.user_id == hold.user_id,
                WalletTransaction.idempotency_key == settle_key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing_settle is not None:
        if (
            existing_settle.kind != "settle"
            or existing_settle.ref_type != hold.ref_type
            or existing_settle.ref_id != hold.ref_id
        ):
            raise queries.http(
                "IDEMPOTENCY_CONFLICT",
                "admin settlement idempotency key has conflicting billing semantics",
                409,
            )
        group = replay_hold_group(existing_settle, hold)
        await ensure_admin_recovery_audit(
            db,
            commands=commands,
            http=queries.http,
            request=request,
            admin=admin,
            target_user_id=hold.user_id,
            event_type="wallet.hold.force_settle",
            transaction=existing_settle,
            transaction_detail_key="settle_tx_id",
            details={
                "hold_tx_id": tx_id,
                "hold_tx_ids": group.tx_ids,
                "hold_count": group.count,
                "aggregate_held_micro": group.aggregate_held_micro,
                "ref_type": hold.ref_type,
                "ref_id": hold.ref_id,
                "settlement_basis": "aggregate_held_micro",
            },
            already_committed=True,
        )
        out = queries.tx_out(existing_settle)
        await db.commit()
        await commands.invalidate_balance_cache(target_user_id)
        return out

    group = await load_hold_group(db, hold)
    if group.aggregate_held_micro <= 0:
        raise queries.http("HOLD_NOT_ACTIVE", "hold is no longer active", 409)
    tx = await billing_core.settle(
        db,
        target_user_id,
        ref_type=hold.ref_type,
        ref_id=hold.ref_id,
        actual_micro=group.aggregate_held_micro,
        idempotency_key=settle_key,
        meta={
            "reason": "admin orphan prompt enhancement default settlement",
            "hold_tx_id": tx_id,
            "hold_tx_ids": group.tx_ids,
            "hold_count": group.count,
            "aggregate_held_micro": group.aggregate_held_micro,
            "settlement_basis": "aggregate_held_micro",
        },
        created_by_admin=admin.id,
    )
    if tx is None:
        await db.rollback()
        raise queries.http(
            "HOLD_ALREADY_CONSUMED",
            "hold was concurrently settled or released",
            409,
        )
    if tx.kind != "settle" or tx.idempotency_key != settle_key:
        await db.rollback()
        raise queries.http(
            "HOLD_ALREADY_CONSUMED",
            "hold was concurrently settled or released",
            409,
        )
    await ensure_admin_recovery_audit(
        db,
        commands=commands,
        http=queries.http,
        request=request,
        admin=admin,
        target_user_id=hold.user_id,
        event_type="wallet.hold.force_settle",
        transaction=tx,
        transaction_detail_key="settle_tx_id",
        details={
            "hold_tx_id": tx_id,
            "hold_tx_ids": group.tx_ids,
            "hold_count": group.count,
            "aggregate_held_micro": group.aggregate_held_micro,
            "ref_type": hold.ref_type,
            "ref_id": hold.ref_id,
            "settlement_basis": "aggregate_held_micro",
        },
        already_committed=False,
    )
    out = queries.tx_out(tx)
    await db.commit()
    await commands.invalidate_balance_cache(target_user_id)
    return out
