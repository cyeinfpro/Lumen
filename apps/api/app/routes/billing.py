"""Wallet, pricing, and redemption APIs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import uuid
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
    AdminBillingUsageOut,
    AdminOrphanHoldOut,
    AdminPricingBulkIn,
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
    BillingSnapshotOut,
    BillingUsageByKindOut,
    BillingWindowOut,
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
from ..services.billing_cache import BillingCacheService
from ..services.idempotency import cache_json, get_cached_json
from ..services.redemption_secret import (
    PreviousRedemptionSecretLocked,
    previous_redemption_secret,
    remember_previous_redemption_secret,
)


router = APIRouter(tags=["billing"])
_billing_cache_service: BillingCacheService | None = None
_CHARGE_KINDS = ("charge", "charge_completion")

REDEMPTION_LIMITER = RateLimiter(
    capacity=10,
    refill_per_sec=10 / 300,
    always_on=True,
)
_DOWNLOAD_TOKEN_PREFIX = "billing:redemption_csv:"
_PLAINTEXT_BATCH_PREFIX = "billing:redemption_plaintext:"
_REDEMPTION_DOWNLOAD_TTL_SECONDS = 300
_REDEMPTION_IDEMPOTENCY_NAMESPACE = "billing:redemption:idempotency"
_REDEMPTION_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
_REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE = uuid.UUID(
    "cf14d7e7-73ca-4b91-89fa-d4ab765034c9"
)
_BILLING_AUDIT_EVENT_PREFIXES = (
    "wallet.",
    "pricing.",
    "redemption.",
    "account.mode_change",
    "billing.",
)
_BILLING_WINDOWS: dict[str, timedelta] = {
    "5h": timedelta(hours=5),
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
}
_BULK_RATE_UNITS: dict[str, str] = {
    "input": "per_1k_tokens_in",
    "output": "per_1k_tokens_out",
    "cache_read": "per_1k_tokens_cache_read",
    "cache_creation": "per_1k_tokens_cache_creation",
    "cache_creation_5m": "per_1k_tokens_cache_creation_5m",
    "cache_creation_1h": "per_1k_tokens_cache_creation_1h",
    "image_output": "per_1k_tokens_image_output",
    "reasoning": "per_1k_tokens_reasoning",
    "input_priority": "per_1k_tokens_input_priority",
    "output_priority": "per_1k_tokens_output_priority",
    "cache_read_priority": "per_1k_tokens_cache_read_priority",
}


def configure_billing_cache(service: BillingCacheService | None) -> None:
    global _billing_cache_service
    _billing_cache_service = service


def _billing_cache() -> BillingCacheService | None:
    return _billing_cache_service


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


def _escape_like_pattern(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _generate_redemption_secret() -> str:
    return secrets.token_urlsafe(48)


def _billing_http(exc: billing_core.BillingError) -> HTTPException:
    return _http(exc.code, exc.message, exc.status_code)


def _money(amount_micro: int) -> MoneyOut:
    return MoneyOut(**billing_core.money_dict(amount_micro))


def _redemption_request_hash(normalized_code: str) -> str:
    return hashlib.sha256(
        f"redemption-code:{normalized_code}".encode("utf-8")
    ).hexdigest()


def _redemption_idempotency_key(
    request: Request,
    *,
    user_id: str,
    normalized_code: str,
) -> str:
    raw = request.headers.get("Idempotency-Key")
    if raw is None:
        digest = hashlib.sha256(
            f"{user_id}:{normalized_code}".encode("utf-8")
        ).hexdigest()[:32]
        return f"derived:{digest}"
    key = raw.strip()
    if not key:
        raise _http(
            "idempotency_key_invalid",
            "Idempotency-Key must not be blank",
            422,
        )
    if len(key) > 128 or any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        raise _http(
            "idempotency_key_invalid",
            "Idempotency-Key must be 1-128 printable ASCII characters",
            422,
        )
    return f"client:{key}"


def _redemption_usage_id(user_id: str, idempotency_key: str) -> str:
    return str(
        uuid.uuid5(
            _REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE,
            f"{user_id}:{idempotency_key}",
        )
    )


def _redemption_idempotency_cache_key(user_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{user_id}:{idempotency_key}".encode("utf-8")).hexdigest()


async def _lock_redemption_idempotency_key(
    db: AsyncSession, user_id: str, idempotency_key: str
) -> None:
    connection = getattr(db, "connection", None)
    if connection is None:
        return
    bind = await connection()
    if bind.dialect.name != "postgresql":
        return
    lock_key = f"redemption:{user_id}:{idempotency_key}"
    lock_id = int.from_bytes(
        hashlib.sha256(lock_key.encode("utf-8")).digest()[:8],
        "big",
        signed=True,
    )
    await db.execute(select(func.pg_advisory_xact_lock(lock_id)))


async def _cached_redemption_out(
    user_id: str,
    idempotency_key: str,
    request_hash: str,
) -> RedemptionOut | None:
    cached = await get_cached_json(
        _REDEMPTION_IDEMPOTENCY_NAMESPACE,
        _redemption_idempotency_cache_key(user_id, idempotency_key),
    )
    if cached is None:
        return None
    if cached.get("request_hash") != request_hash:
        raise _http(
            "idempotency_conflict",
            "Idempotency-Key was already used for a different redemption request",
            409,
        )
    response = cached.get("response")
    if not isinstance(response, dict):
        return None
    return RedemptionOut.model_validate(response)


async def _cache_redemption_out(
    user_id: str,
    idempotency_key: str,
    request_hash: str,
    response: RedemptionOut,
) -> None:
    await cache_json(
        _REDEMPTION_IDEMPOTENCY_NAMESPACE,
        _redemption_idempotency_cache_key(user_id, idempotency_key),
        {"request_hash": request_hash, "response": response},
        _REDEMPTION_IDEMPOTENCY_TTL_SECONDS,
    )


async def _redemption_out_for_usage(
    db: AsyncSession,
    *,
    user_id: str,
    usage_id: str,
    request_hash: str,
) -> RedemptionOut | None:
    row = (
        await db.execute(
            select(RedemptionCodeUsage, WalletTransaction)
            .join(
                WalletTransaction,
                WalletTransaction.id == RedemptionCodeUsage.wallet_tx_id,
            )
            .where(
                RedemptionCodeUsage.id == usage_id,
                RedemptionCodeUsage.user_id == user_id,
            )
        )
    ).first()
    if row is None:
        return None
    usage, tx = row
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    if meta.get("redemption_request_hash") != request_hash:
        raise _http(
            "idempotency_conflict",
            "Idempotency-Key was already used for a different redemption request",
            409,
        )
    return RedemptionOut(
        amount=_money(usage.amount_micro),
        balance=_money(tx.balance_after),
    )


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


async def _redemption_secrets(db: AsyncSession) -> list[str]:
    current = await _redemption_secret(db)
    secrets = [current]
    previous = await previous_redemption_secret(db)
    if previous and previous != current:
        secrets.append(previous)
    return secrets


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


def _redemption_status(code: RedemptionCode, *, now: datetime | None = None) -> str:
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


def _next_cursor(
    rows: list[Any], has_more: bool, attr: str = "created_at"
) -> str | None:
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
        raise _http(
            "download_token_batch_mismatch", "download token does not match batch", 404
        )


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
        raise _http(
            "redemption_plaintext_corrupt", "plaintext cache is invalid", 500
        ) from exc
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
        (
            await db.execute(
                select(PricingRule.key).where(
                    PricingRule.scope == "image_size",
                    PricingRule.unit == "per_image",
                    PricingRule.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
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
        (
            await db.execute(
                select(PricingRule.key).where(
                    PricingRule.scope == "image_size",
                    PricingRule.unit == "per_image",
                    PricingRule.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    enabled = {str(row) for row in rows}
    for item in candidate_items or []:
        if item.get("scope") == "image_size" and item.get("unit") == "per_image":
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


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _meta_int(mapping: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(mapping.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _scaled_meta_cost(mapping: dict[str, Any], key: str) -> int:
    value = _meta_int(mapping, key)
    multiplier = _meta_int(mapping, "rate_multiplier_x10000") or 10_000
    return (value * multiplier) // 10_000


def _usage_by_kind(rows: list[WalletTransaction]) -> BillingUsageByKindOut:
    totals = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "image": 0,
        "reasoning": 0,
    }
    for row in rows:
        meta = row.meta or {}
        breakdown = meta.get("cost_breakdown")
        if isinstance(breakdown, dict):
            totals["input"] += _scaled_meta_cost(breakdown, "input_cost_micro")
            totals["output"] += _scaled_meta_cost(breakdown, "output_cost_micro")
            totals["cache_read"] += _scaled_meta_cost(
                breakdown, "cache_read_cost_micro"
            )
            totals["cache_creation"] += _scaled_meta_cost(
                breakdown, "cache_creation_cost_micro"
            )
            totals["image"] += _scaled_meta_cost(breakdown, "image_output_cost_micro")
            totals["reasoning"] += _scaled_meta_cost(breakdown, "reasoning_cost_micro")
            continue

        if row.ref_type == "generation" or row.kind == "settle":
            totals["image"] += _meta_int(meta, "actual_micro") or abs(
                int(row.amount_micro)
            )
            continue

        if row.kind.startswith("charge") or row.kind == "charge":
            totals["output"] += _meta_int(meta, "cost_micro") or abs(
                int(row.amount_micro)
            )
    return BillingUsageByKindOut(**totals)


def _usage_total(usage: BillingUsageByKindOut) -> int:
    return (
        usage.input
        + usage.output
        + usage.cache_read
        + usage.cache_creation
        + usage.image
        + usage.reasoning
    )


def _window_usage(
    rows: list[WalletTransaction],
    *,
    now: datetime,
    span: timedelta,
    limit_micro: int,
) -> BillingWindowOut:
    cutoff = now - span
    in_window = [row for row in rows if _aware_utc(row.created_at) >= cutoff]
    usage = _usage_by_kind(in_window)
    oldest = min((_aware_utc(row.created_at) for row in in_window), default=None)
    return BillingWindowOut(
        used_micro=_usage_total(usage),
        limit_micro=max(0, int(limit_micro or 0)),
        resets_at=(oldest + span) if oldest is not None else None,
    )


async def _active_credential_limits(db: AsyncSession, user_id: str) -> dict[str, int]:
    row = (
        await db.execute(
            select(
                UserApiCredential.limit_5h_micro,
                UserApiCredential.limit_1d_micro,
                UserApiCredential.limit_7d_micro,
            )
            .where(
                UserApiCredential.user_id == user_id,
                UserApiCredential.status == "active",
                UserApiCredential.deleted_at.is_(None),
            )
            .order_by(UserApiCredential.updated_at.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return {"5h": 0, "1d": 0, "7d": 0}
    return {
        "5h": int(row[0] or 0),
        "1d": int(row[1] or 0),
        "7d": int(row[2] or 0),
    }


async def _billing_rows_for_range(
    db: AsyncSession,
    user_id: str,
    *,
    range_start: datetime,
    range_end: datetime,
) -> list[WalletTransaction]:
    return (
        (
            await db.execute(
                select(WalletTransaction)
                .where(
                    WalletTransaction.user_id == user_id,
                    WalletTransaction.created_at >= range_start,
                    WalletTransaction.created_at <= range_end,
                    WalletTransaction.kind.in_((*_CHARGE_KINDS, "settle")),
                )
                .order_by(WalletTransaction.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


async def _billing_balance_micro(db: AsyncSession, user_id: str) -> int:
    service = _billing_cache()
    if service is not None:
        return await service.get_balance(db, user_id)
    use_cache = billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.use_redis_cache"), True
    )
    redis = None
    if use_cache:
        try:
            redis = get_redis()
        except Exception:  # noqa: BLE001
            redis = None
    return await BillingCacheService(redis=redis).get_balance(db, user_id)


async def _invalidate_balance_cache(user_id: str) -> None:
    service = _billing_cache()
    if service is None:
        return
    await service.invalidate(user_id)


async def _billing_snapshot_parts(
    db: AsyncSession,
    user_id: str,
) -> tuple[
    int,
    str,
    datetime,
    datetime,
    dict[str, BillingWindowOut],
    BillingUsageByKindOut,
    int,
]:
    now = datetime.now(timezone.utc)
    range_start = now - timedelta(days=30)
    user_row = (
        await db.execute(select(User.billing_rate_multiplier).where(User.id == user_id))
    ).scalar_one_or_none()
    try:
        multiplier = f"{Decimal(str(user_row if user_row is not None else 1)).quantize(Decimal('0.0001'))}"
    except InvalidOperation:
        multiplier = "1.0000"
    balance = await _billing_balance_micro(db, user_id)
    rows = await _billing_rows_for_range(
        db, user_id, range_start=range_start, range_end=now
    )
    limits = await _active_credential_limits(db, user_id)
    windows = {
        key: _window_usage(rows, now=now, span=span, limit_micro=limits.get(key, 0))
        for key, span in _BILLING_WINDOWS.items()
    }
    by_kind = _usage_by_kind(rows)
    return balance, multiplier, range_start, now, windows, by_kind, len(rows)


def _bulk_numeric_micro(value: str | int | float | None, *, field: str) -> int | None:
    if value is None or value == "":
        return None
    micro = _rmb_to_micro_or_422(value, field=field)
    if micro < 0:
        raise _http("invalid_amount", f"{field}: price must be non-negative", 422)
    return micro


def _bulk_multiplier_x10000(value: float | None, *, field: str) -> int | None:
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except InvalidOperation as exc:
        raise _http("invalid_amount", f"{field}: multiplier is invalid", 422) from exc
    if not dec.is_finite() or dec < 0:
        raise _http("invalid_amount", f"{field}: multiplier must be non-negative", 422)
    return int((dec * Decimal(10_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def _invalidate_pricing_cache(model: str, variant: str) -> None:
    try:
        redis = get_redis()
        await redis.delete(
            f"lumen:pricing:v1:{variant}:{model}",
            f"lumen:pricing:v1:default:{model}",
        )
    except Exception:  # noqa: BLE001
        return


@router.get("/me/wallet", response_model=WalletOut)
async def get_my_wallet(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WalletOut:
    return await _wallet_out(db, user)


@router.get("/me/billing/snapshot", response_model=BillingSnapshotOut)
async def get_my_billing_snapshot(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BillingSnapshotOut:
    (
        balance,
        multiplier,
        _start,
        _end,
        windows,
        by_kind,
        _count,
    ) = await _billing_snapshot_parts(db, user.id)
    return BillingSnapshotOut(
        balance_micro=balance,
        billing_rate_multiplier=multiplier,
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
    _require_wallet_user(user)
    stmt = (
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user.id)
        .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
        .limit(limit + 1)
    )
    if kind:
        if kind == "charge":
            stmt = stmt.where(WalletTransaction.kind.in_(_CHARGE_KINDS))
        else:
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
                stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(
                    limit
                )
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
    secret_configured = bool(
        (await _setting_raw(db, "billing.redemption_code_secret") or "").strip()
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
                select(
                    func.coalesce(func.sum(WalletTransaction.amount_micro), 0)
                ).where(
                    WalletTransaction.kind.in_((*_CHARGE_KINDS, "settle")),
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


@router.get("/admin/billing/usage/{user_id}", response_model=AdminBillingUsageOut)
async def admin_billing_usage(
    user_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminBillingUsageOut:
    exists = (
        await db.execute(
            select(User.id).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if exists is None:
        raise _http("not_found", "user not found", 404)
    (
        balance,
        multiplier,
        range_start,
        range_end,
        windows,
        by_kind,
        count,
    ) = await _billing_snapshot_parts(db, user_id)
    return AdminBillingUsageOut(
        user_id=user_id,
        balance_micro=balance,
        billing_rate_multiplier=multiplier,
        range_start=range_start,
        range_end=range_end,
        windows=windows,
        by_kind_30d=by_kind,
        total_micro=_usage_total(by_kind),
        transaction_count=count,
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
        raise _http(
            "HOLD_ALREADY_CONSUMED", "hold was already settled or released", 409
        )
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
    await _invalidate_balance_cache(hold.user_id)
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
    provided_redemption_secret = (body.redemption_code_secret or "").strip()
    secret_generated = not provided_redemption_secret
    redemption_secret = provided_redemption_secret or _generate_redemption_secret()
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
    await write_audit(
        db,
        event_type="billing.bootstrap",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "tiers": sorted(body.image_size_thresholds),
            "enabled": body.enabled,
            "redemption_secret_generated": secret_generated,
        },
        autocommit=False,
    )
    await db.commit()
    return await admin_billing_overview(admin, db)


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
    secret_spec = get_spec("billing.redemption_code_secret")
    if secret_spec is None:
        raise _http("invalid_request", "redemption secret setting is unsupported", 500)
    old_secret = await get_setting(db, secret_spec)
    new_secret = _generate_redemption_secret()
    await update_settings(db, [("billing.redemption_code_secret", new_secret)])

    transition_expires_at = None
    if old_secret:
        try:
            transition_expires_at = await remember_previous_redemption_secret(
                db, old_secret
            )
        except PreviousRedemptionSecretLocked as exc:
            raise _http(
                "previous_secret_locked",
                "another rotation is still inside the 24h transition window",
                409,
            ) from exc

    secret_hash8 = hashlib.sha256(new_secret.encode("utf-8")).hexdigest()[:8]
    await write_audit(
        db,
        event_type="billing.secret.rotate"
        if old_secret
        else "billing.secret.configure",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "secret_hash8": secret_hash8,
            "previous_secret_valid_until": transition_expires_at,
            "revoked_unredeemed_count": 0,
            "generated_by": "system",
        },
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
    normalized_code = billing_core.normalize_redemption_code(body.code)
    if len(normalized_code) < 4:
        raise _http("invalid_code", "redemption code is invalid", 422)
    request_hash = _redemption_request_hash(normalized_code)
    idempotency_key = _redemption_idempotency_key(
        request,
        user_id=user.id,
        normalized_code=normalized_code,
    )
    cached = await _cached_redemption_out(user.id, idempotency_key, request_hash)
    if cached is not None:
        return cached
    await _lock_redemption_idempotency_key(db, user.id, idempotency_key)
    cached = await _cached_redemption_out(user.id, idempotency_key, request_hash)
    if cached is not None:
        return cached
    usage_id = _redemption_usage_id(user.id, idempotency_key)
    existing = await _redemption_out_for_usage(
        db,
        user_id=user.id,
        usage_id=usage_id,
        request_hash=request_hash,
    )
    if existing is not None:
        await _cache_redemption_out(user.id, idempotency_key, request_hash, existing)
        return existing

    await _require_redemption_operational(db)
    ip = client_ip(request)
    redis = get_redis()
    await REDEMPTION_LIMITER.check(redis, f"rl:redemption:user:{user.id}")
    await REDEMPTION_LIMITER.check(redis, f"rl:redemption:ip:{ip}")
    code_hashes = [
        billing_core.hash_redemption_code(normalized_code, secret)
        for secret in await _redemption_secrets(db)
    ]
    now = datetime.now(timezone.utc)

    matching_codes = (
        (
            await db.execute(
                select(RedemptionCode)
                .where(RedemptionCode.code_hash.in_(code_hashes))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    codes_by_hash = {item.code_hash: item for item in matching_codes}
    code = next((codes_by_hash.get(code_hash) for code_hash in code_hashes), None)
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

    try:
        tx = await billing_core.topup_redeem(
            db,
            user.id,
            code.amount_micro,
            usage_id=usage_id,
            code_id=code.id,
            meta={
                "client_idempotency_hash": hashlib.sha256(
                    idempotency_key.encode("utf-8")
                ).hexdigest()[:16],
                "redemption_request_hash": request_hash,
            },
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
        await _invalidate_balance_cache(user.id)
    except IntegrityError as exc:
        await db.rollback()
        # Only treat the per-user-redeem unique constraint as CODE_ALREADY_USED.
        # Other constraint violations (FK, wallet_tx idempotency, etc.) bubble
        # up as 500 so misattribution doesn't mask real bugs.
        diag = str(getattr(exc.orig, "diag", None) or "")
        msg = f"{exc!s} {diag}".lower()
        if "uq_redeem_code_user" in msg:
            existing = await _redemption_out_for_usage(
                db,
                user_id=user.id,
                usage_id=usage_id,
                request_hash=request_hash,
            )
            if existing is not None:
                await _cache_redemption_out(
                    user.id, idempotency_key, request_hash, existing
                )
                return existing
            raise _http(
                "CODE_ALREADY_USED",
                "this code was already used by this user",
                409,
            ) from exc
        raise

    response = RedemptionOut(
        amount=_money(code.amount_micro), balance=_money(tx.balance_after)
    )
    await _cache_redemption_out(user.id, idempotency_key, request_hash, response)
    return response


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


@router.get("/admin/billing/pricing", response_model=PricingRulesOut)
async def admin_list_billing_pricing(
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    return await admin_list_pricing(admin, db)


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
    invalidated: set[tuple[str, str]] = set()
    for value in values:
        if value["scope"] == "chat_model":
            invalidated.add((str(value["key"]), str(value["variant"])))
    for model, variant in invalidated:
        await _invalidate_pricing_cache(model, variant)
    return await admin_list_pricing(admin, db)


@router.post(
    "/admin/billing/pricing/bulk",
    response_model=PricingRulesOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_bulk_pricing(
    body: AdminPricingBulkIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    now = datetime.now(timezone.utc)
    model = body.model.strip()
    variant = (body.channel or "default").strip() or "default"
    rates = body.rates.model_dump()
    values: list[dict[str, Any]] = []
    for field, unit in _BULK_RATE_UNITS.items():
        micro = _bulk_numeric_micro(rates.get(field), field=f"rates.{field}")
        if micro is None:
            continue
        values.append(
            {
                "id": new_uuid7(),
                "scope": "chat_model",
                "key": model,
                "variant": variant,
                "unit": unit,
                "price_micro": micro,
                "enabled": body.enabled,
                "note": body.note,
                "updated_at": now,
            }
        )
    if body.rates.long_context_threshold is not None:
        threshold = int(body.rates.long_context_threshold)
        if threshold < 0:
            raise _http(
                "invalid_amount",
                "rates.long_context_threshold: threshold must be non-negative",
                422,
            )
        values.append(
            {
                "id": new_uuid7(),
                "scope": "chat_model",
                "key": model,
                "variant": variant,
                "unit": "long_context_threshold",
                "price_micro": threshold,
                "enabled": body.enabled,
                "note": body.note,
                "updated_at": now,
            }
        )
    for field, unit in (
        ("long_context_input_multiplier", "long_context_input_multiplier"),
        ("long_context_output_multiplier", "long_context_output_multiplier"),
    ):
        multiplier = _bulk_multiplier_x10000(rates.get(field), field=f"rates.{field}")
        if multiplier is None:
            continue
        values.append(
            {
                "id": new_uuid7(),
                "scope": "chat_model",
                "key": model,
                "variant": variant,
                "unit": unit,
                "price_micro": multiplier,
                "enabled": body.enabled,
                "note": body.note,
                "updated_at": now,
            }
        )
    if not values:
        raise _http("invalid_request", "at least one pricing rate is required", 422)

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
    await write_audit(
        db,
        event_type="pricing.bulk_update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "model": model,
            "channel": None if variant == "default" else variant,
            "count": len(values),
            "units": [value["unit"] for value in values],
        },
        autocommit=False,
    )
    await db.commit()
    await _invalidate_pricing_cache(model, variant)
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
    csv_text = (
        data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    )
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
    amount = _rmb_to_micro_or_422(
        str(payload.get("amount_rmb") or "0"), field="amount_rmb"
    )
    expires_raw = payload.get("expires_at")
    expires_at = (
        datetime.fromisoformat(expires_raw)
        if isinstance(expires_raw, str) and expires_raw
        else None
    )
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
        q_clean = q.strip()[:200]
        if q_clean:
            pattern = f"%{_escape_like_pattern(q_clean)}%"
            stmt = stmt.where(
                or_(
                    User.email.ilike(pattern, escape="\\"),
                    User.id.ilike(pattern, escape="\\"),
                )
            )
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
    await _invalidate_balance_cache(user_id)
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
    await _invalidate_balance_cache(user_id)
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
        if kind == "charge":
            stmt = stmt.where(WalletTransaction.kind.in_(_CHARGE_KINDS))
        else:
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
