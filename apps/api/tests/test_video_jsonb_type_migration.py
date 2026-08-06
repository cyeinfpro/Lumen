from __future__ import annotations

from io import StringIO
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0061_video_jsonb_types.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "video_jsonb_type_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ScalarResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> str | None:
        return self._value


class _FakeBind:
    def __init__(self, column_types: dict[tuple[str, str], str], dialect: str) -> None:
        self.column_types = column_types
        self.dialect = SimpleNamespace(name=dialect)

    def execute(self, _statement: object, params: dict[str, str]) -> _ScalarResult:
        key = (params["table_name"], params["column_name"])
        return _ScalarResult(self.column_types.get(key))


class _FakeOperations:
    def __init__(
        self,
        column_types: dict[tuple[str, str], str],
        *,
        dialect: str = "postgresql",
    ) -> None:
        self.bind = _FakeBind(column_types, dialect)
        self.alterations: list[tuple[str, str, dict[str, object]]] = []

    def get_bind(self) -> _FakeBind:
        return self.bind

    def alter_column(
        self,
        table_name: str,
        column_name: str,
        **kwargs: object,
    ) -> None:
        self.alterations.append((table_name, column_name, kwargs))
        target = kwargs["type_"]
        self.bind.column_types[(table_name, column_name)] = (
            "jsonb" if isinstance(target, postgresql.JSONB) else "json"
        )


def _column_types(value: str) -> dict[tuple[str, str], str]:
    return {
        ("videos", "metadata_jsonb"): value,
        ("video_generations", "upstream_request"): value,
        ("video_generations", "upstream_response"): value,
        ("video_generations", "diagnostics"): value,
    }


def test_upgrade_converts_video_json_columns_to_jsonb() -> None:
    migration = _load_migration()
    operations = _FakeOperations(_column_types("json"))
    migration.op = operations

    migration.upgrade()

    assert operations.bind.column_types == _column_types("jsonb")
    assert [
        (table_name, column_name, kwargs["postgresql_using"])
        for table_name, column_name, kwargs in operations.alterations
    ] == [
        ("videos", "metadata_jsonb", "metadata_jsonb::jsonb"),
        (
            "video_generations",
            "upstream_request",
            "upstream_request::jsonb",
        ),
        (
            "video_generations",
            "upstream_response",
            "upstream_response::jsonb",
        ),
        ("video_generations", "diagnostics", "diagnostics::jsonb"),
    ]
    assert all(
        isinstance(kwargs["existing_type"], postgresql.JSON)
        and isinstance(kwargs["type_"], postgresql.JSONB)
        for _, _, kwargs in operations.alterations
    )


def test_upgrade_is_idempotent_when_columns_are_already_jsonb() -> None:
    migration = _load_migration()
    operations = _FakeOperations(_column_types("jsonb"))
    migration.op = operations

    migration.upgrade()

    assert operations.alterations == []


def test_downgrade_converts_video_jsonb_columns_to_json() -> None:
    migration = _load_migration()
    operations = _FakeOperations(_column_types("jsonb"))
    migration.op = operations

    migration.downgrade()

    assert operations.bind.column_types == _column_types("json")
    assert all(
        isinstance(kwargs["existing_type"], postgresql.JSONB)
        and isinstance(kwargs["type_"], postgresql.JSON)
        for _, _, kwargs in operations.alterations
    )


def test_upgrade_rejects_unexpected_column_type() -> None:
    migration = _load_migration()
    column_types = _column_types("json")
    column_types[("videos", "metadata_jsonb")] = "text"
    operations = _FakeOperations(column_types)
    migration.op = operations

    with pytest.raises(
        RuntimeError,
        match=r"expected json, found text",
    ):
        migration.upgrade()

    assert operations.alterations == []


def test_upgrade_rejects_missing_postgres_column() -> None:
    migration = _load_migration()
    column_types = _column_types("json")
    del column_types[("videos", "metadata_jsonb")]
    operations = _FakeOperations(column_types)
    migration.op = operations

    with pytest.raises(
        RuntimeError,
        match=r"missing required column videos\.metadata_jsonb",
    ):
        migration.upgrade()

    assert operations.alterations == []


def test_non_postgres_upgrade_is_a_noop() -> None:
    migration = _load_migration()
    operations = _FakeOperations(_column_types("json"), dialect="sqlite")
    migration.op = operations

    migration.upgrade()

    assert operations.alterations == []


def test_upgrade_renders_postgres_jsonb_type_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration = _load_migration()
    monkeypatch.setattr(migration, "op", Operations(context))
    monkeypatch.setattr(migration, "_column_type", lambda *_args: "json")

    migration.upgrade()

    sql = output.getvalue()
    assert (
        "ALTER TABLE videos ALTER COLUMN metadata_jsonb "
        "TYPE JSONB USING metadata_jsonb::jsonb;"
    ) in sql
    for column_name in (
        "upstream_request",
        "upstream_response",
        "diagnostics",
    ):
        assert (
            "ALTER TABLE video_generations "
            f"ALTER COLUMN {column_name} TYPE JSONB "
            f"USING {column_name}::jsonb;"
        ) in sql
