"""Add durable video submission fencing and idempotency metadata.

Revision ID: 0040_video_submit_fence
Revises: 0039_pricing_priority
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0040_video_submit_fence"
down_revision: str | None = "0039_pricing_priority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "video_generations",
        sa.Column(
            "submission_epoch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "video_generations",
        sa.Column("submit_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "video_generations",
        sa.Column("provider_idempotency_key", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE video_generations
        SET status = 'failed',
            progress_stage = 'finished',
            progress_pct = 100,
            error_code = 'submit_unknown_downgraded',
            error_message = 'submit outcome was unknown during schema downgrade',
            finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
            next_poll_at = NULL
        WHERE status = 'submit_unknown'
        """
    )
    op.drop_column("video_generations", "provider_idempotency_key")
    op.drop_column("video_generations", "submit_started_at")
    op.drop_column("video_generations", "submission_epoch")
