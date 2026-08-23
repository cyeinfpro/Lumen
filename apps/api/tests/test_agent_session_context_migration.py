from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from sqlalchemy import create_engine, text


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0070_agent_session_context.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "agent_session_context_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_session_context_migration_repairs_only_legacy_gpt_56_profiles(
    monkeypatch,
) -> None:
    migration = _load_migration()
    payload = {
        "providers": [
            {
                "name": "Chat",
                "agent_models": ["gpt-5.6-sol", "gpt-5.4"],
                "agent_context_window": 128_000,
            },
            {
                "name": "Custom",
                "agent_models": ["openai/gpt-5.6-terra"],
                "agent_context_window": 400_000,
            },
            {
                "name": "Other GPT-5.6",
                "agent_models": ["gpt-5.6-terra"],
                "agent_context_window": 128_000,
            },
            {
                "name": "Legacy model",
                "agent_models": ["gpt-5.4"],
                "agent_context_window": 128_000,
            },
        ],
        "proxies": [],
    }
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE system_settings (
              id TEXT PRIMARY KEY,
              key TEXT NOT NULL UNIQUE,
              value TEXT NOT NULL,
              updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            text(
                "INSERT INTO system_settings (id, key, value) VALUES "
                "('providers', 'providers', :value), "
                "('default-model', 'upstream.default_model', 'gpt-5.6-sol')"
            ),
            {"value": json.dumps(payload)},
        )
        alter_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        monkeypatch.setattr(
            migration.op,
            "alter_column",
            lambda *args, **kwargs: alter_calls.append((args, kwargs)),
        )

        migration.upgrade()
        assert alter_calls[-1][1]["server_default"] == "max"

        saved = json.loads(
            connection.execute(
                text("SELECT value FROM system_settings WHERE key = 'providers'")
            ).scalar_one()
        )
        assert [
            provider["agent_context_window"] for provider in saved["providers"]
        ] == [272_000, 400_000, 128_000, 128_000]

        migration.downgrade()
        assert alter_calls[-1][1]["server_default"] is None
        unchanged = json.loads(
            connection.execute(
                text("SELECT value FROM system_settings WHERE key = 'providers'")
            ).scalar_one()
        )
        assert unchanged == saved
    engine.dispose()
