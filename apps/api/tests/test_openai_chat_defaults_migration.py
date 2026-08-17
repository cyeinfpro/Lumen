from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import create_engine, text


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0068_openai_chat_defaults.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "openai_chat_defaults_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_contract_tables(connection: Any) -> None:
    for statement in (
        """
        CREATE TABLE system_settings (
          id TEXT PRIMARY KEY,
          key TEXT NOT NULL UNIQUE,
          value TEXT NOT NULL,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE pricing_rules (
          id TEXT PRIMARY KEY,
          scope TEXT NOT NULL,
          key TEXT NOT NULL,
          variant TEXT NOT NULL,
          unit TEXT NOT NULL,
          price_micro INTEGER NOT NULL,
          priority INTEGER NOT NULL DEFAULT 0,
          enabled BOOLEAN NOT NULL DEFAULT 1,
          note TEXT,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          UNIQUE (scope, key, variant, unit)
        )
        """,
        """
        CREATE TABLE api_supplier_templates (
          id TEXT PRIMARY KEY,
          default_chat_model TEXT NOT NULL,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE completions (
          id TEXT PRIMARY KEY,
          model TEXT NOT NULL DEFAULT 'gpt-5.5'
        )
        """,
        """
        CREATE TABLE generations (
          id TEXT PRIMARY KEY,
          model TEXT NOT NULL DEFAULT 'gpt-5.5'
        )
        """,
    ):
        connection.exec_driver_sql(statement)


def test_openai_chat_defaults_migration_seeds_and_preserves_custom_pricing(
    monkeypatch,
) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        _create_contract_tables(connection)
        connection.execute(
            text(
                """
                INSERT INTO system_settings (id, key, value)
                VALUES
                  ('rate', 'billing.usd_to_rmb_rate', '7.2'),
                  ('model', 'upstream.default_model', 'gpt-5.5')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO api_supplier_templates (id, default_chat_model)
                VALUES ('supplier', 'gpt-5.4')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO pricing_rules
                  (id, scope, key, variant, unit, price_micro,
                   priority, enabled, note)
                VALUES
                  ('custom', 'chat_model', 'gpt-5.6-sol', 'default',
                   'per_1k_tokens_in', 123, 0, true, 'operator custom'),
                  ('custom-blank-note', 'chat_model', 'gpt-5.6-terra', 'default',
                   'per_1k_tokens_in', 456, 0, true, NULL)
                """
            )
        )

        alter_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        monkeypatch.setattr(
            migration.op,
            "alter_column",
            lambda *args, **kwargs: alter_calls.append((args, kwargs)),
        )

        migration.upgrade()

        assert len(alter_calls) == 3
        assert connection.execute(
            text(
                "SELECT value FROM system_settings "
                "WHERE key = 'upstream.default_model'"
            )
        ).scalar_one() == "gpt-5.6-sol"
        assert connection.execute(
            text(
                "SELECT default_chat_model FROM api_supplier_templates "
                "WHERE id = 'supplier'"
            )
        ).scalar_one() == "gpt-5.6-sol"
        prices = dict(
            connection.execute(
                text(
                    """
                    SELECT unit, price_micro
                    FROM pricing_rules
                    WHERE key = 'gpt-5.6-sol'
                    """
                )
            ).all()
        )
        assert prices == {
            "per_1k_tokens_in": 123,
            "per_1k_tokens_out": 216_000,
        }
        assert connection.execute(
            text(
                """
                SELECT price_micro
                FROM pricing_rules
                WHERE key = 'gpt-5.6-terra'
                  AND unit = 'per_1k_tokens_in'
                """
            )
        ).scalar_one() == 456
        assert connection.execute(
            text("SELECT count(*) FROM pricing_rules WHERE scope = 'chat_model'")
        ).scalar_one() == len(migration._OPENAI_STANDARD_CHAT_PRICES) * 2  # noqa: SLF001

        migration.downgrade()

        assert connection.execute(
            text(
                "SELECT value FROM system_settings "
                "WHERE key = 'upstream.default_model'"
            )
        ).scalar_one() == "gpt-5.5"
        assert connection.execute(
            text(
                "SELECT default_chat_model FROM api_supplier_templates "
                "WHERE id = 'supplier'"
            )
        ).scalar_one() == "gpt-5.4"
        assert connection.execute(
            text("SELECT count(*) FROM pricing_rules WHERE scope = 'chat_model'")
        ).scalar_one() == len(migration._OPENAI_STANDARD_CHAT_PRICES) * 2  # noqa: SLF001

    engine.dispose()
