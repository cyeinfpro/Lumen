"""Apparel model, poster style, poster render, and dead-letter entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..model_base import Base, TimestampMixin, new_uuid7
from ..sqltypes import JsonType, StringListType

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
    "ModelLibraryItem",
    "ModelLibraryHiddenPreset",
    "PosterStyleItem",
    "PosterStyleHiddenPreset",
    "PosterMaster",
    "PosterRender",
    "OutboxDeadLetter",
]
