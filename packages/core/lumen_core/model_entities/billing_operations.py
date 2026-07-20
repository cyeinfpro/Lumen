"""Billing, invite, settings, audit, and Telegram persistence entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..model_base import Base, TimestampMixin, new_uuid7
from ..sqltypes import JsonType

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
        Index(
            "ix_wallet_tx_user_ref_kind",
            "user_id",
            "ref_type",
            "ref_id",
            "kind",
            "created_at",
            "id",
        ),
        Index(
            "ix_wallet_hold_created",
            "created_at",
            "id",
            postgresql_where=text("kind = 'hold'"),
            sqlite_where=text("kind = 'hold'"),
        ),
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


class BillingWindowUsageEvent(Base):
    """Durable source of truth for per-credential billing windows."""

    __tablename__ = "billing_window_usage_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["credential_id", "user_id"],
            ["user_api_credentials.id", "user_api_credentials.user_id"],
            name="fk_billing_window_credential_user",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "amount_micro > 0",
            name="ck_billing_window_amount_positive",
        ),
        Index(
            "ix_billing_window_credential_created",
            "credential_id",
            "created_at",
        ),
        Index("ix_billing_window_user_created", "user_id", "created_at"),
    )

    wallet_transaction_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("wallet_transactions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    credential_id: Mapped[str] = mapped_column(String(36), nullable=False)
    amount_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
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
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class RedemptionBatch(Base, TimestampMixin):
    __tablename__ = "redemption_batches"
    __table_args__ = (
        UniqueConstraint(
            "created_by",
            "idempotency_key",
            name="uq_redemption_batch_creator_idemp",
        ),
        CheckConstraint(
            "amount_micro > 0",
            name="ck_redemption_batch_amount_positive",
        ),
        CheckConstraint(
            "code_count >= 1",
            name="ck_redemption_batch_count_positive",
        ),
        CheckConstraint(
            "max_redemptions >= 1",
            name="ck_redemption_batch_max_positive",
        ),
        Index(
            "ix_redemption_batches_creator_created",
            "created_by",
            "created_at",
        ),
        Index(
            "ix_redemption_batches_creator_request_created",
            "created_by",
            "request_hash",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    code_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_redemptions: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
        Index(
            "ix_audit_logs_billing_created",
            "created_at",
            "id",
            postgresql_where=text(
                "event_type LIKE 'wallet.%' OR event_type LIKE 'redemption.%' OR event_type LIKE 'billing.%'"
            ),
            sqlite_where=text(
                "event_type LIKE 'wallet.%' OR event_type LIKE 'redemption.%' OR event_type LIKE 'billing.%'"
            ),
        ),
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
    tg_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = [
    "UserWallet",
    "WalletTransaction",
    "BillingWindowUsageEvent",
    "PricingRule",
    "RedemptionBatch",
    "RedemptionCode",
    "RedemptionCodeUsage",
    "InviteLink",
    "SystemSetting",
    "AuditLog",
    "TelegramBinding",
]
