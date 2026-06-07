"""Allow 3 second video generations.

Revision ID: 0031_video_duration_3s
Revises: 0030_seedance_hold_estimates
Create Date: 2026-06-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0031_video_duration_3s"
down_revision: str | None = "0030_seedance_hold_estimates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("video_generations") as batch_op:
        batch_op.drop_constraint("ck_video_gen_duration_positive", type_="check")
        batch_op.create_check_constraint(
            "ck_video_gen_duration_positive",
            "duration_s = -1 OR (duration_s >= 3 AND duration_s <= 15)",
        )


def downgrade() -> None:
    op.execute("UPDATE video_generations SET duration_s = 4 WHERE duration_s = 3")
    with op.batch_alter_table("video_generations") as batch_op:
        batch_op.drop_constraint("ck_video_gen_duration_positive", type_="check")
        batch_op.create_check_constraint(
            "ck_video_gen_duration_positive",
            "duration_s = -1 OR (duration_s >= 4 AND duration_s <= 15)",
        )
