"""Add image metadata JSONB for cached context captions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0012_image_metadata_jsonb"
down_revision: str | None = "0011_provider_pool_only"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "images",
        sa.Column(
            "metadata_jsonb",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("images", "metadata_jsonb")
