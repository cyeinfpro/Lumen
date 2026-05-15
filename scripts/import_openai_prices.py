#!/usr/bin/env python3
"""Import OpenAI USD model prices into pricing_rules.

Example:
    python3 scripts/import_openai_prices.py --rate 1.0 --file ./openai-prices.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent
for candidate in (REPO_ROOT / "apps" / "api", Path.cwd(), Path("/app/apps/api")):
    candidate_str = str(candidate)
    if candidate.is_dir() and candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

try:
    from app.db import SessionLocal  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "Cannot import app.db. Run inside the lumen-api container or set PYTHONPATH=apps/api.\n"
        f"{exc}\n"
    )
    sys.exit(2)

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from lumen_core import billing as billing_core  # noqa: E402
from lumen_core.models import PricingRule, new_uuid7  # noqa: E402


def _parse_price_rows(content: str) -> list[dict[str, Any]]:
    text = content.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed = parsed.get("models", [])
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
        if current is None or ":" not in line:
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


def _micro_from_usd_per_1m(value: Any, rate: float) -> int:
    # Why: ROUND_HALF_UP matches the design (§6.3) and is stable across runs.
    # `round()` in Python 3 uses banker's rounding which would drift one µRMB
    # for half-values like 0.5 → 0.
    try:
        usd = Decimal(str(value))
        rate_value = Decimal(str(rate))
        if not usd.is_finite() or usd < 0:
            raise ValueError("must be a non-negative finite decimal")
        micro = usd * rate_value * Decimal(billing_core.MICRO_RMB) / Decimal(1000)
        return int(micro.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid USD/1M price {value!r}: {exc}") from exc


async def main_async(args: argparse.Namespace) -> int:
    if args.rate <= 0 or not math.isfinite(args.rate):
        sys.stderr.write("--rate must be a positive finite number\n")
        return 2
    if not Path(args.file).is_file():
        sys.stderr.write(f"file not found: {args.file}\n")
        return 2
    content = Path(args.file).read_text(encoding="utf-8")
    rows = _parse_price_rows(content)
    values: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for row in rows:
        model = str(row.get("model") or "").strip()
        if not model:
            continue
        if "input_usd_per_1m" in row:
            key = (model, "per_1k_tokens_in")
            if key in seen_keys:
                sys.stderr.write(f"duplicate {model} input_usd_per_1m in input\n")
                return 2
            seen_keys.add(key)
            try:
                price_micro = _micro_from_usd_per_1m(row["input_usd_per_1m"], args.rate)
            except ValueError as exc:
                sys.stderr.write(f"{model} input_usd_per_1m: {exc}\n")
                return 2
            values.append(
                {
                    "id": new_uuid7(),
                    "scope": "chat_model",
                    "key": model,
                    "variant": "default",
                    "unit": "per_1k_tokens_in",
                    "price_micro": price_micro,
                    "enabled": True,
                    "note": f"OpenAI input USD/1M={row['input_usd_per_1m']} rate={args.rate}",
                }
            )
        if "output_usd_per_1m" in row:
            key = (model, "per_1k_tokens_out")
            if key in seen_keys:
                sys.stderr.write(f"duplicate {model} output_usd_per_1m in input\n")
                return 2
            seen_keys.add(key)
            try:
                price_micro = _micro_from_usd_per_1m(
                    row["output_usd_per_1m"], args.rate
                )
            except ValueError as exc:
                sys.stderr.write(f"{model} output_usd_per_1m: {exc}\n")
                return 2
            values.append(
                {
                    "id": new_uuid7(),
                    "scope": "chat_model",
                    "key": model,
                    "variant": "default",
                    "unit": "per_1k_tokens_out",
                    "price_micro": price_micro,
                    "enabled": True,
                    "note": f"OpenAI output USD/1M={row['output_usd_per_1m']} rate={args.rate}",
                }
            )
    if not values:
        sys.stderr.write("No model price rows found.\n")
        return 1

    async with SessionLocal() as session:
        bind = await session.connection()
        if bind.dialect.name == "postgresql":
            stmt = pg_insert(PricingRule).values(values)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_pricing_scope_key_variant_unit",
                    set_={
                        "price_micro": stmt.excluded.price_micro,
                        "enabled": stmt.excluded.enabled,
                        "note": stmt.excluded.note,
                    },
                )
            )
        else:
            for value in values:
                existing = (
                    await session.execute(
                        select(PricingRule).where(
                            PricingRule.scope == value["scope"],
                            PricingRule.key == value["key"],
                            PricingRule.variant == value["variant"],
                            PricingRule.unit == value["unit"],
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(PricingRule(**value))
                else:
                    existing.price_micro = value["price_micro"]
                    existing.enabled = True
                    existing.note = value["note"]
        await session.commit()
    print(f"Imported {len(values)} pricing rule rows.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--rate", type=float, default=1.0)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
