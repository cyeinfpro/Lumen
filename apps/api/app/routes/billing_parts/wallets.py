"""User and administrator wallet routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import (
    RedemptionCodeUsage,
    User,
    UserApiCredential,
    UserWallet,
    WalletTransaction,
    new_uuid7,
)
from lumen_core.schemas import (
    AdminRedemptionUsageOut,
    AdminSetAccountModeIn,
    AdminWalletDetailOut,
    AdminWalletListOut,
    AdminWalletOut,
    AdminWalletAdjustIn,
    BillingSnapshotOut,
    WalletOut,
    WalletTransactionListOut,
    WalletTransactionOut,
)

from ...audit import hash_email
from ...db import get_db
from ...deps import AdminUser, CurrentUser, verify_csrf
from ...services.billing.usage import CHARGE_KINDS as _CHARGE_KINDS
from .composition import build_billing_services


router = APIRouter()


@router.get("/me/wallet", response_model=WalletOut)
async def get_my_wallet(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WalletOut:
    return await build_billing_services().queries.wallet_out(db, user)


@router.get("/me/billing/snapshot", response_model=BillingSnapshotOut)
async def get_my_billing_snapshot(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BillingSnapshotOut:
    services = build_billing_services()
    queries = services.queries
    (
        balance,
        multiplier,
        credential_id,
        _start,
        _end,
        windows,
        by_kind,
        _count,
    ) = await queries.billing_snapshot_parts(db, user.id)
    return BillingSnapshotOut(
        balance_micro=balance,
        billing_rate_multiplier=multiplier,
        credential_id=credential_id,
        windows=windows,
        by_kind_30d=by_kind,
    )


@router.get("/me/wallet/transactions", response_model=WalletTransactionListOut)
async def list_my_wallet_transactions(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    kind: str | None = Query(default=None, max_length=32),
) -> WalletTransactionListOut:
    services = build_billing_services()
    queries = services.queries
    commands = services.commands
    commands.require_wallet_user(user)
    stmt = (
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user.id)
        .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
        .limit(limit + 1)
    )
    if kind:
        stmt = (
            stmt.where(WalletTransaction.kind.in_(_CHARGE_KINDS))
            if kind == "charge"
            else stmt.where(WalletTransaction.kind == kind)
        )
    stmt = queries.cursor_filter(stmt, WalletTransaction, cursor)
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return WalletTransactionListOut(
        items=[queries.tx_out(row) for row in rows],
        next_cursor=queries.next_cursor(rows, has_more),
    )


@router.get("/admin/wallets", response_model=AdminWalletListOut)
async def admin_list_wallets(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
    mode: str | None = Query(default="wallet"),
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> AdminWalletListOut:
    services = build_billing_services()
    queries = services.queries
    # The soft-delete boundary is intentionally part of the query contract:
    # User.deleted_at.is_(None)
    stmt = (
        select(User, UserWallet)
        .outerjoin(UserWallet, UserWallet.user_id == User.id)
        .where(User.deleted_at.is_(None))
    )
    if mode in {"wallet", "byok"}:
        stmt = stmt.where(User.account_mode == mode)
    if q:
        q_clean = q.strip()[:200]
        if q_clean:
            pattern = f"%{queries.escape_like_pattern(q_clean)}%"
            stmt = stmt.where(
                or_(
                    User.email.ilike(pattern, escape="\\"),
                    User.id.ilike(pattern, escape="\\"),
                )
            )
    stmt = queries.cursor_filter(stmt, User, cursor)
    rows = (
        await db.execute(
            stmt.order_by(User.created_at.desc(), User.id.desc()).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items: list[AdminWalletOut] = []
    threshold = await queries.low_balance_threshold(db)
    user_ids = [user.id for user, _wallet in rows]
    last_topups: dict[str, datetime] = {}
    last_charges: dict[str, datetime] = {}
    if user_ids:
        for user_id, ts in (
            await db.execute(
                select(
                    WalletTransaction.user_id,
                    func.max(WalletTransaction.created_at),
                )
                .where(
                    WalletTransaction.user_id.in_(user_ids),
                    WalletTransaction.kind.in_(
                        ("topup_redeem", "adjust_admin", "grant")
                    ),
                    WalletTransaction.amount_micro > 0,
                )
                .group_by(WalletTransaction.user_id)
            )
        ).all():
            if ts is not None:
                last_topups[str(user_id)] = ts
        for user_id, ts in (
            await db.execute(
                select(
                    WalletTransaction.user_id,
                    func.max(WalletTransaction.created_at),
                )
                .where(
                    WalletTransaction.user_id.in_(user_ids),
                    WalletTransaction.kind.in_((*_CHARGE_KINDS, "settle")),
                    WalletTransaction.amount_micro < 0,
                )
                .group_by(WalletTransaction.user_id)
            )
        ).all():
            if ts is not None:
                last_charges[str(user_id)] = ts
    for user, wallet in rows:
        if user.account_mode == "wallet":
            wallet = wallet or UserWallet(user_id=user.id)
            wallet_out = WalletOut(
                mode="wallet",
                balance=queries.money(wallet.balance_micro),
                hold=queries.money(wallet.hold_micro),
                low_balance_threshold=queries.money(threshold),
                frozen=False,
            )
        elif wallet is not None and (wallet.balance_micro > 0 or wallet.hold_micro > 0):
            wallet_out = WalletOut(
                mode="byok",
                balance=queries.money(wallet.balance_micro),
                hold=queries.money(wallet.hold_micro),
                frozen=True,
            )
        else:
            wallet_out = WalletOut(mode="byok", balance=None, hold=None, frozen=False)
        items.append(
            AdminWalletOut(
                user_id=user.id,
                email=user.email,
                account_mode=user.account_mode,  # type: ignore[arg-type]
                wallet=wallet_out,
                last_topup_at=last_topups.get(user.id),
                last_charge_at=last_charges.get(user.id),
            )
        )
    return AdminWalletListOut(
        items=items,
        next_cursor=queries.next_cursor([user for user, _wallet in rows], has_more),
    )


@router.post(
    "/admin/wallets/{user_id}:adjust",
    response_model=WalletTransactionOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_adjust_wallet(
    user_id: str,
    body: AdminWalletAdjustIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WalletTransactionOut:
    services = build_billing_services()
    queries = services.queries
    commands = services.commands
    target = await db.get(User, user_id)
    if target is None or getattr(target, "deleted_at", None) is not None:
        raise queries.http("not_found", "user not found", 404)
    if target.account_mode != "wallet":
        raise queries.http(
            "ACCOUNT_NOT_WALLET", "target user is not a wallet account", 409
        )
    amount = queries.rmb_to_micro_or_422(
        body.amount_rmb_signed, field="amount_rmb_signed"
    )
    if abs(amount) > services.max_admin_adjust_micro:
        raise queries.http(
            "amount_too_large",
            "admin wallet adjustment exceeds the per-operation limit",
            422,
            max_amount_micro=services.max_admin_adjust_micro,
        )
    allow_negative = await queries.allow_negative_balance(db)
    min_balance_micro = (
        -services.max_admin_negative_balance_micro
        if allow_negative and amount < 0
        else None
    )
    try:
        tx = await billing_core.adjust(
            db,
            user_id,
            amount,
            admin_id=admin.id,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            allow_negative=allow_negative,
            min_balance_micro=min_balance_micro,
        )
    except billing_core.BillingError as exc:
        if exc.code == "negative_balance_limit_exceeded":
            raise queries.http(
                exc.code,
                exc.message,
                exc.status_code,
                max_negative_balance_micro=services.max_admin_negative_balance_micro,
            ) from exc
        raise queries.billing_http(exc)
    await commands.write_audit(
        db,
        event_type="wallet.adjust.admin",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=commands.request_ip_hash(request),
        target_user_id=user_id,
        details={"amount_micro": amount, "reason": body.reason, "tx_id": tx.id},
        autocommit=False,
    )
    await db.commit()
    await commands.invalidate_balance_cache(user_id)
    return queries.tx_out(tx)


@router.get("/admin/wallets/{user_id}", response_model=AdminWalletDetailOut)
async def admin_get_wallet_detail(
    user_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminWalletDetailOut:
    services = build_billing_services()
    queries = services.queries
    user = await db.get(User, user_id)
    if user is None or getattr(user, "deleted_at", None) is not None:
        raise queries.http("not_found", "user not found", 404)
    wallet_out = await queries.wallet_out(db, user)
    tx_rows = list(
        (
            await db.execute(
                select(WalletTransaction)
                .where(WalletTransaction.user_id == user_id)
                .order_by(
                    WalletTransaction.created_at.desc(), WalletTransaction.id.desc()
                )
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    usage_rows = (
        await db.execute(
            select(RedemptionCodeUsage, User.email)
            .join(User, User.id == RedemptionCodeUsage.user_id)
            .where(RedemptionCodeUsage.user_id == user_id)
            .order_by(
                RedemptionCodeUsage.redeemed_at.desc(),
                RedemptionCodeUsage.id.desc(),
            )
            .limit(10)
        )
    ).all()
    last_topup_at = (
        await db.execute(
            select(func.max(WalletTransaction.created_at)).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.kind.in_(("topup_redeem", "adjust_admin", "grant")),
                WalletTransaction.amount_micro > 0,
            )
        )
    ).scalar_one_or_none()
    last_charge_at = (
        await db.execute(
            select(func.max(WalletTransaction.created_at)).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.kind.in_((*_CHARGE_KINDS, "settle")),
                WalletTransaction.amount_micro < 0,
            )
        )
    ).scalar_one_or_none()
    last_redemption_at = (
        await db.execute(
            select(func.max(RedemptionCodeUsage.redeemed_at)).where(
                RedemptionCodeUsage.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return AdminWalletDetailOut(
        user_id=user.id,
        email=user.email,
        account_mode=user.account_mode,  # type: ignore[arg-type]
        wallet=wallet_out,
        last_topup_at=last_topup_at,
        last_charge_at=last_charge_at,
        last_redemption_at=last_redemption_at,
        transactions=[queries.tx_out(tx) for tx in tx_rows],
        redemptions=[
            AdminRedemptionUsageOut(
                id=usage.id,
                code_id=usage.code_id,
                user_id=usage.user_id,
                user_email=email,
                amount=queries.money(usage.amount_micro),
                wallet_tx_id=usage.wallet_tx_id,
                redeemed_at=usage.redeemed_at,
                ip_hash=usage.ip_hash,
            )
            for usage, email in usage_rows
        ],
    )


@router.post(
    "/admin/users/{user_id}:set_account_mode",
    response_model=AdminWalletOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_set_account_mode(
    user_id: str,
    body: AdminSetAccountModeIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminWalletOut:
    services = build_billing_services()
    queries = services.queries
    commands = services.commands
    # Preserve the soft-delete guard alongside the row lock:
    # User.deleted_at.is_(None)
    target = (
        await db.execute(
            select(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise queries.http("not_found", "user not found", 404)
    before = target.account_mode
    if before == body.mode:
        return AdminWalletOut(
            user_id=target.id,
            email=target.email,
            account_mode=target.account_mode,  # type: ignore[arg-type]
            wallet=await queries.wallet_out(db, target),
        )
    now = datetime.now(timezone.utc)
    if before == "byok" and body.mode == "wallet":
        await db.execute(
            update(UserApiCredential)
            .where(
                UserApiCredential.user_id == user_id,
                UserApiCredential.deleted_at.is_(None),
            )
            .values(status="revoked", deleted_at=now, updated_at=now)
        )
        await billing_core.get_wallet(db, user_id, lock=True)
    elif before == "wallet" and body.mode == "byok":
        wallet = await billing_core.get_wallet(db, user_id, lock=True)
        assert wallet is not None
        if wallet.hold_micro > 0:
            raise queries.http(
                "WALLET_HAS_ACTIVE_HOLDS",
                "wallet has active holds; cancel or finish pending tasks first",
                409,
                hold_micro=wallet.hold_micro,
            )
        if body.on_residual_balance == "zero" and wallet.balance_micro > 0:
            try:
                # 每次实际执行的清零都是一次独立操作：reason 与金额都固定
                # (reason="account mode changed to byok")，若用派生幂等键，
                # 同一用户两次切换到 byok 且余额恰好相同时，第二次清零会命中
                # 第一次的流水被静默去重，余额无法归零。每个切换事务生成一个
                # 唯一键；防双击由 before == body.mode 的提前返回保证。
                await billing_core.adjust(
                    db,
                    user_id,
                    -wallet.balance_micro,
                    admin_id=admin.id,
                    reason="account mode changed to byok",
                    idempotency_key=f"mode-change-zero:{new_uuid7()}",
                )
            except billing_core.BillingError as exc:
                raise queries.billing_http(exc)
    target.account_mode = body.mode
    await commands.write_audit(
        db,
        event_type="account.mode_change",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=commands.request_ip_hash(request),
        target_user_id=user_id,
        details={
            "from": before,
            "to": body.mode,
            "on_residual_balance": body.on_residual_balance,
        },
        autocommit=False,
    )
    await db.commit()
    await commands.invalidate_balance_cache(user_id)
    await db.refresh(target)
    return AdminWalletOut(
        user_id=target.id,
        email=target.email,
        account_mode=target.account_mode,  # type: ignore[arg-type]
        wallet=await queries.wallet_out(db, target),
    )


@router.get(
    "/admin/wallets/{user_id}/transactions", response_model=WalletTransactionListOut
)
async def admin_list_wallet_transactions(
    user_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
    kind: str | None = Query(default=None, max_length=32),
    ref_type: str | None = Query(default=None, max_length=32),
    ref_id: str | None = Query(default=None, max_length=64),
) -> WalletTransactionListOut:
    services = build_billing_services()
    queries = services.queries
    stmt = select(WalletTransaction).where(WalletTransaction.user_id == user_id)
    if kind:
        stmt = (
            stmt.where(WalletTransaction.kind.in_(_CHARGE_KINDS))
            if kind == "charge"
            else stmt.where(WalletTransaction.kind == kind)
        )
    if ref_type:
        stmt = stmt.where(WalletTransaction.ref_type == ref_type)
    if ref_id:
        stmt = stmt.where(WalletTransaction.ref_id == ref_id)
    stmt = queries.cursor_filter(stmt, WalletTransaction, cursor)
    rows = (
        (
            await db.execute(
                stmt.order_by(
                    WalletTransaction.created_at.desc(), WalletTransaction.id.desc()
                ).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return WalletTransactionListOut(
        items=[queries.tx_out(row) for row in rows],
        next_cursor=queries.next_cursor(rows, has_more),
    )
