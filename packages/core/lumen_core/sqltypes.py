"""Portable SQLAlchemy column type helpers.

Production keeps PostgreSQL-specific storage types. Tests and lightweight
metadata tooling also create the same ORM models on SQLite, so JSON-like and
pgvector fields expose portable SQLite variants without changing the PostgreSQL
schema.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, String, Text, cast
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import TypeDecorator, TypeEngine, UserDefinedType


def JsonType() -> TypeEngine[object]:
    return PG_JSONB().with_variant(JSON(), "sqlite")


def StringListType(length: int = 36) -> PG_ARRAY[str]:
    return PG_ARRAY(String(length)).with_variant(JSON(), "sqlite")


class HalfvecColumnType(UserDefinedType[str]):
    """PostgreSQL pgvector ``halfvec(N)`` type with explicit wire casts."""

    cache_ok = True

    def __init__(self, dimensions: int = 3072) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = int(dimensions)

    def get_col_spec(self, **_kw: Any) -> str:
        return f"halfvec({self.dimensions})"

    def bind_expression(
        self,
        bindvalue: ColumnElement[Any],
    ) -> ColumnElement[Any]:
        return cast(bindvalue, self)

    def column_expression(self, col: ColumnElement[Any]) -> ColumnElement[str]:
        return cast(col, Text())


class EmbeddingVectorType(TypeDecorator[str]):
    """Dual-dialect vector column: ``halfvec(N)`` on PG, ``TEXT`` elsewhere."""

    impl = Text
    cache_ok = True

    def __init__(self, dimensions: int = 3072) -> None:
        super().__init__()
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = int(dimensions)

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(HalfvecColumnType(self.dimensions))
        return dialect.type_descriptor(Text())

    def process_bind_param(
        self,
        value: Any,
        dialect: Dialect,
    ) -> str | None:
        _ = dialect
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        if isinstance(value, (list, tuple)):
            return "[" + ",".join(f"{float(item):.7f}" for item in value) + "]"
        return str(value)

    def process_result_value(
        self,
        value: Any,
        dialect: Dialect,
    ) -> str | None:
        _ = dialect
        if value is None:
            return None
        return str(value)


__all__ = [
    "EmbeddingVectorType",
    "HalfvecColumnType",
    "JsonType",
    "StringListType",
]
