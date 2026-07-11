"""Persist generation billing retry identity.

Revision ID: 0042_generation_billing_retry
Revises: 0041_billing_window_ledger
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0042_generation_billing_retry"
down_revision: str | None = "0041_billing_window_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generations",
        sa.Column(
            "billing_retry_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                WITH retry_refs AS (
                    SELECT
                        split_part(ref_id, ':retry:', 1) AS generation_id,
                        max(
                            substring(ref_id from ':retry:([0-9]+)$')::integer
                        ) AS retry_count
                    FROM wallet_transactions
                    WHERE ref_type = 'generation'
                      AND ref_id ~ '^[^:]+:retry:[0-9]+$'
                    GROUP BY split_part(ref_id, ':retry:', 1)
                )
                UPDATE generations AS generation
                SET billing_retry_count = retry_refs.retry_count
                FROM retry_refs
                WHERE generation.id = retry_refs.generation_id
                """
            )
        )


def downgrade() -> None:
    op.drop_column("generations", "billing_retry_count")
