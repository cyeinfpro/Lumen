"""Convert account memory embeddings to pgvector halfvec(3072) and add HNSW index.

Revision ID: 0074_memory_halfvec_hnsw
Revises: 0073_seedance_25_flash_defaults
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from lumen_core.sqltypes import HalfvecColumnType

revision: str = "0074_memory_halfvec_hnsw"
down_revision: str | Sequence[str] | None = "0073_seedance_25_flash_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HALFVEC_USING_EXPR = (
    "CASE WHEN embedding IS NULL OR btrim(embedding) = '' "
    "THEN NULL ELSE embedding::halfvec(3072) END"
)


def _column_type(table_name: str, column_name: str) -> str | None:
    if op.get_context().as_sql:
        return "text"
    return op.get_bind().scalar(
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
        {"table_name": table_name, "column_name": column_name},
    )


def _drop_incomplete_postgresql_index(index_name: str, table_name: str) -> None:
    if op.get_context().as_sql:
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
    if op.get_bind().dialect.name != "postgresql":
        return

    if _column_type("user_memories", "embedding") != "halfvec(3072)":
        op.alter_column(
            "user_memories",
            "embedding",
            existing_type=sa.Text(),
            type_=HalfvecColumnType(3072),
            existing_nullable=True,
            postgresql_using=_HALFVEC_USING_EXPR,
        )
    if _column_type("user_memory_staging", "embedding") != "halfvec(3072)":
        op.alter_column(
            "user_memory_staging",
            "embedding",
            existing_type=sa.Text(),
            type_=HalfvecColumnType(3072),
            existing_nullable=True,
            postgresql_using=_HALFVEC_USING_EXPR,
        )

    with op.get_context().autocommit_block():
        _drop_incomplete_postgresql_index(
            "ix_user_memories_embedding_hnsw",
            "user_memories",
        )
        op.create_index(
            "ix_user_memories_embedding_hnsw",
            "user_memories",
            ["embedding"],
            unique=False,
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
            postgresql_where=sa.text(
                "disabled = false AND superseded_by IS NULL AND embedding IS NOT NULL"
            ),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_user_memories_embedding_hnsw",
            table_name="user_memories",
            postgresql_concurrently=True,
            if_exists=True,
        )
    if _column_type("user_memory_staging", "embedding") != "text":
        op.alter_column(
            "user_memory_staging",
            "embedding",
            existing_type=HalfvecColumnType(3072),
            type_=sa.Text(),
            existing_nullable=True,
            postgresql_using="embedding::text",
        )
    if _column_type("user_memories", "embedding") != "text":
        op.alter_column(
            "user_memories",
            "embedding",
            existing_type=HalfvecColumnType(3072),
            type_=sa.Text(),
            existing_nullable=True,
            postgresql_using="embedding::text",
        )
