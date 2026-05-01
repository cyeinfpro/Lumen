"""V1.0 收尾：invite_links + system_settings + users.deleted_at。

Revision ID: 0002_invites_settings_v1_finish
Revises: 0001_initial
Create Date: 2026-04-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_invites_settings_v1_finish"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------- invite_links ----------
    op.create_table(
        "invite_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token", sa.String(48), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("role", sa.String(32), nullable=False, server_default="member"),
        sa.Column(
            "created_by",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "used_by",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("token", name="uq_invite_links_token"),
    )
    op.create_index("ix_invite_links_token", "invite_links", ["token"])
    op.create_index(
        "ix_invite_links_active",
        "invite_links",
        ["revoked_at", "used_at"],
    )

    # ---------- system_settings ----------
    op.create_table(
        "system_settings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("key", name="uq_system_settings_key"),
    )

    # ---------- users.deleted_at ----------
    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "deleted_at")

    op.drop_table("system_settings")

    op.drop_index("ix_invite_links_active", table_name="invite_links")
    op.drop_index("ix_invite_links_token", table_name="invite_links")
    op.drop_table("invite_links")
