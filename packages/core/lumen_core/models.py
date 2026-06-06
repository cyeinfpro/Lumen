"""SQLAlchemy ORM 模型——DESIGN §4 的所有核心表。

所有表：uuid7 主键 + created_at / updated_at + 软删 deleted_at。
所有 id 字段存储为 str（便于 Redis / SSE 消息序列化），用 uuid7 保留时间有序性。
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import cached_property
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .constants import DEFAULT_CHAT_MODEL
from .queue_metadata import completion_queue_metadata, generation_queue_metadata
from .sqltypes import JsonType, StringListType


# review #14：BYOK 凭证状态全局唯一来源；migration / ORM / route / worker 共用。
USER_API_CREDENTIAL_STATUSES: tuple[str, ...] = (
    "active",
    "invalid",
    "replaced",
    "revoked",
)


def new_uuid7() -> str:
    # uuid7 在 Python 里返回 UUID；我们存字符串便于 JSON 直出。
    from uuid_extensions import uuid7

    return str(uuid7())


class Base(DeclarativeBase):
    pass


# ---------- Mixins ----------


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )


# ---------- Users / Auth ----------


class User(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_providers: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    notification_email: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), default="member", nullable=False)
    default_system_prompt_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("system_prompts.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    memory_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    memory_disabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    extraction_threshold: Mapped[float] = mapped_column(
        Float, default=0.85, nullable=False, server_default=text("0.85")
    )
    onboarding_seen: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    account_mode: Mapped[str] = mapped_column(
        String(16), default="wallet", nullable=False, server_default="wallet"
    )
    billing_rate_multiplier: Mapped[float] = mapped_column(
        Numeric(8, 4),
        nullable=False,
        default=1.0,
        server_default="1.0000",
    )
    confirmation_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    # 软删活跃用户索引：admin 列表 / 登录查询都按 deleted_at IS NULL 过滤。
    # requires alembic migration: create index ix_users_alive on users(deleted_at)
    __table_args__ = (
        Index("ix_users_alive", "deleted_at"),
        Index(
            "uq_users_email_active",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_users_account_mode", "account_mode"),
        CheckConstraint(
            "account_mode IN ('wallet', 'byok')",
            name="ck_users_account_mode",
        ),
    )

    # review #23：BYOK 凭证反向关系。lazy="raise" 强制显式 selectinload，
    # 防止 user 序列化时无意触发 N+1。
    api_credentials: Mapped[list["UserApiCredential"]] = relationship(
        "UserApiCredential",
        back_populates="user",
        foreign_keys="UserApiCredential.user_id",
        lazy="raise",
    )


class AllowedEmail(Base, TimestampMixin):
    """邮箱白名单：注册/OAuth 回调时查，命中才创建 user（DESIGN §4 users 注释）。"""

    __tablename__ = "allowed_emails"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # invited_by 删除用户后置 NULL，保留白名单条目；以前未指定 ondelete 会触发默认 NO ACTION。
    # requires alembic migration: alter foreign key to ondelete SET NULL
    invited_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AuthSession(Base, TimestampMixin):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    ua: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SystemPrompt(Base, TimestampMixin):
    """用户维护的系统提示词方案；全局默认由 users.default_system_prompt_id 指向。"""

    __tablename__ = "system_prompts"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_system_prompts_user_name"),
        Index("ix_system_prompts_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)


# ---------- BYOK Supplier Templates / User Credentials ----------


class ApiSupplierTemplate(Base, TimestampMixin, SoftDeleteMixin):
    """Admin-managed BYOK supplier template.

    This stores a trusted upstream URL and model/capability metadata only. It
    never stores an administrator API key and is intentionally separate from
    the global Provider Pool in ``system_settings.providers``.
    """

    __tablename__ = "api_supplier_templates"
    # review #5：slug 全表唯一会阻塞软删后再次创建同名；改为 partial unique index
    # `uq_api_supplier_templates_slug_active`，仅在 deleted_at IS NULL 时唯一。
    # 该索引在 migration 0019 里维护；ORM 仅声明非唯一 helper index 用于查询。
    __table_args__ = (
        Index("ix_api_supplier_templates_enabled", "enabled", "deleted_at"),
        Index(
            "uq_api_supplier_templates_slug_active",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )
    public_signup_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    user_bind_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )
    purposes: Mapped[list[str]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    validation_model: Mapped[str] = mapped_column(
        String(64), nullable=False, default="gpt-5.4", server_default="gpt-5.4"
    )
    default_chat_model: Mapped[str] = mapped_column(
        String(64), nullable=False, default="gpt-5.4", server_default="gpt-5.4"
    )
    # review #12：image 任务用 default_chat_model 在 chat-only supplier 上会错配。
    # 显式独立列；nullable=True 因为并非所有 supplier 都支持 image 生成，
    # 由 admin 在创建/编辑 supplier 时显式选填。
    default_image_model: Mapped[str | None] = mapped_column(
        String(128), nullable=True, default=None
    )
    fast_chat_model: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default="gpt-5.4-mini"
    )
    validation_timeout_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15_000, server_default="15000"
    )
    proxy_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    text_concurrency_per_key: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default="4"
    )
    image_concurrency_per_key: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    capabilities_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # review #23：双向关系，避免 worker / route 手写 JOIN 时漏 deleted_at 过滤；
    # lazy="raise" 强制显式 selectinload / joinedload，避免 N+1。
    credentials: Mapped[list["UserApiCredential"]] = relationship(
        "UserApiCredential",
        back_populates="supplier",
        lazy="raise",
    )


class UserApiCredential(Base, TimestampMixin, SoftDeleteMixin):
    """Encrypted user-owned API key bound to one supplier template."""

    __tablename__ = "user_api_credentials"
    __table_args__ = (
        Index("ix_user_api_credentials_user_status", "user_id", "status"),
        Index("ix_user_api_credentials_supplier", "supplier_id"),
        # review #2：上游 401 反查 / dedup 命中 key_hash 直接走索引。
        Index("ix_user_api_credentials_key_hash", "key_hash"),
        Index(
            "uq_user_api_credentials_one_active",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active' AND deleted_at IS NULL"),
            sqlite_where=text("status = 'active' AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # review #29：supplier 真删极少（admin UI 只软删）；保留 RESTRICT 阻止
    # 误把活跃绑定的 supplier 物理删掉。软删走 deleted_at，FK 不会触发。
    supplier_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("api_supplier_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    key_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    key_hint: Mapped[str] = mapped_column(String(64), nullable=False)
    encryption_key_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="v1", server_default="v1"
    )
    # review #14：用 PG enum 类型，与 migration 同步定义。create_type=False
    # 确保 ORM metadata create_all 不重复 CREATE TYPE（migration 已建好）。
    status: Mapped[str] = mapped_column(
        SAEnum(
            *USER_API_CREDENTIAL_STATUSES,
            name="user_api_credential_status",
            create_type=False,
        ),
        nullable=False,
        default="active",
        server_default="active",
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rate_limited_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    limit_5h_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    limit_1d_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    limit_7d_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    capabilities_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )

    # review #23：back_populates 双向；lazy="raise" 强制显式 eager load。
    supplier: Mapped["ApiSupplierTemplate"] = relationship(
        "ApiSupplierTemplate",
        back_populates="credentials",
    )
    user: Mapped["User"] = relationship(
        "User",
        back_populates="api_credentials",
        foreign_keys=[user_id],
    )


class PendingApiKeyVerification(Base, TimestampMixin):
    """Short-lived one-time token for key-first signup."""

    __tablename__ = "pending_api_key_verifications"
    __table_args__ = (
        UniqueConstraint(
            "token_hash", name="uq_pending_api_key_verifications_token_hash"
        ),
        Index("ix_pending_api_key_verifications_expires", "expires_at"),
        Index("ix_pending_api_key_verifications_supplier", "supplier_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    supplier_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("api_supplier_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    key_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    key_hint: Mapped[str] = mapped_column(String(64), nullable=False)
    challenge_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ua_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


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


# ---------- Tasks (generation / completion) ----------


class Generation(Base, TimestampMixin):
    __tablename__ = "generations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_gen_user_idemp"),
        Index("ix_gen_user_status_created", "user_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # generate/edit
    model: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_CHAT_MODEL
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    size_requested: Mapped[str] = mapped_column(String(32), nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(16), nullable=False)
    # PG ARRAY 字面量必须是 ARRAY[]::type 或 '{}' 文本字面量，不能是裸字符串 "{}"（被当作非法字符串字面量）。
    # requires alembic migration to alter server_default to ARRAY[]::varchar[]
    input_image_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    primary_input_image_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    # 局部 inpaint mask 引用（PostMessageIn.mask_image_id）。指向 images.id；
    # 不加 FK 约束，与 input_image_ids 保持一致：图片记录可能后续被软删，
    # 但 generation 行需要保留历史记录。worker 读到该字段后从存储拉 mask PNG。
    mask_image_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    upstream_request: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType(), nullable=True
    )
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
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    upstream_pixels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # review #23：方便 worker / route 通过 generation 反查 BYOK 凭证；
    # 没有 back_populates，单向即可（避免 UserApiCredential 上挂海量任务列表）。
    user_api_credential: Mapped["UserApiCredential | None"] = relationship(
        "UserApiCredential",
        foreign_keys="Generation.user_api_credential_id",
        lazy="raise",
    )

    @property
    def parent_generation_id(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("parent_generation_id")
        return value if isinstance(value, str) and value else None

    @property
    def diagnostics(self) -> dict[str, Any]:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        raw = request.get("generation_diagnostics")
        if isinstance(raw, dict):
            return raw
        out: dict[str, Any] = {}
        for key in (
            "revised_prompt",
            "requested_params",
            "effective_params",
            "provider_attempts",
            "provider",
            "actual_provider",
            "upstream_route",
            "actual_route",
            "actual_endpoint",
            "proxy_name",
            "proxy_enabled",
            "duration_ms",
            "upstream_duration_ms",
            "failover",
            "failover_count",
            "debug_id",
            "trace_id",
            "request_id",
            "safe_error_summary",
            "upstream_error_summary",
            "error_summary",
        ):
            if key in request:
                out[key] = request[key]
        return out

    @property
    def revised_prompt(self) -> str | None:
        value = self.diagnostics.get("revised_prompt")
        return value if isinstance(value, str) and value else None

    @property
    def requested_params(self) -> dict[str, Any] | None:
        value = self.diagnostics.get("requested_params")
        return value if isinstance(value, dict) else None

    @property
    def effective_params(self) -> dict[str, Any] | None:
        value = self.diagnostics.get("effective_params")
        return value if isinstance(value, dict) else None

    @property
    def provider_attempts(self) -> list[dict[str, Any]]:
        value = self.diagnostics.get("provider_attempts")
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @property
    def source(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("source")
        return value if isinstance(value, str) and value else None

    @property
    def action_source(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("action_source")
        return value if isinstance(value, str) and value else None

    @property
    def trace_id(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("trace_id") or self.diagnostics.get("trace_id")
        return value if isinstance(value, str) and value else None

    @property
    def attachment_roles(self) -> list[dict[str, Any]]:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("attachment_roles")
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @property
    def source_image_id(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("source_image_id") or request.get("primary_input_image_id")
        return (
            value if isinstance(value, str) and value else self.primary_input_image_id
        )

    @cached_property
    def queue_metadata(self) -> dict[str, Any]:
        return generation_queue_metadata(
            upstream_request=self.upstream_request,
            action=self.action,
            size_requested=self.size_requested,
            mask_image_id=self.mask_image_id,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            upstream_pixels=self.upstream_pixels,
            now=datetime.now(timezone.utc),
        )

    @property
    def queue_lane(self) -> str | None:
        value = self.queue_metadata.get("queue_lane")
        return value if isinstance(value, str) and value else None

    @property
    def workflow_type(self) -> str | None:
        value = self.queue_metadata.get("workflow_type")
        return value if isinstance(value, str) and value else None

    @property
    def workflow_step_key(self) -> str | None:
        value = self.queue_metadata.get("workflow_step_key")
        return value if isinstance(value, str) and value else None

    @property
    def pixel_count(self) -> int | None:
        value = self.queue_metadata.get("pixel_count")
        return value if isinstance(value, int) and value >= 0 else None

    @property
    def size_bucket(self) -> str | None:
        value = self.queue_metadata.get("size_bucket")
        return value if isinstance(value, str) and value else None

    @property
    def cost_class(self) -> str | None:
        value = self.queue_metadata.get("cost_class")
        return value if isinstance(value, str) and value else None

    @property
    def queue_wait_ms(self) -> int | None:
        value = self.queue_metadata.get("queue_wait_ms")
        return value if isinstance(value, int) and value >= 0 else None


class Completion(Base, TimestampMixin):
    __tablename__ = "completions"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_comp_user_idemp"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_CHAT_MODEL
    )
    # 见 Generation.input_image_ids 注释。
    # requires alembic migration to alter server_default to ARRAY[]::varchar[]
    input_image_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_request: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType(), nullable=True
    )
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
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_creation_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_creation_5m_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_creation_1h_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reasoning_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    image_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # review #23：与 Generation.user_api_credential 对称，方便从 completion 反查
    # BYOK 凭证；单向，避免 UserApiCredential 挂海量历史任务列表。
    user_api_credential: Mapped["UserApiCredential | None"] = relationship(
        "UserApiCredential",
        foreign_keys="Completion.user_api_credential_id",
        lazy="raise",
    )

    @property
    def source(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("source")
        return value if isinstance(value, str) and value else None

    @property
    def action_source(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("action_source")
        return value if isinstance(value, str) and value else None

    @property
    def trace_id(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("trace_id")
        return value if isinstance(value, str) and value else None

    @property
    def queue_metadata(self) -> dict[str, Any]:
        return completion_queue_metadata(
            upstream_request=self.upstream_request,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            now=datetime.now(timezone.utc),
        )

    @property
    def queue_lane(self) -> str | None:
        value = self.queue_metadata.get("queue_lane")
        return value if isinstance(value, str) and value else None

    @property
    def workflow_type(self) -> str | None:
        value = self.queue_metadata.get("workflow_type")
        return value if isinstance(value, str) and value else None

    @property
    def workflow_step_key(self) -> str | None:
        value = self.queue_metadata.get("workflow_step_key")
        return value if isinstance(value, str) and value else None

    @property
    def pixel_count(self) -> int | None:
        value = self.queue_metadata.get("pixel_count")
        return value if isinstance(value, int) else None

    @property
    def size_bucket(self) -> str | None:
        value = self.queue_metadata.get("size_bucket")
        return value if isinstance(value, str) and value else None

    @property
    def cost_class(self) -> str | None:
        value = self.queue_metadata.get("cost_class")
        return value if isinstance(value, str) and value else None

    @property
    def queue_wait_ms(self) -> int | None:
        value = self.queue_metadata.get("queue_wait_ms")
        return value if isinstance(value, int) else None


class VideoGeneration(Base, TimestampMixin):
    __tablename__ = "video_generations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_video_gen_user_idemp"),
        Index("ix_video_gen_user_status_created", "user_id", "status", "created_at"),
        Index("ix_video_gen_status_next_poll", "status", "next_poll_at"),
        Index(
            "uq_video_gen_provider_task",
            "provider_kind",
            "provider_name",
            "provider_task_id",
            unique=True,
            postgresql_where=text("provider_task_id IS NOT NULL"),
            sqlite_where=text("provider_task_id IS NOT NULL"),
        ),
        CheckConstraint(
            "duration_s = -1 OR (duration_s >= 4 AND duration_s <= 15)",
            name="ck_video_gen_duration_positive",
        ),
        CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="ck_video_gen_progress_pct",
        ),
        CheckConstraint(
            "est_cost_micro >= 0",
            name="ck_video_gen_est_cost_nonnegative",
        ),
        CheckConstraint(
            "est_token_upper >= 0",
            name="ck_video_gen_est_tokens_nonnegative",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    input_image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    input_image_storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    duration_s: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String(16), nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(16), nullable=False)
    fps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generate_audio: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    watermark: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    upstream_request: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType(), nullable=True
    )
    upstream_response: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType(), nullable=True
    )
    diagnostics: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    progress_pct: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    poll_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    est_token_upper: Mapped[int] = mapped_column(BigInteger, nullable=False)
    est_cost_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    billed_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    billed_cost_micro: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------- Images ----------


class Image(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "images"
    __table_args__ = (
        Index("ix_images_parent", "parent_image_id"),
        Index("ix_images_user_alive_created", "user_id", "deleted_at", "created_at"),
        UniqueConstraint("storage_key", name="uq_images_storage_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 删除 generation 后保留图片记录，避免误删用户已下载/分享的资源；保守起见保留 SET NULL。
    owner_generation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("generations.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # generated/uploaded
    # 父图被删除时保留 edit 派生图（用户可能仍想找回），同样 SET NULL 而不是 RESTRICT。
    parent_image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    blurhash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nsfw_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="private"
    )
    metadata_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )


class Video(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "videos"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_videos_storage_key"),
        UniqueConstraint("poster_storage_key", name="uq_videos_poster_storage_key"),
        Index("ix_videos_user_alive_created", "user_id", "deleted_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    owner_generation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("video_generations.id", ondelete="SET NULL"),
        nullable=True,
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    poster_storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime: Mapped[str] = mapped_column(
        String(64), nullable=False, default="video/mp4", server_default="video/mp4"
    )
    width: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    height: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    etag: Mapped[str] = mapped_column(String(96), nullable=False)
    has_audio: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    faststart: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="private", server_default="private"
    )
    metadata_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )


class ImageVariant(Base, TimestampMixin):
    __tablename__ = "image_variants"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_image_variants_storage_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    image_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="CASCADE"), nullable=False
    )
    # thumb256 / preview1024 / display2048
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------- Structured Workflows ----------


class WorkflowRun(Base, TimestampMixin, SoftDeleteMixin):
    """A resumable structured workflow project.

    Workflows are intentionally separate from normal chat messages. A run may
    use a backing conversation for generation/completion tasks, but the current
    stage, approvals, candidate models, and QC reports live here.
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index(
            "ix_workflow_runs_user_status_updated", "user_id", "status", "updated_at"
        ),
        Index("ix_workflow_runs_user_type_updated", "user_id", "type", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    product_image_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    current_step: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="premium"
    )
    metadata_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )


class WorkflowStep(Base, TimestampMixin):
    """Per-stage state for a structured workflow run."""

    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id", "step_key", name="uq_workflow_steps_run_key"
        ),
        Index("ix_workflow_steps_run_key", "workflow_run_id", "step_key"),
        Index("ix_workflow_steps_run_status", "workflow_run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    workflow_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="waiting_input"
    )
    input_json: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    output_json: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    task_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    image_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ModelCandidate(Base, TimestampMixin):
    """Synthetic model option generated before garment try-on."""

    __tablename__ = "model_candidates"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id", "candidate_index", name="uq_model_candidates_run_index"
        ),
        Index("ix_model_candidates_run_status", "workflow_run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    workflow_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    candidate_index: Mapped[int] = mapped_column(Integer, nullable=False)
    portrait_image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    front_image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    side_image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    back_image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    contact_sheet_image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    model_brief_json: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    task_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    selected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class QualityReport(Base, TimestampMixin):
    """Automatic QC result for a generated workflow image."""

    __tablename__ = "quality_reports"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id", "image_id", name="uq_quality_reports_run_image"
        ),
        Index(
            "ix_quality_reports_run_recommendation", "workflow_run_id", "recommendation"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    workflow_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    image_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="CASCADE"), nullable=False
    )
    overall_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    product_fidelity_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    model_consistency_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    aesthetic_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    artifact_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issues_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    recommendation: Mapped[str] = mapped_column(
        String(32), nullable=False, default="review"
    )


# ---------- Shares ----------


class Share(Base, TimestampMixin):
    __tablename__ = "shares"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    image_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="CASCADE"), nullable=False
    )
    image_ids: Mapped[list[str]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    token: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    show_prompt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ---------- Outbox (DESIGN §6.1) ----------


class OutboxEvent(Base, TimestampMixin):
    """Transactional outbox：API 在写 generations/completions 的同一事务里
    INSERT 一条 outbox_events(published_at=NULL)；后台 publisher 周期扫描未发布
    的事件 XADD 到 Redis Stream，保证"关窗不丢"。"""

    __tablename__ = "outbox_events"
    # publisher 按 kind 分通道扫描；带上 kind 后索引可走 (kind, published_at IS NULL) 高效裁剪。
    # requires alembic migration: drop & recreate index ix_outbox_unpublished with leading kind column
    __table_args__ = (
        Index("ix_outbox_unpublished", "kind", "published_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # generation/completion
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ---------- Billing / Wallet ----------


class UserWallet(Base, TimestampMixin):
    __tablename__ = "user_wallets"
    # Why: no `balance_micro >= 0` CHECK — graylist overdraw paths (admin opens
    # `billing.allow_negative_balance=1`) legitimately need negative balances.
    # The application-side `allow_negative=False` default is the gate.
    __table_args__ = (
        CheckConstraint("hold_micro >= 0", name="ck_user_wallet_hold_nonnegative"),
    )

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    balance_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    hold_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    lifetime_topup_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    lifetime_spend_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    billing_rate_multiplier: Mapped[float] = mapped_column(
        Numeric(8, 4),
        nullable=False,
        default=1.0,
        server_default="1.0000",
    )


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_wallet_tx_idemp"),
        Index("ix_wallet_tx_user_created", "user_id", "created_at"),
        Index("ix_wallet_tx_ref", "ref_type", "ref_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    hold_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_admin: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class PricingRule(Base, TimestampMixin):
    __tablename__ = "pricing_rules"
    __table_args__ = (
        UniqueConstraint(
            "scope",
            "key",
            "variant",
            "unit",
            name="uq_pricing_scope_key_variant_unit",
        ),
        CheckConstraint("price_micro >= 0", name="ck_pricing_price_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    variant: Mapped[str] = mapped_column(
        String(32), nullable=False, default="default", server_default="default"
    )
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    price_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class RedemptionCode(Base, TimestampMixin):
    __tablename__ = "redemption_codes"
    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_redemption_codes_code_hash"),
        CheckConstraint("amount_micro > 0", name="ck_redemption_amount_positive"),
        CheckConstraint("max_redemptions >= 1", name="ck_redemption_max_positive"),
        Index("ix_redemption_codes_batch", "batch_id"),
        Index("ix_redemption_codes_status", "revoked_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    amount_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_redemptions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    redeemed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class RedemptionCodeUsage(Base):
    __tablename__ = "redemption_codes_usage"
    __table_args__ = (
        UniqueConstraint("code_id", "user_id", name="uq_redeem_code_user"),
        Index("ix_redeem_user_time", "user_id", "redeemed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    code_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("redemption_codes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    amount_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    wallet_tx_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("wallet_transactions.id"), nullable=False
    )
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ---------- Invite Links（V1.0 收尾） ----------


class InviteLink(Base, TimestampMixin):
    """站长生成一次性邀请链接给朋友。"""

    __tablename__ = "invite_links"
    __table_args__ = (
        Index("ix_invite_links_token", "token"),
        Index("ix_invite_links_active", "revoked_at", "used_at"),
        UniqueConstraint("token", name="uq_invite_links_token"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    token: Mapped[str] = mapped_column(String(48), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    used_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ---------- System Settings（V1.0 收尾） ----------


class SystemSetting(Base, TimestampMixin):
    """管理员可调系统设置（Provider Pool、像素预算等）。

    DB 行只持久化 SUPPORTED_SETTINGS 列表里的 key；其它视为非法。
    """

    __tablename__ = "system_settings"
    __table_args__ = (UniqueConstraint("key", name="uq_system_settings_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------- Audit Logs（V1.0 收尾） ----------


class AuditLog(Base):
    """结构化审计日志双写：进程 logger.info 仍记，同时落 PG 留痕。

    `event_type` 形如 `auth.login.success` / `invite.create` / `admin.settings.update`。
    `details` 是 JSONB（避开 SQLAlchemy 保留字 metadata）。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_event_type", "event_type"),
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------- Telegram Bot 绑定 ----------


class TelegramBinding(Base, TimestampMixin):
    """TG chat_id ↔ Lumen user 的一对一绑定。

    bot 调 API 时带 X-Telegram-Chat-Id；中间件查这张表换出 user 上下文。
    一个 chat_id 只绑一个 user；同一 user 可换 chat（先 unbind 再 bind）。
    """

    __tablename__ = "telegram_bindings"
    __table_args__ = (
        UniqueConstraint("chat_id", name="uq_telegram_bindings_chat_id"),
        UniqueConstraint("user_id", name="uq_telegram_bindings_user_id"),
        Index("ix_telegram_bindings_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ---------- Apparel Model Library（V1.x 收藏 + 自动识别） ----------


class ModelLibraryItem(Base, TimestampMixin):
    """User-owned saved/favorited/generated apparel model.

    Replaces the per-user JSON index file. Each row is independent so
    concurrent favorites and concurrent vision auto-tag writes don't
    trample each other (the file-based design serialized everything
    through a single read-modify-write).

    Item ``id`` keeps the ``user:{uuid7}`` prefix the file index used so
    existing client links continue to resolve. Vision auto-tagging issues
    a single-row UPDATE per item — no whole-file rewrite.
    """

    __tablename__ = "model_library_items"
    __table_args__ = (
        Index("ix_model_library_items_user_age", "user_id", "age_segment"),
        Index("ix_model_library_items_user_source", "user_id", "source"),
        Index("ix_model_library_items_user_created", "user_id", "created_at"),
        Index("ix_model_library_items_image", "image_id"),
        Index(
            "ix_model_library_items_style_tags",
            "style_tags",
            postgresql_using="gin",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    image_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    age_segment: Mapped[str] = mapped_column(
        String(32), nullable=False, default="user_favorites"
    )
    gender: Mapped[str | None] = mapped_column(String(40), nullable=True)
    appearance_direction: Mapped[str | None] = mapped_column(String(80), nullable=True)
    style_tags: Mapped[list[str]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    library_folder: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_tagged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auto_tag_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )


class ModelLibraryHiddenPreset(Base):
    """Per-user hidden preset id list. Presets are global read-only, so a
    user-level "delete" really means "hide from this user's library list".
    """

    __tablename__ = "model_library_hidden_presets"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    preset_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    hidden_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------- Poster Style Library（V1.1 海报工作流） ----------


class PosterStyleItem(Base, TimestampMixin):
    """User-owned saved/favorited/generated poster visual style.

    Mirrors ModelLibraryItem but for visual styles rather than human models.
    Each row is an independent style entry whose ``cover_image_id`` points to
    a rendered preview demonstrating the style. ``prompt_template`` is
    injected into poster generation as a hard style constraint.
    """

    __tablename__ = "poster_style_items"
    __table_args__ = (
        Index("ix_poster_style_items_user_category", "user_id", "category"),
        Index("ix_poster_style_items_user_source", "user_id", "source"),
        Index("ix_poster_style_items_user_created", "user_id", "created_at"),
        Index("ix_poster_style_items_cover_image", "cover_image_id"),
        Index(
            "ix_poster_style_items_style_tags",
            "style_tags",
            postgresql_using="gin",
            postgresql_ops={"style_tags": "jsonb_path_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    cover_image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    sample_image_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="user_favorites"
    )
    mood: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    palette: Mapped[list[str]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    recommended_aspects: Mapped[list[str]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    style_tags: Mapped[list[str]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    library_folder: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auto_tagged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auto_tag_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )


class PosterStyleHiddenPreset(Base):
    """Per-user hidden poster-style preset list."""

    __tablename__ = "poster_style_hidden_presets"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    preset_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    hidden_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PosterMaster(Base, TimestampMixin):
    """Master candidate for a poster design workflow.

    Generated as N candidates per master step. Each candidate is a square
    image capturing the visual style; the user selects one and the remaining
    aspect ratios are re-rendered using the selected master as reference.
    """

    __tablename__ = "poster_masters"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id", "candidate_index", name="uq_poster_masters_run_index"
        ),
        Index("ix_poster_masters_run_status", "workflow_run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    workflow_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    candidate_index: Mapped[int] = mapped_column(Integer, nullable=False)
    image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    style_summary_json: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    task_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    selected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PosterRender(Base, TimestampMixin):
    """Final poster render at a target aspect ratio.

    Created during multi_size_generation step; each row corresponds to one
    aspect ratio output rendered from the selected master as reference.
    """

    __tablename__ = "poster_renders"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id", "aspect_ratio", name="uq_poster_renders_run_aspect"
        ),
        Index("ix_poster_renders_run_status", "workflow_run_id", "status"),
        Index("ix_poster_renders_master", "master_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    workflow_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    master_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("poster_masters.id", ondelete="SET NULL"), nullable=True
    )
    aspect_ratio: Mapped[str] = mapped_column(String(16), nullable=False)
    size: Mapped[str] = mapped_column(String(16), nullable=False)
    image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    task_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    metadata_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )


# ---------- Outbox Dead Letter（V1.0 收尾） ----------


class OutboxDeadLetter(Base):
    """Outbox/SSE publish 链路最终失败的事件持久化 DLQ；admin 端可列表 + 重试。"""

    __tablename__ = "outbox_dead_letter"
    __table_args__ = (
        Index("ix_outbox_dead_letter_failed_at", "failed_at"),
        Index("ix_outbox_dead_letter_resolved_at", "resolved_at"),
        Index("ix_outbox_dead_letter_event_type", "event_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    outbox_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("outbox_events.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = [
    "Base",
    "User",
    "AllowedEmail",
    "AuthSession",
    "SystemPrompt",
    "Conversation",
    "Message",
    "UserMemoryScope",
    "UserMemory",
    "UserMemoryStaging",
    "MemoryAudit",
    "Generation",
    "Completion",
    "Image",
    "ImageVariant",
    "WorkflowRun",
    "WorkflowStep",
    "ModelCandidate",
    "ModelLibraryItem",
    "ModelLibraryHiddenPreset",
    "PosterStyleItem",
    "PosterStyleHiddenPreset",
    "PosterMaster",
    "PosterRender",
    "QualityReport",
    "Share",
    "OutboxEvent",
    "InviteLink",
    "SystemSetting",
    "AuditLog",
    "OutboxDeadLetter",
    "TelegramBinding",
    "ApiSupplierTemplate",
    "UserApiCredential",
    "PendingApiKeyVerification",
    "USER_API_CREDENTIAL_STATUSES",
    "new_uuid7",
]
