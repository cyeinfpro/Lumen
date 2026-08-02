"""Pure billing value, retry-reference, and redemption helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_UP
from typing import Any

from .immutables import immutable_mapping

logger = logging.getLogger(__name__)


class BillingError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _billing_error(code: str, message: str, status_code: int = 400) -> Exception:
    return BillingError(code, message, status_code)


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
        raise _billing_error(
            "INVALID_AMOUNT", "amount is not a valid decimal", 422
        ) from exc
    if not dec.is_finite():
        raise _billing_error("INVALID_AMOUNT", "amount is not a finite decimal", 422)
    try:
        exact = dec * Decimal(MICRO_RMB)
        micro = exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise _billing_error(
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
