"""Billing cache-aware token columns, multipliers, and API-key windows.

Revision ID: 0024_billing_cache_tokens
Revises: 0023_billing_wallet_redemption
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0024_billing_cache_tokens"
down_revision: str | None = "0023_billing_wallet_redemption"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in (
        "cache_read_tokens",
        "cache_creation_tokens",
        "cache_creation_5m_tokens",
        "cache_creation_1h_tokens",
        "reasoning_tokens",
        "image_output_tokens",
    ):
        op.add_column(
            "completions",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )

    op.add_column(
        "users",
        sa.Column(
            "billing_rate_multiplier",
            sa.Numeric(8, 4),
            nullable=False,
            server_default="1.0000",
        ),
    )
    op.add_column(
        "user_wallets",
        sa.Column(
            "billing_rate_multiplier",
            sa.Numeric(8, 4),
            nullable=False,
            server_default="1.0000",
        ),
    )
    for name in ("limit_5h_micro", "limit_1d_micro", "limit_7d_micro"):
        op.add_column(
            "user_api_credentials",
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for name in ("limit_7d_micro", "limit_1d_micro", "limit_5h_micro"):
        op.drop_column("user_api_credentials", name)
    op.drop_column("user_wallets", "billing_rate_multiplier")
    op.drop_column("users", "billing_rate_multiplier")
    for name in (
        "image_output_tokens",
        "reasoning_tokens",
        "cache_creation_1h_tokens",
        "cache_creation_5m_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
    ):
        op.drop_column("completions", name)
