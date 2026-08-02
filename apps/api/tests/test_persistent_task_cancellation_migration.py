from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.runtime.migration import RevisionStep
from alembic.script import Script, ScriptDirectory
from sqlalchemy.engine import Connection, Engine

from lumen_core.models import Completion, Generation


API_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_ROOT = API_ROOT / "alembic" / "versions"

BASE_REVISION = "0050_outbox_claim_v2"
SCHEMA_REVISION = "0051_task_cancel_intent"
EXECUTION_EPOCH_REVISION = "0052_task_execution_epoch"
GENERATIONS_INDEX_REVISION = "0053_cancel_intent_generations"
COMPLETIONS_INDEX_REVISION = "0054_cancel_intent_completions"
UPGRADE_REVISIONS = (
    SCHEMA_REVISION,
    EXECUTION_EPOCH_REVISION,
    GENERATIONS_INDEX_REVISION,
    COMPLETIONS_INDEX_REVISION,
)


class _FailingIndexOperations:
    def __init__(self, operations: Operations, index_name: str) -> None:
        self._operations = operations
        self._index_name = index_name

    def create_index(
        self,
        index_name: str | None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if index_name == self._index_name:
            raise RuntimeError(f"forced index creation failure: {index_name}")
        self._operations.create_index(index_name, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._operations, name)


class _RetryingPostgresqlIndexOperations:
    def __init__(
        self,
        *,
        index_name: str,
        table_name: str,
        columns: tuple[str, ...],
    ) -> None:
        self.index_name = index_name
        self.table_name = table_name
        self.columns = columns
        self.dialect = SimpleNamespace(name="postgresql")
        self.as_sql = False
        self.index_is_valid: bool | None = None
        self.fail_next_create = True
        self.autocommit_depth = 0
        self.autocommit_entries = 0
        self.catalog_queries: list[dict[str, str]] = []
        self.actions: list[str] = []

    def get_bind(self) -> _RetryingPostgresqlIndexOperations:
        return self

    def get_context(self) -> _RetryingPostgresqlIndexOperations:
        return self

    @contextmanager
    def autocommit_block(self) -> Iterator[None]:
        self.autocommit_entries += 1
        self.autocommit_depth += 1
        try:
            yield
        finally:
            self.autocommit_depth -= 1

    def scalar(
        self,
        statement: Any,
        parameters: dict[str, str],
    ) -> bool | None:
        assert self.autocommit_depth == 1
        assert "pg_catalog.pg_index" in str(statement)
        assert "pg_catalog.pg_class" in str(statement)
        assert "indisvalid" in str(statement)
        assert "indisready" in str(statement)
        assert parameters == {
            "index_name": self.index_name,
            "table_name": self.table_name,
        }
        self.catalog_queries.append(parameters)
        if self.index_is_valid is None:
            return None
        return not self.index_is_valid

    def drop_index(
        self,
        index_name: str,
        *,
        table_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        assert self.autocommit_depth == 1
        assert index_name == self.index_name
        assert table_name == self.table_name
        assert kwargs == {
            "postgresql_concurrently": True,
            "if_exists": True,
        }
        assert self.index_is_valid is False
        self.actions.append("drop")
        self.index_is_valid = None

    def create_index(
        self,
        index_name: str,
        table_name: str,
        columns: list[str],
        **kwargs: Any,
    ) -> None:
        assert self.autocommit_depth == 1
        assert index_name == self.index_name
        assert table_name == self.table_name
        assert tuple(columns) == self.columns
        assert kwargs["postgresql_concurrently"] is True
        self.actions.append("create")
        if self.fail_next_create:
            self.fail_next_create = False
            self.index_is_valid = False
            raise RuntimeError(f"forced concurrent index failure: {index_name}")
        assert self.index_is_valid is None
        self.index_is_valid = True


def _script_directory() -> ScriptDirectory:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def _revision(scripts: ScriptDirectory, revision_id: str) -> Script:
    script = scripts.get_revision(revision_id)
    assert script is not None
    return script


def _run_revisions(
    connection: Connection,
    scripts: ScriptDirectory,
    revision_ids: tuple[str, ...],
    *,
    is_upgrade: bool,
    fail_on_index: str | None = None,
) -> None:
    revisions = tuple(_revision(scripts, revision_id) for revision_id in revision_ids)

    def steps(
        _heads: tuple[str, ...],
        _context: MigrationContext,
    ) -> list[RevisionStep]:
        return [
            RevisionStep(scripts.revision_map, script, is_upgrade=is_upgrade)
            for script in revisions
        ]

    context = MigrationContext.configure(
        connection,
        opts={
            "fn": steps,
            "transaction_per_migration": True,
        },
    )
    operations = Operations(context)
    migration_operations: Any = operations
    if fail_on_index is not None:
        migration_operations = _FailingIndexOperations(operations, fail_on_index)
    originals = [(script.module, script.module.op) for script in revisions]
    for module, _original in originals:
        module.op = migration_operations
    try:
        context.run_migrations()
    finally:
        for module, original in originals:
            module.op = original


def _create_0050_baseline(engine: Engine) -> None:
    metadata = sa.MetaData()
    for table_name in ("generations", "completions"):
        sa.Table(
            table_name,
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("status", sa.String(32), nullable=False),
        )
    sa.Table(
        "alembic_version",
        metadata,
        sa.Column("version_num", sa.String(32), nullable=False, primary_key=True),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": BASE_REVISION},
        )
        connection.execute(
            sa.text(
                "INSERT INTO generations (id, status) VALUES "
                "('generation-existing', 'running')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO completions (id, status) VALUES "
                "('completion-existing', 'streaming')"
            )
        )


def _current_revision(connection: Connection) -> str | None:
    return connection.scalar(sa.text("SELECT version_num FROM alembic_version"))


def _index_names(connection: Connection, table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(connection).get_indexes(table_name)
        if index["name"] is not None
    }


def _column_names(connection: Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(connection).get_columns(table_name)}


def _assert_cancel_intent_columns(connection: Connection) -> None:
    for table_name in ("generations", "completions"):
        assert "cancel_requested_at" in _column_names(connection, table_name)
        assert connection.scalar(sa.text(f"SELECT count(*) FROM {table_name}")) == 1


def _render_postgresql_sql(script: Script) -> str:
    output = StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    operations = Operations(context)
    original = script.module.op
    script.module.op = operations
    try:
        script.module.upgrade()
        script.module.downgrade()
    finally:
        script.module.op = original
    return output.getvalue()


def test_task_models_expose_indexed_cancel_intent() -> None:
    for model, index_name in (
        (Generation, "ix_generations_cancel_requested"),
        (Completion, "ix_completions_cancel_requested"),
    ):
        column = model.__table__.c.cancel_requested_at
        assert column.nullable is True
        assert index_name in {index.name for index in model.__table__.indexes}


def test_cancellation_migrations_split_schema_and_concurrent_indexes() -> None:
    scripts = _script_directory()
    schema = _revision(scripts, SCHEMA_REVISION)
    generations_index = _revision(scripts, GENERATIONS_INDEX_REVISION)
    completions_index = _revision(scripts, COMPLETIONS_INDEX_REVISION)

    assert schema.down_revision == BASE_REVISION
    assert generations_index.down_revision == EXECUTION_EPOCH_REVISION
    assert completions_index.down_revision == GENERATIONS_INDEX_REVISION
    assert "create_index(" not in (
        VERSIONS_ROOT / "0051_task_cancel_intent.py"
    ).read_text(encoding="utf-8")


def test_persistent_cancellation_migrations_round_trip_sqlite() -> None:
    engine = sa.create_engine("sqlite://")
    _create_0050_baseline(engine)
    scripts = _script_directory()

    with engine.connect() as connection:
        _run_revisions(
            connection,
            scripts,
            UPGRADE_REVISIONS,
            is_upgrade=True,
        )

    with engine.connect() as connection:
        assert _current_revision(connection) == COMPLETIONS_INDEX_REVISION
        _assert_cancel_intent_columns(connection)
        assert "ix_generations_cancel_requested" in _index_names(
            connection,
            "generations",
        )
        assert "ix_completions_cancel_requested" in _index_names(
            connection,
            "completions",
        )

    with engine.connect() as connection:
        _run_revisions(
            connection,
            scripts,
            tuple(reversed(UPGRADE_REVISIONS)),
            is_upgrade=False,
        )

    with engine.connect() as connection:
        assert _current_revision(connection) == BASE_REVISION
        for table_name in ("generations", "completions"):
            assert "cancel_requested_at" not in _column_names(connection, table_name)
            assert _index_names(connection, table_name).isdisjoint(
                {
                    "ix_generations_cancel_requested",
                    "ix_completions_cancel_requested",
                }
            )
            assert connection.scalar(sa.text(f"SELECT count(*) FROM {table_name}")) == 1


def test_failed_completion_index_retries_from_last_committed_revision() -> None:
    engine = sa.create_engine("sqlite://")
    _create_0050_baseline(engine)
    scripts = _script_directory()

    with engine.connect() as connection:
        with pytest.raises(
            RuntimeError,
            match="forced index creation failure: ix_completions_cancel_requested",
        ):
            _run_revisions(
                connection,
                scripts,
                UPGRADE_REVISIONS,
                is_upgrade=True,
                fail_on_index="ix_completions_cancel_requested",
            )

    with engine.connect() as connection:
        assert _current_revision(connection) == GENERATIONS_INDEX_REVISION
        _assert_cancel_intent_columns(connection)
        assert "ix_generations_cancel_requested" in _index_names(
            connection,
            "generations",
        )
        assert "ix_completions_cancel_requested" not in _index_names(
            connection,
            "completions",
        )

    with engine.connect() as connection:
        _run_revisions(
            connection,
            scripts,
            (COMPLETIONS_INDEX_REVISION,),
            is_upgrade=True,
        )

    with engine.connect() as connection:
        assert _current_revision(connection) == COMPLETIONS_INDEX_REVISION
        _assert_cancel_intent_columns(connection)
        assert "ix_completions_cancel_requested" in _index_names(
            connection,
            "completions",
        )


@pytest.mark.parametrize(
    ("revision_id", "index_name", "table_name", "columns"),
    (
        (
            GENERATIONS_INDEX_REVISION,
            "ix_generations_cancel_requested",
            "generations",
            ("cancel_requested_at", "id"),
        ),
        (
            COMPLETIONS_INDEX_REVISION,
            "ix_completions_cancel_requested",
            "completions",
            ("cancel_requested_at", "id"),
        ),
    ),
)
def test_postgresql_concurrent_index_retry_discards_incomplete_index(
    revision_id: str,
    index_name: str,
    table_name: str,
    columns: tuple[str, ...],
) -> None:
    script = _revision(_script_directory(), revision_id)
    operations = _RetryingPostgresqlIndexOperations(
        index_name=index_name,
        table_name=table_name,
        columns=columns,
    )
    original = script.module.op
    script.module.op = operations
    try:
        with pytest.raises(
            RuntimeError,
            match=f"forced concurrent index failure: {index_name}",
        ):
            script.module.upgrade()

        assert operations.index_is_valid is False

        script.module.upgrade()
    finally:
        script.module.op = original

    assert operations.index_is_valid is True
    assert operations.actions == ["create", "drop", "create"]
    assert operations.autocommit_entries == 2
    assert operations.autocommit_depth == 0
    assert operations.catalog_queries == [
        {"index_name": index_name, "table_name": table_name},
        {"index_name": index_name, "table_name": table_name},
    ]


@pytest.mark.parametrize(
    ("revision_id", "create_sql", "drop_sql"),
    (
        (
            GENERATIONS_INDEX_REVISION,
            "CREATE INDEX CONCURRENTLY ix_generations_cancel_requested",
            "DROP INDEX CONCURRENTLY ix_generations_cancel_requested",
        ),
        (
            COMPLETIONS_INDEX_REVISION,
            "CREATE INDEX CONCURRENTLY ix_completions_cancel_requested",
            "DROP INDEX CONCURRENTLY ix_completions_cancel_requested",
        ),
    ),
)
def test_cancellation_index_migrations_use_concurrent_postgres_indexes(
    revision_id: str,
    create_sql: str,
    drop_sql: str,
) -> None:
    sql = _render_postgresql_sql(_revision(_script_directory(), revision_id))

    assert create_sql in sql
    assert drop_sql in sql
