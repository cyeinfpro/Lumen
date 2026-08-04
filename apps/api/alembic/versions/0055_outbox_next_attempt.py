"""Add durable outbox retry scheduling.

Revision ID: 0055_outbox_next_attempt
Revises: 0054_cancel_intent_completions
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0055_outbox_next_attempt"
down_revision: str | None = "0054_cancel_intent_completions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.add_column(
            sa.Column(
                "next_attempt_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_column("next_attempt_at")
