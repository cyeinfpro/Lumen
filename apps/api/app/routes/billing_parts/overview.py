"""Billing overview, bootstrap, audit, and operational routes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import (
    AuditLog,
    PricingRule,
    RedemptionCode,
    RedemptionCodeUsage,
    User,
    UserWallet,
    WalletTransaction,
    new_uuid7,
)
from lumen_core.schemas import (
    AdminBillingAuditEventOut,
    AdminBillingBootstrapIn,
    AdminBillingOverviewOut,
    AdminBillingUsageOut,
    AdminOrphanHoldOut,
    AdminWalletAuditOut,
    WalletTransactionOut,
)

from ...audit import hash_email
from ...db import get_db
from ...deps import AdminUser
from ...deps import verify_csrf
from ...observability import (
    wallet_balance_total,
    wallet_hold_active,
    wallet_hold_micro,
    wallet_orphan_holds,
)
from ...services.redemption_secret import PreviousRedemptionSecretLocked
from .compat import current_runtime


router = APIRouter()


@router.get("/admin/billing/audit", response_model=list[AdminBillingAuditEventOut])
async def admin_billing_audit(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    event_type: str | None = Query(default=None, max_length=64),
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[AdminBillingAuditEventOut]:
    b = current_runtime()
    stmt = select(AuditLog).where(b._billing_audit_predicate())
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    rows = (
        (
            await db.execute(
                stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(
                    limit
                )
            )
        )
        .scalars()
        .all()
    )
    return [b._audit_out(row) for row in rows]


@router.get("/admin/billing/overview", response_model=AdminBillingOverviewOut)
async def admin_billing_overview(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminBillingOverviewOut:
    b = current_runtime()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    billing_enabled = await b._billing_enabled_setting(db)
    bootstrap_completed = await b._bootstrap_completed_setting(db)
    secret_configured = bool(
        (await b._setting_raw(db, "billing.redemption_code_secret") or "").strip()
    )
    wallet_balance = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(UserWallet.balance_micro), 0))
            )
        ).scalar_one()
        or 0
    )
    hold_row = (
        await db.execute(
            select(
                func.count(UserWallet.user_id),
                func.coalesce(func.sum(UserWallet.hold_micro), 0),
            ).where(UserWallet.hold_micro > 0)
        )
    ).one()
    active_codes = int(
        (
            await db.execute(
                select(func.count(RedemptionCode.id)).where(
                    RedemptionCode.revoked_at.is_(None),
                    or_(
                        RedemptionCode.expires_at.is_(None),
                        # Keep the expiry boundary strict: a code expiring at
                        # ``now`` is already expired.
                        RedemptionCode.expires_at > now,
                    ),
                    RedemptionCode.redeemed_count < RedemptionCode.max_redemptions,
                )
            )
        ).scalar_one()
        or 0
    )
    redeemed_row = (
        await db.execute(
            select(
                func.count(RedemptionCodeUsage.id),
                func.coalesce(func.sum(RedemptionCodeUsage.amount_micro), 0),
            ).where(RedemptionCodeUsage.redeemed_at >= since)
        )
    ).one()
    charges_24h = int(
        (
            await db.execute(
                select(
                    func.coalesce(func.sum(WalletTransaction.amount_micro), 0)
                ).where(
                    WalletTransaction.kind.in_((*b._CHARGE_KINDS, "settle")),
                    WalletTransaction.created_at >= since,
                    WalletTransaction.amount_micro < 0,
                )
            )
        ).scalar_one()
        or 0
    )
    aligned, missing = await b._threshold_price_alignment(db)
    audit_rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(b._billing_audit_predicate())
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    wallet_balance_total.set(wallet_balance)
    wallet_hold_active.set(int(hold_row[0] or 0))
    wallet_hold_micro.set(int(hold_row[1] or 0))
    return AdminBillingOverviewOut(
        billing_enabled=billing_enabled,
        redemption_secret_configured=secret_configured,
        bootstrap_completed=bootstrap_completed,
        wallet_total_balance=b._money(wallet_balance),
        active_holds_count=int(hold_row[0] or 0),
        active_holds=b._money(int(hold_row[1] or 0)),
        codes_active=active_codes,
        codes_redeemed_24h=int(redeemed_row[0] or 0),
        codes_redeemed_24h_amount=b._money(int(redeemed_row[1] or 0)),
        charges_24h=b._money(abs(charges_24h)),
        thresholds_pricing_aligned=aligned,
        thresholds_missing_prices=missing,
        recent_audit_events=[b._audit_out(row) for row in audit_rows],
    )


@router.get("/admin/billing/usage/{user_id}", response_model=AdminBillingUsageOut)
async def admin_billing_usage(
    user_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminBillingUsageOut:
    b = current_runtime()
    exists = (
        await db.execute(
            select(User.id).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if exists is None:
        raise b._http("not_found", "user not found", 404)
    (
        balance,
        multiplier,
        credential_id,
        range_start,
        range_end,
        windows,
        by_kind,
        count,
    ) = await b._billing_snapshot_parts(db, user_id)
    return AdminBillingUsageOut(
        user_id=user_id,
        balance_micro=balance,
        billing_rate_multiplier=multiplier,
        credential_id=credential_id,
        range_start=range_start,
        range_end=range_end,
        windows=windows,
        by_kind_30d=by_kind,
        total_micro=b._usage_total(by_kind),
        transaction_count=count,
    )


@router.get("/admin/billing/wallet_audit", response_model=AdminWalletAuditOut)
async def admin_wallet_audit(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AdminWalletAuditOut:
    b = current_runtime()
    ledger = b._wallet_audit_ledger(user_id)
    mismatch = ledger.c.running_balance != ledger.c.balance_after
    stats = (
        await db.execute(
            select(
                func.count(ledger.c.tx_id),
                func.count(func.distinct(ledger.c.user_id)),
                func.coalesce(func.sum(case((mismatch, 1), else_=0)), 0),
            ).select_from(ledger)
        )
    ).one()
    mismatch_rows = (
        await db.execute(
            select(
                ledger.c.user_id,
                ledger.c.tx_id,
                ledger.c.kind,
                ledger.c.running_balance,
                ledger.c.balance_after,
            )
            .where(mismatch)
            .order_by(
                ledger.c.user_id.asc(),
                ledger.c.created_at.asc(),
                ledger.c.tx_id.asc(),
            )
            .limit(limit)
        )
    ).all()
    mismatches = [
        f"user={row[0]} tx={row[1]} kind={row[2]} "
        f"running={row[3]} balance_after={row[4]}"
        for row in mismatch_rows
    ]
    mismatch_count = int(stats[2] or 0)
    return AdminWalletAuditOut(
        ok=mismatch_count == 0,
        transactions=int(stats[0] or 0),
        users=int(stats[1] or 0),
        mismatch_count=mismatch_count,
        mismatches=mismatches,
    )


@router.get("/admin/billing/orphan_holds", response_model=list[AdminOrphanHoldOut])
async def admin_list_orphan_holds(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    min_age_minutes: Annotated[int, Query(ge=0, le=60 * 24 * 30)] = 60,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AdminOrphanHoldOut]:
    b = current_runtime()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=min_age_minutes)
    holds = (
        (
            await db.execute(
                select(WalletTransaction)
                .where(
                    WalletTransaction.kind == "hold",
                    WalletTransaction.created_at <= cutoff,
                )
                .order_by(
                    WalletTransaction.created_at.asc(), WalletTransaction.id.asc()
                )
                .limit(limit * 2)
            )
        )
        .scalars()
        .all()
    )
    out: list[AdminOrphanHoldOut] = []
    for hold in holds:
        if not hold.ref_type or not hold.ref_id:
            continue
        consumed = (
            await db.execute(
                select(WalletTransaction.id)
                .where(
                    WalletTransaction.user_id == hold.user_id,
                    WalletTransaction.ref_type == hold.ref_type,
                    WalletTransaction.ref_id == hold.ref_id,
                    WalletTransaction.kind.in_(("settle", "release")),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if consumed is not None:
            continue
        created = hold.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        out.append(
            AdminOrphanHoldOut(
                tx=b._tx_out(hold),
                user_id=hold.user_id,
                age_seconds=max(0, int((now - created).total_seconds())),
            )
        )
        if len(out) >= limit:
            break
    wallet_orphan_holds.set(len(out))
    return out


@router.post(
    "/admin/billing/holds/{tx_id}:release",
    response_model=WalletTransactionOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_release_orphan_hold(
    tx_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WalletTransactionOut:
    b = current_runtime()
    hold = await db.get(WalletTransaction, tx_id)
    if hold is None or hold.kind != "hold":
        raise b._http("not_found", "hold transaction not found", 404)
    if not hold.ref_type or not hold.ref_id:
        raise b._http("invalid_hold", "hold transaction has no reference", 422)
    consumed = (
        await db.execute(
            select(WalletTransaction.id)
            .where(
                WalletTransaction.user_id == hold.user_id,
                WalletTransaction.ref_type == hold.ref_type,
                WalletTransaction.ref_id == hold.ref_id,
                WalletTransaction.kind.in_(("settle", "release")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if consumed is not None:
        raise b._http(
            "HOLD_ALREADY_CONSUMED", "hold was already settled or released", 409
        )
    tx = await b.billing_core.release(
        db,
        hold.user_id,
        ref_type=hold.ref_type,
        ref_id=hold.ref_id,
        idempotency_key=f"admin_release_hold:{tx_id}",
        meta={"reason": "admin orphan hold release", "hold_tx_id": tx_id},
    )
    if tx is None:
        raise b._http("HOLD_NOT_ACTIVE", "hold is no longer active", 409)
    await b.write_audit(
        db,
        event_type="wallet.hold.force_release",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=b.request_ip_hash(request),
        target_user_id=hold.user_id,
        details={"hold_tx_id": tx_id, "release_tx_id": tx.id},
        autocommit=False,
    )
    await db.commit()
    await b._invalidate_balance_cache(hold.user_id)
    return b._tx_out(tx)


@router.post(
    "/admin/billing/bootstrap",
    response_model=AdminBillingOverviewOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_billing_bootstrap(
    body: AdminBillingBootstrapIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminBillingOverviewOut:
    b = current_runtime()
    provided_secret = (body.redemption_code_secret or "").strip()
    secret_generated = not provided_secret
    redemption_secret = provided_secret or b._generate_redemption_secret()
    low_balance_micro = b._rmb_to_micro_or_422(
        body.low_balance_warn_rmb, field="low_balance_warn_rmb"
    )
    if low_balance_micro < 0:
        raise b._http(
            "invalid_amount",
            "low_balance_warn_rmb: amount must be non-negative",
            422,
        )
    pricing_items: list[dict[str, Any]] = []
    for tier, threshold in body.image_size_thresholds.items():
        if threshold < 0:
            raise b._http("invalid_request", "thresholds must be non-negative", 422)
        price_rmb = body.image_prices_rmb.get(tier)
        if price_rmb is None:
            raise b._http(
                "invalid_request",
                f"image_prices_rmb.{tier}: enabled tier price is required",
                422,
            )
        price_micro = b._rmb_to_micro_or_422(
            price_rmb, field=f"image_prices_rmb.{tier}"
        )
        b._validate_enabled_pricing_value(
            unit="per_image",
            price_micro=price_micro,
            enabled=True,
            field=f"image_prices_rmb.{tier}",
        )
        pricing_items.append(
            {
                "id": new_uuid7(),
                "scope": "image_size",
                "key": tier,
                "variant": "default",
                "unit": "per_image",
                "price_micro": price_micro,
                "enabled": True,
                "note": "bootstrap default",
                "updated_at": datetime.now(timezone.utc),
            }
        )
    bind = await db.connection()
    if bind.dialect.name == "postgresql":
        insert_stmt = pg_insert(PricingRule).values(pricing_items)
        await db.execute(
            insert_stmt.on_conflict_do_update(
                constraint="uq_pricing_scope_key_variant_unit",
                set_={
                    "price_micro": insert_stmt.excluded.price_micro,
                    "enabled": insert_stmt.excluded.enabled,
                    "note": insert_stmt.excluded.note,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
    else:
        for item in pricing_items:
            existing = (
                await db.execute(
                    select(PricingRule).where(
                        PricingRule.scope == item["scope"],
                        PricingRule.key == item["key"],
                        PricingRule.variant == item["variant"],
                        PricingRule.unit == item["unit"],
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(PricingRule(**item))
            else:
                existing.price_micro = item["price_micro"]
                existing.enabled = True
                existing.note = item["note"]
                existing.updated_at = datetime.now(timezone.utc)
    await b.update_settings(
        db,
        [
            ("billing.redemption_code_secret", redemption_secret),
            ("billing.enabled", "1" if body.enabled else "0"),
            ("billing.usd_to_rmb_rate", str(body.usd_to_rmb_rate)),
            ("billing.low_balance_warn_micro", str(low_balance_micro)),
            (
                "billing.image_size_thresholds",
                json.dumps(body.image_size_thresholds, ensure_ascii=False),
            ),
            ("billing.bootstrap_completed", "1"),
            ("billing.show_estimate_in_composer", "1"),
        ],
    )
    await b.write_audit(
        db,
        event_type="billing.bootstrap",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=b.request_ip_hash(request),
        details={
            "tiers": sorted(body.image_size_thresholds),
            "enabled": body.enabled,
            "redemption_secret_generated": secret_generated,
        },
        autocommit=False,
    )
    await db.commit()
    return await b.admin_billing_overview(admin, db)


@router.post(
    "/admin/billing/redemption_secret:rotate",
    response_model=AdminBillingOverviewOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_rotate_redemption_secret(
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminBillingOverviewOut:
    b = current_runtime()
    secret_spec = b.get_spec("billing.redemption_code_secret")
    if secret_spec is None:
        raise b._http(
            "invalid_request", "redemption secret setting is unsupported", 500
        )
    old_secret = await b.get_setting(db, secret_spec)
    new_secret = b._generate_redemption_secret()
    await b.update_settings(db, [("billing.redemption_code_secret", new_secret)])
    transition_expires_at = None
    if old_secret:
        try:
            transition_expires_at = await b.remember_previous_redemption_secret(
                db, old_secret
            )
        except PreviousRedemptionSecretLocked as exc:
            raise b._http(
                "previous_secret_locked",
                "another rotation is still inside the 24h transition window",
                409,
            ) from exc
    secret_hash8 = hashlib.sha256(new_secret.encode("utf-8")).hexdigest()[:8]
    await b.write_audit(
        db,
        event_type="billing.secret.rotate"
        if old_secret
        else "billing.secret.configure",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=b.request_ip_hash(request),
        details={
            "secret_hash8": secret_hash8,
            "previous_secret_valid_until": transition_expires_at,
            "revoked_unredeemed_count": 0,
            "generated_by": "system",
        },
        autocommit=False,
    )
    await db.commit()
    return await b.admin_billing_overview(admin, db)
