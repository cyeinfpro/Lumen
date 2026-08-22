"""Add Agent persistence and closed-by-default runtime settings.

Revision ID: 0069_agent_foundation
Revises: 0068_openai_chat_defaults
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0069_agent_foundation"
down_revision: str | None = "0068_openai_chat_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runtime_version",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "conversation_id",
            name="uq_agent_sessions_conversation",
        ),
    )
    op.create_index(
        "ix_agent_sessions_user_updated",
        "agent_sessions",
        ["user_id", "updated_at", "id"],
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "agent_session_id",
            sa.String(length=36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_message_id",
            sa.String(length=36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assistant_message_id",
            sa.String(length=36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "execution_epoch", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "last_event_seq", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "request_snapshot_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("account_mode_snapshot", sa.String(length=16), nullable=False),
        sa.Column("system_prompt_snapshot", sa.Text(), nullable=True),
        sa.Column("provider_name", sa.String(length=120), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=16), nullable=True),
        sa.Column(
            "user_api_credential_id",
            sa.String(length=36),
            sa.ForeignKey("user_api_credentials.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "upstream_supplier_id",
            sa.String(length=36),
            sa.ForeignKey("api_supplier_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "text_hold_micro", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "billing_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "dispatch_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "usage_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "tool_call_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "agent_session_id",
            "idempotency_key",
            name="uq_agent_runs_session_idempotency",
        ),
        sa.UniqueConstraint(
            "user_message_id",
            name="uq_agent_runs_user_message",
        ),
        sa.UniqueConstraint(
            "assistant_message_id",
            name="uq_agent_runs_assistant_message",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', "
            "'failed', 'cancelled')",
            name="ck_agent_runs_status",
        ),
        sa.CheckConstraint(
            "execution_epoch >= 0",
            name="ck_agent_runs_execution_epoch_nonnegative",
        ),
        sa.CheckConstraint(
            "attempt >= 0",
            name="ck_agent_runs_attempt_nonnegative",
        ),
        sa.CheckConstraint(
            "last_event_seq >= 0",
            name="ck_agent_runs_event_seq_nonnegative",
        ),
        sa.CheckConstraint(
            "turn_count >= 0",
            name="ck_agent_runs_turn_count_nonnegative",
        ),
        sa.CheckConstraint(
            "tool_call_count >= 0",
            name="ck_agent_runs_tool_count_nonnegative",
        ),
        sa.CheckConstraint(
            "text_hold_micro >= 0",
            name="ck_agent_runs_text_hold_nonnegative",
        ),
    )
    op.create_index(
        "uq_agent_runs_one_active_session",
        "agent_runs",
        ["agent_session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_agent_runs_user_status_created",
        "agent_runs",
        ["user_id", "status", "created_at", "id"],
    )
    op.create_index(
        "ix_agent_runs_session_created",
        "agent_runs",
        ["agent_session_id", "created_at", "id"],
    )

    op.create_table(
        "agent_run_references",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "agent_run_id",
            sa.String(length=36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("reference_label", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("display_label", sa.String(length=80), nullable=True),
        sa.Column(
            "metadata_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "agent_run_id",
            "ordinal",
            name="uq_agent_run_references_ordinal",
        ),
        sa.UniqueConstraint(
            "agent_run_id",
            "reference_label",
            name="uq_agent_run_references_label",
        ),
        sa.UniqueConstraint(
            "agent_run_id",
            "image_id",
            name="uq_agent_run_references_image",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_agent_run_references_ordinal_nonnegative",
        ),
    )
    op.create_index(
        "ix_agent_run_references_run",
        "agent_run_references",
        ["agent_run_id", "ordinal"],
    )

    op.create_table(
        "agent_capability_grants",
        sa.Column("capability_id", sa.String(length=96), primary_key=True),
        sa.Column("nonce", sa.String(length=96), nullable=False),
        sa.Column(
            "agent_run_id",
            sa.String(length=36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_session_id",
            sa.String(length=36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("execution_epoch", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_redemptions", sa.Integer(), nullable=False),
        sa.Column(
            "redeemed_count", sa.Integer(), nullable=False, server_default="0"
        ),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("nonce", name="uq_agent_capability_grants_nonce"),
        sa.CheckConstraint(
            "execution_epoch >= 0",
            name="ck_agent_capability_grants_epoch_nonnegative",
        ),
        sa.CheckConstraint(
            "max_redemptions >= 0",
            name="ck_agent_capability_grants_max_redemptions_nonnegative",
        ),
        sa.CheckConstraint(
            "redeemed_count >= 0 AND redeemed_count <= max_redemptions",
            name="ck_agent_capability_grants_redemptions_bounded",
        ),
    )
    op.create_index(
        "ix_agent_capability_grants_run_epoch",
        "agent_capability_grants",
        ["agent_run_id", "execution_epoch"],
    )

    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "agent_run_id",
            sa.String(length=36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability_id", sa.String(length=96), nullable=False),
        sa.Column("pi_tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("execution_epoch", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("semantic_key", sa.String(length=64), nullable=False),
        sa.Column(
            "arguments_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "result_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "generation_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "agent_run_id",
            "pi_tool_call_id",
            name="uq_agent_tool_calls_pi_id",
        ),
        sa.UniqueConstraint(
            "agent_run_id",
            "semantic_key",
            name="uq_agent_tool_calls_semantic",
        ),
        sa.UniqueConstraint(
            "agent_run_id",
            "ordinal",
            name="uq_agent_tool_calls_ordinal",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'cancelled', 'timed_out')",
            name="ck_agent_tool_calls_status",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_agent_tool_calls_ordinal_nonnegative",
        ),
        sa.CheckConstraint(
            "execution_epoch >= 0",
            name="ck_agent_tool_calls_epoch_nonnegative",
        ),
        sa.CheckConstraint(
            "generation_count >= 0",
            name="ck_agent_tool_calls_generation_count_nonnegative",
        ),
    )
    op.create_index(
        "ix_agent_tool_calls_run_ordinal",
        "agent_tool_calls",
        ["agent_run_id", "ordinal"],
    )
    op.create_index(
        "ix_agent_tool_calls_capability",
        "agent_tool_calls",
        ["capability_id"],
    )
def downgrade() -> None:
    op.drop_table("agent_tool_calls")
    op.drop_table("agent_capability_grants")
    op.drop_table("agent_run_references")
    op.drop_table("agent_runs")
    op.drop_table("agent_sessions")
