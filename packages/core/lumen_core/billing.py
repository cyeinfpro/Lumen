"""Billing and wallet helpers shared by API and worker code."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_UP
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
from .immutables import immutable_mapping
from .pricing import (
    CostBreakdown,
    ModelPricing,
    UsageTokens,
    compute_breakdown,
    missing_pricing_buckets,
    model_pricing_from_snapshot,
)
from .pricing_resolver import PricingResolver


MICRO_RMB = 1_000_000
DEFAULT_IMAGE_SIZE_THRESHOLDS: Mapping[str, int] = immutable_mapping(
    {
        "1k": 1_572_864,
        "2k": 3_686_400,
        "4k": 8_294_400,
    }
)
CROCKFORD_REDEMPTION_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
# users.billing_rate_multiplier 是 Numeric(8, 4)：8 位有效数字、4 位小数，
# 即整数部分最多 4 位，可表示的最大值是 9999.9999。超出这个范围的值不可能
# 由该列合法产生（只能来自迁移遗留 / 直连改库 / 反序列化脏数据），一律按
# 非法输入处理。
MAX_RATE_MULTIPLIER = Decimal("9999.9999")
_IDEMPOTENCY_FINGERPRINT_KEY = "_billing_idempotency_fingerprint"
logger = logging.getLogger(__name__)


class BillingError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


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
        and int(existing.amount_micro)
        != semantics.legacy_transaction_amount_micro
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
    matches = (
        isinstance(stored_fingerprint, str)
        and hmac.compare_digest(stored_fingerprint, expected_fingerprint)
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
    """把「元」字符串换算成整数 µRMB（10^-6 元）。

    µRMB 是账本的最小记账单位，比它更细的位数无处存放，只能取整。以前这里
    静默丢弃零头：运营在后台把单价填成 ``0.0000004`` 会得到 0 micro（这个
    模型从此免费），填成 ``1.9999995`` 会被抬到 2.0，两种情况都没有任何痕迹。
    现在只要 quantize 真的丢了余数就打一条 warning，把「你输入的精度超过了
    账本能表示的范围」这件事暴露给运维。

    这里刻意只告警不报错：调用点遍布充值 / 调账 / 兑换码 / 定价录入，历史数据
    里确实存在六位以上小数的输入，直接 422 会把既有流程打断；而单笔误差上界
    是 0.5 µRMB（5e-7 元），远低于任何一笔真实金额，不构成资金风险。
    """
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise BillingError(
            "INVALID_AMOUNT", "amount is not a valid decimal", 422
        ) from exc
    if not dec.is_finite():
        raise BillingError("INVALID_AMOUNT", "amount is not a finite decimal", 422)
    try:
        exact = dec * Decimal(MICRO_RMB)
        micro = exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise BillingError(
            "INVALID_AMOUNT", "amount is not a valid decimal", 422
        ) from exc
    if exact != micro:
        logger.warning(
            "rmb_to_micro dropped sub-micro precision: raw=%r exact_micro=%s "
            "stored_micro=%s",
            value,
            exact,
            micro,
        )
    return int(micro)


def parse_rate_multiplier_x10000(raw: Any) -> int:
    """把 users.billing_rate_multiplier 换算成万分比整数，全程 Decimal。

    该列是 Numeric(8, 4)：asyncpg 回 Decimal，aiosqlite 等驱动可能回 float。
    早先的实现写成 ``int(float(raw) * 10_000)``，float 无法精确表示 0.0009
    这类四位小数，乘 10000 后落在 10008.999... 上，再被 int() 截断成 10008，
    比准确值 10009 少一档。倍率 1.0009 的用户下一笔 100 元订单就少收 0.01 元，
    差额由平台承担——与「纯转嫁」相悖。改成 Decimal(str(raw)) 后换算精确，
    不再需要任何取整让步。

    非法值一律退回 1.0（原价转嫁）而不是 0：0 是「这个账号免费」的**显式**配置，
    只能由运营真的写下 0.0000 才生效；解析失败、NaN、负数、超出列值域的脏数据
    都属于「不知道该收多少」，此时按原价收才不会让平台白替用户垫上游成本。
    """
    if raw is None:
        return 10_000
    try:
        dec = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return 10_000
    if not dec.is_finite():
        return 10_000
    # 负倍率会算出负费用（倒贴），超上界会算出天价账单，两者都不是合法配置。
    # 早先的实现把负数夹到 0，等于让一条脏数据把该账号变成永久免费——上游照扣，
    # 平台全额吸收，正是纯转嫁禁止的方向。改成与其它非法输入一致退回 1.0 并告警。
    if dec < 0 or dec > MAX_RATE_MULTIPLIER:
        logger.warning(
            "billing rate multiplier out of range; falling back to 1.0 (raw=%r)",
            raw,
        )
        return 10_000
    # 该列只保留 4 位小数，乘 10000 后本就是整数；万一上游写入了更高精度，
    # 向上取整把零头判给用户，与视频取整方向保持一致。
    scaled = (dec * Decimal(10_000)).quantize(Decimal("1"), rounding=ROUND_UP)
    return max(0, int(scaled))


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


def generation_billing_retry_count(generation: Any) -> int:
    try:
        return max(0, int(getattr(generation, "billing_retry_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


def generation_billing_ref_id(generation: Any) -> str:
    return retry_billing_ref_id(
        str(getattr(generation, "id")),
        generation_billing_retry_count(generation),
    )


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


def tier_for_pixels(px: int, thresholds: Mapping[str, int] | None = None) -> str:
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
