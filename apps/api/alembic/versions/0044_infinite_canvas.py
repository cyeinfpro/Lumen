"""Add the infinite-canvas document, version, and execution data model.

Revision ID: 0044_infinite_canvas
Revises: 0043_billing_consistency
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0044_infinite_canvas"
down_revision: str | None = "0043_billing_consistency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _string_list_type(length: int) -> sa.types.TypeEngine:
    return postgresql.ARRAY(sa.String(length=length)).with_variant(sa.JSON(), "sqlite")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    if is_sqlite:
        last_version_column = sa.Column(
            "last_version_id",
            sa.String(length=36),
            sa.ForeignKey(
                "canvas_versions.id",
                name="fk_canvas_documents_last_version",
            ),
            nullable=True,
        )
    else:
        last_version_column = sa.Column(
            "last_version_id",
            sa.String(length=36),
            nullable=True,
        )

    op.create_table(
        "canvas_documents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "graph_schema_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "graph_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        last_version_column,
        sa.Column(
            "thumbnail_image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _created_at(),
        _updated_at(),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_canvas_documents_revision",
        ),
        sa.CheckConstraint(
            "graph_schema_version >= 1",
            name="ck_canvas_documents_graph_schema_version",
        ),
    )
    op.create_index(
        "ix_canvas_documents_user_deleted_updated",
        "canvas_documents",
        ["user_id", "deleted_at", "updated_at", "id"],
    )
    op.create_index(
        "ix_canvas_documents_user_title",
        "canvas_documents",
        ["user_id", "title"],
    )

    op.create_table(
        "canvas_mutations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "canvas_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("mutation_id", sa.String(length=96), nullable=False),
        sa.Column(
            "operation_schema_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("base_revision", sa.BigInteger(), nullable=False),
        sa.Column("result_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "operations_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "response_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        _created_at(),
        sa.UniqueConstraint(
            "canvas_id",
            "client_id",
            "mutation_id",
            name="uq_canvas_mutations_client_mutation",
        ),
        sa.UniqueConstraint(
            "canvas_id",
            "result_revision",
            name="uq_canvas_mutations_result_revision",
        ),
        sa.CheckConstraint(
            "result_revision = base_revision + 1",
            name="ck_canvas_mutations_revision_step",
        ),
        sa.CheckConstraint(
            "operation_schema_version >= 1",
            name="ck_canvas_mutations_operation_schema_version",
        ),
    )
    op.create_index(
        "ix_canvas_mutations_revision_window",
        "canvas_mutations",
        ["canvas_id", "base_revision", "result_revision"],
    )

    op.create_table(
        "canvas_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "canvas_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("graph_schema_version", sa.Integer(), nullable=False),
        sa.Column("graph_hash", sa.String(length=64), nullable=False),
        sa.Column("graph_jsonb", _json_type(), nullable=False),
        sa.Column(
            "selection_snapshot_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("selection_hash", sa.String(length=64), nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "canvas_id",
            "version_no",
            name="uq_canvas_versions_number",
        ),
        sa.CheckConstraint(
            "source_revision >= 1",
            name="ck_canvas_versions_source_revision",
        ),
        sa.CheckConstraint(
            "version_no >= 1",
            name="ck_canvas_versions_number",
        ),
        sa.CheckConstraint(
            "graph_schema_version >= 1",
            name="ck_canvas_versions_graph_schema_version",
        ),
        sa.CheckConstraint(
            "kind IN ('run', 'named', 'restore', 'import', 'template')",
            name="ck_canvas_versions_kind",
        ),
    )
    op.create_index(
        "ix_canvas_versions_canvas_created",
        "canvas_versions",
        ["canvas_id", "created_at"],
    )
    op.create_index(
        "ix_canvas_versions_canvas_graph_hash",
        "canvas_versions",
        ["canvas_id", "graph_hash"],
    )
    if not is_sqlite:
        op.create_foreign_key(
            "fk_canvas_documents_last_version",
            "canvas_documents",
            "canvas_versions",
            ["last_version_id"],
            ["id"],
        )

    op.create_table(
        "canvas_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "canvas_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "parent_run_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="planning",
        ),
        sa.Column(
            "failure_policy",
            sa.String(length=32),
            nullable=False,
            server_default="continue_independent",
        ),
        sa.Column("run_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "last_event_seq",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("target_node_ids", _string_list_type(64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("budget_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "reserved_micro", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("spent_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "estimated_cost_micro",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("actual_cost_micro", sa.BigInteger(), nullable=True),
        sa.Column(
            "summary_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_canvas_runs_user_idempotency",
        ),
        sa.CheckConstraint(
            "kind IN ('single', 'repair', 'upstream', 'selection', 'all')",
            name="ck_canvas_runs_kind",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'planning', 'queued', 'running', 'paused', 'reconciling', "
            "'canceling', 'succeeded', 'partial_failed', 'failed', 'canceled'"
            ")",
            name="ck_canvas_runs_status",
        ),
        sa.CheckConstraint(
            "failure_policy IN ('continue_independent', 'fail_fast')",
            name="ck_canvas_runs_failure_policy",
        ),
        sa.CheckConstraint("run_epoch >= 0", name="ck_canvas_runs_epoch"),
        sa.CheckConstraint(
            "last_event_seq >= 0",
            name="ck_canvas_runs_event_seq",
        ),
        sa.CheckConstraint("budget_micro >= 0", name="ck_canvas_runs_budget"),
        sa.CheckConstraint(
            "reserved_micro >= 0",
            name="ck_canvas_runs_reserved",
        ),
        sa.CheckConstraint("spent_micro >= 0", name="ck_canvas_runs_spent"),
        sa.CheckConstraint(
            "estimated_cost_micro >= 0",
            name="ck_canvas_runs_estimated_cost",
        ),
        sa.CheckConstraint(
            "actual_cost_micro IS NULL OR actual_cost_micro >= 0",
            name="ck_canvas_runs_actual_cost",
        ),
    )
    op.create_index(
        "ix_canvas_runs_canvas_created",
        "canvas_runs",
        ["canvas_id", "created_at"],
    )
    op.create_index(
        "ix_canvas_runs_user_status_updated",
        "canvas_runs",
        ["user_id", "status", "updated_at"],
    )

    op.create_table(
        "canvas_node_executions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "canvas_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("node_type", sa.String(length=48), nullable=False),
        sa.Column("node_schema_version", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("attempt_epoch", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "submission_idempotency_key",
            sa.String(length=96),
            nullable=False,
        ),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("config_snapshot_jsonb", _json_type(), nullable=False),
        sa.Column("input_snapshot_jsonb", _json_type(), nullable=False),
        sa.Column(
            "model_snapshot_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "pricing_snapshot_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("processor_version", sa.String(length=64), nullable=False),
        sa.Column(
            "outputs_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("selection_base_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "retry_of_execution_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_node_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "reused_from_execution_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_node_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "run_id",
            "node_id",
            "attempt",
            name="uq_canvas_node_executions_attempt",
        ),
        sa.UniqueConstraint(
            "user_id",
            "submission_idempotency_key",
            name="uq_canvas_node_executions_submission",
        ),
        sa.CheckConstraint(
            "sequence >= 0",
            name="ck_canvas_node_executions_sequence",
        ),
        sa.CheckConstraint(
            "attempt >= 0",
            name="ck_canvas_node_executions_attempt",
        ),
        sa.CheckConstraint(
            "attempt_epoch >= 0",
            name="ck_canvas_node_executions_attempt_epoch",
        ),
        sa.CheckConstraint(
            "selection_base_revision >= 0",
            name="ck_canvas_node_executions_selection_revision",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'pending', 'ready', 'queued', 'running', 'reconciling', "
            "'canceling', 'succeeded', 'partial_failed', 'failed', 'blocked', "
            "'canceled', 'skipped', 'reused'"
            ")",
            name="ck_canvas_node_executions_status",
        ),
    )
    op.create_index(
        "ix_canvas_node_executions_canvas_node_created",
        "canvas_node_executions",
        ["canvas_id", "node_id", "created_at"],
    )
    op.create_index(
        "ix_canvas_node_executions_run_sequence",
        "canvas_node_executions",
        ["run_id", "sequence"],
    )
    op.create_index(
        "ix_canvas_node_executions_user_status_updated",
        "canvas_node_executions",
        ["user_id", "status", "updated_at"],
    )

    op.create_table(
        "canvas_execution_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "execution_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_node_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("task_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "generation_id",
            sa.String(length=36),
            sa.ForeignKey("generations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "completion_id",
            sa.String(length=36),
            sa.ForeignKey("completions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "video_generation_id",
            sa.String(length=36),
            sa.ForeignKey("video_generations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("billing_ref_type", sa.String(length=32), nullable=False),
        sa.Column("billing_ref_id", sa.String(length=96), nullable=False),
        sa.Column(
            "output_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "execution_id",
            "ordinal",
            name="uq_canvas_execution_tasks_ordinal",
        ),
        sa.UniqueConstraint(
            "task_kind",
            "idempotency_key",
            name="uq_canvas_execution_tasks_idempotency",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_canvas_execution_tasks_ordinal",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'canceled', 'expired')",
            name="ck_canvas_execution_tasks_status",
        ),
    )
    op.create_index(
        "uq_canvas_execution_tasks_generation",
        "canvas_execution_tasks",
        ["generation_id"],
        unique=True,
        postgresql_where=sa.text("generation_id IS NOT NULL"),
        sqlite_where=sa.text("generation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_canvas_execution_tasks_completion",
        "canvas_execution_tasks",
        ["completion_id"],
        unique=True,
        postgresql_where=sa.text("completion_id IS NOT NULL"),
        sqlite_where=sa.text("completion_id IS NOT NULL"),
    )
    op.create_index(
        "uq_canvas_execution_tasks_video_generation",
        "canvas_execution_tasks",
        ["video_generation_id"],
        unique=True,
        postgresql_where=sa.text("video_generation_id IS NOT NULL"),
        sqlite_where=sa.text("video_generation_id IS NOT NULL"),
    )

    op.create_table(
        "canvas_task_terminal_receipts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "execution_task_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_execution_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_kind", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_epoch", sa.BigInteger(), nullable=False),
        sa.Column("terminal_status", sa.String(length=32), nullable=False),
        sa.Column("terminal_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "task_kind",
            "task_id",
            "task_epoch",
            name="uq_canvas_task_terminal_receipts_epoch",
        ),
        sa.CheckConstraint(
            "task_epoch >= 0",
            name="ck_canvas_task_terminal_receipts_epoch",
        ),
    )

    op.create_table(
        "canvas_node_selections",
        sa.Column(
            "canvas_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("node_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "execution_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_node_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("output_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        _updated_at(),
        sa.CheckConstraint(
            "output_index >= 0",
            name="ck_canvas_node_selections_output_index",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_canvas_node_selections_revision",
        ),
    )

    op.create_table(
        "canvas_asset_refs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "canvas_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_versions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "execution_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_node_executions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("node_id", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("retention_class", sa.String(length=16), nullable=False),
        sa.Column(
            "image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "video_id",
            sa.String(length=36),
            sa.ForeignKey("videos.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "(image_id IS NOT NULL AND video_id IS NULL) OR "
            "(image_id IS NULL AND video_id IS NOT NULL)",
            name="ck_canvas_asset_refs_asset",
        ),
        sa.CheckConstraint(
            "scope IN ('head', 'version', 'execution', 'delivery')",
            name="ck_canvas_asset_refs_scope",
        ),
        sa.CheckConstraint(
            "retention_class IN ('current', 'checkpoint', 'history', 'temporary')",
            name="ck_canvas_asset_refs_retention",
        ),
        sa.CheckConstraint(
            "(scope IN ('head', 'delivery') AND node_id IS NOT NULL "
            "AND version_id IS NULL AND execution_id IS NULL) OR "
            "(scope = 'version' AND version_id IS NOT NULL "
            "AND execution_id IS NULL) OR "
            "(scope = 'execution' AND execution_id IS NOT NULL "
            "AND version_id IS NULL)",
            name="ck_canvas_asset_refs_owner",
        ),
    )
    op.create_index(
        "ix_canvas_asset_refs_canvas_scope",
        "canvas_asset_refs",
        ["canvas_id", "scope"],
    )
    op.create_index(
        "ix_canvas_asset_refs_image",
        "canvas_asset_refs",
        ["image_id"],
    )
    op.create_index(
        "ix_canvas_asset_refs_video",
        "canvas_asset_refs",
        ["video_id"],
    )
    op.create_index(
        "uq_canvas_asset_refs_head_image",
        "canvas_asset_refs",
        ["canvas_id", "scope", "node_id", "image_id"],
        unique=True,
        postgresql_where=sa.text(
            "image_id IS NOT NULL AND scope IN ('head', 'delivery')"
        ),
        sqlite_where=sa.text("image_id IS NOT NULL AND scope IN ('head', 'delivery')"),
    )
    op.create_index(
        "uq_canvas_asset_refs_head_video",
        "canvas_asset_refs",
        ["canvas_id", "scope", "node_id", "video_id"],
        unique=True,
        postgresql_where=sa.text(
            "video_id IS NOT NULL AND scope IN ('head', 'delivery')"
        ),
        sqlite_where=sa.text("video_id IS NOT NULL AND scope IN ('head', 'delivery')"),
    )
    op.create_index(
        "uq_canvas_asset_refs_version_image",
        "canvas_asset_refs",
        ["version_id", "image_id"],
        unique=True,
        postgresql_where=sa.text("version_id IS NOT NULL AND image_id IS NOT NULL"),
        sqlite_where=sa.text("version_id IS NOT NULL AND image_id IS NOT NULL"),
    )
    op.create_index(
        "uq_canvas_asset_refs_version_video",
        "canvas_asset_refs",
        ["version_id", "video_id"],
        unique=True,
        postgresql_where=sa.text("version_id IS NOT NULL AND video_id IS NOT NULL"),
        sqlite_where=sa.text("version_id IS NOT NULL AND video_id IS NOT NULL"),
    )
    op.create_index(
        "uq_canvas_asset_refs_execution_image",
        "canvas_asset_refs",
        ["execution_id", "image_id"],
        unique=True,
        postgresql_where=sa.text("execution_id IS NOT NULL AND image_id IS NOT NULL"),
        sqlite_where=sa.text("execution_id IS NOT NULL AND image_id IS NOT NULL"),
    )
    op.create_index(
        "uq_canvas_asset_refs_execution_video",
        "canvas_asset_refs",
        ["execution_id", "video_id"],
        unique=True,
        postgresql_where=sa.text("execution_id IS NOT NULL AND video_id IS NOT NULL"),
        sqlite_where=sa.text("execution_id IS NOT NULL AND video_id IS NOT NULL"),
    )

    op.create_table(
        "canvas_run_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "execution_id",
            sa.String(length=36),
            sa.ForeignKey("canvas_node_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_key", sa.String(length=128), nullable=False),
        sa.Column(
            "payload_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        _created_at(),
        sa.UniqueConstraint(
            "run_id",
            "seq",
            name="uq_canvas_run_events_sequence",
        ),
        sa.UniqueConstraint(
            "run_id",
            "event_key",
            name="uq_canvas_run_events_key",
        ),
        sa.CheckConstraint(
            "seq >= 1",
            name="ck_canvas_run_events_sequence",
        ),
    )
    op.create_index(
        "ix_canvas_run_events_run_sequence",
        "canvas_run_events",
        ["run_id", "seq"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    op.drop_index(
        "ix_canvas_run_events_run_sequence",
        table_name="canvas_run_events",
    )
    op.drop_table("canvas_run_events")

    for index_name in (
        "uq_canvas_asset_refs_execution_video",
        "uq_canvas_asset_refs_execution_image",
        "uq_canvas_asset_refs_version_video",
        "uq_canvas_asset_refs_version_image",
        "uq_canvas_asset_refs_head_video",
        "uq_canvas_asset_refs_head_image",
        "ix_canvas_asset_refs_video",
        "ix_canvas_asset_refs_image",
        "ix_canvas_asset_refs_canvas_scope",
    ):
        op.drop_index(index_name, table_name="canvas_asset_refs")
    op.drop_table("canvas_asset_refs")

    op.drop_table("canvas_node_selections")
    op.drop_table("canvas_task_terminal_receipts")

    for index_name in (
        "uq_canvas_execution_tasks_video_generation",
        "uq_canvas_execution_tasks_completion",
        "uq_canvas_execution_tasks_generation",
    ):
        op.drop_index(index_name, table_name="canvas_execution_tasks")
    op.drop_table("canvas_execution_tasks")

    for index_name in (
        "ix_canvas_node_executions_user_status_updated",
        "ix_canvas_node_executions_run_sequence",
        "ix_canvas_node_executions_canvas_node_created",
    ):
        op.drop_index(index_name, table_name="canvas_node_executions")
    op.drop_table("canvas_node_executions")

    for index_name in (
        "ix_canvas_runs_user_status_updated",
        "ix_canvas_runs_canvas_created",
    ):
        op.drop_index(index_name, table_name="canvas_runs")
    op.drop_table("canvas_runs")

    if is_sqlite:
        op.execute("UPDATE canvas_documents SET last_version_id = NULL")
    else:
        op.drop_constraint(
            "fk_canvas_documents_last_version",
            "canvas_documents",
            type_="foreignkey",
        )
    op.drop_index(
        "ix_canvas_versions_canvas_graph_hash",
        table_name="canvas_versions",
    )
    op.drop_index(
        "ix_canvas_versions_canvas_created",
        table_name="canvas_versions",
    )
    op.drop_table("canvas_versions")

    op.drop_index(
        "ix_canvas_mutations_revision_window",
        table_name="canvas_mutations",
    )
    op.drop_table("canvas_mutations")

    op.drop_index(
        "ix_canvas_documents_user_title",
        table_name="canvas_documents",
    )
    op.drop_index(
        "ix_canvas_documents_user_deleted_updated",
        table_name="canvas_documents",
    )
    op.drop_table("canvas_documents")
