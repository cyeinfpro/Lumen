from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import os
from pathlib import Path
import stat
import sys
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection


GUARD_PATH = (
    Path(__file__).resolve().parents[1] / "alembic" / "telegram_downgrade_guard.py"
)


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "telegram_downgrade_guard_under_test",
        GUARD_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database() -> Iterator[tuple[Connection, dict[str, sa.Table]]]:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    tables = {
        "telegram_control_commands": sa.Table(
            "telegram_control_commands",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column(
                "status",
                sa.String,
                nullable=False,
                server_default="accepted",
            ),
            sa.Column(
                "effect_status",
                sa.String,
                nullable=False,
                server_default="succeeded",
            ),
            sa.Column("payload", sa.JSON, nullable=False, server_default="{}"),
            sa.Column("amount", sa.Numeric(18, 4), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("receipt", sa.LargeBinary, nullable=True),
        ),
        "telegram_delivery_attempts": sa.Table(
            "telegram_delivery_attempts",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column(
                "state",
                sa.String,
                nullable=False,
                server_default="delivered",
            ),
        ),
        "telegram_delivery_quarantines": sa.Table(
            "telegram_delivery_quarantines",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("status", sa.String, nullable=False),
        ),
        "storage_apply_operations": sa.Table(
            "storage_apply_operations",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("status", sa.String, nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("failure_class", sa.String, nullable=True),
        ),
    }
    sa.Table(
        "alembic_version",
        metadata,
        sa.Column("version_num", sa.String, primary_key=True),
    )
    metadata.create_all(engine)
    connection = engine.connect()
    try:
        yield connection, tables
    finally:
        connection.close()
        engine.dispose()


def _export_path(tmp_path: Path) -> Path:
    directory = tmp_path / "migration-export"
    directory.mkdir(mode=0o700)
    return directory / "telegram-export.json"


def _prepare_export(
    guard,
    connection: Connection,
    path: Path,
    *,
    pending_revision_ids: set[str] | None = None,
    target_revision: str = "0059_reference_token_expiry",
):
    session = guard.guard_telegram_downgrade(
        connection,
        pending_revision_ids=pending_revision_ids
        or {
            guard.TELEGRAM_CONTROL_REVISION,
            guard.TELEGRAM_EFFECT_REVISION,
            guard.STORAGE_RETRY_REVISION,
        },
        source_revision="0064_tg_effect_terminal_guard",
        target_revision=target_revision,
    )
    assert session is not None
    assert session.path == path
    return session


def test_unrelated_downgrade_does_not_require_export(
    database: tuple[Connection, dict[str, sa.Table]],
) -> None:
    guard = _load_guard()
    connection, _ = database

    assert (
        guard.guard_telegram_downgrade(
            connection,
            pending_revision_ids={"0059_reference_token_expiry"},
        )
        is None
    )


def test_telegram_downgrade_requires_explicit_export(
    monkeypatch: pytest.MonkeyPatch,
    database: tuple[Connection, dict[str, sa.Table]],
) -> None:
    guard = _load_guard()
    connection, _ = database
    monkeypatch.delenv("LUMEN_MIGRATION_EXPORT_PATH", raising=False)

    with pytest.raises(RuntimeError, match="explicit export"):
        guard.guard_telegram_downgrade(
            connection,
            pending_revision_ids={guard.TELEGRAM_CONTROL_REVISION},
            source_revision="0064_tg_effect_terminal_guard",
            target_revision="0059_reference_token_expiry",
        )


def test_unresolved_quarantine_blocks_before_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: tuple[Connection, dict[str, sa.Table]],
) -> None:
    guard = _load_guard()
    connection, tables = database
    export_path = _export_path(tmp_path)
    connection.execute(
        tables["telegram_delivery_quarantines"].insert(),
        {"id": "q1", "status": "pending"},
    )
    monkeypatch.setenv("LUMEN_MIGRATION_EXPORT_PATH", str(export_path))

    with pytest.raises(RuntimeError, match="unresolved Telegram quarantines"):
        guard.guard_telegram_downgrade(
            connection,
            pending_revision_ids={guard.TELEGRAM_CONTROL_REVISION},
            source_revision="0064_tg_effect_terminal_guard",
            target_revision="0059_reference_token_expiry",
        )

    assert not export_path.exists()


def test_terminal_command_with_claimable_effect_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: tuple[Connection, dict[str, sa.Table]],
) -> None:
    guard = _load_guard()
    connection, tables = database
    export_path = _export_path(tmp_path)
    connection.execute(
        tables["telegram_control_commands"].insert(),
        {
            "id": "accepted-history",
            "status": "accepted",
            "effect_status": "pending",
            "payload": {},
        },
    )
    monkeypatch.setenv("LUMEN_MIGRATION_EXPORT_PATH", str(export_path))

    with pytest.raises(RuntimeError, match="non-terminal effects"):
        guard.guard_telegram_downgrade(
            connection,
            pending_revision_ids={guard.TELEGRAM_EFFECT_REVISION},
            source_revision="0064_tg_effect_terminal_guard",
            target_revision="0061_video_jsonb_types",
        )


def test_typed_manifest_commits_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: tuple[Connection, dict[str, sa.Table]],
) -> None:
    guard = _load_guard()
    connection, tables = database
    export_path = _export_path(tmp_path)
    occurred_at = datetime(2026, 8, 9, 3, 4, 5, tzinfo=timezone.utc)
    connection.execute(
        tables["telegram_control_commands"].insert(),
        {
            "id": "c1",
            "status": "accepted",
            "effect_status": "succeeded",
            "payload": {"nested": [True, 7, None]},
            "amount": Decimal("12.3400"),
            "occurred_at": occurred_at,
            "receipt": b"\x00\xfftyped",
        },
    )
    connection.execute(
        tables["telegram_delivery_attempts"].insert(),
        {"id": "d1", "state": "delivered"},
    )
    connection.execute(
        tables["telegram_delivery_quarantines"].insert(),
        {"id": "q1", "status": "resolved"},
    )
    connection.execute(
        tables["storage_apply_operations"].insert(),
        {"id": "s1", "status": "succeeded"},
    )
    monkeypatch.setenv("LUMEN_MIGRATION_EXPORT_PATH", str(export_path))

    session = _prepare_export(guard, connection, export_path)
    pending = guard.verify_migration_export(
        export_path,
        expected_status="pending",
    )
    assert pending["schema"] == guard.EXPORT_SCHEMA
    assert pending["version"] == guard.EXPORT_VERSION
    assert pending["source_revision"] == "0064_tg_effect_terminal_guard"
    assert pending["target_revision"] == "0059_reference_token_expiry"
    assert pending["tables"]["telegram_control_commands"]["row_count"] == 1
    assert stat.S_IMODE(os.stat(export_path.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(export_path).st_mode) == 0o600

    guard.commit_migration_export(session)
    committed = guard.verify_migration_export(
        export_path,
        expected_status="committed",
    )
    assert committed["request_id"] == session.request_id

    for table in reversed(tuple(tables.values())):
        connection.execute(table.delete())
    imported = guard.import_migration_export(connection, export_path)
    assert imported == {
        "storage_apply_operations": 1,
        "telegram_control_commands": 1,
        "telegram_delivery_attempts": 1,
        "telegram_delivery_quarantines": 1,
    }
    restored = (
        connection.execute(sa.select(tables["telegram_control_commands"]))
        .mappings()
        .one()
    )
    assert restored["payload"] == {"nested": [True, 7, None]}
    assert restored["amount"] == Decimal("12.3400")
    assert (
        restored["occurred_at"].isoformat()
        == occurred_at.replace(tzinfo=None).isoformat()
    )
    assert restored["receipt"] == b"\x00\xfftyped"


def test_partial_effect_import_merges_by_pk_and_preserves_surviving_columns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: tuple[Connection, dict[str, sa.Table]],
) -> None:
    guard = _load_guard()
    connection, tables = database
    export_path = _export_path(tmp_path)
    connection.execute(
        tables["telegram_control_commands"].insert(),
        {
            "id": "c1",
            "status": "accepted",
            "effect_status": "succeeded",
            "payload": {"phase": "before"},
            "amount": Decimal("1.0000"),
        },
    )
    monkeypatch.setenv("LUMEN_MIGRATION_EXPORT_PATH", str(export_path))
    session = _prepare_export(
        guard,
        connection,
        export_path,
        pending_revision_ids={guard.TELEGRAM_EFFECT_REVISION},
        target_revision="0061_video_jsonb_types",
    )
    guard.commit_migration_export(session)
    manifest = guard.verify_migration_export(export_path)
    assert set(manifest["tables"]) == {"telegram_control_commands"}
    assert manifest["tables"]["telegram_control_commands"]["primary_key"] == ["id"]
    assert manifest["tables"]["telegram_control_commands"]["restore_columns"] == [
        "effect_status"
    ]

    connection.execute(
        tables["telegram_control_commands"]
        .update()
        .where(tables["telegram_control_commands"].c.id == "c1")
        .values(
            effect_status="pending",
            payload={"phase": "during-downgrade"},
            amount=Decimal("9.0000"),
        )
    )
    connection.execute(
        tables["telegram_control_commands"].insert(),
        {
            "id": "c2",
            "status": "accepted",
            "effect_status": "succeeded",
            "payload": {"phase": "new"},
        },
    )

    assert guard.import_migration_export(connection, export_path) == {
        "telegram_control_commands": 1
    }
    restored = (
        connection.execute(
            sa.select(tables["telegram_control_commands"]).where(
                tables["telegram_control_commands"].c.id == "c1"
            )
        )
        .mappings()
        .one()
    )
    assert restored["effect_status"] == "succeeded"
    assert restored["payload"] == {"phase": "during-downgrade"}
    assert restored["amount"] == Decimal("9.0000")
    assert (
        connection.scalar(
            sa.select(sa.func.count()).select_from(tables["telegram_control_commands"])
        )
        == 2
    )


def test_partial_storage_import_restores_only_dropped_columns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: tuple[Connection, dict[str, sa.Table]],
) -> None:
    guard = _load_guard()
    connection, tables = database
    export_path = _export_path(tmp_path)
    retry_at = datetime(2026, 8, 9, 4, 5, 6, tzinfo=timezone.utc)
    connection.execute(
        tables["storage_apply_operations"].insert(),
        {
            "id": "s1",
            "status": "succeeded",
            "next_attempt_at": retry_at,
            "failure_class": "transient",
        },
    )
    monkeypatch.setenv("LUMEN_MIGRATION_EXPORT_PATH", str(export_path))
    session = _prepare_export(
        guard,
        connection,
        export_path,
        pending_revision_ids={guard.STORAGE_RETRY_REVISION},
        target_revision=guard.TELEGRAM_EFFECT_REVISION,
    )
    guard.commit_migration_export(session)

    connection.execute(
        tables["storage_apply_operations"]
        .update()
        .where(tables["storage_apply_operations"].c.id == "s1")
        .values(
            status="failed",
            next_attempt_at=None,
            failure_class=None,
        )
    )
    guard.import_migration_export(connection, export_path)
    restored = (
        connection.execute(
            sa.select(tables["storage_apply_operations"]).where(
                tables["storage_apply_operations"].c.id == "s1"
            )
        )
        .mappings()
        .one()
    )
    assert restored["status"] == "failed"
    assert restored["next_attempt_at"] == retry_at.replace(tzinfo=None)
    assert restored["failure_class"] == "transient"


def test_pending_export_records_failure_and_is_not_importable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: tuple[Connection, dict[str, sa.Table]],
) -> None:
    guard = _load_guard()
    connection, _ = database
    export_path = _export_path(tmp_path)
    monkeypatch.setenv("LUMEN_MIGRATION_EXPORT_PATH", str(export_path))
    session = _prepare_export(guard, connection, export_path)

    guard.mark_migration_export_failed(session, RuntimeError("downgrade failed"))
    manifest = guard.verify_migration_export(
        export_path,
        expected_status="pending",
    )
    assert manifest["failure"]["type"] == "RuntimeError"
    assert manifest["failure"]["message"] == "downgrade failed"
    with pytest.raises(RuntimeError, match="expected 'committed'"):
        guard.import_migration_export(connection, export_path)
    with pytest.raises(RuntimeError, match="failed pending"):
        guard.import_migration_export(
            connection,
            export_path,
            allow_pending=True,
        )


def test_allow_pending_requires_database_at_manifest_target_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: tuple[Connection, dict[str, sa.Table]],
) -> None:
    guard = _load_guard()
    connection, _ = database
    export_path = _export_path(tmp_path)
    monkeypatch.setenv("LUMEN_MIGRATION_EXPORT_PATH", str(export_path))
    _prepare_export(
        guard,
        connection,
        export_path,
        pending_revision_ids={guard.TELEGRAM_EFFECT_REVISION},
        target_revision="0061_video_jsonb_types",
    )
    connection.execute(
        sa.text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
        {"revision": "0064_tg_effect_terminal_guard"},
    )

    with pytest.raises(RuntimeError, match="target revision mismatch"):
        guard.import_migration_export(
            connection,
            export_path,
            allow_pending=True,
        )


def test_manifest_tamper_and_unsafe_files_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: tuple[Connection, dict[str, sa.Table]],
) -> None:
    guard = _load_guard()
    connection, _ = database
    export_path = _export_path(tmp_path)
    monkeypatch.setenv("LUMEN_MIGRATION_EXPORT_PATH", str(export_path))
    session = _prepare_export(guard, connection, export_path)
    guard.commit_migration_export(session)

    original = export_path.read_bytes()
    export_path.write_bytes(original.replace(b'"row_count":0', b'"row_count":1', 1))
    with pytest.raises(RuntimeError, match="row count mismatch|digest mismatch"):
        guard.verify_migration_export(export_path)

    export_path.write_bytes(original)
    os.chmod(export_path, 0o644)
    with pytest.raises(RuntimeError, match="mode 0600"):
        guard.verify_migration_export(export_path)
    os.chmod(export_path, 0o600)

    hard_link = export_path.with_name("hard-link.json")
    os.link(export_path, hard_link)
    try:
        with pytest.raises(RuntimeError, match="hard links"):
            guard.verify_migration_export(export_path)
    finally:
        hard_link.unlink()

    symlink = export_path.with_name("symlink.json")
    symlink.symlink_to(export_path)
    with pytest.raises(RuntimeError, match="regular file"):
        guard.verify_migration_export(symlink)


def test_typed_codec_preserves_uuid_and_rejects_unknown_types() -> None:
    guard = _load_guard()
    value = uuid.UUID("00000000-0000-4000-8000-000000000123")

    assert guard._decode_value(guard._encode_value(value)) == value
    with pytest.raises(TypeError, match="cannot preserve value type"):
        guard._encode_value(object())
