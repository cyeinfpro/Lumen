"""Add recoverable image artifact publication state.

Revision ID: 0046_image_artifact_status
Revises: 0045_memory_extraction_runs
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0046_image_artifact_status"
down_revision: str | None = "0045_memory_extraction_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    with op.batch_alter_table("images") as batch_op:
        batch_op.add_column(
            sa.Column(
                "artifact_status",
                sa.String(length=24),
                nullable=False,
                server_default="ready",
            ),
        )
        batch_op.add_column(
            sa.Column(
                "artifact_manifest_jsonb",
                _json_type(),
                nullable=False,
                server_default="{}",
            ),
        )
        batch_op.add_column(
            sa.Column(
                "publish_attempt",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        batch_op.add_column(
            sa.Column(
                "reconcile_after",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        batch_op.add_column(
            sa.Column(
                "last_artifact_error",
                sa.Text(),
                nullable=True,
            ),
        )
        batch_op.add_column(
            sa.Column(
                "ready_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        batch_op.create_check_constraint(
            "ck_images_artifact_status",
            "artifact_status IN "
            "('staging', 'processing', 'publishing', 'ready', "
            "'failed', 'deleting', 'deleted')",
        )
        batch_op.create_check_constraint(
            "ck_images_publish_attempt",
            "publish_attempt >= 0",
        )
        batch_op.create_index(
            "ix_images_artifact_reconcile",
            ["artifact_status", "reconcile_after", "updated_at"],
        )
    op.execute(
        sa.text(
            """
            UPDATE images
            SET artifact_status = 'ready',
                ready_at = COALESCE(ready_at, created_at)
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("images") as batch_op:
        batch_op.drop_index("ix_images_artifact_reconcile")
        batch_op.drop_constraint(
            "ck_images_publish_attempt",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_images_artifact_status",
            type_="check",
        )
        batch_op.drop_column("ready_at")
        batch_op.drop_column("last_artifact_error")
        batch_op.drop_column("reconcile_after")
        batch_op.drop_column("publish_attempt")
        batch_op.drop_column("artifact_manifest_jsonb")
        batch_op.drop_column("artifact_status")
