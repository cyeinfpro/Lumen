from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0069_agent_foundation.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "agent_foundation_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_parent_tables(connection: sa.Connection) -> None:
    statements = (
        "CREATE TABLE users (id TEXT PRIMARY KEY)",
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, user_id TEXT NOT NULL)",
        "CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL)",
        "CREATE TABLE images (id TEXT PRIMARY KEY, user_id TEXT NOT NULL)",
        "CREATE TABLE api_supplier_templates (id TEXT PRIMARY KEY)",
        "CREATE TABLE user_api_credentials (id TEXT PRIMARY KEY)",
        """
        CREATE TABLE system_settings (
          id TEXT PRIMARY KEY,
          key TEXT NOT NULL UNIQUE,
          value TEXT,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
    )
    for statement in statements:
        connection.exec_driver_sql(statement)


def _insert_run(
    connection: sa.Connection,
    *,
    run_id: str,
    user_message_id: str,
    assistant_message_id: str,
    status: str = "queued",
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO agent_runs (
              id, agent_session_id, user_id, user_message_id,
              assistant_message_id, status, idempotency_key,
              request_fingerprint, request_snapshot_jsonb,
              account_mode_snapshot
            ) VALUES (
              :id, 'agent-session-1', 'user-1', :user_message_id,
              :assistant_message_id, :status, :idempotency_key,
              :request_fingerprint, '{}', 'wallet'
            )
            """
        ),
        {
            "id": run_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "status": status,
            "idempotency_key": f"idem-{run_id}",
            "request_fingerprint": (run_id[-1:] or "a") * 64,
        },
    )


def test_agent_foundation_migration_round_trip_and_concurrency_guards() -> None:
    engine = sa.create_engine("sqlite://")
    migration = _load_migration()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        _create_parent_tables(connection)
        connection.execute(
            sa.text(
                """
                INSERT INTO system_settings (id, key, value)
                VALUES ('operator-setting', 'agent.enabled', '1')
                """
            )
        )
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            inspector = sa.inspect(connection)
            assert {
                "agent_sessions",
                "agent_runs",
                "agent_run_references",
                "agent_capability_grants",
                "agent_tool_calls",
            }.issubset(set(inspector.get_table_names()))

            run_checks = {
                item["name"]: item["sqltext"]
                for item in inspector.get_check_constraints("agent_runs")
            }
            assert "execution_epoch >= 0" in run_checks[
                "ck_agent_runs_execution_epoch_nonnegative"
            ]
            assert "queued" in run_checks["ck_agent_runs_status"]
            run_indexes = {
                item["name"]: item for item in inspector.get_indexes("agent_runs")
            }
            assert run_indexes["uq_agent_runs_one_active_session"]["unique"] == 1
            tool_uniques = {
                item["name"]
                for item in inspector.get_unique_constraints("agent_tool_calls")
            }
            assert {
                "uq_agent_tool_calls_pi_id",
                "uq_agent_tool_calls_semantic",
                "uq_agent_tool_calls_ordinal",
            }.issubset(tool_uniques)
            run_foreign_keys = {
                item["constrained_columns"][0]: item
                for item in inspector.get_foreign_keys("agent_runs")
            }
            assert run_foreign_keys["agent_session_id"]["options"]["ondelete"] == (
                "CASCADE"
            )
            assert run_foreign_keys["user_message_id"]["options"]["ondelete"] == (
                "CASCADE"
            )
            assert run_foreign_keys["user_api_credential_id"]["options"][
                "ondelete"
            ] == "SET NULL"

            settings = dict(
                connection.execute(
                    sa.text(
                        "SELECT key, value FROM system_settings WHERE key LIKE 'agent.%' "
                        "OR key = 'ui.nav.agent_visible'"
                    )
                ).all()
            )
            assert settings["agent.enabled"] == "1"
            assert "ui.nav.agent_visible" not in settings
            assert "agent.max_turns" not in settings
            assert "agent.max_output_tokens" not in settings

            connection.execute(sa.text("INSERT INTO users (id) VALUES ('user-1')"))
            connection.execute(
                sa.text(
                    "INSERT INTO conversations (id, user_id) "
                    "VALUES ('conversation-1', 'user-1')"
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO messages (id, conversation_id) VALUES
                      ('user-message-1', 'conversation-1'),
                      ('assistant-message-1', 'conversation-1'),
                      ('user-message-2', 'conversation-1'),
                      ('assistant-message-2', 'conversation-1')
                    """
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO images (id, user_id) VALUES ('image-1', 'user-1')"
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO agent_sessions
                      (id, user_id, conversation_id, runtime_version)
                    VALUES
                      ('agent-session-1', 'user-1', 'conversation-1', '')
                    """
                )
            )
            _insert_run(
                connection,
                run_id="run-1",
                user_message_id="user-message-1",
                assistant_message_id="assistant-message-1",
            )
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    _insert_run(
                        connection,
                        run_id="run-2",
                        user_message_id="user-message-2",
                        assistant_message_id="assistant-message-2",
                    )

            connection.execute(
                sa.text(
                    """
                    INSERT INTO agent_run_references (
                      id, agent_run_id, user_id, image_id, ordinal,
                      reference_label, role, metadata_jsonb
                    ) VALUES (
                      'reference-1', 'run-1', 'user-1', 'image-1', 0,
                      'ref_1', 'product', '{}'
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO agent_tool_calls (
                      id, agent_run_id, capability_id, pi_tool_call_id,
                      ordinal, execution_epoch, name, status, request_hash,
                      semantic_key, arguments_jsonb, result_jsonb
                    ) VALUES (
                      'tool-1', 'run-1', 'capability-1', 'pi-tool-1',
                      0, 0, 'lumen_create_image', 'running', :request_hash,
                      :semantic_key, '{}', '{}'
                    )
                    """
                ),
                {"request_hash": "a" * 64, "semantic_key": "b" * 64},
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO agent_capability_grants (
                      capability_id, nonce, agent_run_id, user_id,
                      agent_session_id, execution_epoch, expires_at,
                      max_redemptions, redeemed_count
                    ) VALUES (
                      'capability-1', 'nonce-1234567890abcdef', 'run-1',
                      'user-1', 'agent-session-1', 0, CURRENT_TIMESTAMP,
                      3, 1
                    )
                    """
                )
            )
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO agent_tool_calls (
                              id, agent_run_id, capability_id, pi_tool_call_id,
                              ordinal, execution_epoch, name, status,
                              request_hash, semantic_key, arguments_jsonb,
                              result_jsonb
                            ) VALUES (
                              'tool-2', 'run-1', 'capability-2', 'pi-tool-2',
                              0, 0, 'lumen_create_image', 'running',
                              :request_hash, :semantic_key, '{}', '{}'
                            )
                            """
                        ),
                        {"request_hash": "c" * 64, "semantic_key": "d" * 64},
                    )

            connection.execute(
                sa.text("DELETE FROM conversations WHERE id = 'conversation-1'")
            )
            for table in (
                "agent_sessions",
                "agent_runs",
                "agent_run_references",
                "agent_capability_grants",
                "agent_tool_calls",
            ):
                assert connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) == 0
            assert connection.scalar(sa.text("SELECT count(*) FROM images")) == 1

            migration.downgrade()
            assert not {
                "agent_sessions",
                "agent_runs",
                "agent_run_references",
                "agent_tool_calls",
            }.intersection(set(sa.inspect(connection).get_table_names()))
            assert connection.scalar(
                sa.text(
                    "SELECT value FROM system_settings WHERE key = 'agent.enabled'"
                )
            ) == "1"
            assert connection.scalar(
                sa.text(
                    "SELECT count(*) FROM system_settings "
                    "WHERE key = 'agent.max_turns'"
                )
            ) == 0
        finally:
            migration.op = original_op
    engine.dispose()
