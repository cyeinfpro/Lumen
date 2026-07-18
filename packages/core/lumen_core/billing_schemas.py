"""Small billing schema primitives shared by the main schema module."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MoneyOut(BaseModel):
    micro: int
    rmb: str


class WalletActivity24hOut(BaseModel):
    topup: MoneyOut = Field(default_factory=lambda: MoneyOut(micro=0, rmb="0"))
    spend: MoneyOut = Field(default_factory=lambda: MoneyOut(micro=0, rmb="0"))


class WalletOut(BaseModel):
    mode: Literal["wallet", "byok"]
    balance: MoneyOut | None
    hold: MoneyOut | None
    low_balance_threshold: MoneyOut | None = None
    frozen: bool = False
    activity_24h: WalletActivity24hOut = Field(default_factory=WalletActivity24hOut)


__all__ = ["MoneyOut", "WalletActivity24hOut", "WalletOut"]
