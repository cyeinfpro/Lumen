"""Structured workflow tables for apparel model showcase.

Revision ID: 0015_workflow_showcase
Revises: 0014_telegram_bindings
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0015_workflow_showcase"
down_revision: str | None = "0014_telegram_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("user_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "product_image_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column("current_step", sa.String(length=64), nullable=False),
        sa.Column(
            "quality_mode",
            sa.String(length=32),
            nullable=False,
            server_default="premium",
        ),
        sa.Column(
            "metadata_jsonb",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_workflow_runs_user_status_updated",
        "workflow_runs",
        ["user_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_workflow_runs_user_type_updated",
        "workflow_runs",
        ["user_id", "type", "updated_at"],
    )

    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=36),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_key", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="waiting_input",
        ),
        sa.Column(
            "input_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "output_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "task_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "image_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approved_by",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "workflow_run_id",
            "step_key",
            name="uq_workflow_steps_run_key",
        ),
    )
    op.create_index(
        "ix_workflow_steps_run_key",
        "workflow_steps",
        ["workflow_run_id", "step_key"],
    )
    op.create_index(
        "ix_workflow_steps_run_status",
        "workflow_steps",
        ["workflow_run_id", "status"],
    )

    op.create_table(
        "model_candidates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=36),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column(
            "portrait_image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "front_image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "side_image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "back_image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "contact_sheet_image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "model_brief_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "task_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "workflow_run_id",
            "candidate_index",
            name="uq_model_candidates_run_index",
        ),
    )
    op.create_index(
        "ix_model_candidates_run_status",
        "model_candidates",
        ["workflow_run_id", "status"],
    )

    op.create_table(
        "quality_reports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=36),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "overall_score",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "product_fidelity_score",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "model_consistency_score",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "aesthetic_score",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "artifact_score",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "issues_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "recommendation",
            sa.String(length=32),
            nullable=False,
            server_default="review",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "workflow_run_id",
            "image_id",
            name="uq_quality_reports_run_image",
        ),
    )
    op.create_index(
        "ix_quality_reports_run_recommendation",
        "quality_reports",
        ["workflow_run_id", "recommendation"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quality_reports_run_recommendation",
        table_name="quality_reports",
    )
    op.drop_table("quality_reports")
    op.drop_index("ix_model_candidates_run_status", table_name="model_candidates")
    op.drop_table("model_candidates")
    op.drop_index("ix_workflow_steps_run_status", table_name="workflow_steps")
    op.drop_index("ix_workflow_steps_run_key", table_name="workflow_steps")
    op.drop_table("workflow_steps")
    op.drop_index("ix_workflow_runs_user_type_updated", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_user_status_updated", table_name="workflow_runs")
    op.drop_table("workflow_runs")
