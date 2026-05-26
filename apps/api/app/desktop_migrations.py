"""Desktop SQLite migration runner."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from .config import settings


_MIGRATION_LOCK_NAME = ".desktop-migration.lock"
_MIGRATION_FAILED_NAME = "migration.failed.json"
_MIGRATION_LOG_NAME = "migration.log"


def _sync_database_url(raw: str) -> str:
    url = make_url(raw)
    if url.drivername == "sqlite+aiosqlite":
        url = url.set(drivername="sqlite")
    return url.render_as_string(hide_password=False)


def _sqlite_database_path(raw: str) -> Path | None:
    url = make_url(raw)
    if not url.drivername.startswith("sqlite"):
        return None
    if not url.database or url.database == ":memory:":
        return None
    return Path(url.database).expanduser()


def _api_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _data_root_for_db(db_path: Path) -> Path:
    # Desktop DB lives at <data_root>/data/db/lumen.sqlite.
    db_dir = db_path.parent
    data_dir = db_dir.parent
    if db_dir.name == "db" and data_dir.name == "data":
        return data_dir.parent
    return data_dir


def _log_path(db_path: Path) -> Path:
    return _data_root_for_db(db_path) / "data" / "logs" / _MIGRATION_LOG_NAME


def _failed_marker_path(db_path: Path) -> Path:
    return _data_root_for_db(db_path) / "data" / "tmp" / _MIGRATION_FAILED_NAME


def _write_migration_log(db_path: Path, message: str) -> None:
    path = _log_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{int(time.time() * 1000)} {message.rstrip()}\n")


def _write_failed_marker(
    db_path: Path,
    *,
    current_revision: str | None,
    target_revision: str | None,
    backup_path: Path | None,
    error: BaseException,
    restored: bool,
) -> None:
    path = _failed_marker_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "failed_at_ms": int(time.time() * 1000),
        "current_revision": current_revision,
        "target_revision": target_revision,
        "backup_path": str(backup_path) if backup_path else None,
        "restored_from_backup": restored,
        "error": str(error),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_failed_marker(db_path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        _failed_marker_path(db_path).unlink()


@contextlib.contextmanager
def _migration_lock(db_path: Path) -> Iterator[None]:
    lock_path = db_path.parent / _MIGRATION_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as fh:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _sqlite_quick_check(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        return str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()


def _current_revision(db_path: Path) -> str | None:
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        return str(row[0]) if row else None
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    finally:
        conn.close()


def _target_revision(config: Config) -> str | None:
    return ScriptDirectory.from_config(config).get_current_head()


def _safe_revision_part(value: str | None) -> str:
    raw = value or "base"
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw)


def _backup_sqlite_database(
    db_path: Path,
    *,
    current_revision: str | None,
    target_revision: str | None,
) -> Path:
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    quick_check = _sqlite_quick_check(db_path)
    if quick_check != "ok":
        raise RuntimeError(f"sqlite quick_check failed before migration: {quick_check}")
    backup_dir = _data_root_for_db(db_path) / "data" / "backup" / "migrations"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    backup_path = backup_dir / (
        "lumen.sqlite.bak."
        f"{_safe_revision_part(current_revision)}-"
        f"{_safe_revision_part(target_revision)}.{stamp}"
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("VACUUM main INTO ?", (str(backup_path),))
    finally:
        conn.close()
    backup_check = _sqlite_quick_check(backup_path)
    if backup_check != "ok":
        with contextlib.suppress(FileNotFoundError):
            backup_path.unlink()
        raise RuntimeError(f"sqlite quick_check failed after migration backup: {backup_check}")
    return backup_path


def _restore_sqlite_backup(backup_path: Path, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            db_path.with_name(f"{db_path.name}{suffix}").unlink()
    shutil.copy2(backup_path, db_path)
    quick_check = _sqlite_quick_check(db_path)
    if quick_check != "ok":
        raise RuntimeError(f"sqlite quick_check failed after migration restore: {quick_check}")


def _config_for_desktop_chain() -> Config:
    script_location = _api_root() / "alembic" / "desktop"
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", _sync_database_url(settings.database_url))
    return config


def _run_upgrade_sync() -> None:
    db_path = _sqlite_database_path(settings.database_url)
    if db_path is None:
        command.upgrade(_config_for_desktop_chain(), "head")
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _migration_lock(db_path):
        config = _config_for_desktop_chain()
        current = _current_revision(db_path)
        target = _target_revision(config)
        backup_path: Path | None = None
        needs_backup = db_path.is_file() and current != target
        if needs_backup:
            backup_path = _backup_sqlite_database(
                db_path,
                current_revision=current,
                target_revision=target,
            )
            _write_migration_log(
                db_path,
                f"backup created path={backup_path} from={current} to={target}",
            )
        try:
            command.upgrade(config, "head")
            if db_path.is_file():
                quick_check = _sqlite_quick_check(db_path)
                if quick_check != "ok":
                    raise RuntimeError(
                        f"sqlite quick_check failed after migration: {quick_check}"
                    )
            _clear_failed_marker(db_path)
            _write_migration_log(db_path, f"migration complete from={current} to={target}")
        except Exception as exc:
            restored = False
            if backup_path is not None and backup_path.is_file():
                try:
                    _restore_sqlite_backup(backup_path, db_path)
                    restored = True
                except Exception as restore_exc:
                    _write_migration_log(
                        db_path,
                        f"restore failed backup={backup_path} error={restore_exc}",
                    )
            _write_failed_marker(
                db_path,
                current_revision=current,
                target_revision=target,
                backup_path=backup_path,
                error=exc,
                restored=restored,
            )
            _write_migration_log(
                db_path,
                f"migration failed from={current} to={target} restored={restored} error={exc}",
            )
            raise


async def run_desktop_migrations() -> None:
    await asyncio.to_thread(_run_upgrade_sync)
