"""Add owner-generation media lookup indexes.

Revision ID: 0029_media_owner_indexes
Revises: 0028_seedance_fast_limits
Create Date: 2026-06-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0029_media_owner_indexes"
down_revision: str | None = "0028_seedance_fast_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_images_owner_alive_created",
        "images",
        ["owner_generation_id", "deleted_at", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_videos_owner_alive_created",
        "videos",
        ["owner_generation_id", "deleted_at", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_videos_owner_alive_created", table_name="videos")
    op.drop_index("ix_images_owner_alive_created", table_name="images")
