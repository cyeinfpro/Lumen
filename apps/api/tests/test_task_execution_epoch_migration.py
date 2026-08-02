from __future__ import annotations

from io import StringIO
import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lumen_core.models import Completion, Generation


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0052_task_execution_epoch.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "task_execution_epoch_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_task_models_expose_durable_execution_epoch() -> None:
    for model in (Generation, Completion):
        column = model.__table__.c.execution_epoch
        assert column.nullable is False
        assert column.default is not None
        assert column.default.arg == 0
        assert str(column.server_default.arg) == "0"


def test_task_execution_epoch_migration_round_trips_sqlite() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    for table_name in ("generations", "completions"):
        sa.Table(
            table_name,
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        )
    metadata.create_all(engine)
    migration = _load_migration()

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO generations (id, attempt) VALUES "
                "('generation-existing', 2)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO completions (id, attempt) VALUES "
                "('completion-existing', 3)"
            )
        )
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        original = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            inspector = sa.inspect(connection)
            for table_name in ("generations", "completions"):
                columns = {
                    column["name"]: column
                    for column in inspector.get_columns(table_name)
                }
                checks = {
                    check["name"]: check["sqltext"]
                    for check in inspector.get_check_constraints(table_name)
                }
                assert columns["execution_epoch"]["nullable"] is False
                assert str(columns["execution_epoch"]["default"]) in {"0", "'0'"}
                assert (
                    checks[f"ck_{table_name}_execution_epoch_nonnegative"]
                    == "execution_epoch >= 0"
                )
                assert (
                    connection.scalar(
                        sa.text(
                            f"SELECT execution_epoch FROM {table_name} WHERE "
                            "id LIKE '%-existing'"
                        )
                    )
                    == 0
                )

            migration.downgrade()
            inspector = sa.inspect(connection)
            for table_name in ("generations", "completions"):
                columns = {
                    column["name"] for column in inspector.get_columns(table_name)
                }
                assert "execution_epoch" not in columns
                assert (
                    connection.scalar(sa.text(f"SELECT count(*) FROM {table_name}"))
                    == 1
                )
        finally:
            migration.op = original


def test_task_execution_epoch_validates_postgres_checks_online() -> None:
    output = StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    operations = Operations(context)
    migration = _load_migration()
    original = migration.op
    migration.op = operations
    try:
        migration.upgrade()
    finally:
        migration.op = original

    sql = output.getvalue()
    for table_name in ("generations", "completions"):
        constraint_name = f"ck_{table_name}_execution_epoch_nonnegative"
        assert (
            f"ADD CONSTRAINT {constraint_name} CHECK (execution_epoch >= 0) NOT VALID"
        ) in sql
        assert (
            f'ALTER TABLE "{table_name}" VALIDATE CONSTRAINT "{constraint_name}"'
        ) in sql
