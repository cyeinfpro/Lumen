"""Media, structured workflow, share, and outbox persistence entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..model_base import Base, SoftDeleteMixin, TimestampMixin, new_uuid7
from ..sqltypes import JsonType, StringListType

# ---------- Images ----------


class Image(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "images"
    __table_args__ = (
        Index("ix_images_parent", "parent_image_id"),
        Index("ix_images_user_alive_created", "user_id", "deleted_at", "created_at"),
        Index(
            "ix_images_owner_alive_created",
            "owner_generation_id",
            "deleted_at",
            "created_at",
            "id",
        ),
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
        Index(
            "ix_videos_owner_alive_created",
            "owner_generation_id",
            "deleted_at",
            "created_at",
            "id",
        ),
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
        Index(
            "ix_outbox_unpublished_created",
            "created_at",
            "id",
            postgresql_where=text("published_at IS NULL"),
            sqlite_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # generation/completion
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = [
    "Image",
    "Video",
    "ImageVariant",
    "WorkflowRun",
    "WorkflowStep",
    "ModelCandidate",
    "QualityReport",
    "Share",
    "OutboxEvent",
]
