"""desktop baseline schema

Revision ID: 0001_desktop_baseline
Revises:
Create Date: 2026-05-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_desktop_baseline"
down_revision = None
branch_labels = ("desktop",)
depends_on = None


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys=ON")
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("oauth_providers", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "notification_email", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("role", sa.String(32), nullable=False, server_default="admin"),
        sa.Column("default_system_prompt_id", sa.String(36), nullable=True),
        sa.Column("memory_paused", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("memory_disabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column(
            "extraction_threshold", sa.Float(), nullable=False, server_default="0.85"
        ),
        sa.Column("onboarding_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("account_mode", sa.String(16), nullable=False, server_default="byok"),
        sa.Column(
            "billing_rate_multiplier",
            sa.Numeric(8, 4),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "confirmation_enabled", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index(
        "uq_users_email_active",
        "users",
        ["email"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "system_settings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("key", name="uq_system_settings_key"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_email_hash", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_ip_hash", sa.String(64), nullable=True),
        sa.Column("target_user_id", sa.String(36), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])

    op.create_table(
        "system_prompts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
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
        sa.UniqueConstraint("user_id", "name", name="uq_system_prompts_user_name"),
    )

    op.create_table(
        "user_memory_scopes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("emoji", sa.String(8), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="0"),
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
    op.create_index("ix_user_memory_scopes_user", "user_memory_scopes", ["user_id"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("default_params", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("default_system", sa.Text(), nullable=True),
        sa.Column("default_system_prompt_id", sa.String(36), nullable=True),
        sa.Column("memory_disabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("active_scope_id", sa.String(36), nullable=True),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("summary_jsonb", sa.JSON(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index(
        "ix_conv_user_activity", "conversations", ["user_id", "last_activity_at"]
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("intent", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("parent_message_id", sa.String(36), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index(
        "ix_messages_conv_created", "messages", ["conversation_id", "created_at"]
    )

    op.create_table(
        "user_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "source_message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_excerpt", sa.Text(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("positive_signal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("negative_signal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("superseded_by", sa.String(36), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "scope_id",
            sa.String(36),
            sa.ForeignKey("user_memory_scopes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("idx_user_memories_user_type", "user_memories", ["user_id", "type"])
    op.create_index(
        "ix_user_memories_source_message", "user_memories", ["source_message_id"]
    )

    op.create_table(
        "user_memory_staging",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "source_message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_excerpt", sa.Text(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "scope_id",
            sa.String(36),
            sa.ForeignKey("user_memory_scopes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recommended_scope_id",
            sa.String(36),
            sa.ForeignKey("user_memory_scopes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decision", sa.String(16), nullable=False, server_default="pending"),
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
    )
    op.create_index(
        "idx_user_memory_staging_user_decision",
        "user_memory_staging",
        ["user_id", "decision"],
    )

    op.create_table(
        "memory_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "memory_id",
            sa.String(36),
            sa.ForeignKey("user_memories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "staging_id",
            sa.String(36),
            sa.ForeignKey("user_memory_staging.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("old_content", sa.Text(), nullable=True),
        sa.Column("new_content", sa.Text(), nullable=True),
        sa.Column("source_message_id", sa.String(36), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "images",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column(
            "visibility", sa.String(32), nullable=False, server_default="private"
        ),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("mime", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("blurhash", sa.String(64), nullable=True),
        sa.Column("nsfw_score", sa.Float(), nullable=True),
        sa.Column("parent_image_id", sa.String(36), nullable=True),
        sa.Column("owner_generation_id", sa.String(36), nullable=True),
        sa.Column("metadata_jsonb", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("ix_images_user_created", "images", ["user_id", "created_at"])

    op.create_table(
        "image_variants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "image_id",
            sa.String(36),
            sa.ForeignKey("images.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("mime", sa.String(64), nullable=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
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
        sa.UniqueConstraint("image_id", "kind", name="uq_image_variants_image_kind"),
    )

    op.create_table(
        "generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("size_requested", sa.String(32), nullable=False),
        sa.Column("aspect_ratio", sa.String(16), nullable=False),
        sa.Column("input_image_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("primary_input_image_id", sa.String(36), nullable=True),
        sa.Column("mask_image_id", sa.String(36), nullable=True),
        sa.Column("upstream_request", sa.JSON(), nullable=True),
        sa.Column("user_api_credential_id", sa.String(36), nullable=True),
        sa.Column("upstream_supplier_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column(
            "progress_stage", sa.String(32), nullable=False, server_default="queued"
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upstream_pixels", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
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
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_gen_user_idemp"),
    )
    op.create_index(
        "ix_gen_user_status_created", "generations", ["user_id", "status", "created_at"]
    )

    op.create_table(
        "completions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "message_id",
            sa.String(36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("input_image_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("upstream_request", sa.JSON(), nullable=True),
        sa.Column("user_api_credential_id", sa.String(36), nullable=True),
        sa.Column("upstream_supplier_id", sa.String(36), nullable=True),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cache_read_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cache_creation_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cache_creation_5m_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cache_creation_1h_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "image_output_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column(
            "progress_stage", sa.String(32), nullable=False, server_default="queued"
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
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
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_comp_user_idemp"),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_table(
        "outbox_dead_letter",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "outbox_id",
            sa.String(36),
            sa.ForeignKey("outbox_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_class", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "failed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_outbox_dead_letter_failed_at", "outbox_dead_letter", ["failed_at"]
    )
    op.create_index(
        "ix_outbox_dead_letter_resolved_at", "outbox_dead_letter", ["resolved_at"]
    )
    op.create_index(
        "ix_outbox_dead_letter_event_type", "outbox_dead_letter", ["event_type"]
    )

    op.create_table(
        "shares",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "image_id",
            sa.String(36),
            sa.ForeignKey("images.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("image_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("token", sa.String(48), nullable=False, unique=True),
        sa.Column("show_prompt", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
    )

    op.execute(
        "INSERT OR IGNORE INTO users "
        "(id,email,email_verified,display_name,role,account_mode,notification_email) "
        "VALUES ('local-user','local@lumen.desktop',1,'Lumen Desktop','admin','byok',0)"
    )
    op.execute(
        "INSERT OR IGNORE INTO user_memory_scopes "
        "(id,user_id,name,emoji,is_default) "
        "VALUES ('default-memory-scope','local-user','默认',NULL,1)"
    )


def downgrade() -> None:
    for table in [
        "shares",
        "outbox_dead_letter",
        "outbox_events",
        "completions",
        "generations",
        "image_variants",
        "images",
        "memory_audit",
        "user_memory_staging",
        "user_memories",
        "messages",
        "conversations",
        "user_memory_scopes",
        "system_prompts",
        "audit_logs",
        "system_settings",
        "users",
    ]:
        op.drop_table(table)
