"""Portable SQLAlchemy column type helpers.

The Docker runtime keeps PostgreSQL-specific storage types.  Desktop uses the
same ORM classes against SQLite, so JSON-like fields need a SQLite variant
without forcing the Postgres deployment to change schema.
"""

from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.types import TypeEngine


def JsonType() -> TypeEngine[object]:
    return PG_JSONB().with_variant(JSON(), "sqlite")


def StringListType(length: int = 36) -> TypeEngine[object]:
    return PG_ARRAY(String(length)).with_variant(JSON(), "sqlite")


__all__ = ["JsonType", "StringListType"]
