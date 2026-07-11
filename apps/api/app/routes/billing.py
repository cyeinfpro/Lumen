"""Wallet, pricing, and redemption APIs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import (
    AuditLog,
    PricingRule,
    RedemptionBatch,
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
from ..billing_cache_state import (
    billing_cache as _shared_billing_cache,
    configure_billing_cache as _configure_shared_billing_cache,
    invalidate_balance_cache as _invalidate_shared_balance_cache,
)
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
from ..services.billing.errors import _http as _http
from ..services.billing.pricing_values import (
    _ZERO_PRICE_ALLOWED_UNITS as _ZERO_PRICE_ALLOWED_UNITS,
    _bulk_multiplier_x10000 as _bulk_multiplier_x10000,
    _bulk_numeric_micro as _bulk_numeric_micro,
    _openai_price_micro as _openai_price_micro,
    _parse_price_rows as _parse_price_rows,
    _pricing_group_priorities as _pricing_group_priorities,
    _rmb_to_micro_or_422 as _rmb_to_micro_or_422,
    _validate_enabled_pricing_value as _validate_enabled_pricing_value,
)
from ..services.billing.redemption_values import (
    _DOWNLOAD_TOKEN_PREFIX as _DOWNLOAD_TOKEN_PREFIX,
    _PLAINTEXT_BATCH_PREFIX as _PLAINTEXT_BATCH_PREFIX,
    _REDEMPTION_ALREADY_USED_CONSTRAINT as _REDEMPTION_ALREADY_USED_CONSTRAINT,
    _REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT as _REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT,
    _REDEMPTION_DOWNLOAD_TTL_SECONDS as _REDEMPTION_DOWNLOAD_TTL_SECONDS,
    _REDEMPTION_IDEMPOTENCY_NAMESPACE as _REDEMPTION_IDEMPOTENCY_NAMESPACE,
    _REDEMPTION_IDEMPOTENCY_TTL_SECONDS as _REDEMPTION_IDEMPOTENCY_TTL_SECONDS,
    _REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE as _REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE,
    _REDEMPTION_KNOWN_CONSTRAINTS as _REDEMPTION_KNOWN_CONSTRAINTS,
    _REDEMPTION_REPLAY_CONSTRAINTS as _REDEMPTION_REPLAY_CONSTRAINTS,
    _client_idempotency_key as _client_idempotency_key,
    _integrity_constraint_name as _integrity_constraint_name,
    _redemption_batch_idempotency_key as _redemption_batch_idempotency_key,
    _redemption_batch_lock_identity as _redemption_batch_lock_identity,
    _redemption_batch_payload_matches as _redemption_batch_payload_matches,
    _redemption_batch_request_hash as _redemption_batch_request_hash,
    _redemption_csv_batch_id as _redemption_csv_batch_id,
    _redemption_csv_payload as _redemption_csv_payload,
    _redemption_idempotency_cache_key as _redemption_idempotency_cache_key,
    _redemption_idempotency_key as _redemption_idempotency_key,
    _redemption_plaintext_payload as _redemption_plaintext_payload,
    _redemption_request_hash as _redemption_request_hash,
    _redemption_status as _redemption_status,
    _redemption_usage_id as _redemption_usage_id,
    _require_redemption_download_batch as _require_redemption_download_batch,
)
from ..services.billing.usage import (
    _CHARGE_KINDS as _CHARGE_KINDS,
    _meta_int as _meta_int,
    _scaled_meta_cost as _scaled_meta_cost,
    _usage_by_kind as _usage_by_kind,
    _usage_total as _usage_total,
)
from ..services.billing_cache import BillingCacheService
from ..services.idempotency import cache_json, get_cached_json
from ..services.pricing_cache import (
    invalidate_pricing_cache as _invalidate_pricing_cache,
)
from ..services.redemption_secret import (
    PreviousRedemptionSecretLocked,
    previous_redemption_secret,
    remember_previous_redemption_secret,
)


router = APIRouter(tags=["billing"])
logger = logging.getLogger(__name__)

REDEMPTION_LIMITER = RateLimiter(
    capacity=10,
    refill_per_sec=10 / 300,
    always_on=True,
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
MAX_ADMIN_ADJUST_MICRO = 1_000_000 * billing_core.MICRO_RMB
MAX_ADMIN_NEGATIVE_BALANCE_MICRO = 100_000 * billing_core.MICRO_RMB
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
    _configure_shared_billing_cache(service)


def _billing_cache() -> BillingCacheService | None:
    return _shared_billing_cache()


def _escape_like_pattern(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _generate_redemption_secret() -> str:
    return secrets.token_urlsafe(48)


def _billing_http(exc: billing_core.BillingError) -> HTTPException:
    return _http(exc.code, exc.message, exc.status_code)


def _money(amount_micro: int) -> MoneyOut:
    return MoneyOut(**billing_core.money_dict(amount_micro))


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


async def _lock_redemption_batch_idempotency_key(
    db: AsyncSession,
    admin_id: str,
    idempotency_key: str,
) -> None:
    connection = getattr(db, "connection", None)
    if connection is None:
        return
    bind = await connection()
    if bind.dialect.name != "postgresql":
        return
    lock_key = f"redemption-batch:{admin_id}:{idempotency_key}"
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
        {"request_hash": request_hash, "response": response.model_dump(mode="json")},
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
        priority=rule.priority,
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
    if wallet is None:
        raise _http("wallet_unavailable", "wallet is unavailable", 500)
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
    rows: Sequence[Any], has_more: bool, attr: str = "created_at"
) -> str | None:
    if not has_more or not rows:
        return None
    last = rows[-1]
    ts = getattr(last, attr)
    return f"{ts.isoformat()}|{last.id}"


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


async def _redemption_batch_for_idempotency(
    db: AsyncSession,
    *,
    admin_id: str,
    idempotency_key: str,
    request_hash: str | None = None,
    created_after: datetime | None = None,
) -> RedemptionBatch | None:
    idempotency_match = RedemptionBatch.idempotency_key == idempotency_key
    if (
        idempotency_key.startswith("derived:")
        and request_hash is not None
        and created_after is not None
    ):
        idempotency_match = or_(
            idempotency_match,
            (
                (RedemptionBatch.request_hash == request_hash)
                & (RedemptionBatch.created_at >= created_after)
            ),
        )
    return (
        await db.execute(
            select(RedemptionBatch)
            .where(
                RedemptionBatch.created_by == admin_id,
                idempotency_match,
            )
            .order_by(RedemptionBatch.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _replay_redemption_batch(
    batch: RedemptionBatch,
    *,
    request_hash: str,
    idempotency_key: str,
    response: Response,
) -> AdminRedemptionCodeCreateOut:
    if batch.request_hash != request_hash:
        raise _http(
            "idempotency_conflict",
            "Idempotency-Key was already used for a different redemption batch",
            409,
            batch_id=batch.id,
        )
    try:
        payload = await _load_redemption_plaintext_batch(batch.id)
    except HTTPException as exc:
        error = exc.detail.get("error") if isinstance(exc.detail, dict) else None
        if (
            isinstance(error, dict)
            and error.get("code") == "redemption_plaintext_expired"
        ):
            raise _http(
                "idempotency_replay_unavailable",
                "the redemption batch already exists, but its plaintext window expired",
                409,
                batch_id=batch.id,
            ) from exc
        raise
    if not _redemption_batch_payload_matches(batch, payload):
        raise _http(
            "redemption_plaintext_corrupt",
            "plaintext cache does not match the persisted redemption batch",
            500,
            batch_id=batch.id,
        )
    codes = [str(code) for code in payload["codes"]]
    try:
        token = await _store_redemption_plaintext_batch(
            batch_id=batch.id,
            amount_micro=batch.amount_micro,
            codes=codes,
            expires_at=batch.expires_at,
        )
    except Exception as exc:  # noqa: BLE001
        raise _http(
            "download_cache_unavailable",
            "the redemption batch exists, but its download cache is unavailable",
            503,
            batch_id=batch.id,
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotency-Key"] = batch.idempotency_key
    return AdminRedemptionCodeCreateOut(
        batch_id=batch.id,
        count=batch.code_count,
        amount=_money(batch.amount_micro),
        download_token=token,
        plaintext_codes=codes,
        expires_at=batch.expires_at,
    )


def _billing_audit_predicate() -> Any:
    return or_(
        *[
            AuditLog.event_type.like(f"{prefix}%")
            if prefix.endswith(".")
            else AuditLog.event_type == prefix
            for prefix in _BILLING_AUDIT_EVENT_PREFIXES
        ]
    )


def _wallet_audit_ledger(user_id: str | None = None) -> Any:
    stmt = select(
        WalletTransaction.id.label("tx_id"),
        WalletTransaction.user_id.label("user_id"),
        WalletTransaction.kind.label("kind"),
        WalletTransaction.balance_after.label("balance_after"),
        WalletTransaction.created_at.label("created_at"),
        func.sum(WalletTransaction.amount_micro)
        .over(
            partition_by=WalletTransaction.user_id,
            order_by=(
                WalletTransaction.created_at.asc(),
                WalletTransaction.id.asc(),
            ),
            rows=(None, 0),
        )
        .label("running_balance"),
    )
    if user_id:
        stmt = stmt.where(WalletTransaction.user_id == user_id)
    return stmt.cte("wallet_audit_ledger")


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


async def _active_credential_window_config(
    db: AsyncSession,
    user_id: str,
) -> tuple[str | None, dict[str, int]]:
    row = (
        await db.execute(
            select(
                UserApiCredential.id,
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
        return None, {"5h": 0, "1d": 0, "7d": 0}
    return str(row[0]), {
        "5h": int(row[1] or 0),
        "1d": int(row[2] or 0),
        "7d": int(row[3] or 0),
    }


async def _credential_windows(
    db: AsyncSession,
    *,
    user_id: str,
    credential_id: str | None,
    limits: dict[str, int],
    now: datetime,
) -> dict[str, BillingWindowOut]:
    if credential_id is None:
        return {
            key: BillingWindowOut(
                used_micro=0,
                limit_micro=max(0, int(limits.get(key, 0) or 0)),
                resets_at=None,
            )
            for key in _BILLING_WINDOWS
        }
    service = BillingCacheService(redis=None)
    windows: dict[str, BillingWindowOut] = {}
    for key in _BILLING_WINDOWS:
        usage = await service.ledger_window_usage(
            db,
            credential_id,
            key,
            limit_micro=limits.get(key, 0),
            now=now,
            user_id=user_id,
        )
        windows[key] = BillingWindowOut(
            used_micro=usage.used_micro,
            limit_micro=usage.limit_micro,
            resets_at=usage.resets_at,
        )
    return windows


async def _billing_rows_for_range(
    db: AsyncSession,
    user_id: str,
    *,
    range_start: datetime,
    range_end: datetime,
) -> list[WalletTransaction]:
    return list(
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
    use_cache = billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.use_redis_cache"), True
    )
    service = _billing_cache() if use_cache else None
    if service is not None:
        return await service.get_balance(db, user_id)
    redis = None
    if use_cache:
        try:
            redis = get_redis()
        except Exception:  # noqa: BLE001
            redis = None
    return await BillingCacheService(redis=redis).get_balance(db, user_id)


async def _invalidate_balance_cache(user_id: str) -> None:
    await _invalidate_shared_balance_cache(user_id)


async def _billing_snapshot_parts(
    db: AsyncSession,
    user_id: str,
) -> tuple[
    int,
    str,
    str | None,
    datetime,
    datetime,
    dict[str, BillingWindowOut],
    BillingUsageByKindOut,
    int,
]:
    now = datetime.now(timezone.utc)
    range_start = now - timedelta(days=30)
    user_row = (
        await db.execute(
            select(User.account_mode, User.billing_rate_multiplier).where(
                User.id == user_id
            )
        )
    ).one_or_none()
    account_mode = str(user_row[0]) if user_row is not None else "wallet"
    multiplier_raw = user_row[1] if user_row is not None else 1
    try:
        multiplier = f"{Decimal(str(multiplier_raw)).quantize(Decimal('0.0001'))}"
    except InvalidOperation:
        multiplier = "1.0000"
    balance = await _billing_balance_micro(db, user_id)
    rows = await _billing_rows_for_range(
        db, user_id, range_start=range_start, range_end=now
    )
    credential_id, limits = (
        await _active_credential_window_config(db, user_id)
        if account_mode == "byok"
        else (None, {"5h": 0, "1d": 0, "7d": 0})
    )
    windows = await _credential_windows(
        db,
        user_id=user_id,
        credential_id=credential_id,
        limits=limits,
        now=now,
    )
    by_kind = _usage_by_kind(rows)
    return (
        balance,
        multiplier,
        credential_id,
        range_start,
        now,
        windows,
        by_kind,
        len(rows),
    )


async def _align_pricing_group_priorities(
    db: AsyncSession,
    values: list[dict[str, Any]],
    *,
    now: datetime,
) -> None:
    priorities = _pricing_group_priorities(values)
    await db.flush()
    for (scope, key, variant), priority in priorities.items():
        await db.execute(
            update(PricingRule)
            .where(
                PricingRule.scope == scope,
                PricingRule.key == key,
                PricingRule.variant == variant,
            )
            .values(priority=priority, updated_at=now)
        )


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
        credential_id,
        _start,
        _end,
        windows,
        by_kind,
        _count,
    ) = await _billing_snapshot_parts(db, user.id)
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
        credential_id,
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
        credential_id=credential_id,
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
    ledger = _wallet_audit_ledger(user_id)
    mismatch = ledger.c.running_balance != ledger.c.balance_after
    stats = (
        await db.execute(
            select(
                func.count(ledger.c.tx_id),
                func.count(func.distinct(ledger.c.user_id)),
                func.coalesce(
                    func.sum(case((mismatch, 1), else_=0)),
                    0,
                ),
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
    transaction_count = int(stats[0] or 0)
    user_count = int(stats[1] or 0)
    mismatch_count = int(stats[2] or 0)
    return AdminWalletAuditOut(
        ok=mismatch_count == 0,
        transactions=transaction_count,
        users=user_count,
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
    if low_balance_micro < 0:
        raise _http(
            "invalid_amount",
            "low_balance_warn_rmb: amount must be non-negative",
            422,
        )
    pricing_items: list[dict[str, Any]] = []
    for tier, threshold in body.image_size_thresholds.items():
        if threshold < 0:
            raise _http("invalid_request", "thresholds must be non-negative", 422)
        price_rmb = body.image_prices_rmb.get(tier)
        if price_rmb is None:
            raise _http(
                "invalid_request",
                f"image_prices_rmb.{tier}: enabled tier price is required",
                422,
            )
        price_micro = _rmb_to_micro_or_422(price_rmb, field=f"image_prices_rmb.{tier}")
        _validate_enabled_pricing_value(
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
                # PostgreSQL's row lock serializes the redeemed_count
                # check/increment across different users for the same code.
                # Same-request replay races are handled by unique constraints
                # in the IntegrityError path below.
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
        constraint_name = _integrity_constraint_name(exc)
        if constraint_name in _REDEMPTION_REPLAY_CONSTRAINTS:
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
        # Only treat the per-user-redeem unique constraint as CODE_ALREADY_USED.
        # Other constraint violations (FK, wallet_tx without a completed usage
        # row, etc.) bubble as 500 so misattribution doesn't mask real bugs.
        if constraint_name == _REDEMPTION_ALREADY_USED_CONSTRAINT:
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
                    PricingRule.scope,
                    PricingRule.variant,
                    PricingRule.priority.desc(),
                    PricingRule.key,
                    PricingRule.unit,
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
    values: list[dict[str, Any]] = []
    for item in body.items:
        price = _rmb_to_micro_or_422(item.price_rmb, field="price_rmb")
        if price < 0:
            raise _http("invalid_amount", "price must be non-negative", 422)
        _validate_enabled_pricing_value(
            unit=item.unit,
            price_micro=price,
            enabled=item.enabled,
            field="price_rmb",
        )
        values.append(
            {
                "id": new_uuid7(),
                "scope": item.scope,
                "key": item.key,
                "variant": item.variant,
                "unit": item.unit,
                "price_micro": price,
                "priority": item.priority,
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
                    "priority": insert_stmt.excluded.priority,
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
                existing.priority = value["priority"]
                existing.enabled = value["enabled"]
                existing.note = value["note"]
                existing.updated_at = now
    await _align_pricing_group_priorities(db, values, now=now)
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
                "priority": body.priority,
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
                "priority": body.priority,
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
                "priority": body.priority,
                "enabled": body.enabled,
                "note": body.note,
                "updated_at": now,
            }
        )
    if not values:
        raise _http("invalid_request", "at least one pricing rate is required", 422)
    for value in values:
        _validate_enabled_pricing_value(
            unit=str(value["unit"]),
            price_micro=int(value["price_micro"]),
            enabled=bool(value["enabled"]),
            field=f"rates.{value['unit']}",
        )

    bind = await db.connection()
    if bind.dialect.name == "postgresql":
        insert_stmt = pg_insert(PricingRule).values(values)
        await db.execute(
            insert_stmt.on_conflict_do_update(
                constraint="uq_pricing_scope_key_variant_unit",
                set_={
                    "price_micro": insert_stmt.excluded.price_micro,
                    "priority": insert_stmt.excluded.priority,
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
                existing.priority = value["priority"]
                existing.enabled = value["enabled"]
                existing.note = value["note"]
                existing.updated_at = now
    await _align_pricing_group_priorities(db, values, now=now)
    await write_audit(
        db,
        event_type="pricing.bulk_update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "model": model,
            "channel": None if variant == "default" else variant,
            "priority": body.priority,
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
            RedemptionCode.expires_at.is_not(None), RedemptionCode.expires_at <= now
        )
    elif status == "exhausted":
        stmt = stmt.where(
            RedemptionCode.redeemed_count >= RedemptionCode.max_redemptions
        )
    elif status == "active":
        stmt = stmt.where(
            RedemptionCode.revoked_at.is_(None),
            or_(RedemptionCode.expires_at.is_(None), RedemptionCode.expires_at > now),
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
    request_hash = _redemption_batch_request_hash(body, amount_micro=amount)
    now = datetime.now(timezone.utc)
    idempotency_key = _redemption_batch_idempotency_key(
        request,
        admin_id=admin.id,
        request_hash=request_hash,
        now=now,
    )
    await _lock_redemption_batch_idempotency_key(
        db,
        admin.id,
        _redemption_batch_lock_identity(idempotency_key, request_hash),
    )
    existing_batch = await _redemption_batch_for_idempotency(
        db,
        admin_id=admin.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        created_after=now - timedelta(seconds=_REDEMPTION_DOWNLOAD_TTL_SECONDS),
    )
    if existing_batch is not None:
        return await _replay_redemption_batch(
            existing_batch,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            response=response,
        )

    await _require_bootstrap_completed(db)
    secret = await _redemption_secret(db)
    batch_id = new_uuid7()
    batch = RedemptionBatch(
        id=batch_id,
        created_by=admin.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        amount_micro=amount,
        code_count=body.count,
        max_redemptions=body.max_redemptions,
        expires_at=body.expires_at,
    )
    db.add(batch)
    try:
        await db.flush([batch])
    except IntegrityError as exc:
        await db.rollback()
        if _integrity_constraint_name(exc) != _REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT:
            raise
        existing_batch = await _redemption_batch_for_idempotency(
            db,
            admin_id=admin.id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            created_after=now - timedelta(seconds=_REDEMPTION_DOWNLOAD_TTL_SECONDS),
        )
        if existing_batch is None:
            raise
        return await _replay_redemption_batch(
            existing_batch,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            response=response,
        )

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
        details={
            "batch_id": batch_id,
            "count": body.count,
            "amount_micro": amount,
            "idempotency_key_hash": hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest()[:16],
        },
        autocommit=False,
    )
    try:
        await db.flush()
    except Exception:
        await db.rollback()
        raise
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
        await db.rollback()
        try:
            redis = get_redis()
            await redis.delete(_DOWNLOAD_TOKEN_PREFIX + token)
            await redis.delete(_PLAINTEXT_BATCH_PREFIX + batch_id)
        except Exception:
            logger.warning(
                "redemption plaintext cache cleanup failed batch_id=%s token=%s",
                batch_id,
                token,
                exc_info=True,
            )
        raise
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotency-Key"] = idempotency_key
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
    if target is None or getattr(target, "deleted_at", None) is not None:
        raise _http("not_found", "user not found", 404)
    if target.account_mode != "wallet":
        raise _http("ACCOUNT_NOT_WALLET", "target user is not a wallet account", 409)
    amount = _rmb_to_micro_or_422(body.amount_rmb_signed, field="amount_rmb_signed")
    if abs(amount) > MAX_ADMIN_ADJUST_MICRO:
        raise _http(
            "amount_too_large",
            "admin wallet adjustment exceeds the per-operation limit",
            422,
            max_amount_micro=MAX_ADMIN_ADJUST_MICRO,
        )
    allow_negative = await _allow_negative_balance(db)
    min_balance_micro = (
        -MAX_ADMIN_NEGATIVE_BALANCE_MICRO if allow_negative and amount < 0 else None
    )
    try:
        tx = await billing_core.adjust(
            db,
            user_id,
            amount,
            admin_id=admin.id,
            reason=body.reason,
            allow_negative=allow_negative,
            min_balance_micro=min_balance_micro,
        )
    except billing_core.BillingError as exc:
        if exc.code == "negative_balance_limit_exceeded":
            raise _http(
                exc.code,
                exc.message,
                exc.status_code,
                max_negative_balance_micro=MAX_ADMIN_NEGATIVE_BALANCE_MICRO,
            ) from exc
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
    if user is None or getattr(user, "deleted_at", None) is not None:
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
        await db.execute(
            select(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .with_for_update()
        )
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
