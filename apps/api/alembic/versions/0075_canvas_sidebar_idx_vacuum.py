"""Add sidebar/canvas partial indexes and hot-table autovacuum parameters.

Revision ID: 0075_canvas_sidebar_idx_vacuum
Revises: 0074_memory_halfvec_hnsw
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0075_canvas_sidebar_idx_vacuum"
down_revision: str | None = "0074_memory_halfvec_hnsw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    with op.get_context().autocommit_block():
        op.create_index(
            "ix_conversations_user_sidebar_active",
            "conversations",
            [
                "user_id",
                "archived",
                sa.text("pinned DESC"),
                sa.text("last_activity_at DESC"),
                sa.text("id DESC"),
            ],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )
        op.create_index(
            "ix_generations_canvas_exec_id",
            "generations",
            ["user_id", sa.text("(upstream_request->>'canvas_execution_id')")],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
            postgresql_where=sa.text("upstream_request ? 'canvas_execution_id'"),
        )
        op.create_index(
            "ix_video_generations_canvas_exec_id",
            "video_generations",
            ["user_id", sa.text("(upstream_request->>'canvas_execution_id')")],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
            postgresql_where=sa.text("upstream_request ? 'canvas_execution_id'"),
        )

    op.execute(
        sa.text(
            "ALTER TABLE outbox_events SET ("
            "autovacuum_vacuum_scale_factor = 0.02, "
            "autovacuum_analyze_scale_factor = 0.01, "
            "fillfactor = 90)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE generations SET ("
            "autovacuum_vacuum_scale_factor = 0.02, "
            "autovacuum_analyze_scale_factor = 0.01, "
            "fillfactor = 90)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE completions SET ("
            "autovacuum_vacuum_scale_factor = 0.02, "
            "autovacuum_analyze_scale_factor = 0.01, "
            "fillfactor = 90)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE canvas_node_executions SET ("
            "autovacuum_vacuum_scale_factor = 0.02, "
            "autovacuum_analyze_scale_factor = 0.01, "
            "fillfactor = 90)"
        )
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name in (
        "canvas_node_executions",
        "completions",
        "generations",
        "outbox_events",
    ):
        op.execute(
            sa.text(
                f'ALTER TABLE "{table_name}" RESET ('
                "autovacuum_vacuum_scale_factor, "
                "autovacuum_analyze_scale_factor, "
                "fillfactor)"
            )
        )

    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_video_generations_canvas_exec_id",
            table_name="video_generations",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_generations_canvas_exec_id",
            table_name="generations",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_conversations_user_sidebar_active",
            table_name="conversations",
            postgresql_concurrently=True,
            if_exists=True,
        )
