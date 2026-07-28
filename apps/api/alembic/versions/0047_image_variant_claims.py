"""Add fenced claims for transaction-free image variant rendering.

Revision ID: 0047_image_variant_claims
Revises: 0046_image_artifact_status
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0047_image_variant_claims"
down_revision: str | None = "0046_image_artifact_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "image_variant_claims",
        sa.Column("image_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["image_id"],
            ["images.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("image_id", "kind"),
    )
    op.create_index(
        "ix_image_variant_claims_lease",
        "image_variant_claims",
        ["lease_until", "retry_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_image_variant_claims_lease",
        table_name="image_variant_claims",
    )
    op.drop_table("image_variant_claims")
