"""Telegram bot 绑定表。

Revision ID: 0014_telegram_bindings
Revises: 0013_multi_image_shares
Create Date: 2026-05-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0014_telegram_bindings"
down_revision: str | None = "0013_multi_image_shares"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_bindings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tg_username", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("chat_id", name="uq_telegram_bindings_chat_id"),
        sa.UniqueConstraint("user_id", name="uq_telegram_bindings_user_id"),
    )
    op.create_index(
        "ix_telegram_bindings_user",
        "telegram_bindings",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_telegram_bindings_user", table_name="telegram_bindings")
    op.drop_table("telegram_bindings")
