"""Test-only UTC restoration for SQLite's lossy timezone-aware timestamp codec."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, event, inspect
from sqlalchemy.orm.attributes import set_committed_value

from lumen_core.models import Base


def install_sqlite_utc_adapter(engine):
    if engine.dialect.name != "sqlite":
        raise RuntimeError("UTC test adapter must never change a production database")

    def restore(target, _context, _attrs=None):
        for column in inspect(target).mapper.columns:
            value = target.__dict__.get(column.key)
            if (
                isinstance(column.type, DateTime)
                and column.type.timezone
                and isinstance(value, datetime)
                and value.tzinfo is None
            ):
                set_committed_value(target, column.key, value.replace(tzinfo=timezone.utc))

    event.listen(Base, "load", restore, propagate=True)
    event.listen(Base, "refresh", restore, propagate=True)
