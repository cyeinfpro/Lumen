"""Persist image reconcile backoff and quarantine state.

Revision ID: 0048_image_reconcile_backoff
Revises: 0047_image_variant_claims
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0048_image_reconcile_backoff"
down_revision: str | None = "0047_image_variant_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("images") as batch_op:
        batch_op.add_column(
            sa.Column(
                "reconcile_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "last_reconcile_error_code",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "last_reconcile_error_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "quarantined_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_images_reconcile_attempts",
            "reconcile_attempts >= 0",
        )
        batch_op.create_index(
            "ix_images_artifact_quarantine",
            ["quarantined_at", "artifact_status", "updated_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("images") as batch_op:
        batch_op.drop_index("ix_images_artifact_quarantine")
        batch_op.drop_constraint(
            "ck_images_reconcile_attempts",
            type_="check",
        )
        batch_op.drop_column("quarantined_at")
        batch_op.drop_column("last_reconcile_error_at")
        batch_op.drop_column("last_reconcile_error_code")
        batch_op.drop_column("reconcile_attempts")
