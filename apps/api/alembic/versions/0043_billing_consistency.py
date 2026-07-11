"""Harden billing window ownership and redemption batch idempotency.

Revision ID: 0043_billing_consistency
Revises: 0042_generation_billing_retry
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0043_billing_consistency"
down_revision: str | None = "0042_generation_billing_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "redemption_batches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "created_by",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("code_count", sa.Integer(), nullable=False),
        sa.Column("max_redemptions", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_micro > 0",
            name="ck_redemption_batch_amount_positive",
        ),
        sa.CheckConstraint(
            "code_count >= 1",
            name="ck_redemption_batch_count_positive",
        ),
        sa.CheckConstraint(
            "max_redemptions >= 1",
            name="ck_redemption_batch_max_positive",
        ),
        sa.UniqueConstraint(
            "created_by",
            "idempotency_key",
            name="uq_redemption_batch_creator_idemp",
        ),
    )
    op.create_index(
        "ix_redemption_batches_creator_created",
        "redemption_batches",
        ["created_by", "created_at"],
    )
    op.create_index(
        "ix_redemption_batches_creator_request_created",
        "redemption_batches",
        ["created_by", "request_hash", "created_at"],
    )

    op.execute(
        sa.text(
            """
            DELETE FROM billing_window_usage_events
            WHERE amount_micro <= 0
               OR NOT EXISTS (
                    SELECT 1
                    FROM user_api_credentials AS credential
                    WHERE credential.id =
                            billing_window_usage_events.credential_id
                      AND credential.user_id =
                            billing_window_usage_events.user_id
               )
               OR NOT EXISTS (
                    SELECT 1
                    FROM wallet_transactions AS wallet_tx
                    WHERE wallet_tx.id =
                            billing_window_usage_events.wallet_transaction_id
                      AND wallet_tx.user_id =
                            billing_window_usage_events.user_id
                      AND wallet_tx.ref_type = 'completion'
                      AND wallet_tx.kind IN (
                            'charge',
                            'charge_completion',
                            'settle'
                      )
               )
            """
        )
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                WITH normalized_usage AS (
                    SELECT
                        wallet_tx.id AS wallet_transaction_id,
                        wallet_tx.user_id,
                        wallet_tx.meta ->> 'api_key_id' AS credential_id,
                        CASE
                            WHEN COALESCE(
                                wallet_tx.meta -> 'cost_breakdown'
                                    ->> 'actual_cost_micro',
                                ''
                            ) ~ '^[0-9]+$'
                            THEN (
                                wallet_tx.meta -> 'cost_breakdown'
                                    ->> 'actual_cost_micro'
                            )::bigint
                            WHEN COALESCE(
                                wallet_tx.meta ->> 'actual_micro',
                                ''
                            ) ~ '^[0-9]+$'
                            THEN (
                                wallet_tx.meta ->> 'actual_micro'
                            )::bigint
                            WHEN COALESCE(
                                wallet_tx.meta ->> 'cost_micro',
                                ''
                            ) ~ '^[0-9]+$'
                            THEN (
                                wallet_tx.meta ->> 'cost_micro'
                            )::bigint
                            ELSE GREATEST(-wallet_tx.amount_micro, 0)
                        END AS billed_micro,
                        wallet_tx.created_at
                    FROM wallet_transactions AS wallet_tx
                    JOIN user_api_credentials AS credential
                      ON credential.id =
                            wallet_tx.meta ->> 'api_key_id'
                     AND credential.user_id = wallet_tx.user_id
                    WHERE wallet_tx.created_at >=
                            now() - interval '7 days'
                      AND wallet_tx.ref_type = 'completion'
                      AND wallet_tx.kind IN (
                            'charge',
                            'charge_completion',
                            'settle'
                      )
                      AND COALESCE(
                            wallet_tx.meta ->> 'api_key_id',
                            ''
                          ) <> ''
                )
                INSERT INTO billing_window_usage_events (
                    wallet_transaction_id,
                    user_id,
                    credential_id,
                    amount_micro,
                    created_at
                )
                SELECT
                    wallet_transaction_id,
                    user_id,
                    credential_id,
                    billed_micro,
                    created_at
                FROM normalized_usage
                WHERE billed_micro > 0
                ON CONFLICT (wallet_transaction_id) DO UPDATE
                SET user_id = EXCLUDED.user_id,
                    credential_id = EXCLUDED.credential_id,
                    amount_micro = EXCLUDED.amount_micro,
                    created_at = EXCLUDED.created_at
                """
            )
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            sa.text(
                """
                INSERT INTO billing_window_usage_events (
                    wallet_transaction_id,
                    user_id,
                    credential_id,
                    amount_micro,
                    created_at
                )
                SELECT
                    wallet_tx.id,
                    wallet_tx.user_id,
                    json_extract(wallet_tx.meta, '$.api_key_id'),
                    CASE
                        WHEN CAST(
                            json_extract(
                                wallet_tx.meta,
                                '$.cost_breakdown.actual_cost_micro'
                            ) AS INTEGER
                        ) > 0
                        THEN CAST(
                            json_extract(
                                wallet_tx.meta,
                                '$.cost_breakdown.actual_cost_micro'
                            ) AS INTEGER
                        )
                        WHEN CAST(
                            json_extract(
                                wallet_tx.meta,
                                '$.actual_micro'
                            ) AS INTEGER
                        ) > 0
                        THEN CAST(
                            json_extract(
                                wallet_tx.meta,
                                '$.actual_micro'
                            ) AS INTEGER
                        )
                        WHEN CAST(
                            json_extract(
                                wallet_tx.meta,
                                '$.cost_micro'
                            ) AS INTEGER
                        ) > 0
                        THEN CAST(
                            json_extract(
                                wallet_tx.meta,
                                '$.cost_micro'
                            ) AS INTEGER
                        )
                        ELSE MAX(-wallet_tx.amount_micro, 0)
                    END,
                    wallet_tx.created_at
                FROM wallet_transactions AS wallet_tx
                JOIN user_api_credentials AS credential
                  ON credential.id =
                        json_extract(wallet_tx.meta, '$.api_key_id')
                 AND credential.user_id = wallet_tx.user_id
                WHERE wallet_tx.created_at >= datetime('now', '-7 days')
                  AND wallet_tx.ref_type = 'completion'
                  AND wallet_tx.kind IN (
                        'charge',
                        'charge_completion',
                        'settle'
                  )
                  AND COALESCE(
                        json_extract(wallet_tx.meta, '$.api_key_id'),
                        ''
                      ) <> ''
                  AND (
                        CAST(
                            json_extract(
                                wallet_tx.meta,
                                '$.cost_breakdown.actual_cost_micro'
                            ) AS INTEGER
                        ) > 0
                     OR CAST(
                            json_extract(
                                wallet_tx.meta,
                                '$.actual_micro'
                            ) AS INTEGER
                        ) > 0
                     OR CAST(
                            json_extract(
                                wallet_tx.meta,
                                '$.cost_micro'
                            ) AS INTEGER
                        ) > 0
                     OR -wallet_tx.amount_micro > 0
                  )
                ON CONFLICT (wallet_transaction_id) DO UPDATE
                SET user_id = excluded.user_id,
                    credential_id = excluded.credential_id,
                    amount_micro = excluded.amount_micro,
                    created_at = excluded.created_at
                """
            )
        )
    with op.batch_alter_table("user_api_credentials") as batch_op:
        batch_op.create_unique_constraint(
            "uq_user_api_credentials_id_user",
            ["id", "user_id"],
        )
    with op.batch_alter_table("billing_window_usage_events") as batch_op:
        batch_op.create_check_constraint(
            "ck_billing_window_amount_positive",
            "amount_micro > 0",
        )
        batch_op.create_foreign_key(
            "fk_billing_window_credential_user",
            "user_api_credentials",
            ["credential_id", "user_id"],
            ["id", "user_id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("billing_window_usage_events") as batch_op:
        batch_op.drop_constraint(
            "fk_billing_window_credential_user",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "ck_billing_window_amount_positive",
            type_="check",
        )
    with op.batch_alter_table("user_api_credentials") as batch_op:
        batch_op.drop_constraint(
            "uq_user_api_credentials_id_user",
            type_="unique",
        )
    op.drop_index(
        "ix_redemption_batches_creator_request_created",
        table_name="redemption_batches",
    )
    op.drop_index(
        "ix_redemption_batches_creator_created",
        table_name="redemption_batches",
    )
    op.drop_table("redemption_batches")
