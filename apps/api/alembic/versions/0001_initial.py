"""Initial schema — DESIGN §4 全部核心表。

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column(
            "oauth_providers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("notification_email", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("role", sa.String(32), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "allowed_emails",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("invited_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("refresh_token_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("ua", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("default_params", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("default_system", sa.Text(), nullable=True),
        sa.Column("summary_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("parent_message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("intent", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_messages_conv_created", "messages", ["conversation_id", "created_at"])

    op.create_table(
        "generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False, server_default="gpt-5.4"),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("size_requested", sa.String(32), nullable=False),
        sa.Column("aspect_ratio", sa.String(16), nullable=False),
        sa.Column("input_image_ids", postgresql.ARRAY(sa.String(36)), nullable=False, server_default="{}"),
        sa.Column("primary_input_image_id", sa.String(36), nullable=True),
        sa.Column("upstream_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("progress_stage", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upstream_pixels", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_gen_user_idemp"),
    )
    op.create_index("ix_gen_user_status_created", "generations", ["user_id", "status", "created_at"])

    op.create_table(
        "completions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model", sa.String(64), nullable=False, server_default="gpt-5.4"),
        sa.Column("input_image_ids", postgresql.ARRAY(sa.String(36)), nullable=False, server_default="{}"),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("upstream_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("progress_stage", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_comp_user_idemp"),
    )

    op.create_table(
        "images",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_generation_id", sa.String(36), sa.ForeignKey("generations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("parent_image_id", sa.String(36), sa.ForeignKey("images.id", ondelete="SET NULL"), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("mime", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("blurhash", sa.String(64), nullable=True),
        sa.Column("nsfw_score", sa.Float(), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="private"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_images_parent", "images", ["parent_image_id"])
    op.create_index("ix_images_user_alive_created", "images", ["user_id", "deleted_at", "created_at"])

    op.create_table(
        "image_variants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("image_id", sa.String(36), sa.ForeignKey("images.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "shares",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("image_id", sa.String(36), sa.ForeignKey("images.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(48), nullable=False, unique=True),
        sa.Column("show_prompt", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_outbox_unpublished", "outbox_events", ["published_at", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_unpublished", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("shares")
    op.drop_table("image_variants")
    op.drop_index("ix_images_user_alive_created", table_name="images")
    op.drop_index("ix_images_parent", table_name="images")
    op.drop_table("images")
    op.drop_table("completions")
    op.drop_index("ix_gen_user_status_created", table_name="generations")
    op.drop_table("generations")
    op.drop_index("ix_messages_conv_created", table_name="messages")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("auth_sessions")
    op.drop_table("allowed_emails")
    op.drop_table("users")
