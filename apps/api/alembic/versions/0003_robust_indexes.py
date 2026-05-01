"""Robustness pass: ARRAY default 修正、索引补全、级联策略明确。

Revision ID: 0003_robust_indexes
Revises: 0002_invites_settings_v1_finish
Create Date: 2026-04-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_robust_indexes"
down_revision: str | None = "0002_invites_settings_v1_finish"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) generations.input_image_ids: server_default '{}' -> ARRAY[]::varchar[]
    op.alter_column(
        "generations",
        "input_image_ids",
        server_default=sa.text("ARRAY[]::varchar[]"),
    )

    # 2) completions.input_image_ids: 同上
    op.alter_column(
        "completions",
        "input_image_ids",
        server_default=sa.text("ARRAY[]::varchar[]"),
    )

    # 3) allowed_emails.invited_by: FK 加 ondelete=SET NULL
    op.drop_constraint("allowed_emails_invited_by_fkey", "allowed_emails", type_="foreignkey")
    op.create_foreign_key(
        "allowed_emails_invited_by_fkey",
        "allowed_emails",
        "users",
        ["invited_by"],
        ["id"],
        ondelete="SET NULL",
    )

    # 4) outbox_events.ix_outbox_unpublished: 重建为 (kind, published_at, created_at)
    op.drop_index("ix_outbox_unpublished", table_name="outbox_events")
    op.create_index(
        "ix_outbox_unpublished",
        "outbox_events",
        ["kind", "published_at", "created_at"],
    )

    # 5) users 加 ix_users_alive (deleted_at) — 加速活跃用户查询
    op.create_index("ix_users_alive", "users", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_users_alive", table_name="users")

    op.drop_index("ix_outbox_unpublished", table_name="outbox_events")
    op.create_index(
        "ix_outbox_unpublished",
        "outbox_events",
        ["published_at", "created_at"],
    )

    op.drop_constraint("allowed_emails_invited_by_fkey", "allowed_emails", type_="foreignkey")
    op.create_foreign_key(
        "allowed_emails_invited_by_fkey",
        "allowed_emails",
        "users",
        ["invited_by"],
        ["id"],
    )

    op.alter_column(
        "completions",
        "input_image_ids",
        server_default=sa.text("'{}'"),
    )

    op.alter_column(
        "generations",
        "input_image_ids",
        server_default=sa.text("'{}'"),
    )
