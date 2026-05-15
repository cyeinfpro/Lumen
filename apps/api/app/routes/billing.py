"""Wallet, pricing, and redemption APIs."""

from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import (
    AuditLog,
    PricingRule,
    RedemptionCode,
    RedemptionCodeUsage,
    User,
    UserApiCredential,
    UserWallet,
    WalletTransaction,
    new_uuid7,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    AdminBillingAuditEventOut,
    AdminBillingBootstrapIn,
    AdminBillingOverviewOut,
    AdminOrphanHoldOut,
    AdminRedemptionBatchRedownloadOut,
    AdminRedemptionCodeCreateIn,
    AdminRedemptionCodeCreateOut,
    AdminRedemptionCodeListOut,
    AdminRedemptionCodeOut,
    AdminRedemptionUsageListOut,
    AdminRedemptionUsageOut,
    AdminSetAccountModeIn,
    AdminWalletAdjustIn,
    AdminWalletDetailOut,
    AdminWalletListOut,
    AdminWalletOut,
    AdminWalletAuditOut,
    MoneyOut,
    PricingImportIn,
    PricingRuleOut,
    PricingRulesOut,
    PricingRulesUpdateIn,
    RedemptionIn,
    RedemptionOut,
    RedemptionUsageListOut,
    RedemptionUsageOut,
    WalletOut,
    WalletTransactionListOut,
    WalletTransactionOut,
)

from ..audit import hash_email, request_ip_hash, write_audit
from ..db import get_db
from ..deps import AdminUser, CurrentUser, verify_csrf
from ..observability import (
    redemption_redeemed_total,
    wallet_balance_total,
    wallet_hold_active,
    wallet_hold_micro,
    wallet_orphan_holds,
)
from ..ratelimit import RateLimiter, client_ip
from ..redis_client import get_redis
from ..runtime_settings import get_setting, update_settings


router = APIRouter(tags=["billing"])

REDEMPTION_LIMITER = RateLimiter(
    capacity=10,
    refill_per_sec=10 / 300,
    always_on=True,
)
_DOWNLOAD_TOKEN_PREFIX = "billing:redemption_csv:"
_PLAINTEXT_BATCH_PREFIX = "billing:redemption_plaintext:"
_REDEMPTION_DOWNLOAD_TTL_SECONDS = 300
_BILLING_AUDIT_EVENT_PREFIXES = (
    "wallet.",
    "pricing.",
    "redemption.",
    "account.mode_change",
    "billing.",
)


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


def _billing_http(exc: billing_core.BillingError) -> HTTPException:
    return _http(exc.code, exc.message, exc.status_code)


def _money(amount_micro: int) -> MoneyOut:
    return MoneyOut(**billing_core.money_dict(amount_micro))


async def _setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    return await get_setting(db, spec)


async def _low_balance_threshold(db: AsyncSession) -> int:
    raw = await _setting_raw(db, "billing.low_balance_warn_micro")
    try:
        return int(raw) if raw is not None else 2_000_000
    except ValueError:
        return 2_000_000


async def _allow_negative_balance(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.allow_negative_balance"), False
    )


async def _image_thresholds(db: AsyncSession) -> dict[str, int]:
    return billing_core.parse_thresholds(
        await _setting_raw(db, "billing.image_size_thresholds")
    )


async def _redemption_secret(db: AsyncSession) -> str:
    secret = (await _setting_raw(db, "billing.redemption_code_secret") or "").strip()
    if not secret:
        raise _http(
            "REDEMPTION_SECRET_NOT_CONFIGURED",
            "redemption code secret is not configured; configure billing.redemption_code_secret in Admin billing settings",
            412,
        )
    return secret


async def _billing_enabled_setting(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.enabled"), False
    )


async def _bootstrap_completed_setting(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.bootstrap_completed"), False
    )


async def _require_bootstrap_completed(db: AsyncSession) -> None:
    if await _bootstrap_completed_setting(db):
        return
    raise _http(
        "BOOTSTRAP_INCOMPLETE",
        "billing bootstrap is incomplete; run admin billing bootstrap first",
        412,
    )


async def _require_redemption_operational(db: AsyncSession) -> None:
    if not await _billing_enabled_setting(db):
        raise _http("BILLING_DISABLED", "billing is disabled", 412)
    await _require_bootstrap_completed(db)


def _require_wallet_user(user: User) -> None:
    if getattr(user, "account_mode", "wallet") != "wallet":
        raise _http(
            "ACCOUNT_MODE_FORBIDDEN", "account mode does not allow wallet access", 403
        )


def _pricing_rule_out(rule: PricingRule) -> PricingRuleOut:
    return PricingRuleOut(
        id=rule.id,
        scope=rule.scope,  # type: ignore[arg-type]
        key=rule.key,
        variant=rule.variant,
        unit=rule.unit,  # type: ignore[arg-type]
        price=_money(rule.price_micro),
        enabled=rule.enabled,
        note=rule.note,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


async def _wallet_out(db: AsyncSession, user: User) -> WalletOut:
    mode = getattr(user, "account_mode", "wallet")
    if mode != "wallet":
        # Why: a wallet→byok admin switch with `on_residual_balance=freeze`
        # leaves a non-zero balance in user_wallets that we still want admins
        # to see (and the user themselves) so it can be reconciled. The
        # `frozen=True` flag tells the UI to render it as inert.
        wallet = await billing_core.get_wallet(db, user.id, lock=False, create=False)
        if wallet is not None and (wallet.balance_micro > 0 or wallet.hold_micro > 0):
            return WalletOut(
                mode="byok",
                balance=_money(wallet.balance_micro),
                hold=_money(wallet.hold_micro),
                frozen=True,
            )
        return WalletOut(mode="byok", balance=None, hold=None, frozen=False)
    wallet = await billing_core.get_wallet(db, user.id, lock=False)
    threshold = await _low_balance_threshold(db)
    return WalletOut(
        mode="wallet",
        balance=_money(wallet.balance_micro),
        hold=_money(wallet.hold_micro),
        low_balance_threshold=_money(threshold),
        frozen=False,
    )


def _tx_out(tx: WalletTransaction) -> WalletTransactionOut:
    return WalletTransactionOut(
        id=tx.id,
        kind=tx.kind,
        amount=_money(tx.amount_micro),
        balance_after=_money(tx.balance_after),
        hold_after=_money(tx.hold_after),
        ref_type=tx.ref_type,
        ref_id=tx.ref_id,
        meta=tx.meta or {},
        created_at=tx.created_at,
        created_by_admin=tx.created_by_admin,
    )


def _redemption_status(
    code: RedemptionCode, *, now: datetime | None = None
) -> str:
    current = now or datetime.now(timezone.utc)
    expires_at = code.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if code.revoked_at is not None:
        return "revoked"
    if expires_at is not None and expires_at < current:
        return "expired"
    if code.redeemed_count >= code.max_redemptions:
        return "exhausted"
    return "active"


def _redemption_code_out(
    code: RedemptionCode, *, now: datetime | None = None
) -> AdminRedemptionCodeOut:
    usable_count = max(0, int(code.max_redemptions) - int(code.redeemed_count))
    return AdminRedemptionCodeOut(
        id=code.id,
        code_prefix=code.code_prefix,
        amount=_money(code.amount_micro),
        max_redemptions=code.max_redemptions,
        redeemed_count=code.redeemed_count,
        usable_count=usable_count,
        status=_redemption_status(code, now=now),  # type: ignore[arg-type]
        batch_id=code.batch_id,
        note=code.note,
        expires_at=code.expires_at,
        revoked_at=code.revoked_at,
        created_by=code.created_by,
        created_at=code.created_at,
        updated_at=code.updated_at,
    )


def _audit_out(row: AuditLog) -> AdminBillingAuditEventOut:
    return AdminBillingAuditEventOut(
        id=row.id,
        event_type=row.event_type,
        user_id=row.user_id,
        target_user_id=row.target_user_id,
        details=row.details or {},
        created_at=row.created_at,
    )


def _cursor_filter(stmt: Any, model: Any, cursor: str | None) -> Any:
    if not cursor:
        return stmt
    try:
        ts_raw, row_id = cursor.split("|", 1)
        ts = datetime.fromisoformat(ts_raw)
    except ValueError:
        raise _http("invalid_cursor", "cursor is invalid", 422)
    return stmt.where(
        (model.created_at < ts) | ((model.created_at == ts) & (model.id < row_id))
    )


def _next_cursor(rows: list[Any], has_more: bool, attr: str = "created_at") -> str | None:
    if not has_more or not rows:
        return None
    last = rows[-1]
    ts = getattr(last, attr)
    return f"{ts.isoformat()}|{last.id}"


def _redemption_plaintext_payload(
    *, batch_id: str, amount_micro: int, codes: list[str], expires_at: datetime | None
) -> str:
    return json.dumps(
        {
            "batch_id": batch_id,
            "amount_rmb": billing_core.micro_to_rmb_str(amount_micro),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "codes": codes,
        },
        ensure_ascii=False,
    )


def _redemption_csv_payload(
    *, batch_id: str, amount_micro: int, codes: list[str], expires_at: datetime | None
) -> str:
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["code", "amount_rmb", "batch_id", "expires_at"])
    for code in codes:
        writer.writerow(
            [
                code,
                billing_core.micro_to_rmb_str(amount_micro),
                batch_id,
                expires_at.isoformat() if expires_at else "",
            ]
        )
    return csv_buf.getvalue()


def _redemption_csv_batch_id(csv_text: str) -> str | None:
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        value = row.get("batch_id")
        return str(value) if value else None
    return None


def _require_redemption_download_batch(csv_text: str, batch_id: str) -> None:
    if _redemption_csv_batch_id(csv_text) != batch_id:
        raise _http("download_token_batch_mismatch", "download token does not match batch", 404)


async def _store_redemption_plaintext_batch(
    *,
    batch_id: str,
    amount_micro: int,
    codes: list[str],
    expires_at: datetime | None,
) -> str:
    token = "tok_" + secrets.token_urlsafe(24)
    redis = get_redis()
    payload = _redemption_plaintext_payload(
        batch_id=batch_id,
        amount_micro=amount_micro,
        codes=codes,
        expires_at=expires_at,
    )
    csv_payload = _redemption_csv_payload(
        batch_id=batch_id,
        amount_micro=amount_micro,
        codes=codes,
        expires_at=expires_at,
    )
    plaintext_key = _PLAINTEXT_BATCH_PREFIX + batch_id
    download_key = _DOWNLOAD_TOKEN_PREFIX + token
    try:
        await redis.set(
            plaintext_key,
            payload,
            ex=_REDEMPTION_DOWNLOAD_TTL_SECONDS,
        )
        await redis.set(
            download_key,
            csv_payload,
            ex=_REDEMPTION_DOWNLOAD_TTL_SECONDS,
        )
    except Exception:
        try:
            await redis.delete(plaintext_key)
            await redis.delete(download_key)
        except Exception:  # noqa: BLE001
            pass
        raise
    return token


async def _load_redemption_plaintext_batch(batch_id: str) -> dict[str, Any]:
    redis = get_redis()
    data = await redis.get(_PLAINTEXT_BATCH_PREFIX + batch_id)
    if data is None:
        raise _http(
            "redemption_plaintext_expired",
            "redemption code plaintext window expired",
            410,
        )
    text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _http("redemption_plaintext_corrupt", "plaintext cache is invalid", 500) from exc
    if not isinstance(payload, dict) or payload.get("batch_id") != batch_id:
        raise _http("redemption_plaintext_corrupt", "plaintext cache is invalid", 500)
    codes = payload.get("codes")
    if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
        raise _http("redemption_plaintext_corrupt", "plaintext cache is invalid", 500)
    return payload


def _billing_audit_predicate() -> Any:
    return or_(
        *[
            AuditLog.event_type.like(f"{prefix}%")
            if prefix.endswith(".")
            else AuditLog.event_type == prefix
            for prefix in _BILLING_AUDIT_EVENT_PREFIXES
        ]
    )


async def _threshold_price_alignment(db: AsyncSession) -> tuple[bool, list[str]]:
    thresholds = await _image_thresholds(db)
    rows = (
        await db.execute(
            select(PricingRule.key).where(
                PricingRule.scope == "image_size",
                PricingRule.unit == "per_image",
                PricingRule.enabled.is_(True),
            )
        )
    ).scalars().all()
    priced = {str(row) for row in rows}
    missing = sorted(key for key in thresholds if key not in priced)
    return not missing, missing


async def _validate_thresholds_have_prices(
    db: AsyncSession,
    thresholds: dict[str, int],
    candidate_items: list[dict[str, Any]] | None = None,
    *,
    force: bool = False,
) -> None:
    if force:
        return
    rows = (
        await db.execute(
            select(PricingRule.key).where(
                PricingRule.scope == "image_size",
                PricingRule.unit == "per_image",
                PricingRule.enabled.is_(True),
            )
        )
    ).scalars().all()
    enabled = {str(row) for row in rows}
    for item in candidate_items or []:
        if (
            item.get("scope") == "image_size"
            and item.get("unit") == "per_image"
        ):
            key = str(item.get("key"))
            if item.get("enabled") is True:
                enabled.add(key)
            else:
                enabled.discard(key)
    missing = sorted(key for key in thresholds if key not in enabled)
    if missing:
        raise _http(
            "THRESHOLDS_PRICING_MISMATCH",
            "every image size threshold must have an enabled pricing rule",
            422,
            missing=missing,
        )


def _parse_price_rows(content: str) -> list[dict[str, Any]]:
    text = content.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("models"), list):
            parsed = parsed["models"]
        if isinstance(parsed, list):
            return [row for row in parsed if isinstance(row, dict)]
    except json.JSONDecodeError:
        pass

    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            if current:
                rows.append(current)
            current = {}
            line = line[2:].strip()
            if not line:
                continue
        if ":" not in line or current is None:
            continue
        key, value = line.split(":", 1)
        value = value.strip().strip("'\"")
        try:
            parsed_value: Any = float(value)
        except ValueError:
            parsed_value = value
        current[key.strip()] = parsed_value
    if current:
        rows.append(current)
    return rows


def _openai_price_micro(usd_per_1m: Any, rate: float) -> int:
    try:
        value = Decimal(str(usd_per_1m))
        rate_value = Decimal(str(rate))
    except InvalidOperation as exc:
        raise _http(
            "invalid_price_file", "price value is not a valid decimal", 422
        ) from exc
    if not rate_value.is_finite() or rate_value <= 0:
        raise _http("invalid_price_file", "rate is not a positive finite decimal", 422)
    if not value.is_finite() or value < 0:
        raise _http(
            "invalid_price_file", "price value is not a non-negative decimal", 422
        )
    micro = value * rate_value * Decimal(billing_core.MICRO_RMB) / Decimal(1000)
    try:
        return int(micro.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise _http("invalid_price_file", "price value is out of range", 422) from exc


def _rmb_to_micro_or_422(value: str | int | float, *, field: str) -> int:
    try:
        return billing_core.rmb_to_micro(value)
    except billing_core.BillingError as exc:
        raise _http(exc.code, f"{field}: {exc.message}", exc.status_code) from exc


@router.get("/me/wallet", response_model=WalletOut)
async def get_my_wallet(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WalletOut:
    return await _wallet_out(db, user)


@router.get("/me/wallet/transactions", response_model=WalletTransactionListOut)
async def list_my_wallet_transactions(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    kind: str | None = Query(default=None, max_length=32),
) -> WalletTransactionListOut:
    _require_wallet_user(user)
    stmt = (
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user.id)
        .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
        .limit(limit + 1)
    )
    if kind:
        stmt = stmt.where(WalletTransaction.kind == kind)
    if cursor:
        try:
            ts_raw, tx_id = cursor.split("|", 1)
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            raise _http("invalid_cursor", "cursor is invalid", 422)
        stmt = stmt.where(
            (WalletTransaction.created_at < ts)
            | ((WalletTransaction.created_at == ts) & (WalletTransaction.id < tx_id))
        )
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{last.created_at.isoformat()}|{last.id}"
    return WalletTransactionListOut(
        items=[_tx_out(row) for row in rows], next_cursor=next_cursor
    )


@router.get("/me/pricing", response_model=PricingRulesOut)
async def get_my_pricing(
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    rows = (
        (
            await db.execute(
                select(PricingRule)
                .where(PricingRule.enabled.is_(True))
                .order_by(PricingRule.scope, PricingRule.key, PricingRule.unit)
            )
        )
        .scalars()
        .all()
    )
    return PricingRulesOut(
        items=[_pricing_rule_out(row) for row in rows],
        image_size_thresholds=await _image_thresholds(db),
        billing_enabled=billing_core.parse_bool_setting(
            await _setting_raw(db, "billing.enabled"), False
        ),
        show_estimate_in_composer=billing_core.parse_bool_setting(
            await _setting_raw(db, "billing.show_estimate_in_composer"), True
        ),
    )


@router.get("/admin/billing/audit", response_model=list[AdminBillingAuditEventOut])
async def admin_billing_audit(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    event_type: str | None = Query(default=None, max_length=64),
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[AdminBillingAuditEventOut]:
    stmt = select(AuditLog).where(_billing_audit_predicate())
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    rows = (
        (
            await db.execute(
                stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_audit_out(row) for row in rows]


@router.get("/admin/billing/overview", response_model=AdminBillingOverviewOut)
async def admin_billing_overview(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminBillingOverviewOut:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    billing_enabled = await _billing_enabled_setting(db)
    bootstrap_completed = await _bootstrap_completed_setting(db)
    secret_configured = bool((await _setting_raw(db, "billing.redemption_code_secret") or "").strip())
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
                        RedemptionCode.expires_at >= now,
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
                select(func.coalesce(func.sum(WalletTransaction.amount_micro), 0)).where(
                    WalletTransaction.kind.in_(("charge", "settle")),
                    WalletTransaction.created_at >= since,
                    WalletTransaction.amount_micro < 0,
                )
            )
        ).scalar_one()
        or 0
    )
    aligned, missing = await _threshold_price_alignment(db)
    audit_rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(_billing_audit_predicate())
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
        wallet_total_balance=_money(wallet_balance),
        active_holds_count=int(hold_row[0] or 0),
        active_holds=_money(int(hold_row[1] or 0)),
        codes_active=active_codes,
        codes_redeemed_24h=int(redeemed_row[0] or 0),
        codes_redeemed_24h_amount=_money(int(redeemed_row[1] or 0)),
        charges_24h=_money(abs(charges_24h)),
        thresholds_pricing_aligned=aligned,
        thresholds_missing_prices=missing,
        recent_audit_events=[_audit_out(row) for row in audit_rows],
    )


@router.get("/admin/billing/wallet_audit", response_model=AdminWalletAuditOut)
async def admin_wallet_audit(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AdminWalletAuditOut:
    stmt = select(WalletTransaction).order_by(
        WalletTransaction.user_id.asc(),
        WalletTransaction.created_at.asc(),
        WalletTransaction.id.asc(),
    )
    if user_id:
        stmt = stmt.where(WalletTransaction.user_id == user_id)
    rows = (await db.execute(stmt)).scalars().all()
    balances: dict[str, int] = {}
    mismatches: list[str] = []
    for tx in rows:
        running = balances.get(tx.user_id, 0) + int(tx.amount_micro)
        balances[tx.user_id] = running
        if running != int(tx.balance_after):
            mismatches.append(
                f"user={tx.user_id} tx={tx.id} kind={tx.kind} "
                f"running={running} balance_after={tx.balance_after}"
            )
    return AdminWalletAuditOut(
        ok=not mismatches,
        transactions=len(rows),
        users=len(balances),
        mismatch_count=len(mismatches),
        mismatches=mismatches[:limit],
    )


@router.get("/admin/billing/orphan_holds", response_model=list[AdminOrphanHoldOut])
async def admin_list_orphan_holds(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    min_age_minutes: Annotated[int, Query(ge=0, le=60 * 24 * 30)] = 60,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AdminOrphanHoldOut]:
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
                tx=_tx_out(hold),
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
    hold = await db.get(WalletTransaction, tx_id)
    if hold is None or hold.kind != "hold":
        raise _http("not_found", "hold transaction not found", 404)
    if not hold.ref_type or not hold.ref_id:
        raise _http("invalid_hold", "hold transaction has no reference", 422)
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
        raise _http("HOLD_ALREADY_CONSUMED", "hold was already settled or released", 409)
    tx = await billing_core.release(
        db,
        hold.user_id,
        ref_type=hold.ref_type,
        ref_id=hold.ref_id,
        idempotency_key=f"admin_release_hold:{tx_id}",
        meta={"reason": "admin orphan hold release", "hold_tx_id": tx_id},
    )
    if tx is None:
        raise _http("HOLD_NOT_ACTIVE", "hold is no longer active", 409)
    await write_audit(
        db,
        event_type="wallet.hold.force_release",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        target_user_id=hold.user_id,
        details={"hold_tx_id": tx_id, "release_tx_id": tx.id},
        autocommit=False,
    )
    await db.commit()
    return _tx_out(tx)


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
    low_balance_micro = _rmb_to_micro_or_422(
        body.low_balance_warn_rmb, field="low_balance_warn_rmb"
    )
    pricing_items = []
    for tier, threshold in body.image_size_thresholds.items():
        if threshold < 0:
            raise _http("invalid_request", "thresholds must be non-negative", 422)
        price_rmb = body.image_prices_rmb.get(tier, "0")
        price_micro = _rmb_to_micro_or_422(price_rmb, field=f"image_prices_rmb.{tier}")
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
    await update_settings(
        db,
        [
            ("billing.redemption_code_secret", body.redemption_code_secret),
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
    await write_audit(
        db,
        event_type="billing.bootstrap",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"tiers": sorted(body.image_size_thresholds), "enabled": body.enabled},
        autocommit=False,
    )
    await db.commit()
    return await admin_billing_overview(admin, db)


@router.post(
    "/me/redemptions",
    response_model=RedemptionOut,
    dependencies=[Depends(verify_csrf)],
)
async def redeem_code(
    body: RedemptionIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedemptionOut:
    _require_wallet_user(user)
    await _require_redemption_operational(db)
    ip = client_ip(request)
    redis = get_redis()
    await REDEMPTION_LIMITER.check(redis, f"rl:redemption:user:{user.id}")
    await REDEMPTION_LIMITER.check(redis, f"rl:redemption:ip:{ip}")
    secret = await _redemption_secret(db)
    code_hash = billing_core.hash_redemption_code(body.code, secret)
    now = datetime.now(timezone.utc)

    code = (
        await db.execute(
            select(RedemptionCode)
            .where(RedemptionCode.code_hash == code_hash)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if code is None:
        raise _http("CODE_NOT_FOUND", "redemption code not found", 404)
    if code.revoked_at is not None:
        raise _http("CODE_REVOKED", "redemption code was revoked", 410)
    expires_at = code.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at <= now:
        raise _http("CODE_EXPIRED", "redemption code expired", 410)
    if code.redeemed_count >= code.max_redemptions:
        raise _http("CODE_EXHAUSTED", "redemption code is exhausted", 409)

    usage_id = new_uuid7()
    try:
        tx = await billing_core.topup_redeem(
            db,
            user.id,
            code.amount_micro,
            usage_id=usage_id,
            code_id=code.id,
        )
        db.add(
            RedemptionCodeUsage(
                id=usage_id,
                code_id=code.id,
                user_id=user.id,
                amount_micro=code.amount_micro,
                wallet_tx_id=tx.id,
                ip_hash=request_ip_hash(request),
            )
        )
        code.redeemed_count += 1
        redemption_redeemed_total.inc()
        await write_audit(
            db,
            event_type="wallet.topup.redeem",
            user_id=user.id,
            actor_email_hash=hash_email(user.email),
            actor_ip_hash=request_ip_hash(request),
            details={
                "code_id": code.id,
                "usage_id": usage_id,
                "amount_micro": code.amount_micro,
                "balance_after": tx.balance_after,
            },
            autocommit=False,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # Only treat the per-user-redeem unique constraint as CODE_ALREADY_USED.
        # Other constraint violations (FK, wallet_tx idempotency, etc.) bubble
        # up as 500 so misattribution doesn't mask real bugs.
        diag = str(getattr(exc.orig, "diag", None) or "")
        msg = f"{exc!s} {diag}".lower()
        if "uq_redeem_code_user" in msg:
            raise _http(
                "CODE_ALREADY_USED",
                "this code was already used by this user",
                409,
            ) from exc
        raise

    return RedemptionOut(
        amount=_money(code.amount_micro), balance=_money(tx.balance_after)
    )


@router.get("/me/redemptions", response_model=RedemptionUsageListOut)
async def list_my_redemptions(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> RedemptionUsageListOut:
    _require_wallet_user(user)
    stmt = (
        select(RedemptionCodeUsage)
        .where(RedemptionCodeUsage.user_id == user.id)
        .order_by(RedemptionCodeUsage.redeemed_at.desc(), RedemptionCodeUsage.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        try:
            ts_raw, usage_id = cursor.split("|", 1)
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            raise _http("invalid_cursor", "cursor is invalid", 422)
        stmt = stmt.where(
            (RedemptionCodeUsage.redeemed_at < ts)
            | (
                (RedemptionCodeUsage.redeemed_at == ts)
                & (RedemptionCodeUsage.id < usage_id)
            )
        )
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{last.redeemed_at.isoformat()}|{last.id}"
    return RedemptionUsageListOut(
        items=[
            RedemptionUsageOut(
                id=row.id,
                code_id=row.code_id,
                amount=_money(row.amount_micro),
                redeemed_at=row.redeemed_at,
            )
            for row in rows
        ],
        next_cursor=next_cursor,
    )


@router.get("/admin/pricing", response_model=PricingRulesOut)
async def admin_list_pricing(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    rows = (
        (
            await db.execute(
                select(PricingRule).order_by(
                    PricingRule.scope, PricingRule.key, PricingRule.unit
                )
            )
        )
        .scalars()
        .all()
    )
    return PricingRulesOut(
        items=[_pricing_rule_out(row) for row in rows],
        image_size_thresholds=await _image_thresholds(db),
        billing_enabled=billing_core.parse_bool_setting(
            await _setting_raw(db, "billing.enabled"), False
        ),
        show_estimate_in_composer=billing_core.parse_bool_setting(
            await _setting_raw(db, "billing.show_estimate_in_composer"), True
        ),
    )


@router.put(
    "/admin/pricing",
    response_model=PricingRulesOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_update_pricing(
    body: PricingRulesUpdateIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    now = datetime.now(timezone.utc)
    values = []
    for item in body.items:
        price = _rmb_to_micro_or_422(item.price_rmb, field="price_rmb")
        if price < 0:
            raise _http("invalid_amount", "price must be non-negative", 422)
        values.append(
            {
                "id": new_uuid7(),
                "scope": item.scope,
                "key": item.key,
                "variant": item.variant,
                "unit": item.unit,
                "price_micro": price,
                "enabled": item.enabled,
                "note": item.note,
                "updated_at": now,
            }
        )
    thresholds_to_write = body.image_size_thresholds
    thresholds_for_check = thresholds_to_write or await _image_thresholds(db)
    await _validate_thresholds_have_prices(
        db,
        thresholds_for_check,
        values,
        force=body.force,
    )
    bind = await db.connection()
    if bind.dialect.name == "postgresql":
        insert_stmt = pg_insert(PricingRule).values(values)
        await db.execute(
            insert_stmt.on_conflict_do_update(
                constraint="uq_pricing_scope_key_variant_unit",
                set_={
                    "price_micro": insert_stmt.excluded.price_micro,
                    "enabled": insert_stmt.excluded.enabled,
                    "note": insert_stmt.excluded.note,
                    "updated_at": now,
                },
            )
        )
    else:
        for value in values:
            existing = (
                await db.execute(
                    select(PricingRule).where(
                        PricingRule.scope == value["scope"],
                        PricingRule.key == value["key"],
                        PricingRule.variant == value["variant"],
                        PricingRule.unit == value["unit"],
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(PricingRule(**value))
            else:
                existing.price_micro = value["price_micro"]
                existing.enabled = value["enabled"]
                existing.note = value["note"]
                existing.updated_at = now
    if thresholds_to_write is not None:
        await update_settings(
            db,
            [
                (
                    "billing.image_size_thresholds",
                    json.dumps(thresholds_to_write, ensure_ascii=False),
                )
            ],
        )
    await write_audit(
        db,
        event_type="pricing.update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "count": len(values),
            "thresholds_updated": thresholds_to_write is not None,
            "force": body.force,
        },
        autocommit=False,
    )
    await db.commit()
    return await admin_list_pricing(admin, db)


@router.post(
    "/admin/pricing/import_openai",
    response_model=PricingRulesOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_import_openai_pricing(
    body: PricingImportIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    rows = _parse_price_rows(body.content)
    items = []
    for row in rows:
        model = str(row.get("model") or "").strip()
        if not model:
            continue
        if "input_usd_per_1m" in row:
            items.append(
                {
                    "scope": "chat_model",
                    "key": model,
                    "variant": "default",
                    "unit": "per_1k_tokens_in",
                    "price_rmb": billing_core.micro_to_rmb_str(
                        _openai_price_micro(row["input_usd_per_1m"], body.rate)
                    ),
                    "enabled": True,
                    "note": f"OpenAI input USD/1M={row['input_usd_per_1m']} rate={body.rate}",
                }
            )
        if "output_usd_per_1m" in row:
            items.append(
                {
                    "scope": "chat_model",
                    "key": model,
                    "variant": "default",
                    "unit": "per_1k_tokens_out",
                    "price_rmb": billing_core.micro_to_rmb_str(
                        _openai_price_micro(row["output_usd_per_1m"], body.rate)
                    ),
                    "enabled": True,
                    "note": f"OpenAI output USD/1M={row['output_usd_per_1m']} rate={body.rate}",
                }
            )
    if not items:
        raise _http("invalid_price_file", "no model prices found", 422)
    update_body = PricingRulesUpdateIn.model_validate({"items": items})
    return await admin_update_pricing(update_body, request, admin, db)


@router.get("/admin/redemption_codes", response_model=AdminRedemptionCodeListOut)
async def admin_list_redemption_codes(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = "active",
    batch_id: str | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> AdminRedemptionCodeListOut:
    stmt = select(RedemptionCode)
    now = datetime.now(timezone.utc)
    if batch_id:
        stmt = stmt.where(RedemptionCode.batch_id == batch_id)
    if q:
        needle = q.strip()
        if needle:
            stmt = stmt.where(
                or_(
                    RedemptionCode.code_prefix.ilike(f"{needle[:8]}%"),
                    RedemptionCode.batch_id.ilike(f"%{needle}%"),
                )
            )
    if status == "revoked":
        stmt = stmt.where(RedemptionCode.revoked_at.is_not(None))
    elif status == "expired":
        stmt = stmt.where(
            RedemptionCode.expires_at.is_not(None), RedemptionCode.expires_at < now
        )
    elif status == "exhausted":
        stmt = stmt.where(
            RedemptionCode.redeemed_count >= RedemptionCode.max_redemptions
        )
    elif status == "active":
        stmt = stmt.where(
            RedemptionCode.revoked_at.is_(None),
            or_(RedemptionCode.expires_at.is_(None), RedemptionCode.expires_at >= now),
            RedemptionCode.redeemed_count < RedemptionCode.max_redemptions,
        )
    elif status in {None, "", "all"}:
        pass
    else:
        raise _http("invalid_status", "status is invalid", 422)
    stmt = _cursor_filter(stmt, RedemptionCode, cursor)
    rows = (
        (
            await db.execute(
                stmt.order_by(
                    RedemptionCode.created_at.desc(), RedemptionCode.id.desc()
                ).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return AdminRedemptionCodeListOut(
        items=[_redemption_code_out(row, now=now) for row in rows],
        next_cursor=_next_cursor(rows, has_more),
    )


@router.get(
    "/admin/redemption_codes/{code_id}/usage",
    response_model=AdminRedemptionUsageListOut,
)
async def admin_list_redemption_code_usage(
    code_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> AdminRedemptionUsageListOut:
    rows = (
        await db.execute(
            select(RedemptionCodeUsage, User.email)
            .join(User, User.id == RedemptionCodeUsage.user_id)
            .where(RedemptionCodeUsage.code_id == code_id)
            .order_by(
                RedemptionCodeUsage.redeemed_at.desc(),
                RedemptionCodeUsage.id.desc(),
            )
            .limit(limit)
        )
    ).all()
    return AdminRedemptionUsageListOut(
        items=[
            AdminRedemptionUsageOut(
                id=usage.id,
                code_id=usage.code_id,
                user_id=usage.user_id,
                user_email=email,
                amount=_money(usage.amount_micro),
                wallet_tx_id=usage.wallet_tx_id,
                redeemed_at=usage.redeemed_at,
                ip_hash=usage.ip_hash,
            )
            for usage, email in rows
        ]
    )


@router.post(
    "/admin/redemption_codes",
    response_model=AdminRedemptionCodeCreateOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_create_redemption_codes(
    body: AdminRedemptionCodeCreateIn,
    request: Request,
    response: Response,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedemptionCodeCreateOut:
    amount = _rmb_to_micro_or_422(body.amount_rmb, field="amount_rmb")
    if amount <= 0:
        raise _http("invalid_amount", "amount must be positive", 422)
    await _require_bootstrap_completed(db)
    secret = await _redemption_secret(db)
    batch_id = new_uuid7()
    plaintext_codes: list[str] = []
    for _ in range(body.count):
        code = billing_core.generate_redemption_code()
        plaintext_codes.append(code)
        db.add(
            RedemptionCode(
                id=new_uuid7(),
                code_hash=billing_core.hash_redemption_code(code, secret),
                code_prefix=billing_core.code_prefix(code),
                amount_micro=amount,
                max_redemptions=body.max_redemptions,
                batch_id=batch_id,
                note=body.note,
                expires_at=body.expires_at,
                created_by=admin.id,
            )
        )
    await write_audit(
        db,
        event_type="redemption.create",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"batch_id": batch_id, "count": body.count, "amount_micro": amount},
        autocommit=False,
    )
    try:
        token = await _store_redemption_plaintext_batch(
            batch_id=batch_id,
            amount_micro=amount,
            codes=plaintext_codes,
            expires_at=body.expires_at,
        )
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        raise _http(
            "download_cache_unavailable",
            "redemption code download cache is unavailable; no codes were created",
            503,
        ) from exc
    try:
        await db.commit()
    except Exception:
        try:
            redis = get_redis()
            await redis.delete(_DOWNLOAD_TOKEN_PREFIX + token)
            await redis.delete(_PLAINTEXT_BATCH_PREFIX + batch_id)
        except Exception:  # noqa: BLE001
            pass
        raise
    response.headers["Cache-Control"] = "no-store"
    return AdminRedemptionCodeCreateOut(
        batch_id=batch_id,
        count=body.count,
        amount=_money(amount),
        download_token=token,
        plaintext_codes=plaintext_codes,
        expires_at=body.expires_at,
    )


@router.get("/admin/redemption_codes/batches/{batch_id}.csv")
async def admin_download_redemption_batch_csv(
    batch_id: str,
    _admin: AdminUser,
    download_token: str = Query(min_length=8),
) -> StreamingResponse:
    key = _DOWNLOAD_TOKEN_PREFIX + download_token
    redis = get_redis()
    data = await redis.get(key)
    if data is None:
        raise _http("download_token_expired", "download token expired", 410)
    text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    _require_redemption_download_batch(text, batch_id)
    return StreamingResponse(
        io.BytesIO(text.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="redemption-{batch_id}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/admin/redemption_codes/batches/{batch_id}.txt")
async def admin_download_redemption_batch_txt(
    batch_id: str,
    _admin: AdminUser,
    download_token: str = Query(min_length=8),
) -> StreamingResponse:
    key = _DOWNLOAD_TOKEN_PREFIX + download_token
    redis = get_redis()
    data = await redis.get(key)
    if data is None:
        raise _http("download_token_expired", "download token expired", 410)
    csv_text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    _require_redemption_download_batch(csv_text, batch_id)
    reader = csv.DictReader(io.StringIO(csv_text))
    codes = [str(row.get("code") or "") for row in reader if row.get("code")]
    text = "\n".join(codes) + ("\n" if codes else "")
    return StreamingResponse(
        io.BytesIO(text.encode("utf-8")),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="redemption-{batch_id}.txt"',
            "Cache-Control": "no-store",
        },
    )


@router.post(
    "/admin/redemption_codes/batches/{batch_id}/redownload",
    response_model=AdminRedemptionBatchRedownloadOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_redownload_redemption_batch(
    batch_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedemptionBatchRedownloadOut:
    payload = await _load_redemption_plaintext_batch(batch_id)
    codes = [str(code) for code in payload["codes"]]
    amount = _rmb_to_micro_or_422(str(payload.get("amount_rmb") or "0"), field="amount_rmb")
    expires_raw = payload.get("expires_at")
    expires_at = datetime.fromisoformat(expires_raw) if isinstance(expires_raw, str) and expires_raw else None
    token = await _store_redemption_plaintext_batch(
        batch_id=batch_id,
        amount_micro=amount,
        codes=codes,
        expires_at=expires_at,
    )
    await write_audit(
        db,
        event_type="redemption.batch.redownload",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"batch_id": batch_id, "count": len(codes)},
        autocommit=False,
    )
    await db.commit()
    return AdminRedemptionBatchRedownloadOut(
        batch_id=batch_id,
        count=len(codes),
        download_token=token,
        plaintext_codes=codes,
        expires_in_seconds=_REDEMPTION_DOWNLOAD_TTL_SECONDS,
    )


@router.post(
    "/admin/redemption_codes/{code_id}:revoke",
    response_model=AdminRedemptionCodeOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_revoke_redemption_code(
    code_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedemptionCodeOut:
    code = await db.get(RedemptionCode, code_id)
    if code is None:
        raise _http("not_found", "redemption code not found", 404)
    if code.revoked_at is not None:
        raise _http("ALREADY_REVOKED", "redemption code was already revoked", 409)
    code.revoked_at = datetime.now(timezone.utc)
    await write_audit(
        db,
        event_type="redemption.revoke",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"code_id": code_id},
        autocommit=False,
    )
    await db.commit()
    await db.refresh(code)
    return _redemption_code_out(code)


@router.post(
    "/admin/redemption_codes/batches/{batch_id}:revoke",
    response_model=AdminRedemptionCodeListOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_revoke_redemption_batch(
    batch_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedemptionCodeListOut:
    now = datetime.now(timezone.utc)
    await db.execute(
        update(RedemptionCode)
        .where(RedemptionCode.batch_id == batch_id, RedemptionCode.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await write_audit(
        db,
        event_type="redemption.batch.revoke",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"batch_id": batch_id},
        autocommit=False,
    )
    await db.commit()
    return await admin_list_redemption_codes(admin, db, status="all", batch_id=batch_id)


@router.get("/admin/wallets", response_model=AdminWalletListOut)
async def admin_list_wallets(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
    mode: str | None = Query(default="wallet"),
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> AdminWalletListOut:
    stmt = select(User, UserWallet).outerjoin(UserWallet, UserWallet.user_id == User.id)
    if mode in {"wallet", "byok"}:
        stmt = stmt.where(User.account_mode == mode)
    if q:
        q_clean = q.strip()
        if len(q_clean) >= 3:
            stmt = stmt.where(or_(User.email.ilike(f"%{q_clean}%"), User.id == q_clean))
        else:
            stmt = stmt.where(User.id == q_clean)
    stmt = _cursor_filter(stmt, User, cursor)
    rows = (
        await db.execute(
            stmt.order_by(User.created_at.desc(), User.id.desc()).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items: list[AdminWalletOut] = []
    threshold = await _low_balance_threshold(db)
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
                    WalletTransaction.kind.in_(("topup_redeem", "adjust_admin", "grant")),
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
                    WalletTransaction.kind.in_(("charge", "settle")),
                    WalletTransaction.amount_micro < 0,
                )
                .group_by(WalletTransaction.user_id)
            )
        ).all():
            if ts is not None:
                last_charges[str(user_id)] = ts
    for user, wallet in rows:
        if user.account_mode == "wallet":
            if wallet is None:
                wallet = UserWallet(user_id=user.id)
            wallet_out = WalletOut(
                mode="wallet",
                balance=_money(wallet.balance_micro),
                hold=_money(wallet.hold_micro),
                low_balance_threshold=_money(threshold),
                frozen=False,
            )
        else:
            # Why: surface frozen residual balance for byok users so admin can
            # see and reconcile balances left by a wallet→byok switch.
            if wallet is not None and (
                wallet.balance_micro > 0 or wallet.hold_micro > 0
            ):
                wallet_out = WalletOut(
                    mode="byok",
                    balance=_money(wallet.balance_micro),
                    hold=_money(wallet.hold_micro),
                    frozen=True,
                )
            else:
                wallet_out = WalletOut(
                    mode="byok", balance=None, hold=None, frozen=False
                )
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
        next_cursor=_next_cursor([user for user, _wallet in rows], has_more),
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
    target = await db.get(User, user_id)
    if target is None:
        raise _http("not_found", "user not found", 404)
    if target.account_mode != "wallet":
        raise _http("ACCOUNT_NOT_WALLET", "target user is not a wallet account", 409)
    amount = _rmb_to_micro_or_422(body.amount_rmb_signed, field="amount_rmb_signed")
    try:
        tx = await billing_core.adjust(
            db,
            user_id,
            amount,
            admin_id=admin.id,
            reason=body.reason,
            allow_negative=await _allow_negative_balance(db),
        )
    except billing_core.BillingError as exc:
        raise _billing_http(exc)
    await write_audit(
        db,
        event_type="wallet.adjust.admin",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        target_user_id=user_id,
        details={"amount_micro": amount, "reason": body.reason, "tx_id": tx.id},
        autocommit=False,
    )
    await db.commit()
    return _tx_out(tx)


@router.get("/admin/wallets/{user_id}", response_model=AdminWalletDetailOut)
async def admin_get_wallet_detail(
    user_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminWalletDetailOut:
    user = await db.get(User, user_id)
    if user is None:
        raise _http("not_found", "user not found", 404)
    wallet_out = await _wallet_out(db, user)
    tx_rows = (
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
                WalletTransaction.kind.in_(("charge", "settle")),
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
        transactions=[_tx_out(tx) for tx in tx_rows],
        redemptions=[
            AdminRedemptionUsageOut(
                id=usage.id,
                code_id=usage.code_id,
                user_id=usage.user_id,
                user_email=email,
                amount=_money(usage.amount_micro),
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
    target = (
        await db.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if target is None:
        raise _http("not_found", "user not found", 404)
    before = target.account_mode
    if before == body.mode:
        return AdminWalletOut(
            user_id=target.id,
            email=target.email,
            account_mode=target.account_mode,  # type: ignore[arg-type]
            wallet=await _wallet_out(db, target),
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
            raise _http(
                "WALLET_HAS_ACTIVE_HOLDS",
                "wallet has active holds; cancel or finish pending tasks first",
                409,
                hold_micro=wallet.hold_micro,
            )
        if body.on_residual_balance == "zero" and wallet.balance_micro > 0:
            try:
                await billing_core.adjust(
                    db,
                    user_id,
                    -wallet.balance_micro,
                    admin_id=admin.id,
                    reason="account mode changed to byok",
                )
            except billing_core.BillingError as exc:
                raise _billing_http(exc)
    target.account_mode = body.mode
    await write_audit(
        db,
        event_type="account.mode_change",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        target_user_id=user_id,
        details={
            "from": before,
            "to": body.mode,
            "on_residual_balance": body.on_residual_balance,
        },
        autocommit=False,
    )
    await db.commit()
    await db.refresh(target)
    return AdminWalletOut(
        user_id=target.id,
        email=target.email,
        account_mode=target.account_mode,  # type: ignore[arg-type]
        wallet=await _wallet_out(db, target),
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
    stmt = select(WalletTransaction).where(WalletTransaction.user_id == user_id)
    if kind:
        stmt = stmt.where(WalletTransaction.kind == kind)
    if ref_type:
        stmt = stmt.where(WalletTransaction.ref_type == ref_type)
    if ref_id:
        stmt = stmt.where(WalletTransaction.ref_id == ref_id)
    stmt = _cursor_filter(stmt, WalletTransaction, cursor)
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
        items=[_tx_out(row) for row in rows],
        next_cursor=_next_cursor(rows, has_more),
    )
