"""Add (user_id, created_at DESC) index on generations for feed queries.

Revision ID: 0006_gen_feed_idx
Revises: 0005_system_prompts
Create Date: 2026-04-24

灵感流 Tab (`GET /api/generations/feed`) 按当前用户 + created_at DESC 排序翻页。
现有 `ix_gen_user_status_created (user_id, status, created_at)` 虽能用，但 leading
status 对这个查询不利；新建 `(user_id, created_at DESC)` 专用索引，让分页 tuple
比较可直接走索引扫描。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0006_gen_feed_idx"
down_revision: str | None = "0005_system_prompts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_generations_user_created",
        "generations",
        ["user_id", "created_at"],
        unique=False,
        postgresql_ops={"created_at": "DESC"},
    )


def downgrade() -> None:
    op.drop_index("ix_generations_user_created", table_name="generations")
