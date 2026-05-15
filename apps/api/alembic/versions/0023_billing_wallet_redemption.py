"""Billing wallet, pricing, redemption codes, and account modes.

Revision ID: 0023_billing_wallet_redemption
Revises: 0022_poster_design_workflow
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0023_billing_wallet_redemption"
down_revision: str | None = "0022_poster_design_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "account_mode",
            sa.String(length=16),
            nullable=False,
            server_default="wallet",
        ),
    )
    op.create_index("ix_users_account_mode", "users", ["account_mode"])
    op.create_check_constraint(
        "ck_users_account_mode",
        "users",
        "account_mode IN ('wallet', 'byok')",
    )

    op.execute(
        """
        UPDATE users u
           SET account_mode = 'byok'
         WHERE EXISTS (
               SELECT 1
                 FROM audit_logs a
                WHERE a.user_id = u.id
                  AND a.event_type = 'auth.signup.byok.success'
         )
        """
    )

    op.create_table(
        "user_wallets",
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("balance_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("hold_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "lifetime_topup_micro",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "lifetime_spend_micro",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Why: no `balance_micro >= 0` CHECK — graylist overdraw paths
        # (admin opens `billing.allow_negative_balance=1`) legitimately need
        # negative balances. App-side `allow_negative=False` default is the gate.
        sa.CheckConstraint("hold_micro >= 0", name="ck_user_wallet_hold_nonnegative"),
    )

    op.create_table(
        "wallet_transactions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column("hold_after", sa.BigInteger(), nullable=False),
        sa.Column("ref_type", sa.String(length=32), nullable=True),
        sa.Column("ref_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_by_admin",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_wallet_tx_idemp"),
    )
    op.create_index(
        "ix_wallet_tx_user_created",
        "wallet_transactions",
        ["user_id", "created_at"],
    )
    op.create_index("ix_wallet_tx_ref", "wallet_transactions", ["ref_type", "ref_id"])

    op.create_table(
        "pricing_rules",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column(
            "variant",
            sa.String(length=32),
            nullable=False,
            server_default="default",
        ),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("price_micro", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("price_micro >= 0", name="ck_pricing_price_nonnegative"),
        sa.UniqueConstraint(
            "scope",
            "key",
            "variant",
            "unit",
            name="uq_pricing_scope_key_variant_unit",
        ),
    )

    op.create_table(
        "redemption_codes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("code_prefix", sa.String(length=8), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("max_redemptions", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("redeemed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("amount_micro > 0", name="ck_redemption_amount_positive"),
        sa.CheckConstraint("max_redemptions >= 1", name="ck_redemption_max_positive"),
        sa.UniqueConstraint("code_hash", name="uq_redemption_codes_code_hash"),
    )
    op.create_index("ix_redemption_codes_batch", "redemption_codes", ["batch_id"])
    op.create_index(
        "ix_redemption_codes_status",
        "redemption_codes",
        ["revoked_at", "expires_at"],
    )

    op.create_table(
        "redemption_codes_usage",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "code_id",
            sa.String(length=36),
            sa.ForeignKey("redemption_codes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column(
            "wallet_tx_id",
            sa.String(length=36),
            sa.ForeignKey("wallet_transactions.id"),
            nullable=False,
        ),
        sa.Column(
            "redeemed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.UniqueConstraint("code_id", "user_id", name="uq_redeem_code_user"),
    )
    op.create_index(
        "ix_redeem_user_time",
        "redemption_codes_usage",
        ["user_id", "redeemed_at"],
    )

    op.execute(
        """
        INSERT INTO pricing_rules
          (id, scope, key, variant, unit, price_micro, enabled, note)
        VALUES
          ('00000000-0000-7000-8000-000000000001', 'image_size', '1k', 'default', 'per_image', 200000, true, '默认 0.20 元/张'),
          ('00000000-0000-7000-8000-000000000002', 'image_size', '2k', 'default', 'per_image', 400000, true, '默认 0.40 元/张'),
          ('00000000-0000-7000-8000-000000000003', 'image_size', '4k', 'default', 'per_image', 800000, true, '默认 0.80 元/张')
        ON CONFLICT (scope, key, variant, unit) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("redemption_codes_usage")
    op.drop_index("ix_redemption_codes_status", table_name="redemption_codes")
    op.drop_index("ix_redemption_codes_batch", table_name="redemption_codes")
    op.drop_table("redemption_codes")
    op.drop_table("pricing_rules")
    op.drop_index("ix_wallet_tx_ref", table_name="wallet_transactions")
    op.drop_index("ix_wallet_tx_user_created", table_name="wallet_transactions")
    op.drop_table("wallet_transactions")
    op.drop_table("user_wallets")
    op.drop_constraint("ck_users_account_mode", "users", type_="check")
    op.drop_index("ix_users_account_mode", table_name="users")
    op.drop_column("users", "account_mode")
