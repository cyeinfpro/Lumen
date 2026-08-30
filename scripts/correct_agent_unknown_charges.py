#!/usr/bin/env python3
"""Dry-run or apply the legacy Agent unknown-result wallet correction."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/worker"))

from app.agent_billing_corrections import run_agent_unknown_charge_backfill  # noqa: E402
from app.db import engine  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    try:
        result = await run_agent_unknown_charge_backfill(
            dry_run=not args.apply,
            batch_size=args.batch_size,
        )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scan every legacy full-hold Agent unknown-result settlement. "
            "The default is a read-only dry run; --apply appends correction credits."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="append idempotent wallet correction transactions",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="bounded database page size (1-1000, default: 100)",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
