from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from app.routes.telegram import GenerateIn
from lumen_core.constants import MAX_MESSAGE_ATTACHMENTS
from lumen_core.model_entities.control_operations import TelegramControlCommand


ROOT = Path(__file__).resolve().parents[3]
API_ROOT = ROOT / "apps" / "api"
DOWNGRADE_SCRIPT = ROOT / "scripts" / "alembic_downgrade.py"
EFFECT_TERMINAL_MIGRATION = (
    API_ROOT / "alembic" / "versions" / "0064_telegram_effect_terminal_guard.py"
)


def test_telegram_generate_accepts_shared_attachment_limit() -> None:
    body = GenerateIn(
        idempotency_key="tg:attachments",
        prompt="retry with many references",
        attachment_image_ids=[
            f"img-{index}" for index in range(MAX_MESSAGE_ATTACHMENTS)
        ],
    )

    assert len(body.attachment_image_ids) == 16


def test_telegram_generate_rejects_too_many_attachments() -> None:
    with pytest.raises(ValidationError):
        GenerateIn(
            idempotency_key="tg:too-many-attachments",
            prompt="too many references",
            attachment_image_ids=[
                f"img-{index}" for index in range(MAX_MESSAGE_ATTACHMENTS + 1)
            ],
        )


def test_telegram_generate_requires_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        GenerateIn(prompt="missing key")  # type: ignore[call-arg]


def test_telegram_control_model_declares_terminal_effect_constraint() -> None:
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in TelegramControlCommand.__table__.constraints
        if isinstance(constraint, CheckConstraint) and getattr(constraint, "name", None)
    }

    assert constraints["ck_tg_control_effect_active_command"] == (
        "status IN ('pending','published') OR effect_status IN ('succeeded','failed')"
    )


def test_telegram_effect_terminal_repair_preserves_active_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "telegram_effect_terminal_migration_under_test",
        EFFECT_TERMINAL_MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://")
    constraints: list[tuple[str, str]] = []
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TABLE telegram_control_commands (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    effect_status TEXT NOT NULL,
                    effect_owner TEXT,
                    effect_lease_until DATETIME,
                    effect_completed_at DATETIME,
                    completed_at DATETIME,
                    accepted_at DATETIME,
                    updated_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
            connection.execute(
                text(
                    """
                    INSERT INTO telegram_control_commands (
                        id, status, effect_status, effect_owner,
                        effect_lease_until, updated_at, created_at
                    )
                    VALUES
                        ('accepted', 'accepted', 'pending', 'old-owner',
                         '2026-08-09 01:00:00', '2026-08-09 02:00:00',
                         '2026-08-09 00:00:00'),
                        ('failed', 'failed', 'running', 'old-owner',
                         '2026-08-09 01:00:00', '2026-08-09 02:00:00',
                         '2026-08-09 00:00:00'),
                        ('pending', 'pending', 'running', 'active-owner',
                         '2026-08-09 03:00:00', '2026-08-09 02:00:00',
                         '2026-08-09 00:00:00'),
                        ('published', 'published', 'pending', NULL,
                         NULL, '2026-08-09 02:00:00',
                         '2026-08-09 00:00:00')
                    """
                )
            )

            class MigrationOp:
                @staticmethod
                def get_bind():
                    return connection

                @staticmethod
                def execute(statement):
                    return connection.execute(statement)

                @staticmethod
                def create_check_constraint(
                    name,
                    _table_name,
                    expression,
                    **_kwargs,
                ):
                    constraints.append((name, expression))

            monkeypatch.setattr(migration, "op", MigrationOp)
            migration.upgrade()

            rows = {
                row.id: row
                for row in connection.execute(
                    text(
                        "SELECT id, status, effect_status, effect_owner, "
                        "effect_lease_until, effect_completed_at "
                        "FROM telegram_control_commands"
                    )
                ).mappings()
            }
        assert rows["accepted"].effect_status == "succeeded"
        assert rows["accepted"].effect_owner is None
        assert rows["accepted"].effect_lease_until is None
        assert rows["accepted"].effect_completed_at is not None
        assert rows["failed"].effect_status == "failed"
        assert rows["failed"].effect_owner is None
        assert rows["pending"].effect_status == "running"
        assert rows["pending"].effect_owner == "active-owner"
        assert rows["published"].effect_status == "pending"
        assert constraints == [
            (
                "ck_tg_control_effect_active_command",
                "status IN ('pending','published') "
                "OR effect_status IN ('succeeded','failed')",
            )
        ]
    finally:
        engine.dispose()


def _postgres_url() -> URL:
    raw_url = os.environ.get("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip(
            "LUMEN_TEST_POSTGRES_URL is not set; Telegram Alembic round-trip skipped"
        )
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("LUMEN_TEST_POSTGRES_URL must use PostgreSQL")
    return url.set(drivername="postgresql+psycopg2")


def _schema_url(base_url: URL, schema: str) -> URL:
    query = dict(base_url.query)
    existing_options = str(query.get("options", "")).strip()
    search_path = f"-csearch_path={schema},public"
    query["options"] = " ".join(
        item for item in (existing_options, search_path) if item
    )
    return base_url.set(query=query)


def _migration_env(database_url: URL) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "BYOK_API_KEY_MASTER_SECRET": ("test-byok-master-secret-0123456789-test"),
            "DATABASE_URL": database_url.render_as_string(hide_password=False),
            "PUBLIC_BASE_URL": "http://localhost:3000",
        }
    )
    return environment


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.fail(
            f"command failed: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed


def _run_alembic(database_url: URL, action: str, revision: str) -> None:
    _run_command(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            action,
            revision,
        ],
        cwd=API_ROOT,
        env=_migration_env(database_url),
    )


def test_telegram_effect_migration_and_export_round_trip_postgres(
    tmp_path: Path,
) -> None:
    base_url = _postgres_url()
    schema = f"lumen_telegram_migration_{uuid4().hex[:16]}"
    admin_engine = create_engine(base_url, poolclass=NullPool)
    schema_engine = None
    schema_created = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        schema_created = True
        database_url = _schema_url(base_url, schema)
        schema_engine = create_engine(database_url, poolclass=NullPool)

        _run_alembic(database_url, "upgrade", "0061_video_jsonb_types")
        with schema_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO telegram_control_commands (
                        id, target, command, payload, status, active_slot
                    )
                    VALUES
                        ('accepted-history', 'target-a', 'restart',
                         CAST(:payload AS JSON), 'accepted', NULL),
                        ('failed-history', 'target-b', 'restart',
                         CAST(:payload AS JSON), 'failed', NULL),
                        ('pending-history', 'target-c', 'restart',
                         CAST(:payload AS JSON), 'pending', 1),
                        ('published-history', 'target-d', 'restart',
                         CAST(:payload AS JSON), 'published', 1)
                    """
                ),
                {"payload": json.dumps({"source": "migration-test"})},
            )

        _run_alembic(database_url, "upgrade", "head")
        with schema_engine.connect() as connection:
            statuses = dict(
                connection.execute(
                    text(
                        "SELECT id, effect_status "
                        "FROM telegram_control_commands ORDER BY id"
                    )
                ).all()
            )
        assert statuses == {
            "accepted-history": "succeeded",
            "failed-history": "failed",
            "pending-history": "pending",
            "published-history": "pending",
        }

        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE telegram_control_commands "
                        "SET effect_status = 'pending' "
                        "WHERE id = 'accepted-history'"
                    )
                )

        with schema_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE telegram_control_commands
                    SET status = CASE id
                            WHEN 'published-history' THEN 'failed'
                            ELSE 'accepted'
                        END,
                        effect_status = CASE id
                            WHEN 'published-history' THEN 'failed'
                            ELSE 'succeeded'
                        END,
                        active_slot = NULL
                    WHERE id IN ('pending-history', 'published-history')
                    """
                )
            )

        export_directory = tmp_path / "postgres-export"
        export_directory.mkdir(mode=0o700)
        export_path = export_directory / "telegram.json"
        migration_env = _migration_env(database_url)
        _run_command(
            [
                sys.executable,
                str(DOWNGRADE_SCRIPT),
                "downgrade",
                "0059_reference_token_expiry",
                "--export-path",
                str(export_path),
            ],
            cwd=ROOT,
            env=migration_env,
        )
        verify = _run_command(
            [
                sys.executable,
                str(DOWNGRADE_SCRIPT),
                "verify",
                str(export_path),
            ],
            cwd=ROOT,
            env=migration_env,
        )
        assert json.loads(verify.stdout)["status"] == "committed"

        with schema_engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0059_reference_token_expiry"
            )
            assert (
                connection.scalar(
                    text("SELECT to_regclass('telegram_control_commands')")
                )
                is None
            )

        _run_alembic(database_url, "upgrade", "head")
        _run_command(
            [
                sys.executable,
                str(DOWNGRADE_SCRIPT),
                "import",
                str(export_path),
            ],
            cwd=ROOT,
            env=migration_env,
        )
        with schema_engine.connect() as connection:
            restored = connection.scalar(
                text("SELECT count(*) FROM telegram_control_commands")
            )
        assert restored == 4
    finally:
        if schema_engine is not None:
            schema_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin_engine.dispose()


def test_partial_downgrade_import_merges_nonempty_tables_postgres(
    tmp_path: Path,
) -> None:
    base_url = _postgres_url()
    schema = f"lumen_partial_migration_{uuid4().hex[:16]}"
    admin_engine = create_engine(base_url, poolclass=NullPool)
    schema_engine = None
    schema_created = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        schema_created = True
        database_url = _schema_url(base_url, schema)
        schema_engine = create_engine(database_url, poolclass=NullPool)
        _run_alembic(database_url, "upgrade", "head")

        with schema_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO telegram_control_commands (
                        id, target, command, payload, status, active_slot,
                        effect_status, effect_fence, effect_attempts,
                        effect_error
                    )
                    VALUES (
                        'partial-command', 'partial-target', 'restart',
                        CAST(:payload AS JSON), 'accepted', NULL,
                        'succeeded', 9, 4, 'historical-effect-error'
                    )
                    """
                ),
                {"payload": json.dumps({"phase": "before-downgrade"})},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO storage_apply_operations (
                        id, desired_config_sha256, status, active_slot,
                        result_message, failure_class
                    )
                    VALUES (
                        'partial-storage', :sha256, 'succeeded', NULL,
                        'before-downgrade', 'permanent'
                    )
                    """
                ),
                {"sha256": "a" * 64},
            )

        export_directory = tmp_path / "partial-postgres-export"
        export_directory.mkdir(mode=0o700)
        export_path = export_directory / "partial.json"
        migration_env = _migration_env(database_url)
        _run_command(
            [
                sys.executable,
                str(DOWNGRADE_SCRIPT),
                "downgrade",
                "0061_video_jsonb_types",
                "--export-path",
                str(export_path),
            ],
            cwd=ROOT,
            env=migration_env,
        )

        with schema_engine.begin() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0061_video_jsonb_types"
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM telegram_control_commands")
                )
                == 1
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM storage_apply_operations"))
                == 1
            )
            connection.execute(
                text(
                    """
                    UPDATE telegram_control_commands
                    SET payload = CAST(:payload AS JSON)
                    WHERE id = 'partial-command'
                    """
                ),
                {"payload": json.dumps({"phase": "during-downgrade"})},
            )
            connection.execute(
                text(
                    """
                    UPDATE storage_apply_operations
                    SET result_message = 'during-downgrade'
                    WHERE id = 'partial-storage'
                    """
                )
            )

        _run_alembic(database_url, "upgrade", "head")
        _run_command(
            [
                sys.executable,
                str(DOWNGRADE_SCRIPT),
                "import",
                str(export_path),
            ],
            cwd=ROOT,
            env=migration_env,
        )

        with schema_engine.connect() as connection:
            command = (
                connection.execute(
                    text(
                        """
                    SELECT payload, effect_status, effect_fence,
                           effect_attempts, effect_error
                    FROM telegram_control_commands
                    WHERE id = 'partial-command'
                    """
                    )
                )
                .mappings()
                .one()
            )
            storage = (
                connection.execute(
                    text(
                        """
                    SELECT result_message, next_attempt_at, failure_class
                    FROM storage_apply_operations
                    WHERE id = 'partial-storage'
                    """
                    )
                )
                .mappings()
                .one()
            )
        assert command["payload"] == {"phase": "during-downgrade"}
        assert command["effect_status"] == "succeeded"
        assert command["effect_fence"] == 9
        assert command["effect_attempts"] == 4
        assert command["effect_error"] == "historical-effect-error"
        assert storage["result_message"] == "during-downgrade"
        assert storage["next_attempt_at"] is None
        assert storage["failure_class"] == "permanent"
    finally:
        if schema_engine is not None:
            schema_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin_engine.dispose()
