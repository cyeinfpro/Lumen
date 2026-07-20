"""Identity, authentication, and BYOK persistence entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..model_base import Base, SoftDeleteMixin, TimestampMixin, new_uuid7
from ..sqltypes import JsonType

# review #14：BYOK 凭证状态全局唯一来源；migration / ORM / route / worker 共用。
USER_API_CREDENTIAL_STATUSES: tuple[str, ...] = (
    "active",
    "invalid",
    "replaced",
    "revoked",
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
        Float, default=0.80, nullable=False, server_default=text("0.80")
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
        UniqueConstraint(
            "id",
            "user_id",
            name="uq_user_api_credentials_id_user",
        ),
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


__all__ = [
    "USER_API_CREDENTIAL_STATUSES",
    "User",
    "AllowedEmail",
    "AuthSession",
    "SystemPrompt",
    "ApiSupplierTemplate",
    "UserApiCredential",
    "PendingApiKeyVerification",
]
