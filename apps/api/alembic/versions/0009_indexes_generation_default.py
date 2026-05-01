"""Add missing indexes and align generation default model.

Revision ID: 0009_indexes_generation_default
Revises: 0008_audit_dlq
Create Date: 2026-04-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0009_indexes_generation_default"
down_revision: str | None = "0008_audit_dlq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_completions_user_status_created",
        "completions",
        ["user_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_auth_sessions_user_id",
        "auth_sessions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_user_last_activity",
        "conversations",
        ["user_id", "last_activity_at"],
        unique=False,
        postgresql_ops={"last_activity_at": "DESC"},
    )
    op.create_index(
        "ix_shares_image_id",
        "shares",
        ["image_id"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_image_variants_image_kind",
        "image_variants",
        ["image_id", "kind"],
    )
    op.alter_column(
        "generations",
        "model",
        server_default="gpt-5.5",
        existing_type=sa.String(length=64),
    )


def downgrade() -> None:
    op.alter_column(
        "generations",
        "model",
        server_default="gpt-5.4",
        existing_type=sa.String(length=64),
    )
    op.drop_constraint(
        "uq_image_variants_image_kind",
        "image_variants",
        type_="unique",
    )
    op.drop_index("ix_shares_image_id", table_name="shares")
    op.drop_index("ix_conversations_user_last_activity", table_name="conversations")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_index("ix_completions_user_status_created", table_name="completions")
