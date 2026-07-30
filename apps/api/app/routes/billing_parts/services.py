# ruff: noqa: F401
"""Billing route queries, commands, and compatibility helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from fastapi import HTTPException, Response
from sqlalchemy import func, or_, select, update
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

from ...audit import hash_email, request_ip_hash, write_audit
from ...billing_cache_state import (
    billing_cache as _shared_billing_cache,
    configure_billing_cache as _configure_shared_billing_cache,
    invalidate_balance_cache as _invalidate_shared_balance_cache,
)
from ...observability import (
    redemption_redeemed_total,
    wallet_balance_total,
    wallet_hold_active,
    wallet_hold_micro,
    wallet_orphan_holds,
)
from ...ratelimit import RateLimiter
from ...redis_client import get_redis
from ...runtime_settings import get_setting, update_settings
from ...services.billing.errors import http_error as _http
from ...services.billing.pricing_units import BULK_RATE_UNITS as _BULK_RATE_UNITS
from ...services.billing.pricing_values import (
    ZERO_PRICE_ALLOWED_UNITS as _ZERO_PRICE_ALLOWED_UNITS,
    bulk_multiplier_x10000 as _bulk_multiplier_x10000,
    bulk_numeric_micro as _bulk_numeric_micro,
    openai_price_micro as _openai_price_micro,
    parse_price_rows as _parse_price_rows,
    pricing_group_priorities as _pricing_group_priorities,
    rmb_to_micro_or_422 as _rmb_to_micro_or_422,
    validate_enabled_pricing_value as _validate_enabled_pricing_value,
)
from ...services.billing.redemption_values import (
    DOWNLOAD_TOKEN_PREFIX as _DOWNLOAD_TOKEN_PREFIX,
    PLAINTEXT_BATCH_PREFIX as _PLAINTEXT_BATCH_PREFIX,
    REDEMPTION_ALREADY_USED_CONSTRAINT as _REDEMPTION_ALREADY_USED_CONSTRAINT,
    REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT as _REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT,
    REDEMPTION_DOWNLOAD_TTL_SECONDS as _REDEMPTION_DOWNLOAD_TTL_SECONDS,
    REDEMPTION_IDEMPOTENCY_NAMESPACE as _REDEMPTION_IDEMPOTENCY_NAMESPACE,
    REDEMPTION_IDEMPOTENCY_TTL_SECONDS as _REDEMPTION_IDEMPOTENCY_TTL_SECONDS,
    REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE as _REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE,
    REDEMPTION_KNOWN_CONSTRAINTS as _REDEMPTION_KNOWN_CONSTRAINTS,
    REDEMPTION_REPLAY_CONSTRAINTS as _REDEMPTION_REPLAY_CONSTRAINTS,
    client_idempotency_key as _client_idempotency_key,
    integrity_constraint_name as _integrity_constraint_name,
    redemption_batch_idempotency_key as _redemption_batch_idempotency_key,
    redemption_batch_lock_identity as _redemption_batch_lock_identity,
    redemption_batch_payload_matches as _redemption_batch_payload_matches,
    redemption_batch_request_hash as _redemption_batch_request_hash,
    redemption_csv_batch_id as _redemption_csv_batch_id,
    redemption_csv_payload as _redemption_csv_payload,
    redemption_idempotency_cache_key as _redemption_idempotency_cache_key,
    redemption_idempotency_key as _redemption_idempotency_key,
    redemption_plaintext_payload as _redemption_plaintext_payload,
    redemption_request_hash as _redemption_request_hash,
    redemption_status as _redemption_status,
    redemption_usage_id as _redemption_usage_id,
    require_redemption_download_batch as _require_redemption_download_batch,
)
from ...services.billing.usage import (
    CHARGE_KINDS as _CHARGE_KINDS,
    meta_int as _meta_int,
    scaled_meta_cost as _scaled_meta_cost,
    usage_by_kind as _usage_by_kind,
    usage_total as _usage_total,
)
from ...services.billing.wallet_activity import (
    money_out as _money,
    wallet_activity_24h as _wallet_activity_24h,
    wallet_activity_window_end as _wallet_activity_window_end,
)
from ...services.billing_cache import BillingCacheService
from .presenters import (
    audit_out as _audit_out,
    cursor_filter as _cursor_filter,
    next_cursor as _next_cursor,
    pricing_rule_out as _pricing_rule_out,
    redemption_code_out as _redemption_code_out,
    transaction_out as _tx_out,
)
from ...services.idempotency import cache_json, get_cached_json
from ...services.pricing_cache import (
    invalidate_pricing_cache as _invalidate_pricing_cache,
)
from ...services.redemption_secret import (
    PreviousRedemptionSecretLocked,
    previous_redemption_secret,
    remember_previous_redemption_secret,
)

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
_BILLING_WINDOWS = MappingProxyType(
    {
        "5h": timedelta(hours=5),
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
    }
)
MAX_ADMIN_ADJUST_MICRO = 1_000_000 * billing_core.MICRO_RMB
MAX_ADMIN_NEGATIVE_BALANCE_MICRO = 100_000 * billing_core.MICRO_RMB


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
    activity_24h = await _wallet_activity_24h(
        db,
        user.id,
        now=_wallet_activity_window_end(),
    )
    return WalletOut(
        mode="wallet",
        balance=_money(wallet.balance_micro),
        hold=_money(wallet.hold_micro),
        low_balance_threshold=_money(threshold),
        frozen=False,
        activity_24h=activity_24h,
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


# Public service names are the supported cross-module surface. The billing
# facade aliases these back to historical private names for compatibility.
BILLING_AUDIT_EVENT_PREFIXES = _BILLING_AUDIT_EVENT_PREFIXES
BILLING_WINDOWS = _BILLING_WINDOWS
BULK_RATE_UNITS = _BULK_RATE_UNITS
CHARGE_KINDS = _CHARGE_KINDS
DOWNLOAD_TOKEN_PREFIX = _DOWNLOAD_TOKEN_PREFIX
PLAINTEXT_BATCH_PREFIX = _PLAINTEXT_BATCH_PREFIX
REDEMPTION_ALREADY_USED_CONSTRAINT = _REDEMPTION_ALREADY_USED_CONSTRAINT
REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT = _REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT
REDEMPTION_DOWNLOAD_TTL_SECONDS = _REDEMPTION_DOWNLOAD_TTL_SECONDS
REDEMPTION_IDEMPOTENCY_NAMESPACE = _REDEMPTION_IDEMPOTENCY_NAMESPACE
REDEMPTION_IDEMPOTENCY_TTL_SECONDS = _REDEMPTION_IDEMPOTENCY_TTL_SECONDS
REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE = _REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE
REDEMPTION_KNOWN_CONSTRAINTS = _REDEMPTION_KNOWN_CONSTRAINTS
REDEMPTION_REPLAY_CONSTRAINTS = _REDEMPTION_REPLAY_CONSTRAINTS
active_credential_window_config = _active_credential_window_config
align_pricing_group_priorities = _align_pricing_group_priorities
allow_negative_balance = _allow_negative_balance
audit_out = _audit_out
billing_audit_predicate = _billing_audit_predicate
billing_balance_micro = _billing_balance_micro
billing_cache = _billing_cache
billing_enabled_setting = _billing_enabled_setting
billing_http = _billing_http
billing_rows_for_range = _billing_rows_for_range
billing_snapshot_parts = _billing_snapshot_parts
bootstrap_completed_setting = _bootstrap_completed_setting
bulk_multiplier_x10000 = _bulk_multiplier_x10000
bulk_numeric_micro = _bulk_numeric_micro
cache_redemption_out = _cache_redemption_out
cached_redemption_out = _cached_redemption_out
client_idempotency_key = _client_idempotency_key
credential_windows = _credential_windows
cursor_filter = _cursor_filter
escape_like_pattern = _escape_like_pattern
generate_redemption_secret = _generate_redemption_secret
image_thresholds = _image_thresholds
integrity_constraint_name = _integrity_constraint_name
invalidate_balance_cache = _invalidate_balance_cache
load_redemption_plaintext_batch = _load_redemption_plaintext_batch
lock_redemption_batch_idempotency_key = _lock_redemption_batch_idempotency_key
lock_redemption_idempotency_key = _lock_redemption_idempotency_key
low_balance_threshold = _low_balance_threshold
money = _money
next_cursor = _next_cursor
openai_price_micro = _openai_price_micro
parse_price_rows = _parse_price_rows
pricing_group_priorities = _pricing_group_priorities
pricing_rule_out = _pricing_rule_out
redemption_batch_for_idempotency = _redemption_batch_for_idempotency
redemption_batch_idempotency_key = _redemption_batch_idempotency_key
redemption_batch_lock_identity = _redemption_batch_lock_identity
redemption_batch_payload_matches = _redemption_batch_payload_matches
redemption_batch_request_hash = _redemption_batch_request_hash
redemption_code_out = _redemption_code_out
redemption_csv_batch_id = _redemption_csv_batch_id
redemption_csv_payload = _redemption_csv_payload
redemption_idempotency_cache_key = _redemption_idempotency_cache_key
redemption_idempotency_key = _redemption_idempotency_key
redemption_out_for_usage = _redemption_out_for_usage
redemption_plaintext_payload = _redemption_plaintext_payload
redemption_request_hash = _redemption_request_hash
redemption_secret = _redemption_secret
redemption_secrets = _redemption_secrets
redemption_status = _redemption_status
redemption_usage_id = _redemption_usage_id
replay_redemption_batch = _replay_redemption_batch
require_bootstrap_completed = _require_bootstrap_completed
require_redemption_download_batch = _require_redemption_download_batch
require_redemption_operational = _require_redemption_operational
require_wallet_user = _require_wallet_user
rmb_to_micro_or_422 = _rmb_to_micro_or_422
scaled_meta_cost = _scaled_meta_cost
setting_raw = _setting_raw
store_redemption_plaintext_batch = _store_redemption_plaintext_batch
threshold_price_alignment = _threshold_price_alignment
tx_out = _tx_out
usage_by_kind = _usage_by_kind
usage_total = _usage_total
validate_enabled_pricing_value = _validate_enabled_pricing_value
validate_thresholds_have_prices = _validate_thresholds_have_prices
wallet_activity_24h = _wallet_activity_24h
wallet_activity_window_end = _wallet_activity_window_end
wallet_audit_ledger = _wallet_audit_ledger
wallet_out = _wallet_out
