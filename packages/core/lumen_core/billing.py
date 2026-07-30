"""Billing and wallet helpers shared by API and worker code."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import billing_values as _billing_values
from .models import (
    PricingRule,
    UserWallet,
    WalletTransaction,
    new_uuid7,
)
from .pricing import (
    CostBreakdown,
    ModelPricing,
    UsageTokens,
    compute_breakdown,
    missing_pricing_buckets,
    model_pricing_from_snapshot,
)
from .pricing_resolver import PricingResolver


_IDEMPOTENCY_FINGERPRINT_KEY = "_billing_idempotency_fingerprint"
logger = logging.getLogger(__name__)


class BillingError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


MICRO_RMB = _billing_values.MICRO_RMB
DEFAULT_IMAGE_SIZE_THRESHOLDS = _billing_values.DEFAULT_IMAGE_SIZE_THRESHOLDS
CROCKFORD_REDEMPTION_ALPHABET = _billing_values.CROCKFORD_REDEMPTION_ALPHABET
MAX_RATE_MULTIPLIER = _billing_values.MAX_RATE_MULTIPLIER
micro_to_rmb_str = _billing_values.micro_to_rmb_str
money_dict = _billing_values.money_dict
rmb_to_micro = _billing_values.rmb_to_micro
parse_rate_multiplier_x10000 = _billing_values.parse_rate_multiplier_x10000
parse_bool_setting = _billing_values.parse_bool_setting
parse_thresholds = _billing_values.parse_thresholds
retry_billing_ref_id = _billing_values.retry_billing_ref_id
generation_billing_retry_count = _billing_values.generation_billing_retry_count
generation_billing_ref_id = _billing_values.generation_billing_ref_id
completion_billing_retry_count = _billing_values.completion_billing_retry_count
completion_billing_ref_id = _billing_values.completion_billing_ref_id
tier_for_pixels = _billing_values.tier_for_pixels
normalize_redemption_code = _billing_values.normalize_redemption_code
format_redemption_code = _billing_values.format_redemption_code
generate_redemption_code = _billing_values.generate_redemption_code
hash_redemption_code = _billing_values.hash_redemption_code
code_prefix = _billing_values.code_prefix


@dataclass(frozen=True)
class _IdempotencySemantics:
    kind: str
    ref_type: str | None
    ref_id: str | None
    amount_micro: int | None
    request_meta: Mapping[str, Any]
    legacy_transaction_amount_micro: int | None = None
    legacy_amount_meta_key: str | None = None
    created_by_admin: str | None = None


def _idempotency_fingerprint(semantics: _IdempotencySemantics) -> str:
    payload = {
        "version": 1,
        "kind": semantics.kind,
        "ref_type": semantics.ref_type,
        "ref_id": semantics.ref_id,
        "amount_micro": semantics.amount_micro,
        "meta": dict(semantics.request_meta),
        "created_by_admin": semantics.created_by_admin,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"v1:{hashlib.sha256(encoded).hexdigest()}"


def _idempotency_meta(
    meta: Mapping[str, Any],
    semantics: _IdempotencySemantics,
) -> dict[str, Any]:
    return {
        **dict(meta),
        _IDEMPOTENCY_FINGERPRINT_KEY: _idempotency_fingerprint(semantics),
    }


def _legacy_idempotency_matches(
    existing: WalletTransaction,
    semantics: _IdempotencySemantics,
    existing_meta: Mapping[str, Any],
) -> bool:
    for attribute, expected in (
        ("kind", semantics.kind),
        ("ref_type", semantics.ref_type),
        ("ref_id", semantics.ref_id),
        ("created_by_admin", semantics.created_by_admin),
    ):
        if hasattr(existing, attribute) and getattr(existing, attribute) != expected:
            return False
    if (
        semantics.legacy_transaction_amount_micro is not None
        and hasattr(existing, "amount_micro")
        and int(existing.amount_micro) != semantics.legacy_transaction_amount_micro
    ):
        return False
    if semantics.legacy_amount_meta_key is not None:
        raw_amount = existing_meta.get(semantics.legacy_amount_meta_key)
        try:
            legacy_amount = int(raw_amount)
        except (TypeError, ValueError):
            return False
        if legacy_amount != semantics.amount_micro:
            return False
    return all(
        key in existing_meta and existing_meta[key] == value
        for key, value in semantics.request_meta.items()
    )


def _idempotent_replay(
    existing: WalletTransaction | None,
    semantics: _IdempotencySemantics,
) -> WalletTransaction | None:
    if existing is None:
        return None
    raw_meta = getattr(existing, "meta", None)
    existing_meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    stored_fingerprint = existing_meta.get(_IDEMPOTENCY_FINGERPRINT_KEY)
    expected_fingerprint = _idempotency_fingerprint(semantics)
    matches = isinstance(stored_fingerprint, str) and hmac.compare_digest(
        stored_fingerprint, expected_fingerprint
    )
    if stored_fingerprint is None:
        matches = _legacy_idempotency_matches(
            existing,
            semantics,
            existing_meta,
        )
    if not matches:
        raise BillingError(
            "IDEMPOTENCY_CONFLICT",
            "idempotency key was already used with different billing semantics",
            409,
        )
    return existing


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


def _require_wallet(wallet: UserWallet | None) -> UserWallet:
    if wallet is None:
        raise BillingError(
            "WALLET_UNAVAILABLE",
            "wallet could not be initialized",
            500,
        )
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
    if unit is None or int(unit) <= 0:
        raise BillingError(
            "PRICING_MISSING",
            f"missing enabled image pricing rule for {tier}",
            503,
        )
    return int(unit) * max(1, int(n)), tier


async def estimate_image_cost_for_tier(
    db: AsyncSession,
    *,
    tier: str,
    n: int = 1,
) -> tuple[int, str]:
    unit = await pricing_price_micro(db, scope="image_size", key=tier, unit="per_image")
    if unit is None or int(unit) <= 0:
        raise BillingError(
            "PRICING_MISSING",
            f"missing enabled image pricing rule for {tier}",
            503,
        )
    return int(unit) * max(1, int(n)), tier


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
    missing_buckets = missing_pricing_buckets(
        pricing,
        tokens,
        service_tier=service_tier,
    )
    if pricing.pricing_source == "missing" or missing_buckets:
        detail = (
            f"; missing rates for {', '.join(missing_buckets)}"
            if missing_buckets
            else ""
        )
        raise BillingError(
            "PRICING_MISSING",
            f"missing enabled chat pricing rule for {model}{detail}",
            503,
        )
    if int(rate_multiplier_x10000) < 0:
        raise BillingError(
            "PRICING_MISSING",
            f"negative billing multiplier for {model}",
            503,
        )
    return compute_breakdown(
        pricing,
        tokens,
        rate_multiplier_x10000=rate_multiplier_x10000,
        service_tier=service_tier,
    )


async def completion_pricing_snapshot(
    db: AsyncSession,
    *,
    model: str,
    service_tier: str = "standard",
    channel: str | None = None,
    resolver: PricingResolver | None = None,
) -> dict[str, Any]:
    pricing = await (resolver or PricingResolver()).resolve(
        db,
        model,
        channel=channel,
    )
    probe_usage = UsageTokens(input_tokens=1, output_tokens=1)
    missing_buckets = missing_pricing_buckets(
        pricing,
        probe_usage,
        service_tier=service_tier,
    )
    if pricing.pricing_source == "missing" or missing_buckets:
        detail = (
            f"; missing rates for {', '.join(missing_buckets)}"
            if missing_buckets
            else ""
        )
        raise BillingError(
            "PRICING_MISSING",
            f"missing enabled chat pricing rule for {model}{detail}",
            503,
        )
    return pricing.with_defaults().model_dump()


def completion_breakdown_from_snapshot(
    snapshot: dict[str, Any],
    *,
    model: str,
    tokens: UsageTokens,
    rate_multiplier_x10000: int = 10_000,
    service_tier: str = "standard",
) -> CostBreakdown:
    try:
        pricing: ModelPricing = model_pricing_from_snapshot(snapshot)
    except ValueError as exc:
        raise BillingError(
            "PRICING_SNAPSHOT_INVALID",
            f"invalid billing pricing snapshot for {model}",
            500,
        ) from exc
    missing_buckets = missing_pricing_buckets(
        pricing,
        tokens,
        service_tier=service_tier,
    )
    if missing_buckets:
        raise BillingError(
            "PRICING_SNAPSHOT_INVALID",
            (
                f"billing pricing snapshot for {model} is missing rates for "
                f"{', '.join(missing_buckets)}"
            ),
            500,
        )
    if int(rate_multiplier_x10000) < 0:
        raise BillingError(
            "PRICING_SNAPSHOT_INVALID",
            f"negative billing multiplier for {model}",
            500,
        )
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
    amount = int(amount_micro)
    if amount <= 0:
        raise BillingError("INVALID_AMOUNT", "hold amount must be positive", 422)
    semantics = _IdempotencySemantics(
        kind="hold",
        ref_type=ref_type,
        ref_id=ref_id,
        amount_micro=amount,
        request_meta=meta or {},
        legacy_transaction_amount_micro=-amount,
    )
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, idempotency_key),
        semantics,
    )
    if existing is not None:
        return existing
    wallet = _require_wallet(await get_wallet(db, user_id, lock=True))
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, idempotency_key),
        semantics,
    )
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
        meta=_idempotency_meta(
            {**(meta or {}), "hold_delta": amount},
            semantics,
        ),
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
    record_zero: bool = False,
    meta: dict[str, Any] | None = None,
) -> WalletTransaction | None:
    raw_actual = int(actual_micro)
    if raw_actual < 0:
        raise BillingError(
            "NEGATIVE_AMOUNT", "settle actual amount must not be negative", 422
        )
    if raw_actual == 0 and not record_zero:
        raise BillingError(
            "ZERO_SETTLEMENT", "settle actual amount must be positive", 422
        )
    semantics = _IdempotencySemantics(
        kind="settle",
        ref_type=ref_type,
        ref_id=ref_id,
        amount_micro=raw_actual,
        request_meta=meta or {},
        legacy_amount_meta_key="reported_actual_micro",
    )
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, idempotency_key),
        semantics,
    )
    if existing is not None:
        return existing
    wallet = _require_wallet(await get_wallet(db, user_id, lock=True))
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, idempotency_key),
        semantics,
    )
    if existing is not None:
        return existing
    # The per-user wallet row lock serializes settle/release for the same ref.
    # Once either path records a consumption transaction, later attempts return
    # that transaction instead of mutating balances again.
    consumed = await _existing_ref_consumption_tx(db, user_id, ref_type, ref_id)
    if consumed is not None:
        return consumed
    held = await _held_amount_for_ref(db, user_id, ref_type, ref_id)
    before_balance = wallet.balance_micro
    # Once the upstream cost exists, settlement must record it in full. The
    # hold gates dispatch while the cost is still avoidable; it is not a cap
    # that makes the platform absorb a provider-side estimate overrun.
    actual = raw_actual
    unauthorized_micro = max(0, raw_actual - held)
    balance_delta = held - actual
    next_balance = wallet.balance_micro + balance_delta
    overdraw_micro = max(0, -next_balance) if not allow_negative else 0
    wallet.balance_micro = next_balance
    wallet.hold_micro = max(0, wallet.hold_micro - held)
    # lifetime_spend_micro tracks gross consumed service cost. Debt remains
    # visible in transaction metadata, but spend analytics must not hide it.
    wallet.lifetime_spend_micro += max(0, actual)
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
        meta=_idempotency_meta(
            {
                **(meta or {}),
                "held_micro": held,
                "actual_micro": actual,
                "reported_actual_micro": raw_actual,
                "unauthorized_micro": unauthorized_micro,
                "hold_delta": -held,
                "overdraw_micro": overdraw_micro,
            },
            semantics,
        ),
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
    semantics = _IdempotencySemantics(
        kind="release",
        ref_type=ref_type,
        ref_id=ref_id,
        amount_micro=None,
        request_meta=meta or {},
    )
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, idempotency_key),
        semantics,
    )
    if existing is not None:
        return existing
    wallet = _require_wallet(await get_wallet(db, user_id, lock=True))
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, idempotency_key),
        semantics,
    )
    if existing is not None:
        return existing
    # Recompute after taking the wallet lock; a concurrent settle may have
    # consumed the hold while this release was waiting.
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
        meta=_idempotency_meta(
            {**(meta or {}), "released_micro": held, "hold_delta": -held},
            semantics,
        ),
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
    record_zero: bool = False,
    kind: str = "charge",
    meta: dict[str, Any] | None = None,
) -> WalletTransaction | None:
    amount = int(amount_micro)
    if amount < 0:
        raise BillingError("NEGATIVE_AMOUNT", "charge amount must not be negative", 422)
    if amount == 0 and not record_zero:
        return None
    semantics = _IdempotencySemantics(
        kind=kind,
        ref_type=ref_type,
        ref_id=ref_id,
        amount_micro=amount,
        request_meta=meta or {},
        legacy_transaction_amount_micro=-amount,
    )
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, idempotency_key),
        semantics,
    )
    if existing is not None:
        return existing
    wallet = _require_wallet(await get_wallet(db, user_id, lock=True))
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, idempotency_key),
        semantics,
    )
    if existing is not None:
        return existing
    before_balance = wallet.balance_micro
    # 纯转嫁：charge 走的是「没有预授权、上游已经扣过费」的直扣路径，金额必须
    # 全额落到用户头上。这里曾有 cap_overdraw 开关：默认 True 时余额不足就把
    # 余额封顶成 0、差额只写进 meta，那部分成本由平台吸收，等于用户白嫖；
    # 传 False 时又改成抛 INSUFFICIENT_BALANCE，成本同样落在平台身上（服务已
    # 经产生，账却没记）。两种分支都违反「上游扣费用户必付」，因此整个开关删除，
    # 与 settle 对齐：余额照实扣穿，overdraw_micro 只作为欠费标记供追缴。
    # 是否放行请求由 hold（预授权阶段，成本尚未发生）的 allow_negative 决定。
    next_balance = wallet.balance_micro - amount
    overdraw_micro = max(0, -next_balance) if not allow_negative else 0
    wallet.balance_micro = next_balance
    wallet.lifetime_spend_micro += max(0, amount)
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
        meta=_idempotency_meta(
            {
                **(meta or {}),
                "cost_micro": amount,
                "overdraw_micro": overdraw_micro,
            },
            semantics,
        ),
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
    min_balance_micro: int | None = None,
) -> WalletTransaction:
    amount = int(amount_micro_signed)
    key = idempotency_key or f"adjust:{new_uuid7()}"
    semantics = _IdempotencySemantics(
        kind="adjust_admin",
        ref_type="admin_adjust",
        ref_id=key,
        amount_micro=amount,
        request_meta={"reason": reason},
        legacy_transaction_amount_micro=amount,
        created_by_admin=admin_id,
    )
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, key),
        semantics,
    )
    if existing is not None:
        return existing
    wallet = _require_wallet(await get_wallet(db, user_id, lock=True))
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, key),
        semantics,
    )
    if existing is not None:
        return existing
    next_balance = wallet.balance_micro + amount
    if next_balance < 0 and not allow_negative:
        raise BillingError(
            "INSUFFICIENT_BALANCE", "adjustment would make balance negative", 422
        )
    if min_balance_micro is not None and next_balance < min_balance_micro:
        raise BillingError(
            "negative_balance_limit_exceeded",
            "admin wallet adjustment would exceed the negative balance limit",
            422,
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
        meta=_idempotency_meta({"reason": reason}, semantics),
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
    request_meta = {**(meta or {}), "code_id": code_id}
    semantics = _IdempotencySemantics(
        kind="topup_redeem",
        ref_type="redemption",
        ref_id=usage_id,
        amount_micro=amount,
        request_meta=request_meta,
        legacy_transaction_amount_micro=amount,
    )
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, key),
        semantics,
    )
    if existing is not None:
        return existing
    wallet = _require_wallet(await get_wallet(db, user_id, lock=True))
    existing = _idempotent_replay(
        await _existing_tx(db, user_id, key),
        semantics,
    )
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
        meta=_idempotency_meta(request_meta, semantics),
    )
