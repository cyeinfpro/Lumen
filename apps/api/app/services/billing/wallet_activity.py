"""Wallet response value helpers and 24-hour activity aggregation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import WalletTransaction
from lumen_core.schemas import MoneyOut, WalletActivity24hOut


def money_out(amount_micro: int) -> MoneyOut:
    return MoneyOut(**billing_core.money_dict(amount_micro))


def wallet_activity_window_end() -> datetime:
    return datetime.now(timezone.utc)


async def wallet_activity_24h(
    db: AsyncSession,
    user_id: str,
    *,
    now: datetime,
) -> WalletActivity24hOut:
    window_start = now - timedelta(hours=24)
    row = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                WalletTransaction.amount_micro > 0,
                                WalletTransaction.amount_micro,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                WalletTransaction.amount_micro < 0,
                                -WalletTransaction.amount_micro,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.created_at >= window_start,
                WalletTransaction.created_at <= now,
            )
        )
    ).one()
    return WalletActivity24hOut(
        topup=money_out(int(row[0] or 0)),
        spend=money_out(int(row[1] or 0)),
    )


__all__ = ["money_out", "wallet_activity_24h", "wallet_activity_window_end"]
