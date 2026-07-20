"""SQLAlchemy models for the infinite-canvas persistence boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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

from .model_base import Base, SoftDeleteMixin, TimestampMixin, new_uuid7
from .sqltypes import JsonType, StringListType


class CanvasDocument(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "canvas_documents"
    __table_args__ = (
        Index(
            "ix_canvas_documents_user_deleted_updated",
            "user_id",
            "deleted_at",
            "updated_at",
            "id",
        ),
        Index("ix_canvas_documents_user_title", "user_id", "title"),
        CheckConstraint("revision >= 1", name="ck_canvas_documents_revision"),
        CheckConstraint(
            "graph_schema_version >= 1",
            name="ck_canvas_documents_graph_schema_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    graph_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    graph_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    last_version_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "canvas_versions.id",
            name="fk_canvas_documents_last_version",
            use_alter=True,
        ),
        nullable=True,
    )
    thumbnail_image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )


class CanvasMutation(Base):
    __tablename__ = "canvas_mutations"
    __table_args__ = (
        UniqueConstraint(
            "canvas_id",
            "client_id",
            "mutation_id",
            name="uq_canvas_mutations_client_mutation",
        ),
        UniqueConstraint(
            "canvas_id",
            "result_revision",
            name="uq_canvas_mutations_result_revision",
        ),
        Index(
            "ix_canvas_mutations_revision_window",
            "canvas_id",
            "base_revision",
            "result_revision",
        ),
        CheckConstraint(
            "result_revision = base_revision + 1",
            name="ck_canvas_mutations_revision_step",
        ),
        CheckConstraint(
            "operation_schema_version >= 1",
            name="ck_canvas_mutations_operation_schema_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    canvas_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canvas_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mutation_id: Mapped[str] = mapped_column(String(96), nullable=False)
    operation_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    base_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operations_jsonb: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    response_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CanvasVersion(Base):
    __tablename__ = "canvas_versions"
    __table_args__ = (
        UniqueConstraint("canvas_id", "version_no", name="uq_canvas_versions_number"),
        Index("ix_canvas_versions_canvas_created", "canvas_id", "created_at"),
        Index("ix_canvas_versions_canvas_graph_hash", "canvas_id", "graph_hash"),
        CheckConstraint(
            "source_revision >= 1", name="ck_canvas_versions_source_revision"
        ),
        CheckConstraint("version_no >= 1", name="ck_canvas_versions_number"),
        CheckConstraint(
            "graph_schema_version >= 1",
            name="ck_canvas_versions_graph_schema_version",
        ),
        CheckConstraint(
            "kind IN ('run', 'named', 'restore', 'import', 'template')",
            name="ck_canvas_versions_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    canvas_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canvas_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    graph_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    graph_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_jsonb: Mapped[dict[str, Any]] = mapped_column(JsonType(), nullable=False)
    selection_snapshot_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    selection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CanvasRun(Base, TimestampMixin):
    __tablename__ = "canvas_runs"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_canvas_runs_user_idempotency"
        ),
        Index("ix_canvas_runs_canvas_created", "canvas_id", "created_at"),
        Index("ix_canvas_runs_user_status_updated", "user_id", "status", "updated_at"),
        CheckConstraint(
            "kind IN ('single', 'repair', 'upstream', 'selection', 'all')",
            name="ck_canvas_runs_kind",
        ),
        CheckConstraint(
            "status IN ("
            "'planning', 'queued', 'running', 'paused', 'reconciling', "
            "'canceling', 'succeeded', 'partial_failed', 'failed', 'canceled'"
            ")",
            name="ck_canvas_runs_status",
        ),
        CheckConstraint(
            "failure_policy IN ('continue_independent', 'fail_fast')",
            name="ck_canvas_runs_failure_policy",
        ),
        CheckConstraint("run_epoch >= 0", name="ck_canvas_runs_epoch"),
        CheckConstraint("last_event_seq >= 0", name="ck_canvas_runs_event_seq"),
        CheckConstraint("budget_micro >= 0", name="ck_canvas_runs_budget"),
        CheckConstraint("reserved_micro >= 0", name="ck_canvas_runs_reserved"),
        CheckConstraint("spent_micro >= 0", name="ck_canvas_runs_spent"),
        CheckConstraint(
            "estimated_cost_micro >= 0", name="ck_canvas_runs_estimated_cost"
        ),
        CheckConstraint(
            "actual_cost_micro IS NULL OR actual_cost_micro >= 0",
            name="ck_canvas_runs_actual_cost",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    canvas_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canvas_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canvas_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("canvas_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="planning", server_default="planning"
    )
    failure_policy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="continue_independent",
        server_default="continue_independent",
    )
    run_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    last_event_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    target_node_ids: Mapped[list[str]] = mapped_column(
        StringListType(64), nullable=False, default=list
    )
    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reserved_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    spent_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    estimated_cost_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    actual_cost_micro: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    summary_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CanvasNodeExecution(Base, TimestampMixin):
    __tablename__ = "canvas_node_executions"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "node_id",
            "attempt",
            name="uq_canvas_node_executions_attempt",
        ),
        UniqueConstraint(
            "user_id",
            "submission_idempotency_key",
            name="uq_canvas_node_executions_submission",
        ),
        Index(
            "ix_canvas_node_executions_canvas_node_created",
            "canvas_id",
            "node_id",
            "created_at",
        ),
        Index("ix_canvas_node_executions_run_sequence", "run_id", "sequence"),
        Index(
            "ix_canvas_node_executions_user_status_updated",
            "user_id",
            "status",
            "updated_at",
        ),
        CheckConstraint("sequence >= 0", name="ck_canvas_node_executions_sequence"),
        CheckConstraint("attempt >= 0", name="ck_canvas_node_executions_attempt"),
        CheckConstraint(
            "attempt_epoch >= 0", name="ck_canvas_node_executions_attempt_epoch"
        ),
        CheckConstraint(
            "selection_base_revision >= 0",
            name="ck_canvas_node_executions_selection_revision",
        ),
        CheckConstraint(
            "status IN ("
            "'pending', 'ready', 'queued', 'running', 'reconciling', "
            "'canceling', 'succeeded', 'partial_failed', 'failed', 'blocked', "
            "'canceled', 'skipped', 'reused'"
            ")",
            name="ck_canvas_node_executions_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    canvas_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canvas_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvas_runs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(48), nullable=False)
    node_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    submission_idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    config_snapshot_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False
    )
    input_snapshot_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False
    )
    model_snapshot_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    pricing_snapshot_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    processor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    outputs_jsonb: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType(), nullable=False, default=list, server_default="[]"
    )
    selection_base_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retry_of_execution_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("canvas_node_executions.id", ondelete="SET NULL"),
        nullable=True,
    )
    reused_from_execution_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("canvas_node_executions.id", ondelete="SET NULL"),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CanvasExecutionTask(Base, TimestampMixin):
    __tablename__ = "canvas_execution_tasks"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "ordinal",
            name="uq_canvas_execution_tasks_ordinal",
        ),
        UniqueConstraint(
            "task_kind",
            "idempotency_key",
            name="uq_canvas_execution_tasks_idempotency",
        ),
        Index(
            "uq_canvas_execution_tasks_generation",
            "generation_id",
            unique=True,
            postgresql_where=text("generation_id IS NOT NULL"),
            sqlite_where=text("generation_id IS NOT NULL"),
        ),
        Index(
            "uq_canvas_execution_tasks_completion",
            "completion_id",
            unique=True,
            postgresql_where=text("completion_id IS NOT NULL"),
            sqlite_where=text("completion_id IS NOT NULL"),
        ),
        Index(
            "uq_canvas_execution_tasks_video_generation",
            "video_generation_id",
            unique=True,
            postgresql_where=text("video_generation_id IS NOT NULL"),
            sqlite_where=text("video_generation_id IS NOT NULL"),
        ),
        CheckConstraint("ordinal >= 0", name="ck_canvas_execution_tasks_ordinal"),
        CheckConstraint(
            "("
            "task_kind = 'generation' AND generation_id IS NOT NULL "
            "AND completion_id IS NULL AND video_generation_id IS NULL"
            ") OR ("
            "task_kind = 'completion' AND generation_id IS NULL "
            "AND completion_id IS NOT NULL AND video_generation_id IS NULL"
            ") OR ("
            "task_kind = 'video_generation' AND generation_id IS NULL "
            "AND completion_id IS NULL AND video_generation_id IS NOT NULL"
            ")",
            name="ck_canvas_execution_tasks_task_owner",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'canceled', 'expired')",
            name="ck_canvas_execution_tasks_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canvas_node_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    task_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    generation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("generations.id", ondelete="RESTRICT"), nullable=True
    )
    completion_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("completions.id", ondelete="RESTRICT"), nullable=True
    )
    video_generation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("video_generations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    billing_ref_type: Mapped[str] = mapped_column(String(32), nullable=False)
    billing_ref_id: Mapped[str] = mapped_column(String(96), nullable=False)
    output_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )


class CanvasTaskTerminalReceipt(Base):
    __tablename__ = "canvas_task_terminal_receipts"
    __table_args__ = (
        UniqueConstraint(
            "task_kind",
            "task_id",
            "task_epoch",
            name="uq_canvas_task_terminal_receipts_epoch",
        ),
        CheckConstraint(
            "task_epoch >= 0", name="ck_canvas_task_terminal_receipts_epoch"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    execution_task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canvas_execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    terminal_status: Mapped[str] = mapped_column(String(32), nullable=False)
    terminal_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CanvasNodeSelection(Base):
    __tablename__ = "canvas_node_selections"
    __table_args__ = (
        CheckConstraint(
            "output_index >= 0", name="ck_canvas_node_selections_output_index"
        ),
        CheckConstraint("revision >= 0", name="ck_canvas_node_selections_revision"),
    )

    canvas_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canvas_documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("canvas_node_executions.id", ondelete="SET NULL"),
        nullable=True,
    )
    output_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CanvasAssetRef(Base):
    __tablename__ = "canvas_asset_refs"
    __table_args__ = (
        Index("ix_canvas_asset_refs_canvas_scope", "canvas_id", "scope"),
        Index("ix_canvas_asset_refs_image", "image_id"),
        Index("ix_canvas_asset_refs_video", "video_id"),
        Index(
            "uq_canvas_asset_refs_head_image",
            "canvas_id",
            "scope",
            "node_id",
            "image_id",
            unique=True,
            postgresql_where=text(
                "image_id IS NOT NULL AND scope IN ('head', 'delivery')"
            ),
            sqlite_where=text("image_id IS NOT NULL AND scope IN ('head', 'delivery')"),
        ),
        Index(
            "uq_canvas_asset_refs_head_video",
            "canvas_id",
            "scope",
            "node_id",
            "video_id",
            unique=True,
            postgresql_where=text(
                "video_id IS NOT NULL AND scope IN ('head', 'delivery')"
            ),
            sqlite_where=text("video_id IS NOT NULL AND scope IN ('head', 'delivery')"),
        ),
        Index(
            "uq_canvas_asset_refs_version_image",
            "version_id",
            "image_id",
            unique=True,
            postgresql_where=text("version_id IS NOT NULL AND image_id IS NOT NULL"),
            sqlite_where=text("version_id IS NOT NULL AND image_id IS NOT NULL"),
        ),
        Index(
            "uq_canvas_asset_refs_version_video",
            "version_id",
            "video_id",
            unique=True,
            postgresql_where=text("version_id IS NOT NULL AND video_id IS NOT NULL"),
            sqlite_where=text("version_id IS NOT NULL AND video_id IS NOT NULL"),
        ),
        Index(
            "uq_canvas_asset_refs_execution_image",
            "execution_id",
            "image_id",
            unique=True,
            postgresql_where=text("execution_id IS NOT NULL AND image_id IS NOT NULL"),
            sqlite_where=text("execution_id IS NOT NULL AND image_id IS NOT NULL"),
        ),
        Index(
            "uq_canvas_asset_refs_execution_video",
            "execution_id",
            "video_id",
            unique=True,
            postgresql_where=text("execution_id IS NOT NULL AND video_id IS NOT NULL"),
            sqlite_where=text("execution_id IS NOT NULL AND video_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(image_id IS NOT NULL AND video_id IS NULL) OR "
            "(image_id IS NULL AND video_id IS NOT NULL)",
            name="ck_canvas_asset_refs_asset",
        ),
        CheckConstraint(
            "scope IN ('head', 'version', 'execution', 'delivery')",
            name="ck_canvas_asset_refs_scope",
        ),
        CheckConstraint(
            "retention_class IN ('current', 'checkpoint', 'history', 'temporary')",
            name="ck_canvas_asset_refs_retention",
        ),
        CheckConstraint(
            "(scope IN ('head', 'delivery') AND node_id IS NOT NULL "
            "AND version_id IS NULL AND execution_id IS NULL) OR "
            "(scope = 'version' AND version_id IS NOT NULL "
            "AND execution_id IS NULL) OR "
            "(scope = 'execution' AND execution_id IS NOT NULL "
            "AND version_id IS NULL)",
            name="ck_canvas_asset_refs_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    canvas_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canvas_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("canvas_versions.id", ondelete="CASCADE"),
        nullable=True,
    )
    execution_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("canvas_node_executions.id", ondelete="CASCADE"),
        nullable=True,
    )
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    retention_class: Mapped[str] = mapped_column(String(16), nullable=False)
    image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="RESTRICT"), nullable=True
    )
    video_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="RESTRICT"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CanvasRunEvent(Base):
    __tablename__ = "canvas_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_canvas_run_events_sequence"),
        UniqueConstraint("run_id", "event_key", name="uq_canvas_run_events_key"),
        Index("ix_canvas_run_events_run_sequence", "run_id", "seq"),
        CheckConstraint("seq >= 1", name="ck_canvas_run_events_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvas_runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    execution_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("canvas_node_executions.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_jsonb: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "CanvasAssetRef",
    "CanvasDocument",
    "CanvasExecutionTask",
    "CanvasMutation",
    "CanvasNodeExecution",
    "CanvasNodeSelection",
    "CanvasRun",
    "CanvasRunEvent",
    "CanvasTaskTerminalReceipt",
    "CanvasVersion",
]
