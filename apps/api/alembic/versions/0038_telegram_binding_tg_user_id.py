"""Add Telegram user id to bot bindings.

Revision ID: 0038_telegram_tg_user_id
Revises: 0037_seedance_mini_alias
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0038_telegram_tg_user_id"
down_revision: str | None = "0037_seedance_mini_alias"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "telegram_bindings",
        sa.Column("tg_user_id", sa.String(length=64), nullable=True),
    )
    # Existing bindings were created only from private Telegram chats, where
    # chat.id is the user's TG id. Backfill so the security hardening does not
    # force every existing bot user to bind again after upgrade.
    op.execute(
        """
        UPDATE telegram_bindings
        SET tg_user_id = chat_id
        WHERE tg_user_id IS NULL OR tg_user_id = ''
        """
    )
    with op.batch_alter_table("telegram_bindings") as batch_op:
        batch_op.alter_column(
            "tg_user_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
    op.create_index(
        "ix_telegram_bindings_tg_user_id",
        "telegram_bindings",
        ["tg_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_telegram_bindings_tg_user_id", table_name="telegram_bindings")
    with op.batch_alter_table("telegram_bindings") as batch_op:
        batch_op.alter_column(
            "tg_user_id",
            existing_type=sa.String(length=64),
            nullable=True,
        )
    op.drop_column("telegram_bindings", "tg_user_id")
