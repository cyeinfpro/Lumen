from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0071_agent_audit_hardening.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "agent_audit_hardening_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_audit_hardening_migration_is_nullable_and_reversible(
    monkeypatch,
) -> None:
    migration = _load_migration()
    altered: list[tuple[str, str, object]] = []
    added: list[tuple[str, object]] = []
    created: list[str] = []
    dropped_columns: list[tuple[str, str]] = []

    class Result:
        def scalar(self) -> int:
            return 0

    class Bind:
        def execute(self, _statement):
            return Result()

    monkeypatch.setattr(
        migration.op,
        "alter_column",
        lambda table, column, **kwargs: altered.append(
            (table, column, kwargs.get("server_default"))
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added.append((table, column)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *_args, **_kwargs: created.append(name),
    )
    monkeypatch.setattr(migration.op, "create_index", lambda *_a, **_kw: None)
    monkeypatch.setattr(migration.op, "drop_index", lambda *_a, **_kw: None)
    monkeypatch.setattr(migration.op, "drop_table", lambda *_a, **_kw: None)
    monkeypatch.setattr(migration.op, "get_bind", lambda: Bind())
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: dropped_columns.append((table, column)),
    )

    migration.upgrade()

    assert altered[0] == ("agent_runs", "reasoning_effort", None)
    assert all(column.nullable for _table, column in added)
    assert "agent_session_images" in created

    migration.downgrade()

    assert altered[-1] == ("agent_runs", "reasoning_effort", "max")
    assert ("agent_runs", "continuation_source_run_id") in dropped_columns
    assert ("agent_sessions", "active_pi_compaction_run_id") in dropped_columns


def test_agent_audit_hardening_downgrade_refuses_new_state(monkeypatch) -> None:
    migration = _load_migration()

    class Result:
        def scalar(self) -> int:
            return 1

    class Bind:
        def execute(self, _statement):
            return Result()

    monkeypatch.setattr(migration.op, "get_bind", lambda: Bind())

    with pytest.raises(RuntimeError, match="backup restoration"):
        migration.downgrade()
