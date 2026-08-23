from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from io import StringIO
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool


API_ROOT = Path(__file__).resolve().parents[1]
VERSIONS = API_ROOT / "alembic" / "versions"
ALEMBIC_ROOT = API_ROOT / "alembic"
if str(ALEMBIC_ROOT) not in sys.path:
    sys.path.insert(0, str(ALEMBIC_ROOT))

from concurrent_index_retry import (  # noqa: E402
    COMPLETIONS_CANCEL_REVISION,
    GENERATIONS_CANCEL_REVISION,
    USERS_ACTIVE_EMAIL_REVISION,
    prepare_concurrent_index_retry_boundary,
)
from app.config import settings  # noqa: E402

REPAIR_FILENAME = "0057_repair_concurrent_indexes.py"
REPAIR_REVISION = "0057_repair_concurrent_indexes"
STORAGE_OPERATION_REVISION = "0058_storage_apply_operations"
REFERENCE_TOKEN_REVISION = "0059_reference_token_expiry"
TELEGRAM_CONTROL_REVISION = "0060_telegram_delivery_control"
VIDEO_JSONB_TYPES_REVISION = "0061_video_jsonb_types"
TELEGRAM_EFFECT_REVISION = "0062_tg_control_effect_fence"
STORAGE_RETRY_REVISION = "0063_storage_apply_retry_fence"
TELEGRAM_TERMINAL_GUARD_REVISION = "0064_tg_effect_terminal_guard"
SEEDANCE_25_REVISION = "0065_seedance_25_defaults"
SEEDANCE_DURATION_ONLINE_REVISION = "0066_seedance_duration_online"
SEEDANCE_25_1080P_REVISION = "0067_seedance_25_1080p"
OPENAI_CHAT_DEFAULTS_REVISION = "0068_openai_chat_defaults"
AGENT_FOUNDATION_REVISION = "0069_agent_foundation"
AGENT_SESSION_CONTEXT_REVISION = "0070_agent_session_context"


@dataclass(frozen=True)
class _IndexCase:
    name: str
    table_name: str
    columns: tuple[str, ...]
    predicate: str
    catalog_predicate: str
    unique: bool = False


CASES = (
    _IndexCase(
        name="uq_users_email_active",
        table_name="users",
        columns=("email",),
        predicate="deleted_at IS NULL",
        catalog_predicate="(deleted_at IS NULL)",
        unique=True,
    ),
    _IndexCase(
        name="ix_generations_cancel_requested",
        table_name="generations",
        columns=("cancel_requested_at", "id"),
        predicate=(
            "cancel_requested_at IS NOT NULL AND status IN ('queued', 'running')"
        ),
        catalog_predicate=(
            "((cancel_requested_at IS NOT NULL) AND "
            "((status)::text = ANY ((ARRAY['queued'::character varying, "
            "'running'::character varying])::text[])))"
        ),
    ),
    _IndexCase(
        name="ix_completions_cancel_requested",
        table_name="completions",
        columns=("cancel_requested_at", "id"),
        predicate=(
            "cancel_requested_at IS NOT NULL AND status IN ('queued', 'streaming')"
        ),
        catalog_predicate=(
            "((cancel_requested_at IS NOT NULL) AND "
            "((status)::text = ANY ((ARRAY['queued'::character varying, "
            "'streaming'::character varying])::text[])))"
        ),
    ),
)
CASES_BY_NAME = {case.name: case for case in CASES}


def _load_migration(filename: str = REPAIR_FILENAME) -> ModuleType:
    path = VERSIONS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, mapping: dict[str, Any] | None) -> None:
        self._mapping = mapping

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._mapping


class _PostgresqlOperations:
    def __init__(self) -> None:
        self.dialect = SimpleNamespace(name="postgresql")
        self.as_sql = False
        self.autocommit_depth = 0
        self.autocommit_entries = 0
        self.actions: list[str] = []
        self.indexes: dict[str, dict[str, Any]] = {}
        self.constraint: dict[str, Any] | None = None
        self.create_outcomes: dict[str, str] = {}
        self.drop_outcomes: dict[str, str] = {}
        self.constraint_create_outcome = "success"
        self.constraint_drop_outcome = "success"

    def get_bind(self) -> _PostgresqlOperations:
        return self

    def get_context(self) -> _PostgresqlOperations:
        return self

    @contextmanager
    def autocommit_block(self) -> Iterator[None]:
        self.autocommit_entries += 1
        self.autocommit_depth += 1
        try:
            yield
        finally:
            self.autocommit_depth -= 1

    def set_index(
        self,
        index_name: str,
        *,
        valid: bool,
        columns: tuple[str, ...] | None = None,
        predicate: str | None = None,
        is_being_built: bool = False,
        attribute_count: int | None = None,
    ) -> None:
        case = CASES_BY_NAME[index_name]
        actual_columns = columns or case.columns
        self.indexes[index_name] = {
            "is_valid": valid,
            "is_ready": valid,
            "is_live": True,
            "is_unique": case.unique,
            "is_primary": False,
            "is_exclusion": False,
            "is_replica_identity": False,
            "is_clustered": False,
            "access_method": "btree",
            "key_expressions": list(actual_columns),
            "key_opclasses": ["text_ops"] * len(actual_columns),
            "key_opclasses_are_default": [True] * len(actual_columns),
            "key_collations_match_columns": [True] * len(actual_columns),
            "key_options": [0] * len(actual_columns),
            "attribute_count": (
                len(actual_columns) if attribute_count is None else attribute_count
            ),
            "key_attribute_count": len(actual_columns),
            "predicate": (case.catalog_predicate if predicate is None else predicate),
            "backs_constraint": False,
            "uses_default_tablespace": True,
            "reloptions": None,
            "is_being_built": is_being_built,
            "nulls_not_distinct": False,
            "definition": (
                f"CREATE {'UNIQUE ' if case.unique else ''}INDEX "
                f"{case.name} ON {case.table_name} "
                f"USING btree ({', '.join(actual_columns)})"
            ),
        }

    def set_all_indexes(self, *, valid: bool = True) -> None:
        for case in CASES:
            self.set_index(case.name, valid=valid)

    def set_legacy_constraint(
        self,
        *,
        columns: tuple[str, ...] = ("email",),
        definition: str | None = None,
        key_expressions: tuple[str, ...] | None = None,
        key_opclasses_are_default: tuple[bool, ...] | None = None,
        key_collations_match_columns: tuple[bool, ...] | None = None,
        key_options: tuple[int, ...] | None = None,
        attribute_count: int | None = None,
        predicate: str | None = None,
        nulls_not_distinct: bool = False,
    ) -> None:
        actual_key_expressions = key_expressions or columns
        actual_attribute_count = (
            len(actual_key_expressions) if attribute_count is None else attribute_count
        )
        self.constraint = {
            "constraint_type": "u",
            "is_validated": True,
            "is_deferrable": False,
            "is_initially_deferred": False,
            "columns": list(columns),
            "backing_index_name": "users_email_key",
            "backing_index_is_valid": True,
            "backing_index_is_ready": True,
            "backing_index_is_live": True,
            "backing_index_is_unique": True,
            "backing_index_is_primary": False,
            "backing_index_is_exclusion": False,
            "backing_index_is_replica_identity": False,
            "backing_index_is_clustered": False,
            "backing_index_access_method": "btree",
            "backing_index_key_expressions": list(actual_key_expressions),
            "backing_index_key_opclasses": ["text_ops"] * len(actual_key_expressions),
            "backing_index_key_opclasses_are_default": list(
                key_opclasses_are_default or (True,) * len(actual_key_expressions)
            ),
            "backing_index_key_collations_match_columns": list(
                key_collations_match_columns or (True,) * len(actual_key_expressions)
            ),
            "backing_index_key_options": list(
                key_options or (0,) * len(actual_key_expressions)
            ),
            "backing_index_attribute_count": actual_attribute_count,
            "backing_index_key_attribute_count": len(actual_key_expressions),
            "backing_index_predicate": predicate,
            "backing_index_backs_constraint": True,
            "backing_index_uses_default_tablespace": True,
            "backing_index_reloptions": None,
            "backing_index_is_being_built": False,
            "backing_index_nulls_not_distinct": nulls_not_distinct,
            "backing_index_definition": (
                "CREATE UNIQUE INDEX users_email_key ON users "
                f"USING btree ({', '.join(actual_key_expressions)})"
            ),
            "definition": (
                f"UNIQUE ({', '.join(columns)})" if definition is None else definition
            ),
        }

    def execute(
        self,
        statement: Any,
        parameters: dict[str, str],
    ) -> _Result:
        sql = str(statement)
        if "FROM pg_catalog.pg_index AS index_metadata" in sql:
            assert self.autocommit_depth == 1
            index_name = parameters["index_name"]
            case = CASES_BY_NAME[index_name]
            assert parameters == {
                "index_name": index_name,
                "table_name": case.table_name,
            }
            assert "pg_get_indexdef" in sql
            assert "pg_get_expr" in sql
            assert "pg_stat_progress_create_index" in sql
            return _Result(self.indexes.get(index_name))
        if "FROM pg_catalog.pg_constraint AS constraint_metadata" in sql:
            assert self.autocommit_depth == 0
            assert parameters == {
                "constraint_name": "users_email_key",
                "table_name": "users",
            }
            return _Result(self.constraint)
        raise AssertionError(f"unexpected SQL: {sql}")

    def create_index(
        self,
        index_name: str,
        table_name: str,
        columns: list[str],
        **kwargs: Any,
    ) -> None:
        assert self.autocommit_depth == 1
        case = CASES_BY_NAME[index_name]
        assert table_name == case.table_name
        assert tuple(columns) == case.columns
        assert kwargs["unique"] is case.unique
        assert kwargs["postgresql_concurrently"] is True
        assert str(kwargs["postgresql_where"]) == case.predicate
        assert set(kwargs) == {
            "unique",
            "postgresql_concurrently",
            "postgresql_where",
        }
        assert index_name not in self.indexes
        self.actions.append(f"create:{index_name}")
        outcome = self.create_outcomes.pop(index_name, "success")
        if outcome == "interrupted":
            self.set_index(index_name, valid=False)
            raise RuntimeError(f"forced interrupted create: {index_name}")
        self.set_index(index_name, valid=True)
        if outcome == "unknown_ack":
            raise RuntimeError(f"forced unknown create acknowledgement: {index_name}")

    def drop_index(
        self,
        index_name: str,
        *,
        table_name: str | None = None,
        schema: str | None = None,
        **kwargs: Any,
    ) -> None:
        assert self.autocommit_depth == 1
        case = CASES_BY_NAME[index_name]
        assert table_name == case.table_name
        assert schema is None
        assert kwargs == {
            "postgresql_concurrently": True,
            "if_exists": True,
        }
        assert index_name in self.indexes
        self.actions.append(f"drop:{index_name}")
        outcome = self.drop_outcomes.pop(index_name, "success")
        del self.indexes[index_name]
        if outcome == "unknown_ack":
            raise RuntimeError(f"forced unknown drop acknowledgement: {index_name}")

    def drop_constraint(
        self,
        constraint_name: str,
        table_name: str,
        *,
        type_: str,
        schema: str | None = None,
        if_exists: bool | None = None,
    ) -> None:
        assert self.autocommit_depth == 0
        assert constraint_name == "users_email_key"
        assert table_name == "users"
        assert type_ == "unique"
        assert schema is None
        assert if_exists is True
        assert self.constraint is not None
        self.actions.append("drop_constraint:users_email_key")
        outcome = self.constraint_drop_outcome
        self.constraint_drop_outcome = "success"
        self.constraint = None
        if outcome == "unknown_ack":
            raise RuntimeError("forced unknown constraint drop acknowledgement")

    def create_unique_constraint(
        self,
        constraint_name: str,
        table_name: str,
        columns: list[str],
        *,
        schema: str | None = None,
    ) -> None:
        assert self.autocommit_depth == 0
        assert constraint_name == "users_email_key"
        assert table_name == "users"
        assert columns == ["email"]
        assert schema is None
        assert self.constraint is None
        self.actions.append("create_constraint:users_email_key")
        outcome = self.constraint_create_outcome
        self.constraint_create_outcome = "success"
        self.set_legacy_constraint()
        if outcome == "unknown_ack":
            raise RuntimeError("forced unknown constraint create acknowledgement")


def _run(
    operations: _PostgresqlOperations,
    action: str = "upgrade",
) -> None:
    migration = _load_migration()
    original = migration.op
    migration.op = operations
    try:
        getattr(migration, action)()
    finally:
        migration.op = original


def _run_postgresql_migration(
    connection: sa.Connection,
    migration: ModuleType,
) -> None:
    context = MigrationContext.configure(connection)
    operations = Operations(context)
    original = migration.op
    migration.op = operations
    try:
        with context.begin_transaction():
            migration.upgrade()
    finally:
        migration.op = original


def _script_directory() -> ScriptDirectory:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def _postgresql_test_url() -> URL:
    raw_url = os.environ.get("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is required")
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("LUMEN_TEST_POSTGRES_URL must use PostgreSQL")
    return url.set(drivername="postgresql+psycopg2")


def _url_for_schema(url: URL, schema: str) -> URL:
    existing_options = str(url.query.get("options", "")).strip()
    search_path_option = f"-csearch_path={schema}"
    options = " ".join(
        value for value in (existing_options, search_path_option) if value
    )
    return url.update_query_dict({"options": options})


@contextmanager
def _isolated_postgresql_schema() -> Iterator[tuple[Engine, str, URL]]:
    url = _postgresql_test_url()
    engine = sa.create_engine(url, poolclass=NullPool)
    schema = f"concurrent_index_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(sa.schema.CreateSchema(schema))
    try:
        yield engine, schema, _url_for_schema(url, schema)
    finally:
        with engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()


def _programmatic_alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ALEMBIC_ROOT))
    config.set_main_option("prepend_sys_path", str(API_ROOT))
    assert config.cmd_opts is None
    return config


def _prepare_revision(
    connection: sa.Connection,
    *,
    schema: str,
    revision: str,
) -> None:
    connection.exec_driver_sql(f'SET search_path TO "{schema}"')
    connection.exec_driver_sql(
        """
        CREATE TABLE alembic_version (
            version_num varchar(32) NOT NULL PRIMARY KEY
        )
        """
    )
    connection.execute(
        sa.text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
        {"revision": revision},
    )


def _current_revision(engine: Engine, schema: str) -> str:
    with engine.connect() as connection:
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert isinstance(revision, str)
    return revision


def _relation_oid(engine: Engine, schema: str, relation_name: str) -> int | None:
    with engine.connect() as connection:
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        oid = connection.scalar(
            sa.text("SELECT pg_catalog.to_regclass(:name)::oid"),
            {"name": relation_name},
        )
    return None if oid is None else int(oid)


def _constraint_state(
    engine: Engine,
    schema: str,
    constraint_name: str,
) -> tuple[int, int] | None:
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                """
                SELECT
                    constraint_metadata.oid,
                    constraint_metadata.conindid
                FROM pg_catalog.pg_constraint AS constraint_metadata
                JOIN pg_catalog.pg_class AS table_relation
                  ON table_relation.oid = constraint_metadata.conrelid
                JOIN pg_catalog.pg_namespace AS table_namespace
                  ON table_namespace.oid = table_relation.relnamespace
                WHERE table_namespace.nspname = :schema
                  AND constraint_metadata.conname = :constraint_name
                """
            ),
            {
                "schema": schema,
                "constraint_name": constraint_name,
            },
        ).one_or_none()
    if row is None:
        return None
    return int(row[0]), int(row[1])


@pytest.mark.parametrize(
    ("filename", "expected_sha256"),
    [
        (
            "0025_users_active_email_unique.py",
            "25cd16719b35fc9df3932404c623ae33192058e78761ec8def03a160ef4bf2c0",
        ),
        (
            "0053_cancel_intent_generations.py",
            "37fe4c57b84bf443361eac834798f9629e4c6ca95b95ce0696ee86cad90b2ba5",
        ),
        (
            "0054_cancel_intent_completions.py",
            "53673b0619be9aed26cebf4dae0b5a70c8d8c41d899c82d722c6cb8483b3d4de",
        ),
    ],
)
def test_historical_concurrent_index_migrations_are_immutable(
    filename: str,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256((VERSIONS / filename).read_bytes()).hexdigest()

    assert digest == expected_sha256


def test_repair_revision_remains_in_the_single_head_chain() -> None:
    scripts = _script_directory()
    repair = scripts.get_revision(REPAIR_REVISION)
    storage_operations = scripts.get_revision(STORAGE_OPERATION_REVISION)
    reference_tokens = scripts.get_revision(REFERENCE_TOKEN_REVISION)
    telegram_control = scripts.get_revision(TELEGRAM_CONTROL_REVISION)
    video_jsonb_types = scripts.get_revision(VIDEO_JSONB_TYPES_REVISION)
    telegram_effect = scripts.get_revision(TELEGRAM_EFFECT_REVISION)
    storage_retry = scripts.get_revision(STORAGE_RETRY_REVISION)
    telegram_terminal_guard = scripts.get_revision(TELEGRAM_TERMINAL_GUARD_REVISION)
    seedance_25 = scripts.get_revision(SEEDANCE_25_REVISION)
    seedance_duration_online = scripts.get_revision(SEEDANCE_DURATION_ONLINE_REVISION)
    seedance_25_1080p = scripts.get_revision(SEEDANCE_25_1080P_REVISION)
    openai_chat_defaults = scripts.get_revision(OPENAI_CHAT_DEFAULTS_REVISION)
    agent_foundation = scripts.get_revision(AGENT_FOUNDATION_REVISION)
    agent_session_context = scripts.get_revision(AGENT_SESSION_CONTEXT_REVISION)

    assert repair is not None
    assert repair.down_revision == "0056_outbox_due_index"
    assert storage_operations is not None
    assert storage_operations.down_revision == REPAIR_REVISION
    assert reference_tokens is not None
    assert reference_tokens.down_revision == STORAGE_OPERATION_REVISION
    assert telegram_control is not None
    assert telegram_control.down_revision == REFERENCE_TOKEN_REVISION
    assert video_jsonb_types is not None
    assert video_jsonb_types.down_revision == TELEGRAM_CONTROL_REVISION
    assert telegram_effect is not None
    assert telegram_effect.down_revision == VIDEO_JSONB_TYPES_REVISION
    assert storage_retry is not None
    assert storage_retry.down_revision == TELEGRAM_EFFECT_REVISION
    assert telegram_terminal_guard is not None
    assert telegram_terminal_guard.down_revision == STORAGE_RETRY_REVISION
    assert seedance_25 is not None
    assert seedance_25.down_revision == TELEGRAM_TERMINAL_GUARD_REVISION
    assert seedance_duration_online is not None
    assert seedance_duration_online.down_revision == SEEDANCE_25_REVISION
    assert seedance_25_1080p is not None
    assert seedance_25_1080p.down_revision == SEEDANCE_DURATION_ONLINE_REVISION
    assert openai_chat_defaults is not None
    assert openai_chat_defaults.down_revision == SEEDANCE_25_1080P_REVISION
    assert agent_foundation is not None
    assert agent_foundation.down_revision == OPENAI_CHAT_DEFAULTS_REVISION
    assert agent_session_context is not None
    assert agent_session_context.down_revision == AGENT_FOUNDATION_REVISION
    assert scripts.get_heads() == [AGENT_SESSION_CONTEXT_REVISION]


@pytest.mark.parametrize(
    ("revision", "parent", "index_name", "expected_actions"),
    [
        (
            USERS_ACTIVE_EMAIL_REVISION,
            "0024_billing_cache_tokens",
            "uq_users_email_active",
            [
                "create_constraint:users_email_key",
                "drop:uq_users_email_active",
            ],
        ),
        (
            GENERATIONS_CANCEL_REVISION,
            "0052_task_execution_epoch",
            "ix_generations_cancel_requested",
            ["drop:ix_generations_cancel_requested"],
        ),
        (
            COMPLETIONS_CANCEL_REVISION,
            GENERATIONS_CANCEL_REVISION,
            "ix_completions_cancel_requested",
            ["drop:ix_completions_cancel_requested"],
        ),
    ],
)
def test_retry_preflight_restores_historical_upgrade_entry_state(
    revision: str,
    parent: str,
    index_name: str,
    expected_actions: list[str],
) -> None:
    operations = _PostgresqlOperations()
    operations.set_index(index_name, valid=True)

    prepare_concurrent_index_retry_boundary(
        operations,
        command="upgrade",
        current_revision=parent,
        pending_revision_ids={revision},
    )

    assert operations.actions == expected_actions
    assert index_name not in operations.indexes
    if revision == USERS_ACTIVE_EMAIL_REVISION:
        assert operations.constraint is not None


@pytest.mark.parametrize(
    ("revision", "index_name", "expected_actions"),
    [
        (
            USERS_ACTIVE_EMAIL_REVISION,
            "uq_users_email_active",
            [
                "create:uq_users_email_active",
                "drop_constraint:users_email_key",
            ],
        ),
        (
            GENERATIONS_CANCEL_REVISION,
            "ix_generations_cancel_requested",
            ["create:ix_generations_cancel_requested"],
        ),
        (
            COMPLETIONS_CANCEL_REVISION,
            "ix_completions_cancel_requested",
            ["create:ix_completions_cancel_requested"],
        ),
    ],
)
def test_retry_preflight_restores_historical_downgrade_entry_state(
    revision: str,
    index_name: str,
    expected_actions: list[str],
) -> None:
    operations = _PostgresqlOperations()
    if revision == USERS_ACTIVE_EMAIL_REVISION:
        operations.set_legacy_constraint()

    prepare_concurrent_index_retry_boundary(
        operations,
        command="downgrade",
        current_revision=revision,
        pending_revision_ids={revision},
    )

    assert operations.actions == expected_actions
    assert index_name in operations.indexes
    if revision == USERS_ACTIVE_EMAIL_REVISION:
        assert operations.constraint is None


def test_retry_preflight_ignores_noop_or_unrelated_targets() -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    operations.set_legacy_constraint()

    prepare_concurrent_index_retry_boundary(
        operations,
        command="upgrade",
        current_revision="0052_task_execution_epoch",
        pending_revision_ids=set(),
    )
    prepare_concurrent_index_retry_boundary(
        operations,
        command="downgrade",
        current_revision=COMPLETIONS_CANCEL_REVISION,
        pending_revision_ids={GENERATIONS_CANCEL_REVISION},
    )

    assert operations.actions == []


def test_retry_preflight_constraint_create_unknown_ack_is_idempotent() -> None:
    operations = _PostgresqlOperations()
    operations.set_index("uq_users_email_active", valid=True)
    operations.constraint_create_outcome = "unknown_ack"

    prepare_concurrent_index_retry_boundary(
        operations,
        command="upgrade",
        current_revision="0024_billing_cache_tokens",
        pending_revision_ids={USERS_ACTIVE_EMAIL_REVISION},
    )

    assert operations.actions == [
        "create_constraint:users_email_key",
        "drop:uq_users_email_active",
    ]
    assert operations.constraint is not None


def test_retry_preflight_include_constraint_fails_before_any_index_drop() -> None:
    operations = _PostgresqlOperations()
    operations.set_index("uq_users_email_active", valid=True)
    operations.set_legacy_constraint(
        definition="UNIQUE (email) INCLUDE (id)",
        attribute_count=2,
    )

    with pytest.raises(RuntimeError, match="existing definition is incompatible"):
        prepare_concurrent_index_retry_boundary(
            operations,
            command="upgrade",
            current_revision="0024_billing_cache_tokens",
            pending_revision_ids={USERS_ACTIVE_EMAIL_REVISION},
        )

    assert operations.actions == []
    assert operations.constraint is not None
    assert "uq_users_email_active" in operations.indexes


def test_already_upgraded_postgresql_state_is_idempotent() -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()

    _run(operations)
    _run(operations)

    assert operations.actions == []
    assert set(operations.indexes) == {case.name for case in CASES}
    assert operations.constraint is None


def test_missing_indexes_and_residual_legacy_constraint_are_repaired() -> None:
    operations = _PostgresqlOperations()
    operations.set_legacy_constraint()

    _run(operations)
    _run(operations)

    assert operations.actions == [
        "create:uq_users_email_active",
        "create:ix_generations_cancel_requested",
        "create:ix_completions_cancel_requested",
        "drop_constraint:users_email_key",
    ]
    assert all(metadata["is_valid"] for metadata in operations.indexes.values())
    assert operations.constraint is None


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_matching_invalid_index_is_cleaned_and_rebuilt(
    case: _IndexCase,
) -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    operations.set_index(case.name, valid=False)

    _run(operations)
    _run(operations)

    assert operations.actions == [
        f"drop:{case.name}",
        f"create:{case.name}",
    ]
    assert operations.indexes[case.name]["is_valid"] is True


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_interrupted_creation_recovers_when_the_revision_retries(
    case: _IndexCase,
) -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    del operations.indexes[case.name]
    operations.create_outcomes[case.name] = "interrupted"

    with pytest.raises(RuntimeError, match=f"forced interrupted create: {case.name}"):
        _run(operations)

    assert operations.indexes[case.name]["is_valid"] is False

    _run(operations)

    assert operations.actions == [
        f"create:{case.name}",
        f"drop:{case.name}",
        f"create:{case.name}",
    ]
    assert operations.indexes[case.name]["is_valid"] is True
    assert operations.autocommit_depth == 0


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_unknown_create_ack_requires_valid_catalog_confirmation(
    case: _IndexCase,
) -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    del operations.indexes[case.name]
    operations.create_outcomes[case.name] = "unknown_ack"

    _run(operations)
    _run(operations)

    assert operations.actions == [f"create:{case.name}"]
    assert operations.indexes[case.name]["is_valid"] is True


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_unknown_invalid_index_drop_ack_continues_with_rebuild(
    case: _IndexCase,
) -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    operations.set_index(case.name, valid=False)
    operations.drop_outcomes[case.name] = "unknown_ack"

    _run(operations)

    assert operations.actions == [
        f"drop:{case.name}",
        f"create:{case.name}",
    ]
    assert operations.indexes[case.name]["is_valid"] is True


@pytest.mark.parametrize("valid", (False, True), ids=("invalid", "valid"))
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_incompatible_same_name_index_fails_closed_without_drop(
    case: _IndexCase,
    valid: bool,
) -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    incompatible_columns = ("wrong_column", *case.columns[1:])
    operations.set_index(
        case.name,
        valid=valid,
        columns=incompatible_columns,
    )

    with pytest.raises(RuntimeError, match="existing definition is incompatible"):
        _run(operations)

    assert operations.actions == []
    assert case.name in operations.indexes


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_include_column_drift_fails_closed(
    case: _IndexCase,
) -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    operations.set_index(
        case.name,
        valid=True,
        attribute_count=len(case.columns) + 1,
    )

    with pytest.raises(RuntimeError, match="attribute_count"):
        _run(operations)

    assert operations.actions == []


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_active_invalid_index_build_is_not_dropped(
    case: _IndexCase,
) -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    operations.set_index(
        case.name,
        valid=False,
        is_being_built=True,
    )

    with pytest.raises(RuntimeError, match="build is still active"):
        _run(operations)

    assert operations.actions == []
    assert case.name in operations.indexes


def test_legacy_constraint_drop_unknown_ack_is_idempotent() -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    operations.set_legacy_constraint()
    operations.constraint_drop_outcome = "unknown_ack"

    _run(operations)
    _run(operations)

    assert operations.actions == ["drop_constraint:users_email_key"]
    assert operations.constraint is None


def test_incompatible_legacy_constraint_fails_closed() -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    operations.set_legacy_constraint(columns=("username",))

    with pytest.raises(RuntimeError, match="existing definition is incompatible"):
        _run(operations)

    assert operations.actions == []
    assert operations.constraint is not None


@pytest.mark.parametrize(
    ("constraint_kwargs", "message"),
    [
        (
            {"key_expressions": ("lower(email)",)},
            "columns=",
        ),
        (
            {"key_opclasses_are_default": (False,)},
            "operator classes",
        ),
        (
            {"key_collations_match_columns": (False,)},
            "collations",
        ),
        (
            {"key_options": (1,)},
            "ordering/null options",
        ),
        (
            {"predicate": "email IS NOT NULL"},
            "predicate=",
        ),
        (
            {"nulls_not_distinct": True},
            "NULLS NOT DISTINCT",
        ),
    ],
)
def test_legacy_constraint_backing_index_drift_fails_closed(
    constraint_kwargs: dict[str, Any],
    message: str,
) -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    operations.set_legacy_constraint(**constraint_kwargs)

    with pytest.raises(RuntimeError, match=message):
        _run(operations)

    assert operations.actions == []
    assert operations.constraint is not None


def test_repair_revision_downgrade_does_not_remove_historical_indexes() -> None:
    operations = _PostgresqlOperations()
    operations.set_all_indexes()
    operations.set_legacy_constraint()

    _run(operations, "downgrade")

    assert operations.actions == []
    assert set(operations.indexes) == {case.name for case in CASES}
    assert operations.constraint is not None


def test_repair_revision_fails_closed_for_offline_postgresql_sql() -> None:
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
        with pytest.raises(
            RuntimeError,
            match="requires an online PostgreSQL migration",
        ):
            migration.upgrade()
    finally:
        migration.op = original

    assert output.getvalue() == ""


def test_repair_revision_is_a_sqlite_noop() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    users = sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    generations = sa.Table(
        "generations",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False),
    )
    completions = sa.Table(
        "completions",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False),
    )
    sa.Index(
        "uq_users_email_active",
        users.c.email,
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
    )
    sa.Index(
        "ix_generations_cancel_requested",
        generations.c.cancel_requested_at,
        generations.c.id,
        sqlite_where=sa.text(
            "cancel_requested_at IS NOT NULL AND status IN ('queued', 'running')"
        ),
    )
    sa.Index(
        "ix_completions_cancel_requested",
        completions.c.cancel_requested_at,
        completions.c.id,
        sqlite_where=sa.text(
            "cancel_requested_at IS NOT NULL AND status IN ('queued', 'streaming')"
        ),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    with engine.begin() as connection:
        before = {
            table_name: {
                index["name"]
                for index in sa.inspect(connection).get_indexes(table_name)
            }
            for table_name in ("users", "generations", "completions")
        }
        operations = Operations(MigrationContext.configure(connection))
        original = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            migration.downgrade()
        finally:
            migration.op = original
        after = {
            table_name: {
                index["name"]
                for index in sa.inspect(connection).get_indexes(table_name)
            }
            for table_name in ("users", "generations", "completions")
        }

    assert after == before


def test_programmatic_upgrade_recovers_crashed_0053_boundary_against_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _isolated_postgresql_schema() as (engine, schema, schema_url):
        with engine.begin() as connection:
            _prepare_revision(
                connection,
                schema=schema,
                revision="0052_task_execution_epoch",
            )
            connection.exec_driver_sql(
                """
                CREATE TABLE generations (
                    id varchar(36) PRIMARY KEY,
                    cancel_requested_at timestamptz,
                    status varchar(32) NOT NULL
                )
                """
            )
            connection.exec_driver_sql(
                """
                CREATE INDEX ix_generations_cancel_requested
                ON generations (cancel_requested_at, id)
                WHERE cancel_requested_at IS NOT NULL
                  AND status IN ('queued', 'running')
                """
            )
        crashed_index_oid = _relation_oid(
            engine,
            schema,
            "ix_generations_cancel_requested",
        )
        assert crashed_index_oid is not None

        monkeypatch.setattr(
            settings,
            "database_url",
            schema_url.render_as_string(hide_password=False),
        )
        config = _programmatic_alembic_config()

        command.upgrade(config, GENERATIONS_CANCEL_REVISION)

        repaired_index_oid = _relation_oid(
            engine,
            schema,
            "ix_generations_cancel_requested",
        )
        assert _current_revision(engine, schema) == GENERATIONS_CANCEL_REVISION
        assert repaired_index_oid is not None
        assert repaired_index_oid != crashed_index_oid

        command.upgrade(config, GENERATIONS_CANCEL_REVISION)

        assert _current_revision(engine, schema) == GENERATIONS_CANCEL_REVISION
        assert (
            _relation_oid(
                engine,
                schema,
                "ix_generations_cancel_requested",
            )
            == repaired_index_oid
        )


def test_programmatic_downgrade_recovers_crashed_0053_boundary_against_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _isolated_postgresql_schema() as (engine, schema, schema_url):
        with engine.begin() as connection:
            _prepare_revision(
                connection,
                schema=schema,
                revision=GENERATIONS_CANCEL_REVISION,
            )
            connection.exec_driver_sql(
                """
                CREATE TABLE generations (
                    id varchar(36) PRIMARY KEY,
                    cancel_requested_at timestamptz,
                    status varchar(32) NOT NULL
                )
                """
            )
        assert (
            _relation_oid(
                engine,
                schema,
                "ix_generations_cancel_requested",
            )
            is None
        )

        monkeypatch.setattr(
            settings,
            "database_url",
            schema_url.render_as_string(hide_password=False),
        )
        config = _programmatic_alembic_config()

        command.downgrade(config, "0052_task_execution_epoch")

        assert _current_revision(engine, schema) == "0052_task_execution_epoch"
        assert (
            _relation_oid(
                engine,
                schema,
                "ix_generations_cancel_requested",
            )
            is None
        )

        command.downgrade(config, "0052_task_execution_epoch")

        assert _current_revision(engine, schema) == "0052_task_execution_epoch"
        assert (
            _relation_oid(
                engine,
                schema,
                "ix_generations_cancel_requested",
            )
            is None
        )


def test_programmatic_upgrade_rejects_include_constraint_without_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _isolated_postgresql_schema() as (engine, schema, schema_url):
        with engine.begin() as connection:
            _prepare_revision(
                connection,
                schema=schema,
                revision="0024_billing_cache_tokens",
            )
            connection.exec_driver_sql(
                """
                CREATE TABLE users (
                    id varchar(36) PRIMARY KEY,
                    email varchar(255) NOT NULL,
                    deleted_at timestamptz,
                    CONSTRAINT users_email_key
                        UNIQUE (email) INCLUDE (id)
                )
                """
            )
            connection.exec_driver_sql(
                """
                CREATE UNIQUE INDEX uq_users_email_active
                ON users (email)
                WHERE deleted_at IS NULL
                """
            )

        before = (
            _constraint_state(engine, schema, "users_email_key"),
            _relation_oid(engine, schema, "uq_users_email_active"),
            _current_revision(engine, schema),
        )
        assert before[0] is not None
        assert before[1] is not None

        monkeypatch.setattr(
            settings,
            "database_url",
            schema_url.render_as_string(hide_password=False),
        )
        config = _programmatic_alembic_config()

        with pytest.raises(
            RuntimeError,
            match="existing definition is incompatible",
        ):
            command.upgrade(config, USERS_ACTIVE_EMAIL_REVISION)

        after = (
            _constraint_state(engine, schema, "users_email_key"),
            _relation_oid(engine, schema, "uq_users_email_active"),
            _current_revision(engine, schema),
        )
        assert after == before


def test_repair_revision_against_postgresql() -> None:
    raw_url = os.environ.get("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is required")
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("LUMEN_TEST_POSTGRES_URL must use PostgreSQL")

    engine = sa.create_engine(
        url.set(drivername="postgresql+psycopg2"),
        poolclass=NullPool,
    )
    schema = f"concurrent_index_{uuid4().hex}"
    historical = [
        _load_migration("0025_users_active_email_unique.py"),
        _load_migration("0053_cancel_intent_generations.py"),
        _load_migration("0054_cancel_intent_completions.py"),
    ]
    repair = _load_migration()

    with engine.connect() as connection:
        try:
            connection.execute(sa.schema.CreateSchema(schema))
            connection.exec_driver_sql(f'SET search_path TO "{schema}"')
            connection.exec_driver_sql(
                """
                CREATE TABLE users (
                    id varchar(36) PRIMARY KEY,
                    email varchar(255) NOT NULL,
                    deleted_at timestamptz,
                    CONSTRAINT users_email_key UNIQUE (email)
                )
                """
            )
            connection.exec_driver_sql(
                """
                CREATE TABLE generations (
                    id varchar(36) PRIMARY KEY,
                    cancel_requested_at timestamptz,
                    status varchar(32) NOT NULL
                )
                """
            )
            connection.exec_driver_sql(
                """
                CREATE TABLE completions (
                    id varchar(36) PRIMARY KEY,
                    cancel_requested_at timestamptz,
                    status varchar(32) NOT NULL
                )
                """
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO users (id, email)
                    VALUES ('user-1', 'first@example.test')
                    """
                )
            )
            connection.commit()

            for migration in historical:
                _run_postgresql_migration(connection, migration)
            _run_postgresql_migration(connection, repair)
            _run_postgresql_migration(connection, repair)

            valid_indexes = set(
                connection.scalars(
                    sa.text(
                        """
                        SELECT index_relation.relname
                        FROM pg_catalog.pg_index AS index_metadata
                        JOIN pg_catalog.pg_class AS index_relation
                          ON index_relation.oid = index_metadata.indexrelid
                        JOIN pg_catalog.pg_class AS table_relation
                          ON table_relation.oid = index_metadata.indrelid
                        JOIN pg_catalog.pg_namespace AS table_namespace
                          ON table_namespace.oid = table_relation.relnamespace
                        WHERE table_namespace.nspname = :schema
                          AND index_metadata.indisvalid
                          AND index_metadata.indisready
                        """
                    ),
                    {"schema": schema},
                )
            )
            assert {case.name for case in CASES} <= valid_indexes
            assert (
                connection.scalar(
                    sa.text(
                        """
                        SELECT count(*)
                        FROM pg_catalog.pg_constraint AS constraint_metadata
                        JOIN pg_catalog.pg_class AS table_relation
                          ON table_relation.oid = constraint_metadata.conrelid
                        JOIN pg_catalog.pg_namespace AS table_namespace
                          ON table_namespace.oid = table_relation.relnamespace
                        WHERE table_namespace.nspname = :schema
                          AND constraint_metadata.conname = 'users_email_key'
                        """
                    ),
                    {"schema": schema},
                )
                == 0
            )
            connection.rollback()

            with engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as autocommit_connection:
                autocommit_connection.exec_driver_sql(f'SET search_path TO "{schema}"')
                autocommit_connection.exec_driver_sql(
                    "DROP INDEX CONCURRENTLY ix_generations_cancel_requested"
                )

            _run_postgresql_migration(connection, repair)
            assert connection.scalar(
                sa.text(
                    """
                    SELECT index_metadata.indisvalid
                    FROM pg_catalog.pg_index AS index_metadata
                    WHERE index_metadata.indexrelid =
                        pg_catalog.to_regclass(
                            'ix_generations_cancel_requested'
                        )
                    """
                )
            )
            connection.rollback()

            with engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as autocommit_connection:
                autocommit_connection.exec_driver_sql(f'SET search_path TO "{schema}"')
                autocommit_connection.exec_driver_sql(
                    "DROP INDEX CONCURRENTLY uq_users_email_active"
                )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO users (id, email)
                    VALUES
                        ('user-2', 'duplicate@example.test'),
                        ('user-3', 'duplicate@example.test')
                    """
                )
            )
            connection.commit()
            with engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as autocommit_connection:
                autocommit_connection.exec_driver_sql(f'SET search_path TO "{schema}"')
                with pytest.raises(DBAPIError):
                    autocommit_connection.exec_driver_sql(
                        """
                        CREATE UNIQUE INDEX CONCURRENTLY uq_users_email_active
                        ON users (email)
                        WHERE deleted_at IS NULL
                        """
                    )

            connection.execute(sa.text("DELETE FROM users WHERE id = 'user-3'"))
            connection.commit()
            _run_postgresql_migration(connection, repair)
            _run_postgresql_migration(connection, repair)

            repaired = connection.scalar(
                sa.text(
                    """
                    SELECT
                        index_metadata.indisvalid
                        AND index_metadata.indisready
                    FROM pg_catalog.pg_index AS index_metadata
                    WHERE index_metadata.indexrelid =
                        pg_catalog.to_regclass('uq_users_email_active')
                    """
                )
            )
            assert repaired is True
        finally:
            connection.rollback()
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
            connection.commit()
    engine.dispose()
