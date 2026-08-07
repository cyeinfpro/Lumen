from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa


GUARD_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "telegram_downgrade_guard.py"
)


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "telegram_downgrade_guard_under_test",
        GUARD_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _connection() -> sa.Connection:
    engine = sa.create_engine("sqlite://")
    connection = engine.connect()
    connection.exec_driver_sql(
        """
        CREATE TABLE telegram_control_commands (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'accepted',
            effect_status TEXT NOT NULL DEFAULT 'succeeded'
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE telegram_delivery_attempts (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'delivered'
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE telegram_delivery_quarantines (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL
        )
        """
    )
    return connection


def test_unrelated_downgrade_does_not_require_export() -> None:
    guard = _load_guard()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        guard.guard_telegram_downgrade(
            connection,
            pending_revision_ids={"0063_storage_apply_retry_fence"},
        )


def test_telegram_downgrade_requires_explicit_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _load_guard()
    connection = _connection()
    try:
        monkeypatch.delenv("LUMEN_MIGRATION_EXPORT_PATH", raising=False)
        with pytest.raises(RuntimeError, match="explicit export"):
            guard.guard_telegram_downgrade(
                connection,
                pending_revision_ids={"0060_telegram_delivery_control"},
            )
    finally:
        connection.close()


def test_unresolved_quarantine_blocks_before_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    connection = _connection()
    export_path = tmp_path / "telegram-export.json"
    try:
        connection.execute(
            sa.text(
                "INSERT INTO telegram_delivery_quarantines (id, status) "
                "VALUES ('q1', 'pending')"
            )
        )
        monkeypatch.setenv("LUMEN_MIGRATION_EXPORT_PATH", str(export_path))
        with pytest.raises(RuntimeError, match="unresolved Telegram quarantines"):
            guard.guard_telegram_downgrade(
                connection,
                pending_revision_ids={"0060_telegram_delivery_control"},
            )
        assert not export_path.exists()
    finally:
        connection.close()


def test_resolved_telegram_state_is_exported_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    connection = _connection()
    export_path = tmp_path / "nested" / "telegram-export.json"
    try:
        connection.execute(
            sa.text(
                "INSERT INTO telegram_control_commands (id) VALUES ('c1')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO telegram_delivery_attempts (id) VALUES ('d1')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO telegram_delivery_quarantines (id, status) "
                "VALUES ('q1', 'resolved')"
            )
        )
        monkeypatch.setenv("LUMEN_MIGRATION_EXPORT_PATH", str(export_path))
        guard.guard_telegram_downgrade(
            connection,
            pending_revision_ids={"0060_telegram_delivery_control"},
        )
        payload = json.loads(export_path.read_text(encoding="utf-8"))
        assert payload["telegram_control_commands"][0]["id"] == "c1"
        assert payload["telegram_delivery_attempts"][0]["id"] == "d1"
        assert payload["telegram_delivery_quarantines"][0]["status"] == "resolved"
    finally:
        connection.close()
