"""Add user system prompt library.

Revision ID: 0005_system_prompts
Revises: 0004_chat_model_gpt55
Create Date: 2026-04-24 05:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_system_prompts"
down_revision = "0004_chat_model_gpt55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_prompts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_system_prompts_user_name"),
    )
    op.create_index(
        "ix_system_prompts_user_updated",
        "system_prompts",
        ["user_id", "updated_at"],
        unique=False,
    )
    op.add_column(
        "users",
        sa.Column("default_system_prompt_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("default_system_prompt_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_default_system_prompt_id_system_prompts",
        "users",
        "system_prompts",
        ["default_system_prompt_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_conversations_default_system_prompt_id_system_prompts",
        "conversations",
        "system_prompts",
        ["default_system_prompt_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_conversations_default_system_prompt_id_system_prompts",
        "conversations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_users_default_system_prompt_id_system_prompts",
        "users",
        type_="foreignkey",
    )
    op.drop_column("conversations", "default_system_prompt_id")
    op.drop_column("users", "default_system_prompt_id")
    op.drop_index("ix_system_prompts_user_updated", table_name="system_prompts")
    op.drop_table("system_prompts")
