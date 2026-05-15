#!/usr/bin/env python3
"""Replay wallet_transactions and report ledger mismatches."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

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

from lumen_core.models import WalletTransaction  # noqa: E402


async def main_async(args: argparse.Namespace) -> int:
    async with SessionLocal() as session:
        stmt = select(WalletTransaction).order_by(
            WalletTransaction.user_id.asc(),
            WalletTransaction.created_at.asc(),
            WalletTransaction.id.asc(),
        )
        if args.user_id:
            stmt = stmt.where(WalletTransaction.user_id == args.user_id)
        rows = (await session.execute(stmt)).scalars().all()

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

    if mismatches:
        if args.report_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "transactions": len(rows),
                        "users": len(balances),
                        "mismatch_count": len(mismatches),
                        "mismatches": mismatches[: args.limit],
                    },
                    ensure_ascii=False,
                )
            )
            return 1
        print(f"FAILED: {len(mismatches)} wallet ledger mismatch(es)")
        for line in mismatches[: args.limit]:
            print(line)
        if len(mismatches) > args.limit:
            print(f"... {len(mismatches) - args.limit} more")
        return 1
    if args.report_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "transactions": len(rows),
                    "users": len(balances),
                    "mismatch_count": 0,
                    "mismatches": [],
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"OK: replayed {len(rows)} wallet transaction(s) for {len(balances)} user(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--report-json", action="store_true")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
