"""SQLAlchemy ORM 模型——DESIGN §4 的所有核心表。

所有表：uuid7 主键 + created_at / updated_at + 软删 deleted_at。
所有 id 字段存储为 str（便于 Redis / SSE 消息序列化），用 uuid7 保留时间有序性。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .constants import DEFAULT_CHAT_MODEL


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
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_providers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
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
    # 软删活跃用户索引：admin 列表 / 登录查询都按 deleted_at IS NULL 过滤。
    # requires alembic migration: create index ix_users_alive on users(deleted_at)
    __table_args__ = (
        Index("ix_users_alive", "deleted_at"),
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
    refresh_token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    ua: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    default_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_system_prompt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("system_prompts.id", ondelete="SET NULL"), nullable=True
    )
    # { up_to_message_id, text, tokens, updated_at }
    summary_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conv_created", "conversation_id", "created_at"),
        Index("ix_messages_conv_alive_created", "conversation_id", "deleted_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user/assistant/system
    content: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    parent_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    conversation: Mapped[Conversation] = relationship(
        "Conversation", back_populates="messages"
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
    model: Mapped[str] = mapped_column(String(64), nullable=False, default=DEFAULT_CHAT_MODEL)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    size_requested: Mapped[str] = mapped_column(String(32), nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(16), nullable=False)
    # PG ARRAY 字面量必须是 ARRAY[]::type 或 '{}' 文本字面量，不能是裸字符串 "{}"（被当作非法字符串字面量）。
    # requires alembic migration to alter server_default to ARRAY[]::varchar[]
    input_image_ids: Mapped[list[str]] = mapped_column(
        ARRAY(String(36)),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    primary_input_image_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    upstream_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
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
        ARRAY(String(36)),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # generated/uploaded
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
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="private")
    metadata_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
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


# ---------- Shares ----------

class Share(Base, TimestampMixin):
    __tablename__ = "shares"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    image_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="CASCADE"), nullable=False
    )
    image_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
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
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # generation/completion
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
    __table_args__ = (
        UniqueConstraint("key", name="uq_system_settings_key"),
    )

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
        JSONB, nullable=False, default=dict, server_default="{}"
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
        JSONB, nullable=False, default=dict, server_default="{}"
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
    "Generation",
    "Completion",
    "Image",
    "ImageVariant",
    "Share",
    "OutboxEvent",
    "InviteLink",
    "SystemSetting",
    "AuditLog",
    "OutboxDeadLetter",
    "TelegramBinding",
    "new_uuid7",
]
