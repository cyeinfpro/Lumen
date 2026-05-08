"""Add mask_image_id column to generations for local inpaint.

V1 为「图生图」加局部 inpaint：上传 RGBA mask PNG（alpha=0 处要重画），
通过 POST /images/upload 拿到 image_id，填进 PostMessageIn.mask_image_id。
后端落到 generations.mask_image_id（nullable，FK on delete set null，允许
image 行后续清理时保留 generation 历史）。

worker 侧从存储拉 mask PNG、用 PIL 按第一张参考图尺寸 resize，再送上游。

Revision ID: 0017_mask_image_id
Revises: 0016_model_library_items
Create Date: 2026-05-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0017_mask_image_id"
down_revision: str | None = "0016_model_library_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generations",
        sa.Column("mask_image_id", sa.String(length=36), nullable=True),
    )
    op.create_check_constraint(
        "ck_generations_mask_image_id_uuid",
        "generations",
        # Case-sensitive (~) — Image.id is produced by ``new_uuid7`` which
        # always emits lowercase hex; case-insensitive (~*) here would silently
        # accept upstream callers that pass mixed-case IDs and let those
        # diverge from the lookup keys. Lock the format strictly to lowercase.
        "mask_image_id IS NULL OR mask_image_id ~ "
        "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'",
    )
    op.create_foreign_key(
        "fk_generations_mask_image_id_images",
        "generations",
        "images",
        ["mask_image_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_generations_mask_image_id",
        "generations",
        ["mask_image_id"],
        postgresql_where=sa.text("mask_image_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_generations_mask_image_id",
        table_name="generations",
        postgresql_where=sa.text("mask_image_id IS NOT NULL"),
    )
    op.drop_constraint(
        "fk_generations_mask_image_id_images",
        "generations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_generations_mask_image_id_uuid",
        "generations",
        type_="check",
    )
    op.drop_column("generations", "mask_image_id")
