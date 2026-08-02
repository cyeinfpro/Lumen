"""Add the generations cancellation-intent index in its own retry boundary.

Revision ID: 0053_cancel_intent_generations
Revises: 0052_task_execution_epoch
Create Date: 2026-08-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0053_cancel_intent_generations"
down_revision: str | None = "0052_task_execution_epoch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_incomplete_postgresql_index(index_name: str, table_name: str) -> None:
    context = op.get_context()
    if context.as_sql:
        return

    is_incomplete = op.get_bind().scalar(
        sa.text(
            """
            SELECT NOT (index_metadata.indisvalid AND index_metadata.indisready)
            FROM pg_catalog.pg_index AS index_metadata
            JOIN pg_catalog.pg_class AS index_relation
              ON index_relation.oid = index_metadata.indexrelid
            JOIN pg_catalog.pg_class AS table_relation
              ON table_relation.oid = index_metadata.indrelid
            WHERE table_relation.oid = to_regclass(:table_name)
              AND index_relation.relnamespace = table_relation.relnamespace
              AND index_relation.relname = :index_name
            """
        ),
        {"index_name": index_name, "table_name": table_name},
    )
    if is_incomplete:
        op.drop_index(
            index_name,
            table_name=table_name,
            postgresql_concurrently=True,
            if_exists=True,
        )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            _drop_incomplete_postgresql_index(
                "ix_generations_cancel_requested",
                "generations",
            )
            op.create_index(
                "ix_generations_cancel_requested",
                "generations",
                ["cancel_requested_at", "id"],
                postgresql_concurrently=True,
                postgresql_where=sa.text(
                    "cancel_requested_at IS NOT NULL AND status IN ('queued', 'running')"
                ),
            )
    else:
        op.create_index(
            "ix_generations_cancel_requested",
            "generations",
            ["cancel_requested_at", "id"],
            sqlite_where=sa.text(
                "cancel_requested_at IS NOT NULL AND status IN ('queued', 'running')"
            ),
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(
                "ix_generations_cancel_requested",
                table_name="generations",
                postgresql_concurrently=True,
            )
    else:
        op.drop_index(
            "ix_generations_cancel_requested",
            table_name="generations",
        )
