"""Deterministic billing pricing parsers and conversions."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from lumen_core import billing as billing_core

from .errors import _http


_ZERO_PRICE_ALLOWED_UNITS = {"long_context_threshold"}


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


def _validate_enabled_pricing_value(
    *,
    unit: str,
    price_micro: int,
    enabled: bool,
    field: str,
) -> None:
    if enabled and unit not in _ZERO_PRICE_ALLOWED_UNITS and int(price_micro) <= 0:
        raise _http(
            "invalid_amount",
            f"{field}: enabled pricing must be positive",
            422,
        )


def _pricing_group_priorities(
    values: list[dict[str, Any]],
) -> dict[tuple[str, str, str], int]:
    grouped: dict[tuple[str, str, str], set[int]] = {}
    for value in values:
        group = (
            str(value["scope"]),
            str(value["key"]),
            str(value["variant"]),
        )
        grouped.setdefault(group, set()).add(int(value["priority"]))
    mixed = [group for group, priorities in grouped.items() if len(priorities) > 1]
    if mixed:
        scope, key, variant = mixed[0]
        raise _http(
            "pricing_priority_mismatch",
            "all units in one pricing rule group must share one priority",
            422,
            scope=scope,
            key=key,
            variant=variant,
        )
    return {group: next(iter(priorities)) for group, priorities in grouped.items()}
