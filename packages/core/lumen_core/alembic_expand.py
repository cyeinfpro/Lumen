"""Helpers for expand-then-contract Alembic migrations."""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(name: str) -> str:
    parts = name.split(".")
    if not parts or any(not _IDENT_RE.match(part) for part in parts):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return ".".join(f'"{part}"' for part in parts)


def add_column_nullable_then_backfill(
    table: str,
    column: sa.Column,
    *,
    backfill_sql: str | sa.TextClause | None = None,
    enforce_not_null: bool = False,
) -> None:
    """Add a column in an expand-safe way, optionally backfill, then enforce.

    By default the new column stays nullable so old and new app versions can run
    together. Set enforce_not_null=True only when the migration also provides a
    server_default or the backfill is known to cover every existing row.
    """

    expanded = column.copy()
    expanded.nullable = True
    op.add_column(table, expanded)
    if backfill_sql is not None:
        op.execute(backfill_sql if isinstance(backfill_sql, sa.TextClause) else sa.text(backfill_sql))
    if enforce_not_null:
        op.alter_column(table, column.name, nullable=False)


def add_check_not_valid(table: str, name: str, expr: str) -> None:
    """Add a PostgreSQL CHECK constraint without validating existing rows."""

    op.execute(
        sa.text(
            f"ALTER TABLE {_quote_ident(table)} "
            f"ADD CONSTRAINT {_quote_ident(name)} CHECK ({expr}) NOT VALID"
        )
    )


def validate_check(table: str, name: str) -> None:
    """Validate a previously added NOT VALID CHECK constraint."""

    op.execute(
        sa.text(
            f"ALTER TABLE {_quote_ident(table)} "
            f"VALIDATE CONSTRAINT {_quote_ident(name)}"
        )
    )


def rename_via_alias(table: str, old: str, new: str) -> None:
    """Install a temporary dual-write trigger for two already-existing columns.

    The expand migration should add the new nullable column first. This helper
    keeps old and new columns in sync until a later contract migration removes
    the old column and trigger.
    """

    table_q = _quote_ident(table)
    old_q = _quote_ident(old)
    new_q = _quote_ident(new)
    suffix = re.sub(r"[^A-Za-z0-9_]", "_", f"{table}_{old}_{new}")[:48]
    fn = _quote_ident(f"lumen_alias_{suffix}")
    trigger = _quote_ident(f"lumen_alias_{suffix}_trg")
    op.execute(
        sa.text(
            f"""
CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$
BEGIN
    IF NEW.{new_q} IS NULL AND NEW.{old_q} IS NOT NULL THEN
        NEW.{new_q} := NEW.{old_q};
    ELSIF NEW.{old_q} IS NULL AND NEW.{new_q} IS NOT NULL THEN
        NEW.{old_q} := NEW.{new_q};
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS {trigger} ON {table_q};
CREATE TRIGGER {trigger}
BEFORE INSERT OR UPDATE ON {table_q}
FOR EACH ROW EXECUTE FUNCTION {fn}();
"""
        )
    )


def drop_alias_trigger(table: str, old: str, new: str) -> None:
    """Remove the trigger/function created by rename_via_alias."""

    table_q = _quote_ident(table)
    suffix = re.sub(r"[^A-Za-z0-9_]", "_", f"{table}_{old}_{new}")[:48]
    fn = _quote_ident(f"lumen_alias_{suffix}")
    trigger = _quote_ident(f"lumen_alias_{suffix}_trg")
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table_q};"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {fn}();"))
