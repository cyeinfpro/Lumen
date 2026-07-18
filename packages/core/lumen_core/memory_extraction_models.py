"""Durable SQLAlchemy state for assistant memory extraction delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .model_base import Base, TimestampMixin, new_uuid7
from .sqltypes import JsonType


class MemoryExtractionRun(Base, TimestampMixin):
    """Durable ownership and delivery state for one assistant memory extraction."""

    __tablename__ = "memory_extraction_runs"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            name="uq_memory_extraction_runs_event_id",
        ),
        UniqueConstraint(
            "source_message_id",
            "assistant_message_id",
            name="uq_memory_extraction_runs_source_assistant",
        ),
        Index(
            "ix_memory_extraction_runs_status_lease",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_memory_extraction_runs_user_status",
            "user_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_memory_extraction_runs_conversation_status",
            "conversation_id",
            "status",
        ),
        Index(
            "ix_memory_extraction_runs_undo_expiry",
            "status",
            "undo_expires_at",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'retryable', 'committed', 'canceled')",
            name="ck_memory_extraction_runs_status",
        ),
        CheckConstraint(
            "undo_status IN ('none', 'pending', 'ready')",
            name="ck_memory_extraction_runs_undo_status",
        ),
        CheckConstraint("fence >= 0", name="ck_memory_extraction_runs_fence"),
        CheckConstraint("attempt >= 0", name="ck_memory_extraction_runs_attempt"),
        CheckConstraint(
            "recovery_count >= 0",
            name="ck_memory_extraction_runs_recovery_count",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    source_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    assistant_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    recovery_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    undo_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    memory_writes: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    undo_operations: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    undo_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )


__all__ = ["MemoryExtractionRun"]
