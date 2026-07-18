from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "apps/api/alembic/versions/0045_memory_extraction_runs.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "memory_extraction_runs_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_memory_extraction_migration_scrubs_historical_message_state() -> None:
    migration = _load_migration()
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql(
            """
            CREATE TABLE conversations (
                id VARCHAR(36) PRIMARY KEY,
                user_id VARCHAR(36) NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE messages (
                id VARCHAR(36) PRIMARY KEY,
                conversation_id VARCHAR(36) NOT NULL,
                role VARCHAR(16) NOT NULL,
                content JSON NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(text("INSERT INTO users (id) VALUES ('user-1')"))
        connection.execute(
            text(
                """
                INSERT INTO conversations (id, user_id)
                VALUES ('conversation-1', 'user-1')
                """
            )
        )
        historical_content = {
            "text": "public",
            "memory_writes": [{"kind": "added", "id": "memory-1"}],
            "_memory_extraction": {
                "status": "committed",
                "assistant_message_id": "assistant-1",
                "event_id": "memory-extract:message-1:assistant-1",
                "owner": "old-owner",
                "job_id": "old-job",
                "fence": 4,
                "recovery_count": 2,
                "memory_writes": [
                    {
                        "kind": "added",
                        "id": "memory-1",
                        "undo_token": "private-token",
                    }
                ],
            },
        }
        connection.execute(
            text(
                """
                INSERT INTO messages (
                    id,
                    conversation_id,
                    role,
                    content,
                    updated_at
                )
                VALUES (
                    :id,
                    :conversation_id,
                    'user',
                    :content,
                    :updated_at
                )
                """
            ),
            {
                "id": "message-1",
                "conversation_id": "conversation-1",
                "content": json.dumps(historical_content),
                "updated_at": "2026-07-18T00:00:00+00:00",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO messages (
                    id,
                    conversation_id,
                    role,
                    content,
                    updated_at
                )
                VALUES (
                    'assistant-1',
                    'conversation-1',
                    'assistant',
                    '{}',
                    '2026-07-18T00:00:00+00:00'
                )
                """
            )
        )

        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()

        raw_content = connection.execute(
            text("SELECT content FROM messages WHERE id = 'message-1'")
        ).scalar_one()
        content = (
            json.loads(raw_content) if isinstance(raw_content, str) else raw_content
        )
        inspector = inspect(connection)

        assert "_memory_extraction" not in content
        assert content["text"] == "public"
        assert content["memory_writes"] == [{"kind": "added", "id": "memory-1"}]
        assert "memory_extraction_runs" in inspector.get_table_names()
        backfilled = (
            connection.execute(
                text(
                    """
                SELECT
                    event_id,
                    status,
                    fence,
                    recovery_count,
                    memory_writes,
                    undo_operations,
                    undo_status
                FROM memory_extraction_runs
                """
                )
            )
            .mappings()
            .one()
        )
        backfilled_writes = (
            json.loads(backfilled["memory_writes"])
            if isinstance(backfilled["memory_writes"], str)
            else backfilled["memory_writes"]
        )
        assert backfilled["event_id"] == "memory-extract:message-1:assistant-1"
        assert backfilled["status"] == "committed"
        assert backfilled["fence"] == 4
        assert backfilled["recovery_count"] == 2
        assert backfilled_writes == [{"kind": "added", "id": "memory-1"}]
        assert "private-token" not in json.dumps(backfilled_writes)
        assert backfilled["undo_operations"] in ("[]", [])
        assert backfilled["undo_status"] == "none"
        columns = {
            column["name"] for column in inspector.get_columns("memory_extraction_runs")
        }
        assert "undo_expires_at" in columns
        indexes = {
            index["name"] for index in inspector.get_indexes("memory_extraction_runs")
        }
        assert "ix_memory_extraction_runs_undo_expiry" in indexes
        unique_constraints = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("memory_extraction_runs")
        }
        assert "uq_memory_extraction_runs_event_id" in unique_constraints
        assert "uq_memory_extraction_runs_source_assistant" in unique_constraints

        migration.downgrade()
        assert "memory_extraction_runs" not in inspect(connection).get_table_names()

    engine.dispose()
