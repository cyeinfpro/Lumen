"""Add durable per-credential billing window usage ledger.

Revision ID: 0041_billing_window_ledger
Revises: 0040_video_submit_fence
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0041_billing_window_ledger"
down_revision: str | None = "0040_video_submit_fence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_window_usage_events",
        sa.Column("wallet_transaction_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("credential_id", sa.String(length=36), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_transaction_id"],
            ["wallet_transactions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("wallet_transaction_id"),
    )
    op.create_index(
        "ix_billing_window_credential_created",
        "billing_window_usage_events",
        ["credential_id", "created_at"],
    )
    op.create_index(
        "ix_billing_window_user_created",
        "billing_window_usage_events",
        ["user_id", "created_at"],
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                WITH recent_usage AS (
                    SELECT
                        id,
                        user_id,
                        meta ->> 'api_key_id' AS credential_id,
                        CASE
                            WHEN COALESCE(
                                meta -> 'cost_breakdown'
                                    ->> 'actual_cost_micro',
                                ''
                            ) ~ '^[0-9]+$'
                            THEN (
                                meta -> 'cost_breakdown'
                                    ->> 'actual_cost_micro'
                            )::bigint
                            ELSE GREATEST(-amount_micro, 0)
                        END AS billed_micro,
                        created_at
                    FROM wallet_transactions
                    WHERE created_at >= now() - interval '7 days'
                      AND kind IN ('charge', 'charge_completion', 'settle')
                      AND COALESCE(meta ->> 'api_key_id', '') <> ''
                )
                INSERT INTO billing_window_usage_events (
                    wallet_transaction_id,
                    user_id,
                    credential_id,
                    amount_micro,
                    created_at
                )
                SELECT
                    id,
                    user_id,
                    credential_id,
                    billed_micro,
                    created_at
                FROM recent_usage
                WHERE billed_micro > 0
                ON CONFLICT (wallet_transaction_id) DO NOTHING
                """
            )
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            sa.text(
                """
                INSERT OR IGNORE INTO billing_window_usage_events (
                    wallet_transaction_id,
                    user_id,
                    credential_id,
                    amount_micro,
                    created_at
                )
                SELECT
                    id,
                    user_id,
                    json_extract(meta, '$.api_key_id'),
                    COALESCE(
                        CAST(
                            json_extract(
                                meta,
                                '$.cost_breakdown.actual_cost_micro'
                            ) AS INTEGER
                        ),
                        MAX(-amount_micro, 0)
                    ),
                    created_at
                FROM wallet_transactions
                WHERE created_at >= datetime('now', '-7 days')
                  AND kind IN ('charge', 'charge_completion', 'settle')
                  AND COALESCE(json_extract(meta, '$.api_key_id'), '') <> ''
                  AND COALESCE(
                        CAST(
                            json_extract(
                                meta,
                                '$.cost_breakdown.actual_cost_micro'
                            ) AS INTEGER
                        ),
                        MAX(-amount_micro, 0)
                      ) > 0
                """
            )
        )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_window_user_created",
        table_name="billing_window_usage_events",
    )
    op.drop_index(
        "ix_billing_window_credential_created",
        table_name="billing_window_usage_events",
    )
    op.drop_table("billing_window_usage_events")
