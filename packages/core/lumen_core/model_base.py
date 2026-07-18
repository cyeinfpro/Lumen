"""Shared SQLAlchemy declarative base, mixins, and primary-key factory."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_uuid7() -> str:
    uuid7 = import_module("uuid_extensions").uuid7
    return str(uuid7())


class Base(DeclarativeBase):
    pass


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


__all__ = ["Base", "SoftDeleteMixin", "TimestampMixin", "new_uuid7"]
