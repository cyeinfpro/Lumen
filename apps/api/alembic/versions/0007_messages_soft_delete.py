"""Add soft-delete column to messages and storage key uniqueness.

Revision ID: 0007_messages_soft_delete
Revises: 0006_gen_feed_idx
Create Date: 2026-04-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007_messages_soft_delete"
down_revision: str | None = "0006_gen_feed_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_messages_conv_alive_created",
        "messages",
        ["conversation_id", "deleted_at", "created_at"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_images_storage_key",
        "images",
        ["storage_key"],
    )
    op.create_unique_constraint(
        "uq_image_variants_storage_key",
        "image_variants",
        ["storage_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_image_variants_storage_key", "image_variants", type_="unique")
    op.drop_constraint("uq_images_storage_key", "images", type_="unique")
    op.drop_index("ix_messages_conv_alive_created", table_name="messages")
    op.drop_column("messages", "deleted_at")
