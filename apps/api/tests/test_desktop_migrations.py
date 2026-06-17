from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import desktop_migrations


def _create_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE example (id TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO example (id, value) VALUES ('one', 'before')")
        conn.commit()
    finally:
        conn.close()


def _read_value(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(
            conn.execute("SELECT value FROM example WHERE id='one'").fetchone()[0]
        )
    finally:
        conn.close()


def test_backup_sqlite_database_uses_vacuum_into(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "db" / "lumen.sqlite"
    _create_db(db_path)

    backup_path = desktop_migrations._backup_sqlite_database(
        db_path,
        current_revision="old",
        target_revision="new",
    )

    assert backup_path.is_file()
    assert backup_path.name.startswith("lumen.sqlite.bak.old-new.")
    assert _read_value(backup_path) == "before"


def test_current_revision_reads_alembic_version_table(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "db" / "lumen.sqlite"
    _create_db(db_path)
    assert desktop_migrations._current_revision(db_path) is None

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version (version_num) VALUES ('rev-one')")
        conn.commit()
    finally:
        conn.close()

    assert desktop_migrations._current_revision(db_path) == "rev-one"


def test_desktop_baseline_migration_creates_sqlite_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "data" / "db" / "lumen.sqlite"
    monkeypatch.setattr(
        desktop_migrations.settings,
        "database_url",
        f"sqlite+aiosqlite:///{db_path}",
    )

    desktop_migrations._run_upgrade_sync()

    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0001_desktop_baseline"
        )
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"users", "conversations", "messages", "generations", "images"} <= tables
        user_columns = {
            str(row[1]): row
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        assert str(user_columns["extraction_threshold"][4]).strip("'\"") == "0.80"
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_failed_migration_restores_pre_migration_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "data" / "db" / "lumen.sqlite"
    _create_db(db_path)
    monkeypatch.setattr(
        desktop_migrations.settings,
        "database_url",
        f"sqlite+aiosqlite:///{db_path}",
    )
    monkeypatch.setattr(
        desktop_migrations,
        "_current_revision",
        lambda _db_path: "old",
    )
    monkeypatch.setattr(
        desktop_migrations,
        "_target_revision",
        lambda _config: "new",
    )

    def fail_upgrade(_config, _target: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE example SET value='during' WHERE id='one'")
            conn.commit()
        finally:
            conn.close()
        raise RuntimeError("boom")

    monkeypatch.setattr(desktop_migrations.command, "upgrade", fail_upgrade)

    with pytest.raises(RuntimeError, match="boom"):
        desktop_migrations._run_upgrade_sync()

    assert _read_value(db_path) == "before"
    failed_marker = tmp_path / "data" / "tmp" / "migration.failed.json"
    assert failed_marker.is_file()
    assert '"restored_from_backup": true' in failed_marker.read_text(encoding="utf-8")
    backups = list((tmp_path / "data" / "backup" / "migrations").glob("*.bak.*"))
    assert backups
