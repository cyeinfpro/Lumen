"""Add account-level user memory tables and provider purpose prerequisites.

Revision ID: 0018_account_memory
Revises: 0017_mask_image_id
Create Date: 2026-05-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0018_account_memory"
down_revision: str | None = "0017_mask_image_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # pgcrypto 提供 gen_random_uuid;PG13+ 内置但低版本需要扩展,
    # default scope id 走它生成,与应用层其它 uuid7 形态混存可接受。
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.add_column(
        "users",
        sa.Column(
            "memory_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "memory_disabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "extraction_threshold",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.85"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("onboarding_seen", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column(
            "confirmation_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_table(
        "user_memory_scopes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("emoji", sa.String(length=8), nullable=True),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "name", name="uq_user_memory_scopes_user_name"),
    )
    op.create_index(
        "ix_user_memory_scopes_user",
        "user_memory_scopes",
        ["user_id"],
    )
    op.create_index(
        "uq_user_memory_scopes_default",
        "user_memory_scopes",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )

    op.execute(
        """
        INSERT INTO user_memory_scopes (id, user_id, name, is_default, created_at, updated_at)
        SELECT
          gen_random_uuid()::text AS id,
          users.id,
          'default',
          true,
          now(),
          now()
        FROM users
        ON CONFLICT (user_id, name) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_default_user_memory_scope()
        RETURNS trigger AS $$
        BEGIN
          INSERT INTO user_memory_scopes (id, user_id, name, is_default, created_at, updated_at)
          VALUES (
            gen_random_uuid()::text,
            NEW.id,
            'default',
            true,
            now(),
            now()
          )
          ON CONFLICT (user_id, name) DO NOTHING;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_users_default_memory_scope
        AFTER INSERT ON users
        FOR EACH ROW
        EXECUTE FUNCTION ensure_default_user_memory_scope()
        """
    )

    op.create_table(
        "user_memories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "source_message_id",
            sa.String(length=36),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_excerpt", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "disabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "positive_signal", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "negative_signal", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("superseded_by", sa.String(length=36), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "scope_id",
            sa.String(length=36),
            sa.ForeignKey("user_memory_scopes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "type IN ('profile','preference','avoid','project')",
            name="ck_user_memories_type",
        ),
        sa.CheckConstraint(
            "source IN ('explicit','auto','manual')",
            name="ck_user_memories_source",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_user_memories_confidence",
        ),
    )
    op.create_index(
        "idx_user_memories_alive",
        "user_memories",
        ["user_id", "scope_id"],
        postgresql_where=sa.text("disabled = false AND superseded_by IS NULL"),
    )
    op.create_index(
        "idx_user_memories_user_type",
        "user_memories",
        ["user_id", "type"],
    )
    op.create_index(
        "ix_user_memories_source_message",
        "user_memories",
        ["source_message_id"],
    )
    op.execute(
        "ALTER TABLE user_memories ALTER COLUMN embedding "
        "TYPE vector(3072) USING embedding::vector"
    )
    op.execute(
        "CREATE INDEX idx_user_memories_embedding "
        "ON user_memories USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL"
    )

    op.create_table(
        "user_memory_staging",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "source_message_id",
            sa.String(length=36),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_excerpt", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "scope_id",
            sa.String(length=36),
            sa.ForeignKey("user_memory_scopes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recommended_scope_id",
            sa.String(length=36),
            sa.ForeignKey("user_memory_scopes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decision", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "type IN ('profile','preference','avoid','project')",
            name="ck_user_memory_staging_type",
        ),
        sa.CheckConstraint(
            "source IN ('explicit','auto','manual')",
            name="ck_user_memory_staging_source",
        ),
        sa.CheckConstraint(
            "decision IN ('pending','accepted','rejected')",
            name="ck_user_memory_staging_decision",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_user_memory_staging_confidence",
        ),
    )
    op.create_index(
        "idx_user_memory_staging_user_decision",
        "user_memory_staging",
        ["user_id", "decision"],
    )
    op.create_index(
        "ix_user_memory_staging_expires",
        "user_memory_staging",
        ["expires_at"],
    )
    op.execute(
        "ALTER TABLE user_memory_staging ALTER COLUMN embedding "
        "TYPE vector(3072) USING embedding::vector"
    )

    op.create_table(
        "memory_audit",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "memory_id",
            sa.String(length=36),
            sa.ForeignKey("user_memories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "staging_id",
            sa.String(length=36),
            sa.ForeignKey("user_memory_staging.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("old_content", sa.Text(), nullable=True),
        sa.Column("new_content", sa.Text(), nullable=True),
        sa.Column("source_message_id", sa.String(length=36), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_memory_audit_user_created",
        "memory_audit",
        ["user_id", "created_at"],
    )
    op.create_index("ix_memory_audit_memory", "memory_audit", ["memory_id"])

    op.add_column(
        "conversations",
        sa.Column(
            "memory_disabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("active_scope_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversations_active_scope_user_memory_scopes",
        "conversations",
        "user_memory_scopes",
        ["active_scope_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_conversations_active_scope_id",
        "conversations",
        ["active_scope_id"],
        postgresql_where=sa.text("active_scope_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversations_active_scope_id",
        table_name="conversations",
        postgresql_where=sa.text("active_scope_id IS NOT NULL"),
    )
    op.drop_constraint(
        "fk_conversations_active_scope_user_memory_scopes",
        "conversations",
        type_="foreignkey",
    )
    op.drop_column("conversations", "active_scope_id")
    op.drop_column("conversations", "memory_disabled")

    op.drop_index("ix_memory_audit_memory", table_name="memory_audit")
    op.drop_index("ix_memory_audit_user_created", table_name="memory_audit")
    op.drop_table("memory_audit")

    op.drop_index("ix_user_memory_staging_expires", table_name="user_memory_staging")
    op.drop_index(
        "idx_user_memory_staging_user_decision",
        table_name="user_memory_staging",
    )
    op.drop_table("user_memory_staging")

    op.drop_index("ix_user_memories_source_message", table_name="user_memories")
    op.drop_index("idx_user_memories_embedding", table_name="user_memories")
    op.drop_index("idx_user_memories_user_type", table_name="user_memories")
    op.drop_index(
        "idx_user_memories_alive",
        table_name="user_memories",
        postgresql_where=sa.text("disabled = false AND superseded_by IS NULL"),
    )
    op.drop_table("user_memories")

    op.execute("DROP TRIGGER IF EXISTS trg_users_default_memory_scope ON users")
    op.execute("DROP FUNCTION IF EXISTS ensure_default_user_memory_scope()")
    op.drop_index("uq_user_memory_scopes_default", table_name="user_memory_scopes")
    op.drop_index("ix_user_memory_scopes_user", table_name="user_memory_scopes")
    op.drop_table("user_memory_scopes")

    op.drop_column("users", "confirmation_enabled")
    op.drop_column("users", "onboarding_seen")
    op.drop_column("users", "extraction_threshold")
    op.drop_column("users", "memory_disabled")
    op.drop_column("users", "memory_paused")
