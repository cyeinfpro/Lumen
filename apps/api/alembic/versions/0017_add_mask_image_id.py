"""Add mask_image_id column to generations for local inpaint.

V1 为「图生图」加局部 inpaint：上传 RGBA mask PNG（alpha=0 处要重画），
通过 POST /images/upload 拿到 image_id，填进 PostMessageIn.mask_image_id。
后端落到 generations.mask_image_id（nullable，不加 FK：与 input_image_ids
保持一致，允许 image 行后续软删而保留 generation 历史）。

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


def downgrade() -> None:
    op.drop_column("generations", "mask_image_id")
