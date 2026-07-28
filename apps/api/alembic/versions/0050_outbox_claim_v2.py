"""Add durable outbox delivery claims.

Revision ID: 0050_outbox_claim_v2
Revises: 0049_image_reconcile_fence
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0050_outbox_claim_v2"
down_revision: str | None = "0049_image_reconcile_fence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.add_column(sa.Column("claim_owner", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("claim_until", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "delivery_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("last_delivery_error", sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            "ck_outbox_events_delivery_attempts",
            "delivery_attempts >= 0",
        )
        batch_op.create_index(
            "ix_outbox_claimable",
            ["claim_until", "created_at", "id"],
            postgresql_where=sa.text("published_at IS NULL"),
            sqlite_where=sa.text("published_at IS NULL"),
        )


def downgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_index("ix_outbox_claimable")
        batch_op.drop_constraint(
            "ck_outbox_events_delivery_attempts",
            type_="check",
        )
        batch_op.drop_column("last_delivery_error")
        batch_op.drop_column("delivery_attempts")
        batch_op.drop_column("claim_until")
        batch_op.drop_column("claim_owner")
