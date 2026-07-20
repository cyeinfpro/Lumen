"""Conversation and account-memory persistence entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..model_base import Base, SoftDeleteMixin, TimestampMixin, new_uuid7
from ..sqltypes import JsonType

# ---------- Conversations / Messages ----------


class Conversation(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    default_params: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    default_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_system_prompt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("system_prompts.id", ondelete="SET NULL"), nullable=True
    )
    # { up_to_message_id, text, tokens, updated_at }
    summary_jsonb: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType(), nullable=True
    )
    memory_disabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    active_scope_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("user_memory_scopes.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )

    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conv_created", "conversation_id", "created_at"),
        Index(
            "ix_messages_conv_alive_created",
            "conversation_id",
            "deleted_at",
            "created_at",
        ),
        Index(
            "ix_messages_conv_alive_created_id",
            "conversation_id",
            "deleted_at",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # user/assistant/system
    content: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    parent_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    conversation: Mapped[Conversation] = relationship(
        "Conversation", back_populates="messages"
    )


# ---------- Account Memory ----------


class UserMemoryScope(Base, TimestampMixin):
    __tablename__ = "user_memory_scopes"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_memory_scopes_user_name"),
        Index(
            "uq_user_memory_scopes_default",
            "user_id",
            unique=True,
            postgresql_where=text("is_default = true"),
            sqlite_where=text("is_default = 1"),
        ),
        Index("ix_user_memory_scopes_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    emoji: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )


class UserMemory(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "user_memories"
    __table_args__ = (
        Index(
            "idx_user_memories_alive",
            "user_id",
            "scope_id",
            postgresql_where=text("disabled = false AND superseded_by IS NULL"),
            sqlite_where=text("disabled = 0 AND superseded_by IS NULL"),
        ),
        Index("idx_user_memories_user_type", "user_id", "type"),
        Index("ix_user_memories_source_message", "source_message_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    # The PostgreSQL migration stores this as pgvector vector(3072). The ORM uses
    # a string representation so SQLite tests can create metadata and the worker
    # can still write pgvector-compatible "[...]" literals without pgvector as a
    # Python dependency.
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pinned: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    disabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    positive_signal: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    negative_signal: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scope_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user_memory_scopes.id", ondelete="CASCADE"),
        nullable=False,
    )
    last_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UserMemoryStaging(Base, TimestampMixin):
    __tablename__ = "user_memory_staging"
    __table_args__ = (
        Index("idx_user_memory_staging_user_decision", "user_id", "decision"),
        Index("ix_user_memory_staging_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    scope_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user_memory_scopes.id", ondelete="CASCADE"),
        nullable=False,
    )
    recommended_scope_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("user_memory_scopes.id", ondelete="SET NULL"),
        nullable=True,
    )
    decision: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class MemoryAudit(Base):
    __tablename__ = "memory_audit"
    __table_args__ = (
        Index("ix_memory_audit_user_created", "user_id", "created_at"),
        Index("ix_memory_audit_memory", "memory_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    memory_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("user_memories.id", ondelete="SET NULL"), nullable=True
    )
    staging_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("user_memory_staging.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    old_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "Conversation",
    "Message",
    "UserMemoryScope",
    "UserMemory",
    "UserMemoryStaging",
    "MemoryAudit",
]
