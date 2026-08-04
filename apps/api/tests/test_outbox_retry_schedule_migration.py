from __future__ import annotations

from io import StringIO
import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lumen_core.models import OutboxEvent


VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration(filename: str) -> ModuleType:
    path = VERSIONS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_outbox_model_exposes_nullable_retry_schedule_and_due_index() -> None:
    table = OutboxEvent.__table__
    assert table.c.next_attempt_at.nullable is True
    index = next(item for item in table.indexes if item.name == "ix_outbox_due")
    assert [column.name for column in index.columns] == [
        "next_attempt_at",
        "created_at",
        "id",
    ]


def test_outbox_retry_schedule_migrations_round_trip_sqlite() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "outbox_events",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)
    schema = _load_migration("0055_outbox_next_attempt.py")
    index = _load_migration("0056_outbox_due_index.py")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO outbox_events (id, created_at) "
                "VALUES ('existing-event', CURRENT_TIMESTAMP)"
            )
        )
        operations = Operations(MigrationContext.configure(connection))
        original_schema_op = schema.op
        original_index_op = index.op
        schema.op = operations
        index.op = operations
        try:
            schema.upgrade()
            index.upgrade()
            inspector = sa.inspect(connection)
            columns = {
                column["name"]: column
                for column in inspector.get_columns("outbox_events")
            }
            assert columns["next_attempt_at"]["nullable"] is True
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT next_attempt_at FROM outbox_events "
                        "WHERE id = 'existing-event'"
                    )
                )
                is None
            )
            due_index = next(
                item
                for item in inspector.get_indexes("outbox_events")
                if item["name"] == "ix_outbox_due"
            )
            assert due_index["column_names"] == [
                "next_attempt_at",
                "created_at",
                "id",
            ]

            index.downgrade()
            schema.downgrade()
            inspector = sa.inspect(connection)
            assert "next_attempt_at" not in {
                column["name"] for column in inspector.get_columns("outbox_events")
            }
            assert connection.scalar(sa.text("SELECT count(*) FROM outbox_events")) == 1
        finally:
            schema.op = original_schema_op
            index.op = original_index_op


def test_outbox_due_index_is_concurrent_on_postgresql() -> None:
    output = StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    operations = Operations(context)
    migration = _load_migration("0056_outbox_due_index.py")
    original = migration.op
    migration.op = operations
    try:
        migration.upgrade()
        migration.downgrade()
    finally:
        migration.op = original

    sql = output.getvalue()
    assert "CREATE INDEX CONCURRENTLY ix_outbox_due" in sql
    assert "DROP INDEX CONCURRENTLY IF EXISTS ix_outbox_due" in sql
    assert "WHERE published_at IS NULL" in sql
