"""Portable SQLAlchemy column type helpers.

Production keeps PostgreSQL-specific storage types. Tests and lightweight
metadata tooling also create the same ORM models on SQLite, so JSON-like fields
need a SQLite variant without changing the PostgreSQL schema.
"""

from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.types import TypeEngine


def JsonType() -> TypeEngine[object]:
    return PG_JSONB().with_variant(JSON(), "sqlite")


def StringListType(length: int = 36) -> PG_ARRAY[str]:
    return PG_ARRAY(String(length)).with_variant(JSON(), "sqlite")


__all__ = ["JsonType", "StringListType"]
