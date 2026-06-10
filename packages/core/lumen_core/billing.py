"""Billing and wallet helpers shared by API and worker code."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    PricingRule,
    UserWallet,
    WalletTransaction,
    new_uuid7,
)
from .pricing import CostBreakdown, UsageTokens, compute_breakdown
from .pricing_resolver import PricingResolver


MICRO_RMB = 1_000_000
DEFAULT_IMAGE_SIZE_THRESHOLDS: dict[str, int] = {
    "1k": 1_572_864,
    "2k": 3_686_400,
    "4k": 8_294_400,
}
CROCKFORD_REDEMPTION_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
logger = logging.getLogger(__name__)


class BillingError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def micro_to_rmb_str(amount_micro: int) -> str:
    value = (Decimal(int(amount_micro)) / Decimal(MICRO_RMB)).quantize(
        Decimal("0.000001")
    )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def money_dict(amount_micro: int) -> dict[str, Any]:
    return {"micro": int(amount_micro), "rmb": micro_to_rmb_str(amount_micro)}


def rmb_to_micro(value: str | int | float | Decimal) -> int:
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise BillingError(
            "INVALID_AMOUNT", "amount is not a valid decimal", 422
        ) from exc
    if not dec.is_finite():
        raise BillingError("INVALID_AMOUNT", "amount is not a finite decimal", 422)
    try:
        micro = (dec * Decimal(MICRO_RMB)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    except InvalidOperation as exc:
        raise BillingError(
            "INVALID_AMOUNT", "amount is not a valid decimal", 422
        ) from exc
    return int(micro)


def parse_bool_setting(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    value = raw.strip()
    if value == "1":
        return True
    if value == "0":
        return False
    return default


def parse_thresholds(raw: str | None) -> dict[str, int]:
    if not raw:
        return dict(DEFAULT_IMAGE_SIZE_THRESHOLDS)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Invalid billing image size thresholds JSON; using defaults",
            exc_info=exc,
        )
        return dict(DEFAULT_IMAGE_SIZE_THRESHOLDS)
    if not isinstance(parsed, dict):
        return dict(DEFAULT_IMAGE_SIZE_THRESHOLDS)
    out: dict[str, int] = dict(DEFAULT_IMAGE_SIZE_THRESHOLDS)
    for raw_key, value in parsed.items():
        key = str(raw_key).strip()
        if not key:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            if key not in DEFAULT_IMAGE_SIZE_THRESHOLDS:
                continue
            out[key] = DEFAULT_IMAGE_SIZE_THRESHOLDS[key]
            continue
        out[key] = value
    return out


def retry_billing_ref_id(task_id: str, retry_count: int | None) -> str:
    try:
        count = max(0, int(retry_count or 0))
    except (TypeError, ValueError):
        count = 0
    return task_id if count <= 0 else f"{task_id}:retry:{count}"


def completion_billing_retry_count(completion_or_request: Any) -> int:
    upstream_request = getattr(
        completion_or_request,
        "upstream_request",
        completion_or_request,
    )
    if isinstance(upstream_request, dict):
        try:
            return max(0, int(upstream_request.get("billing_retry_count") or 0))
        except (TypeError, ValueError):
            return 0
    return 0


def completion_billing_ref_id(completion: Any) -> str:
    return retry_billing_ref_id(
        str(getattr(completion, "id")),
        completion_billing_retry_count(completion),
    )


def tier_for_pixels(px: int, thresholds: dict[str, int] | None = None) -> str:
    values = thresholds or DEFAULT_IMAGE_SIZE_THRESHOLDS
    tier = "1k"
    for name, lower in sorted(values.items(), key=lambda item: item[1]):
        if px >= lower:
            tier = name
    return tier


def normalize_redemption_code(code: str) -> str:
    cleaned = "".join(ch for ch in code.strip().upper() if ch.isalnum())
    if cleaned.startswith("LMN"):
        cleaned = cleaned[3:]
    return cleaned


def format_redemption_code(raw_16: str) -> str:
    chunks = [raw_16[i : i + 4] for i in range(0, len(raw_16), 4)]
    return "LMN-" + "-".join(chunks)


def generate_redemption_code() -> str:
    raw = "".join(secrets.choice(CROCKFORD_REDEMPTION_ALPHABET) for _ in range(16))
    return format_redemption_code(raw)


def hash_redemption_code(code: str, secret: str) -> str:
    norm = normalize_redemption_code(code)
    return hmac.new(
        secret.encode("utf-8"), norm.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def code_prefix(code: str) -> str:
    return normalize_redemption_code(code)[:4]


async def _ensure_wallet(db: AsyncSession, user_id: str) -> None:
    get_bind = getattr(db, "get_bind", None)
    if callable(get_bind):
        try:
            bind = get_bind()
        except Exception:
            bind = None
        dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
        if dialect_name == "postgresql":
            await db.execute(
                pg_insert(UserWallet)
                .values(user_id=user_id)
                .on_conflict_do_nothing(index_elements=["user_id"])
            )
            return
    try:
        async with db.begin_nested():
            db.add(UserWallet(user_id=user_id))
            await db.flush()
    except IntegrityError:
        return


async def get_wallet(
    db: AsyncSession,
    user_id: str,
    *,
    lock: bool = False,
    create: bool = True,
) -> UserWallet | None:
    stmt = select(UserWallet).where(UserWallet.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    wallet = (await db.execute(stmt)).scalar_one_or_none()
    if wallet is not None:
        return wallet
    if not create:
        return None
    await _ensure_wallet(db, user_id)
    stmt = select(UserWallet).where(UserWallet.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    wallet = (await db.execute(stmt)).scalar_one_or_none()
    if wallet is None:
        wallet = UserWallet(user_id=user_id)
        db.add(wallet)
        await db.flush()
    return wallet


async def _existing_tx(
    db: AsyncSession,
    user_id: str,
    idempotency_key: str,
) -> WalletTransaction | None:
    return (
        await db.execute(
            select(WalletTransaction).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


async def _insert_tx(
    db: AsyncSession,
    wallet: UserWallet,
    *,
    user_id: str,
    kind: str,
    amount_micro: int,
    ref_type: str | None,
    ref_id: str | None,
    idempotency_key: str,
    meta: dict[str, Any] | None = None,
    created_by_admin: str | None = None,
) -> WalletTransaction:
    tx = WalletTransaction(
        id=new_uuid7(),
        user_id=user_id,
        kind=kind,
        amount_micro=amount_micro,
        balance_after=wallet.balance_micro,
        hold_after=wallet.hold_micro,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
        meta=meta or {},
        created_by_admin=created_by_admin,
    )
    try:
        async with db.begin_nested():
            db.add(tx)
            await db.flush([tx])
    except IntegrityError:
        # Callers re-check idempotency after taking the per-user wallet lock and
        # before mutating balances. If the unique index still fires here, a path
        # bypassed that contract; bubbling the error lets the outer transaction
        # roll back instead of committing a balance change with no ledger row.
        raise
    return tx


async def pricing_price_micro(
    db: AsyncSession,
    *,
    scope: str,
    key: str,
    unit: str,
    variant: str = "default",
) -> int | None:
    return (
        await db.execute(
            select(PricingRule.price_micro).where(
                PricingRule.scope == scope,
                PricingRule.key == key,
                PricingRule.variant == variant,
                PricingRule.unit == unit,
                PricingRule.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()


async def estimate_image_cost(
    db: AsyncSession,
    *,
    size_px: int,
    n: int = 1,
    thresholds: dict[str, int] | None = None,
) -> tuple[int, str]:
    tier = tier_for_pixels(size_px, thresholds)
    unit = await pricing_price_micro(db, scope="image_size", key=tier, unit="per_image")
    return int(unit or 0) * max(1, int(n)), tier


async def estimate_image_cost_for_tier(
    db: AsyncSession,
    *,
    tier: str,
    n: int = 1,
) -> tuple[int, str]:
    unit = await pricing_price_micro(db, scope="image_size", key=tier, unit="per_image")
    return int(unit or 0) * max(1, int(n)), tier


async def estimate_completion_cost(
    db: AsyncSession,
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_creation_5m_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
    reasoning_tokens: int = 0,
    image_output_tokens: int = 0,
    rate_multiplier_x10000: int = 10_000,
    service_tier: str = "standard",
) -> int:
    breakdown = await estimate_completion_breakdown(
        db,
        model=model,
        tokens=UsageTokens(
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_creation_5m_tokens=cache_creation_5m_tokens,
            cache_creation_1h_tokens=cache_creation_1h_tokens,
            reasoning_tokens=reasoning_tokens,
            image_output_tokens=image_output_tokens,
        ),
        rate_multiplier_x10000=rate_multiplier_x10000,
        service_tier=service_tier,
    )
    return breakdown.actual_cost_micro


async def estimate_completion_breakdown(
    db: AsyncSession,
    *,
    model: str,
    tokens: UsageTokens,
    rate_multiplier_x10000: int = 10_000,
    service_tier: str = "standard",
    channel: str | None = None,
    resolver: PricingResolver | None = None,
) -> CostBreakdown:
    pricing = await (resolver or PricingResolver()).resolve(db, model, channel=channel)
    return compute_breakdown(
        pricing,
        tokens,
        rate_multiplier_x10000=rate_multiplier_x10000,
        service_tier=service_tier,
    )


async def hold(
    db: AsyncSession,
    user_id: str,
    amount_micro: int,
    *,
    ref_type: str,
    ref_id: str,
    idempotency_key: str,
    allow_negative: bool = False,
    meta: dict[str, Any] | None = None,
) -> WalletTransaction | None:
    existing = await _existing_tx(db, user_id, idempotency_key)
    if existing is not None:
        return existing
    amount = int(amount_micro)
    if amount <= 0:
        raise BillingError("INVALID_AMOUNT", "hold amount must be positive", 422)
    wallet = await get_wallet(db, user_id, lock=True)
    assert wallet is not None
    existing = await _existing_tx(db, user_id, idempotency_key)
    if existing is not None:
        return existing
    if not allow_negative and wallet.balance_micro < amount:
        raise BillingError("INSUFFICIENT_BALANCE", "insufficient wallet balance", 402)
    wallet.balance_micro -= amount
    wallet.hold_micro += amount
    wallet.version += 1
    return await _insert_tx(
        db,
        wallet,
        user_id=user_id,
        kind="hold",
        amount_micro=-amount,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
        meta={**(meta or {}), "hold_delta": amount},
    )


async def _held_amount_for_ref(
    db: AsyncSession,
    user_id: str,
    ref_type: str,
    ref_id: str,
) -> int:
    """Return the still-outstanding hold amount for ref_id, in µRMB.

    Returns 0 if there is no hold OR if a `settle` / `release` for the same
    ref_id has already consumed it. This protects against double settle/release
    on the same generation, which would otherwise double-credit the user.
    """
    consumed = (
        await db.execute(
            select(WalletTransaction.id)
            .where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.ref_type == ref_type,
                WalletTransaction.ref_id == ref_id,
                WalletTransaction.kind.in_(("settle", "release")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if consumed is not None:
        return 0
    tx = (
        await db.execute(
            select(WalletTransaction)
            .where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.kind == "hold",
                WalletTransaction.ref_type == ref_type,
                WalletTransaction.ref_id == ref_id,
            )
            .order_by(WalletTransaction.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return max(0, -int(tx.amount_micro)) if tx is not None else 0


async def _existing_ref_consumption_tx(
    db: AsyncSession,
    user_id: str,
    ref_type: str,
    ref_id: str,
) -> WalletTransaction | None:
    return (
        await db.execute(
            select(WalletTransaction)
            .where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.ref_type == ref_type,
                WalletTransaction.ref_id == ref_id,
                WalletTransaction.kind.in_(("settle", "release")),
            )
            .order_by(WalletTransaction.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def settle(
    db: AsyncSession,
    user_id: str,
    *,
    ref_type: str,
    ref_id: str,
    actual_micro: int,
    idempotency_key: str,
    allow_negative: bool = False,
    meta: dict[str, Any] | None = None,
) -> WalletTransaction | None:
    raw_actual = int(actual_micro)
    if raw_actual < 0:
        raise BillingError(
            "NEGATIVE_AMOUNT", "settle actual amount must not be negative", 422
        )
    existing = await _existing_tx(db, user_id, idempotency_key)
    if existing is not None:
        return existing
    wallet = await get_wallet(db, user_id, lock=True)
    assert wallet is not None
    existing = await _existing_tx(db, user_id, idempotency_key)
    if existing is not None:
        return existing
    consumed = await _existing_ref_consumption_tx(db, user_id, ref_type, ref_id)
    if consumed is not None:
        return consumed
    held = await _held_amount_for_ref(db, user_id, ref_type, ref_id)
    before_balance = wallet.balance_micro
    actual = raw_actual
    balance_delta = held - actual
    next_balance = wallet.balance_micro + balance_delta
    overdraw_micro = 0
    if next_balance < 0 and not allow_negative:
        overdraw_micro = -next_balance
        next_balance = 0
    wallet.balance_micro = next_balance
    wallet.hold_micro = max(0, wallet.hold_micro - held)
    wallet.lifetime_spend_micro += max(0, actual - overdraw_micro)
    wallet.version += 1
    return await _insert_tx(
        db,
        wallet,
        user_id=user_id,
        kind="settle",
        amount_micro=wallet.balance_micro - before_balance,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
        meta={
            **(meta or {}),
            "held_micro": held,
            "actual_micro": actual,
            "hold_delta": -held,
            "overdraw_micro": overdraw_micro,
        },
    )


async def release(
    db: AsyncSession,
    user_id: str,
    *,
    ref_type: str,
    ref_id: str,
    idempotency_key: str,
    meta: dict[str, Any] | None = None,
) -> WalletTransaction | None:
    existing = await _existing_tx(db, user_id, idempotency_key)
    if existing is not None:
        return existing
    wallet = await get_wallet(db, user_id, lock=True)
    assert wallet is not None
    existing = await _existing_tx(db, user_id, idempotency_key)
    if existing is not None:
        return existing
    held = await _held_amount_for_ref(db, user_id, ref_type, ref_id)
    if held <= 0:
        consumed = await _existing_ref_consumption_tx(db, user_id, ref_type, ref_id)
        if consumed is not None:
            return consumed
        return None
    wallet.balance_micro += held
    wallet.hold_micro = max(0, wallet.hold_micro - held)
    wallet.version += 1
    return await _insert_tx(
        db,
        wallet,
        user_id=user_id,
        kind="release",
        amount_micro=held,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
        meta={**(meta or {}), "released_micro": held, "hold_delta": -held},
    )


async def charge(
    db: AsyncSession,
    user_id: str,
    amount_micro: int,
    *,
    ref_type: str,
    ref_id: str,
    idempotency_key: str,
    allow_negative: bool = False,
    cap_overdraw: bool = True,
    record_zero: bool = False,
    kind: str = "charge",
    meta: dict[str, Any] | None = None,
) -> WalletTransaction | None:
    amount = int(amount_micro)
    if amount < 0:
        raise BillingError("NEGATIVE_AMOUNT", "charge amount must not be negative", 422)
    if amount == 0 and not record_zero:
        return None
    existing = await _existing_tx(db, user_id, idempotency_key)
    if existing is not None:
        return existing
    wallet = await get_wallet(db, user_id, lock=True)
    assert wallet is not None
    existing = await _existing_tx(db, user_id, idempotency_key)
    if existing is not None:
        return existing
    before_balance = wallet.balance_micro
    if wallet.balance_micro < amount and not allow_negative and not cap_overdraw:
        raise BillingError("INSUFFICIENT_BALANCE", "insufficient wallet balance", 402)
    overdraw_micro = 0
    if wallet.balance_micro < amount and cap_overdraw:
        overdraw_micro = amount - wallet.balance_micro
        wallet.balance_micro = 0
    else:
        wallet.balance_micro -= amount
    wallet.lifetime_spend_micro += max(0, amount - overdraw_micro)
    wallet.version += 1
    return await _insert_tx(
        db,
        wallet,
        user_id=user_id,
        kind=kind,
        amount_micro=wallet.balance_micro - before_balance,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
        meta={**(meta or {}), "cost_micro": amount, "overdraw_micro": overdraw_micro},
    )


async def adjust(
    db: AsyncSession,
    user_id: str,
    amount_micro_signed: int,
    *,
    admin_id: str,
    reason: str,
    idempotency_key: str | None = None,
    allow_negative: bool = False,
) -> WalletTransaction:
    amount = int(amount_micro_signed)
    key = idempotency_key or f"adjust:{new_uuid7()}"
    existing = await _existing_tx(db, user_id, key)
    if existing is not None:
        return existing
    wallet = await get_wallet(db, user_id, lock=True)
    assert wallet is not None
    existing = await _existing_tx(db, user_id, key)
    if existing is not None:
        return existing
    next_balance = wallet.balance_micro + amount
    if next_balance < 0 and not allow_negative:
        raise BillingError(
            "INSUFFICIENT_BALANCE", "adjustment would make balance negative", 422
        )
    wallet.balance_micro = next_balance
    if amount > 0:
        wallet.lifetime_topup_micro += amount
    wallet.version += 1
    return await _insert_tx(
        db,
        wallet,
        user_id=user_id,
        kind="adjust_admin",
        amount_micro=amount,
        ref_type="admin_adjust",
        ref_id=key,
        idempotency_key=key,
        meta={"reason": reason},
        created_by_admin=admin_id,
    )


async def topup_redeem(
    db: AsyncSession,
    user_id: str,
    amount_micro: int,
    *,
    usage_id: str,
    code_id: str,
    idempotency_key: str | None = None,
    meta: dict[str, Any] | None = None,
) -> WalletTransaction:
    amount = int(amount_micro)
    if amount <= 0:
        raise BillingError("INVALID_AMOUNT", "redeem amount must be positive", 422)
    key = idempotency_key or f"redeem:{usage_id}"
    existing = await _existing_tx(db, user_id, key)
    if existing is not None:
        return existing
    wallet = await get_wallet(db, user_id, lock=True)
    assert wallet is not None
    existing = await _existing_tx(db, user_id, key)
    if existing is not None:
        return existing
    wallet.balance_micro += amount
    wallet.lifetime_topup_micro += amount
    wallet.version += 1
    return await _insert_tx(
        db,
        wallet,
        user_id=user_id,
        kind="topup_redeem",
        amount_micro=amount,
        ref_type="redemption",
        ref_id=usage_id,
        idempotency_key=key,
        meta={**(meta or {}), "code_id": code_id},
    )
