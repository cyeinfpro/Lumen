"""Agent session, run, reference, and tool-call persistence entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..model_base import Base, TimestampMixin, new_uuid7
from ..sqltypes import JsonType


class AgentSession(Base, TimestampMixin):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            name="uq_agent_sessions_conversation",
        ),
        Index(
            "ix_agent_sessions_user_updated",
            "user_id",
            "updated_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    runtime_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint(
            "agent_session_id",
            "idempotency_key",
            name="uq_agent_runs_session_idempotency",
        ),
        UniqueConstraint("user_message_id", name="uq_agent_runs_user_message"),
        UniqueConstraint(
            "assistant_message_id", name="uq_agent_runs_assistant_message"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', "
            "'failed', 'cancelled')",
            name="ck_agent_runs_status",
        ),
        CheckConstraint(
            "execution_epoch >= 0",
            name="ck_agent_runs_execution_epoch_nonnegative",
        ),
        CheckConstraint("attempt >= 0", name="ck_agent_runs_attempt_nonnegative"),
        CheckConstraint(
            "last_event_seq >= 0",
            name="ck_agent_runs_event_seq_nonnegative",
        ),
        CheckConstraint(
            "turn_count >= 0", name="ck_agent_runs_turn_count_nonnegative"
        ),
        CheckConstraint(
            "tool_call_count >= 0",
            name="ck_agent_runs_tool_count_nonnegative",
        ),
        CheckConstraint(
            "text_hold_micro >= 0",
            name="ck_agent_runs_text_hold_nonnegative",
        ),
        Index(
            "uq_agent_runs_one_active_session",
            "agent_session_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            "ix_agent_runs_user_status_created",
            "user_id",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "ix_agent_runs_session_created",
            "agent_session_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    agent_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # User-facing deletion is soft at Conversation. Physical erasure cascades
    # the run with its message anchors so account hard-delete cannot be blocked.
    user_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    assistant_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    execution_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_event_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_snapshot_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    account_mode_snapshot: Mapped[str] = mapped_column(String(16), nullable=False)
    system_prompt_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    user_api_credential_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("user_api_credentials.id", ondelete="SET NULL"),
        nullable=True,
    )
    upstream_supplier_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("api_supplier_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    text_hold_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    billing_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    dispatch_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    usage_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    turn_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    tool_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentRunReference(Base, TimestampMixin):
    __tablename__ = "agent_run_references"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id", "ordinal", name="uq_agent_run_references_ordinal"
        ),
        UniqueConstraint(
            "agent_run_id", "reference_label", name="uq_agent_run_references_label"
        ),
        UniqueConstraint(
            "agent_run_id", "image_id", name="uq_agent_run_references_image"
        ),
        CheckConstraint(
            "ordinal >= 0", name="ck_agent_run_references_ordinal_nonnegative"
        ),
        Index("ix_agent_run_references_run", "agent_run_id", "ordinal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    agent_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    image_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="RESTRICT"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_label: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    display_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    metadata_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )


class AgentCapabilityGrant(Base, TimestampMixin):
    __tablename__ = "agent_capability_grants"
    __table_args__ = (
        UniqueConstraint("nonce", name="uq_agent_capability_grants_nonce"),
        CheckConstraint(
            "execution_epoch >= 0",
            name="ck_agent_capability_grants_epoch_nonnegative",
        ),
        CheckConstraint(
            "max_redemptions >= 0",
            name="ck_agent_capability_grants_max_redemptions_nonnegative",
        ),
        CheckConstraint(
            "redeemed_count >= 0 AND redeemed_count <= max_redemptions",
            name="ck_agent_capability_grants_redemptions_bounded",
        ),
        Index(
            "ix_agent_capability_grants_run_epoch",
            "agent_run_id",
            "execution_epoch",
        ),
    )

    capability_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(96), nullable=False)
    agent_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    agent_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    max_redemptions: Mapped[int] = mapped_column(Integer, nullable=False)
    redeemed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class AgentToolCall(Base, TimestampMixin):
    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id", "pi_tool_call_id", name="uq_agent_tool_calls_pi_id"
        ),
        UniqueConstraint(
            "agent_run_id", "semantic_key", name="uq_agent_tool_calls_semantic"
        ),
        UniqueConstraint(
            "agent_run_id", "ordinal", name="uq_agent_tool_calls_ordinal"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'cancelled', 'timed_out')",
            name="ck_agent_tool_calls_status",
        ),
        CheckConstraint(
            "ordinal >= 0", name="ck_agent_tool_calls_ordinal_nonnegative"
        ),
        CheckConstraint(
            "execution_epoch >= 0",
            name="ck_agent_tool_calls_epoch_nonnegative",
        ),
        CheckConstraint(
            "generation_count >= 0",
            name="ck_agent_tool_calls_generation_count_nonnegative",
        ),
        Index("ix_agent_tool_calls_run_ordinal", "agent_run_id", "ordinal"),
        Index("ix_agent_tool_calls_capability", "capability_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    agent_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    capability_id: Mapped[str] = mapped_column(String(96), nullable=False)
    pi_tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_key: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    result_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    generation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = [
    "AgentCapabilityGrant",
    "AgentRun",
    "AgentRunReference",
    "AgentSession",
    "AgentToolCall",
]
