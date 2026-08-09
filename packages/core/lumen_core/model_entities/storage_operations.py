"""Durable control-plane operations for host storage changes."""

from __future__ import annotations

from datetime import datetime

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

from ..model_base import Base, TimestampMixin


class StorageApplyOperation(Base, TimestampMixin):
    """Desired storage change awaiting a terminal host result."""

    __tablename__ = "storage_apply_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','dispatched','succeeded','failed')",
            name="ck_storage_apply_operations_status",
        ),
        CheckConstraint(
            "(status IN ('pending','dispatched') AND active_slot = 1) OR "
            "(status IN ('succeeded','failed') AND active_slot IS NULL)",
            name="ck_storage_apply_operations_active_slot",
        ),
        UniqueConstraint("active_slot", name="uq_storage_apply_one_active"),
        Index(
            "ix_storage_apply_dispatch_due",
            "status",
            "dispatch_lease_until",
            "created_at",
        ),
        Index(
            "ix_storage_apply_next_attempt",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    requested_by: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    desired_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    active_slot: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        default=1,
        server_default="1",
    )
    dispatch_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    dispatch_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatch_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    dispatch_fence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="Globally monotonic host adoption fence assigned at dispatch claim.",
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    result_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    host_started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    host_finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    failure_class: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )


__all__ = ["StorageApplyOperation"]
