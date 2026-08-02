"""Persist generation and completion cancellation intent columns.

Revision ID: 0051_task_cancel_intent
Revises: 0050_outbox_claim_v2
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0051_task_cancel_intent"
down_revision: str | None = "0050_outbox_claim_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("generations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cancel_requested_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

    with op.batch_alter_table("completions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cancel_requested_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("completions") as batch_op:
        batch_op.drop_column("cancel_requested_at")

    with op.batch_alter_table("generations") as batch_op:
        batch_op.drop_column("cancel_requested_at")
