"""Align video JSON columns with the ORM JSONB contract.

Revision ID: 0061_video_jsonb_types
Revises: 0060_telegram_delivery_control
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0061_video_jsonb_types"
down_revision: str | None = "0060_telegram_delivery_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_COLUMNS = (
    ("videos", "metadata_jsonb"),
    ("video_generations", "upstream_request"),
    ("video_generations", "upstream_response"),
    ("video_generations", "diagnostics"),
)


def _column_type(table_name: str, column_name: str) -> str | None:
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            """
            SELECT format_type(attribute.atttypid, attribute.atttypmod)
            FROM pg_attribute AS attribute
            JOIN pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = current_schema()
              AND relation.relname = :table_name
              AND attribute.attname = :column_name
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            """
        ),
        {
            "table_name": table_name,
            "column_name": column_name,
        },
    ).scalar_one_or_none()


def _convert_column(
    table_name: str,
    column_name: str,
    *,
    expected_type: str,
    target_type: str,
) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    current_type = _column_type(table_name, column_name)
    if current_type == target_type:
        return
    if current_type is None:
        raise RuntimeError(f"missing required column {table_name}.{column_name}")
    if current_type != expected_type:
        raise RuntimeError(
            f"refusing to convert {table_name}.{column_name}: "
            f"expected {expected_type}, found {current_type}"
        )

    if target_type == "jsonb":
        existing_type = postgresql.JSON()
        type_ = postgresql.JSONB()
    else:
        existing_type = postgresql.JSONB()
        type_ = postgresql.JSON()
    op.alter_column(
        table_name,
        column_name,
        existing_type=existing_type,
        type_=type_,
        postgresql_using=f"{column_name}::{target_type}",
    )


def upgrade() -> None:
    for table_name, column_name in _JSON_COLUMNS:
        _convert_column(
            table_name,
            column_name,
            expected_type="json",
            target_type="jsonb",
        )


def downgrade() -> None:
    for table_name, column_name in reversed(_JSON_COLUMNS):
        _convert_column(
            table_name,
            column_name,
            expected_type="jsonb",
            target_type="json",
        )
