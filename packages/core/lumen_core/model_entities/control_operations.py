"""Durable Telegram delivery and control-plane operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal, TypedDict

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
)
from sqlalchemy.orm import Mapped, mapped_column

from ..model_base import Base, TimestampMixin, new_uuid7
from ..sqltypes import JsonType


TELEGRAM_CONTROL_EFFECT_PROTOCOL_VERSION: Final = 1
TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY: Final = "effect_receipt_v1"
TELEGRAM_CONTROL_RESTART_INTENT_KEY: Final = "restart_intent_v1"


class TelegramControlEffectReceipt(TypedDict, total=False):
    """Durable external-effect receipt stored in the command payload."""

    idempotency_key: str
    state: Literal[
        "dispatching",
        "succeeded",
        "outcome_unknown",
        "retryable",
    ]
    owner: str
    fence: int
    attempt: int
    started_at: str
    completed_at: str
    error: str
    reconciled_at: str
    reconciliation: Literal["succeeded", "retry"]
    reconciliation_note: str


class TelegramControlRestartIntent(TypedDict, total=False):
    """Cross-process restart intent stored without requiring a schema change."""

    state: Literal["stop_intent_committed", "new_generation_ready"]
    requested_generation: str
    committed_at: str
    completed_by_generation: str
    ready_at: str


class TelegramDeliveryAttempt(Base, TimestampMixin):
    """PostgreSQL proof for one Telegram generation/image delivery."""

    __tablename__ = "telegram_delivery_attempts"
    __table_args__ = (
        UniqueConstraint(
            "generation_id",
            "image_id",
            name="uq_tg_delivery_generation_image",
        ),
        CheckConstraint(
            "state IN ('dispatching','delivered','failed_before_accept',"
            "'delivery_result_unknown')",
            name="ck_tg_delivery_attempt_state",
        ),
        Index(
            "ix_tg_delivery_attempt_state_updated",
            "state",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    generation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("generations.id", ondelete="CASCADE"),
        nullable=False,
    )
    image_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("images.id", ondelete="CASCADE"),
        nullable=False,
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dispatch_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class TelegramControlCommand(Base, TimestampMixin):
    """Durable source of truth for commands transported over Redis Streams."""

    __tablename__ = "telegram_control_commands"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','published','accepted','failed')",
            name="ck_tg_control_command_status",
        ),
        CheckConstraint(
            "(status IN ('pending','published') AND active_slot = 1) OR "
            "(status IN ('accepted','failed') AND active_slot IS NULL)",
            name="ck_tg_control_command_active_slot",
        ),
        UniqueConstraint(
            "target",
            "active_slot",
            name="uq_tg_control_target_active",
        ),
        Index(
            "ix_tg_control_dispatch_due",
            "status",
            "publish_lease_until",
            "created_at",
        ),
        CheckConstraint(
            "effect_status IN ('pending','running','succeeded','failed')",
            name="ck_tg_control_effect_status",
        ),
        CheckConstraint(
            "status IN ('pending','published') "
            "OR effect_status IN ('succeeded','failed')",
            name="ck_tg_control_effect_active_command",
        ),
        Index(
            "ix_tg_control_effect_due",
            "effect_status",
            "effect_lease_until",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    command: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JsonType(),
        nullable=False,
        default=dict,
        server_default="{}",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    requested_by: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    stream_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_slot: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        default=1,
        server_default="1",
    )
    publish_owner: Mapped[str | None] = mapped_column(String(96), nullable=True)
    publish_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    publish_fence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    publish_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    effect_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    effect_owner: Mapped[str | None] = mapped_column(String(96), nullable=True)
    effect_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    effect_fence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    effect_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    effect_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    effect_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class TelegramDeliveryQuarantine(Base, TimestampMixin):
    """Operator-visible durable record for a quarantined bot stream entry."""

    __tablename__ = "telegram_delivery_quarantines"
    __table_args__ = (
        UniqueConstraint(
            "source_stream",
            "source_id",
            name="uq_tg_quarantine_source_entry",
        ),
        CheckConstraint(
            "status IN ('pending','redrive_queued','resolved')",
            name="ck_tg_quarantine_status",
        ),
        Index(
            "ix_tg_quarantine_status_created",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    source_stream: Mapped[str] = mapped_column(String(255), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stream_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload_raw: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    redrive_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    redrive_command_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("telegram_control_commands.id", ondelete="SET NULL"),
        nullable=True,
    )
    redis_stream_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cleaned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


__all__ = [
    "TELEGRAM_CONTROL_EFFECT_PROTOCOL_VERSION",
    "TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY",
    "TELEGRAM_CONTROL_RESTART_INTENT_KEY",
    "TelegramControlCommand",
    "TelegramControlEffectReceipt",
    "TelegramControlRestartIntent",
    "TelegramDeliveryAttempt",
    "TelegramDeliveryQuarantine",
]
