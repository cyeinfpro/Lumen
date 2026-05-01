"""Support multi-image shares.

Revision ID: 0013_multi_image_shares
Revises: 0012_image_metadata_jsonb
Create Date: 2026-04-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0013_multi_image_shares"
down_revision: str | None = "0012_image_metadata_jsonb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shares",
        sa.Column(
            "image_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Idempotent backfill: only touch rows that still hold the default empty
    # array AND have a legacy single-image id to copy. Re-running this UPDATE
    # (e.g. after an interrupted upgrade) is a no-op once image_ids is set.
    op.execute(
        """
        UPDATE shares
        SET image_ids = jsonb_build_array(image_id)
        WHERE image_ids = '[]'::jsonb
          AND image_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("shares", "image_ids")
