"""Add monotonic fencing for image reconciliation.

Revision ID: 0049_image_reconcile_fence
Revises: 0048_image_reconcile_backoff
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0049_image_reconcile_fence"
down_revision: str | None = "0048_image_reconcile_backoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("images") as batch_op:
        batch_op.add_column(
            sa.Column(
                "reconcile_fence",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_check_constraint(
            "ck_images_reconcile_fence",
            "reconcile_fence >= 0",
        )
    epoch_table = op.create_table(
        "image_reconcile_epochs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "value",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.CheckConstraint(
            "value >= 0",
            name="ck_image_reconcile_epochs_value",
        ),
    )
    op.bulk_insert(epoch_table, [{"id": 1, "value": 0}])


def downgrade() -> None:
    op.drop_table("image_reconcile_epochs")
    with op.batch_alter_table("images") as batch_op:
        batch_op.drop_constraint(
            "ck_images_reconcile_fence",
            type_="check",
        )
        batch_op.drop_column("reconcile_fence")
