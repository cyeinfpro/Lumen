from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0072_agent_runtime_contracts.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_runtime_contracts", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_runtime_contracts_migration_shape(monkeypatch) -> None:
    migration = _load()
    added: list[tuple[str, str]] = []
    tables: list[str] = []
    constraints: list[tuple[str, bool]] = []
    validated: list[str] = []

    class Bind:
        class Dialect:
            name = "postgresql"

        dialect = Dialect()

    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added.append((table, column.name)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, *_args, **kwargs: constraints.append(
            (name, kwargs.get("postgresql_not_valid") is True)
        ),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: Bind())
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: validated.append(str(statement)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *_args, **_kwargs: tables.append(name),
    )
    monkeypatch.setattr(migration.op, "create_index", lambda *_a, **_kw: None)

    migration.upgrade()

    assert added == [
        ("agent_runs", "output_revision"),
        ("agent_runs", "output_runtime_seq"),
        ("agent_runs", "transcript_jsonb"),
    ]
    assert "agent_provider_calls" in tables
    assert constraints == [
        ("ck_agent_runs_output_revision_nonnegative", True),
        ("ck_agent_runs_output_runtime_seq_nonnegative", True),
    ]
    assert validated == [
        'ALTER TABLE "agent_runs" VALIDATE CONSTRAINT '
        '"ck_agent_runs_output_revision_nonnegative"',
        'ALTER TABLE "agent_runs" VALIDATE CONSTRAINT '
        '"ck_agent_runs_output_runtime_seq_nonnegative"',
    ]


def test_agent_runtime_contracts_downgrade_refuses_new_state(monkeypatch) -> None:
    migration = _load()

    class Result:
        def scalar(self) -> int:
            return 1

    class Bind:
        def execute(self, _statement):
            return Result()

    monkeypatch.setattr(migration.op, "get_bind", lambda: Bind())
    with pytest.raises(RuntimeError, match="backup restoration"):
        migration.downgrade()
