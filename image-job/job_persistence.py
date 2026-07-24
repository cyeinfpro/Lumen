"""SQLite, job state, reference, and retention persistence helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ALLOWED_SQLITE_JOURNAL_MODES = {
    "WAL",
    "DELETE",
    "TRUNCATE",
    "PERSIST",
    "MEMORY",
    "OFF",
}
_SQLITE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _sqlite_identifier(value: str) -> str:
    if _SQLITE_IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"invalid SQLite identifier: {value!r}")
    return f'"{value}"'


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is not allowed: {value}")
    return parsed


def _strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        parse_float=_parse_finite_float,
    )


def sqlite_tuning_pragmas(journal_mode: str) -> tuple[str, ...]:
    mode = journal_mode if journal_mode in ALLOWED_SQLITE_JOURNAL_MODES else "WAL"
    return (
        f"PRAGMA journal_mode = {mode}",
        "PRAGMA synchronous = NORMAL",
        "PRAGMA temp_store = MEMORY",
        "PRAGMA mmap_size = 67108864",
        "PRAGMA cache_size = -16384",
        "PRAGMA busy_timeout = 5000",
    )


def open_connection(
    db_path: Path,
    pragmas: tuple[str, ...],
) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in pragmas:
        conn.execute(pragma)
    return conn


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    name: str,
    decl: str,
) -> None:
    table_sql = _sqlite_identifier(table)
    name_sql = _sqlite_identifier(name)
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_sql})")}
    if name not in cols:
        conn.execute(f"ALTER TABLE {table_sql} ADD COLUMN {name_sql} {decl}")


_REFS_LEGACY_TABLE = "refs_legacy_auth_migration"
_REFS_MIGRATION_TABLE = "refs_auth_migration_new"
_REFS_REBUILD_TABLE = "refs_auth_migration_rebuild"
_REFS_REQUIRED_COLUMNS = {
    "sha256",
    "token",
    "ext",
    "size",
    "created_at",
}


def _table_info(
    conn: sqlite3.Connection,
    table: str,
) -> list[sqlite3.Row]:
    return list(conn.execute(f"PRAGMA table_info({_sqlite_identifier(table)})"))


def _create_refs_table(
    conn: sqlite3.Connection,
    table: str = "refs",
) -> None:
    table_sql = _sqlite_identifier(table)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_sql} (
            auth_hash TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            token TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(auth_hash, sha256)
        )
        """
    )
    if table == "refs":
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS refs_auth_sha_idx
                ON refs(auth_hash, sha256)
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS refs_created_idx ON refs(created_at)")


def _refs_schema_is_current(rows: list[sqlite3.Row]) -> bool:
    columns = {row["name"] for row in rows}
    primary_key = [
        row["name"]
        for row in sorted(rows, key=lambda item: int(item["pk"] or 0))
        if int(row["pk"] or 0) > 0
    ]
    return (
        "auth_hash" in columns
        and _REFS_REQUIRED_COLUMNS <= columns
        and primary_key != ["sha256"]
    )


def _copy_refs_rows(
    conn: sqlite3.Connection,
    source: str,
    target: str,
) -> None:
    rows = _table_info(conn, source)
    columns = {row["name"] for row in rows}
    if not _REFS_REQUIRED_COLUMNS <= columns:
        missing = sorted(_REFS_REQUIRED_COLUMNS - columns)
        raise sqlite3.OperationalError(
            f"refs migration source {source} is missing columns: {missing}"
        )
    auth_expression = "auth_hash" if "auth_hash" in columns else "'legacy:' || sha256"
    source_sql = _sqlite_identifier(source)
    target_sql = _sqlite_identifier(target)
    # SQLite does not parameterize identifiers; every interpolated identifier
    # above passed the strict identifier validator.
    copy_sql = f"""
        INSERT OR IGNORE INTO {target_sql} (
            auth_hash, sha256, token, ext, size, created_at
        )
        SELECT {auth_expression}, sha256, token, ext, size, created_at
        FROM {source_sql}
        """  # nosec B608
    conn.execute(copy_sql)


def _ensure_refs_auth_schema(conn: sqlite3.Connection) -> None:
    savepoint = "refs_auth_schema_migration"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        refs_rows = _table_info(conn, "refs")
        legacy_rows = _table_info(conn, _REFS_LEGACY_TABLE)
        migration_rows = _table_info(conn, _REFS_MIGRATION_TABLE)
        if (
            refs_rows
            and _refs_schema_is_current(refs_rows)
            and not legacy_rows
            and not migration_rows
        ):
            _create_refs_table(conn)
        elif not refs_rows and not legacy_rows and not migration_rows:
            _create_refs_table(conn)
        else:
            conn.execute(f"DROP TABLE IF EXISTS {_REFS_REBUILD_TABLE}")
            _create_refs_table(conn, _REFS_REBUILD_TABLE)
            if refs_rows:
                _copy_refs_rows(conn, "refs", _REFS_REBUILD_TABLE)
            if legacy_rows:
                _copy_refs_rows(
                    conn,
                    _REFS_LEGACY_TABLE,
                    _REFS_REBUILD_TABLE,
                )
            if migration_rows:
                _copy_refs_rows(
                    conn,
                    _REFS_MIGRATION_TABLE,
                    _REFS_REBUILD_TABLE,
                )
            if refs_rows:
                conn.execute("DROP TABLE refs")
            if legacy_rows:
                conn.execute(f"DROP TABLE {_REFS_LEGACY_TABLE}")
            if migration_rows:
                conn.execute(f"DROP TABLE {_REFS_MIGRATION_TABLE}")
            conn.execute(f"ALTER TABLE {_REFS_REBUILD_TABLE} RENAME TO refs")
            _create_refs_table(conn)
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")


def init_storage(
    *,
    data_dir: Path,
    refs_dir: Path,
    db_path: Path,
    open_conn: Callable[[], sqlite3.Connection],
    auth_hash: Callable[[str], str],
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    refs_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.chmod(0o700)
    conn = open_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                auth_hash TEXT NOT NULL,
                upstream_auth_hash TEXT,
                auth_header TEXT,
                idempotency_key TEXT,
                request_hash TEXT,
                request_type TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                relay_url TEXT NOT NULL,
                retention_days INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                elapsed_ms INTEGER,
                upstream_status INTEGER,
                image_count INTEGER NOT NULL DEFAULT 0,
                images_json TEXT,
                error TEXT,
                upstream_body TEXT,
                retryable INTEGER NOT NULL DEFAULT 0,
                retry_suppressed INTEGER NOT NULL DEFAULT 0,
                outcome_uncertain INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status);
            CREATE INDEX IF NOT EXISTS jobs_created_idx ON jobs(created_at);
            CREATE INDEX IF NOT EXISTS jobs_finished_idx ON jobs(finished_at);
            """
        )
        _ensure_refs_auth_schema(conn)
        _ensure_column(conn, "jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "jobs", "error_class", "TEXT")
        _ensure_column(conn, "jobs", "endpoint_used", "TEXT")
        _ensure_column(conn, "jobs", "upstream_auth_hash", "TEXT")
        _ensure_column(conn, "jobs", "idempotency_key", "TEXT")
        _ensure_column(conn, "jobs", "request_hash", "TEXT")
        _ensure_column(conn, "jobs", "retryable", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(
            conn,
            "jobs",
            "retry_suppressed",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            conn,
            "jobs",
            "outcome_uncertain",
            "INTEGER NOT NULL DEFAULT 0",
        )
        rows = conn.execute(
            """
            SELECT job_id, auth_header
            FROM jobs
            WHERE upstream_auth_hash IS NULL AND auth_header IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            try:
                digest = auth_hash(row["auth_header"])
            except ValueError:
                continue
            conn.execute(
                """
                UPDATE jobs
                SET upstream_auth_hash = ?
                WHERE job_id = ? AND upstream_auth_hash IS NULL
                """,
                (digest, row["job_id"]),
            )
        conn.execute("DROP INDEX IF EXISTS jobs_auth_idempotency_idx")
        conn.execute(
            """
            CREATE UNIQUE INDEX jobs_auth_idempotency_idx
                ON jobs(auth_hash, upstream_auth_hash, idempotency_key)
                WHERE idempotency_key IS NOT NULL
            """
        )
    finally:
        conn.close()
    db_path.chmod(0o600)


def db_one_sync(
    open_conn: Callable[[], sqlite3.Connection],
    sql: str,
    params: tuple[Any, ...],
) -> sqlite3.Row | None:
    conn = open_conn()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def db_all_sync(
    open_conn: Callable[[], sqlite3.Connection],
    sql: str,
    params: tuple[Any, ...],
) -> list[sqlite3.Row]:
    conn = open_conn()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def db_exec_sync(
    open_conn: Callable[[], sqlite3.Connection],
    sql: str,
    params: tuple[Any, ...],
) -> int:
    conn = open_conn()
    try:
        cur = conn.execute(sql, params)
        return cur.rowcount
    finally:
        conn.close()


DbExec = Callable[[str, tuple[Any, ...]], Awaitable[int]]
DbAll = Callable[[str, tuple[Any, ...]], Awaitable[list[sqlite3.Row]]]
EnqueueJob = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class JobPersistenceFacade:
    db_exec: DbExec
    enqueue_job: EnqueueJob
    now_iso: Callable[[], str]
    auth_hash: Callable[[str], str]
    json_dump: Callable[[Any], str]
    upstream_base_url: Callable[[], str]
    upstream_idempotency_guaranteed: Callable[[], bool]
    error_class_internal: Callable[[], str]
    error_class_network: Callable[[], str]
    log: logging.Logger

    async def insert_job(
        self,
        job_id: str,
        payload: dict[str, Any],
        auth_header: str,
        *,
        owner_auth_header: str | None = None,
        idempotency_key: str | None = None,
        payload_hash: str | None = None,
    ) -> None:
        now = self.now_iso()
        owner_auth = owner_auth_header or auth_header
        await self.db_exec(
            """
            INSERT INTO jobs (
                job_id, auth_hash, upstream_auth_hash, auth_header,
                idempotency_key, request_hash, request_type, endpoint,
                payload_json, status, relay_url, retention_days,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (
                job_id,
                self.auth_hash(owner_auth),
                self.auth_hash(auth_header),
                auth_header,
                idempotency_key,
                payload_hash,
                payload["request_type"],
                payload["endpoint"],
                self.json_dump(payload),
                self.upstream_base_url(),
                payload["retention_days"],
                now,
                now,
            ),
        )

    async def ensure_queued_job_scheduled(self, row: sqlite3.Row) -> None:
        if row["status"] != "queued" or not row["auth_header"]:
            return
        result = await self.enqueue_job(row["job_id"])
        if result == "enqueued":
            await self.db_exec(
                "UPDATE jobs SET updated_at = ? WHERE job_id = ? AND status = 'queued'",
                (self.now_iso(), row["job_id"]),
            )

    async def mark_running(self, job_id: str) -> bool:
        now = self.now_iso()
        changed = await self.db_exec(
            "UPDATE jobs SET status = 'running', "
            "started_at = COALESCE(started_at, ?), "
            "updated_at = ?, attempts = attempts + 1 "
            "WHERE job_id = ? AND status = 'queued'",
            (now, now, job_id),
        )
        return changed == 1

    async def touch_running(self, job_id: str) -> None:
        await self.db_exec(
            "UPDATE jobs SET updated_at = ? WHERE job_id = ? AND status = 'running'",
            (self.now_iso(), job_id),
        )

    async def mark_succeeded(
        self,
        job_id: str,
        *,
        upstream_status: int,
        elapsed_ms: int,
        images: list[dict[str, Any]],
        endpoint_used: str | None = None,
    ) -> None:
        now = self.now_iso()
        await self.db_exec(
            """
            UPDATE jobs
            SET status = 'succeeded', auth_header = NULL,
                finished_at = ?, updated_at = ?, elapsed_ms = ?,
                upstream_status = ?, image_count = ?, images_json = ?,
                error = NULL, upstream_body = NULL, error_class = NULL,
                retryable = 0, retry_suppressed = 0, outcome_uncertain = 0,
                endpoint_used = COALESCE(?, endpoint_used)
            WHERE job_id = ?
            """,
            (
                now,
                now,
                elapsed_ms,
                upstream_status,
                len(images),
                self.json_dump(images),
                endpoint_used,
                job_id,
            ),
        )

    async def mark_failed(
        self,
        job_id: str,
        *,
        error: str,
        upstream_status: int | None = None,
        upstream_body: Any | None = None,
        elapsed_ms: int | None = None,
        error_class: str | None = None,
        endpoint_used: str | None = None,
        retryable: bool = False,
        retry_suppressed: bool = False,
        outcome_uncertain: bool = False,
    ) -> None:
        now = self.now_iso()
        terminal_status = "uncertain" if outcome_uncertain else "failed"
        await self.db_exec(
            """
            UPDATE jobs
            SET status = ?, auth_header = NULL, finished_at = ?, updated_at = ?,
                elapsed_ms = ?, upstream_status = ?, error = ?,
                upstream_body = ?, error_class = ?, retryable = ?,
                retry_suppressed = ?, outcome_uncertain = ?,
                endpoint_used = COALESCE(?, endpoint_used)
            WHERE job_id = ?
            """,
            (
                terminal_status,
                now,
                now,
                elapsed_ms,
                upstream_status,
                error,
                (self.json_dump(upstream_body) if upstream_body is not None else None),
                error_class or self.error_class_internal(),
                int(retryable),
                int(retry_suppressed),
                int(outcome_uncertain),
                endpoint_used,
                job_id,
            ),
        )

    async def fail_interrupted_running_jobs(self) -> None:
        now = self.now_iso()
        if self.upstream_idempotency_guaranteed():
            requeued = await self.db_exec(
                """
                UPDATE jobs
                SET status = 'queued',
                    started_at = NULL,
                    updated_at = ?,
                    attempts = COALESCE(attempts, 0) + 1,
                    retryable = 0,
                    retry_suppressed = 0,
                    outcome_uncertain = 0
                WHERE status = 'running'
                  AND auth_header IS NOT NULL
                  AND auth_header != ''
                """,
                (now,),
            )
            if requeued:
                self.log.info(
                    "restored %d running jobs with upstream idempotency guarantee",
                    requeued,
                )
        else:
            uncertain = await self.db_exec(
                """
                UPDATE jobs
                SET status = 'uncertain',
                    auth_header = NULL,
                    finished_at = ?,
                    updated_at = ?,
                    error = 'image job worker restarted while the upstream result was unresolved',
                    error_class = ?,
                    retryable = 1,
                    retry_suppressed = 1,
                    outcome_uncertain = 1
                WHERE status = 'running'
                  AND auth_header IS NOT NULL
                  AND auth_header != ''
                """,
                (now, now, self.error_class_network()),
            )
            if uncertain:
                self.log.warning(
                    "marked %d interrupted running jobs uncertain; "
                    "automatic retry suppressed",
                    uncertain,
                )

        failed = await self.db_exec(
            """
            UPDATE jobs
            SET status = 'failed', finished_at = ?, updated_at = ?,
                error = 'image job worker restarted; no auth header to retry',
                error_class = ?, retryable = 0, retry_suppressed = 0,
                outcome_uncertain = 0
            WHERE status = 'running'
            """,
            (now, now, self.error_class_internal()),
        )
        if failed:
            self.log.warning(
                "failed %d running jobs without auth header after restart",
                failed,
            )

    def row_to_response(self, row: sqlite3.Row) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": row["job_id"],
            "status": row["status"],
            "request_type": row["request_type"],
            "endpoint": row["endpoint"],
            "relay_url": row["relay_url"],
            "retention_days": row["retention_days"],
        }
        endpoint_used = self.row_get(row, "endpoint_used")
        if endpoint_used:
            payload["endpoint_used"] = endpoint_used
        if row["status"] == "succeeded":
            try:
                images = _strict_json_loads(row["images_json"] or "[]")
            except (
                json.JSONDecodeError,
                RecursionError,
                TypeError,
                ValueError,
            ):
                images = []
            if not isinstance(images, list):
                images = []
            payload.update(
                {
                    "upstream_status": row["upstream_status"],
                    "elapsed_ms": row["elapsed_ms"],
                    "image_count": row["image_count"],
                    "images": images,
                }
            )
        elif row["status"] in {"failed", "uncertain"}:
            upstream_body: Any = None
            if row["upstream_body"]:
                try:
                    upstream_body = _strict_json_loads(row["upstream_body"])
                except (
                    json.JSONDecodeError,
                    RecursionError,
                    TypeError,
                    ValueError,
                ):
                    upstream_body = row["upstream_body"]
            payload.update(
                {
                    "upstream_status": row["upstream_status"],
                    "elapsed_ms": row["elapsed_ms"],
                    "error": row["error"],
                    "error_class": (
                        self.row_get(row, "error_class") or self.error_class_internal()
                    ),
                    "upstream_body": upstream_body,
                    "retryable": bool(self.row_get(row, "retryable")),
                    "retry_suppressed": bool(self.row_get(row, "retry_suppressed")),
                    "outcome_uncertain": bool(self.row_get(row, "outcome_uncertain")),
                }
            )
            if payload["retry_suppressed"]:
                payload["retry_policy"] = (
                    "automatic retry suppressed because upstream "
                    "idempotency is not guaranteed"
                )
        return payload

    @staticmethod
    def row_get(row: sqlite3.Row, key: str) -> Any:
        try:
            return row[key]
        except (IndexError, KeyError):
            return None


@dataclass(frozen=True)
class ReferencePersistenceFacade:
    db_one_sync: Callable[
        [str, tuple[Any, ...]],
        sqlite3.Row | None,
    ]
    db_exec_sync: Callable[[str, tuple[Any, ...]], int]
    refs_dir: Callable[[], Path]
    now_iso: Callable[[], str]
    token_hex: Callable[[int], str] = secrets.token_hex
    file_path_fn: Callable[[str, str], Path] | None = None

    def file_path(self, token: str, ext: str) -> Path:
        return self.refs_dir() / f"{token}.{ext}"

    def existing_ref(
        self,
        auth_digest: str,
        sha: str,
    ) -> tuple[str, str] | None:
        row = self.db_one_sync(
            "SELECT token, ext FROM refs WHERE auth_hash = ? AND sha256 = ?",
            (auth_digest, sha),
        )
        if row is None:
            return None
        token = row["token"]
        ext = row["ext"]
        file_path = self.file_path_fn or self.file_path
        if file_path(token, ext).exists():
            return token, ext
        self.db_exec_sync(
            "DELETE FROM refs WHERE auth_hash = ? AND sha256 = ?",
            (auth_digest, sha),
        )
        return None

    def write_ref(
        self,
        auth_digest: str,
        sha: str,
        token: str,
        ext: str,
        raw: bytes,
    ) -> None:
        file_path = self.file_path_fn or self.file_path
        path = file_path(token, ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{self.token_hex(8)}.tmp")
        try:
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        try:
            self.db_exec_sync(
                """
                INSERT INTO refs (
                    auth_hash, sha256, token, ext, size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    auth_digest,
                    sha,
                    token,
                    ext,
                    len(raw),
                    self.now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            pass


@dataclass(frozen=True)
class RetentionFacade:
    data_dir: Callable[[], Path]
    refs_dir: Callable[[], Path]
    db_exec_sync: Callable[[str, tuple[Any, ...]], int]
    db_exec: DbExec
    db_all: DbAll
    utc_now: Callable[[], datetime]
    max_retention_days: Callable[[], int]
    job_ttl_days: Callable[[], int]
    log: logging.Logger
    sweep_dir_fn: Callable[[Path, float], tuple[int, int]] | None = None
    sweep_filesystem_fn: Callable[[float], tuple[int, int]] | None = None

    def sweep_dir(self, base: Path, cutoff_ts: float) -> tuple[int, int]:
        if not base.exists():
            return 0, 0
        removed_files = 0
        removed_bytes = 0
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if stat.st_mtime < cutoff_ts:
                try:
                    size = stat.st_size
                    path.unlink()
                    removed_files += 1
                    removed_bytes += size
                except OSError:
                    continue
        for path in sorted(
            base.rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    continue
        return removed_files, removed_bytes

    def sweep_filesystem(self, cutoff_ts: float) -> tuple[int, int]:
        sweep_dir = self.sweep_dir_fn or self.sweep_dir
        total_files, total_bytes = sweep_dir(self.refs_dir(), cutoff_ts)
        cutoff_iso = datetime.fromtimestamp(
            cutoff_ts,
            tz=timezone.utc,
        ).isoformat()
        try:
            self.db_exec_sync(
                "DELETE FROM refs WHERE created_at < ?",
                (cutoff_iso,),
            )
        except sqlite3.OperationalError:
            pass
        return total_files, total_bytes

    @staticmethod
    def _row_value(row: Any, key: str) -> Any:
        try:
            return row[key]
        except (IndexError, KeyError, TypeError):
            return None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def job_artifact_expiry(self, row: Any) -> datetime | None:
        created_at = self._parse_datetime(self._row_value(row, "created_at"))
        if created_at is None:
            return None
        try:
            retention_days = int(self._row_value(row, "retention_days"))
        except (TypeError, ValueError):
            retention_days = self.max_retention_days()
        retention_days = min(
            self.max_retention_days(),
            max(1, retention_days),
        )
        expiries = [created_at + timedelta(days=retention_days)]

        images_json = self._row_value(row, "images_json")
        if isinstance(images_json, str) and images_json:
            try:
                images = _strict_json_loads(images_json)
            except (
                json.JSONDecodeError,
                RecursionError,
                TypeError,
                ValueError,
            ):
                images = []
            if isinstance(images, list):
                for image in images:
                    if not isinstance(image, dict):
                        continue
                    expires_at = self._parse_datetime(image.get("expires_at"))
                    if expires_at is not None:
                        expiries.append(expires_at)
        return min(expiries)

    def job_effective_expiry(self, row: Any) -> datetime | None:
        artifact_expiry = self.job_artifact_expiry(row)
        if artifact_expiry is None:
            return None
        finished_at = self._parse_datetime(self._row_value(row, "finished_at"))
        if finished_at is None:
            return artifact_expiry
        row_ttl_expiry = finished_at + timedelta(days=self.job_ttl_days())
        return min(artifact_expiry, row_ttl_expiry)

    def remove_job_artifacts(self, row: Any) -> tuple[int, int, bool]:
        job_id = self._row_value(row, "job_id")
        created_at = self._parse_datetime(self._row_value(row, "created_at"))
        if (
            not isinstance(job_id, str)
            or not job_id
            or "/" in job_id
            or "\\" in job_id
            or created_at is None
        ):
            return 0, 0, False

        temp_root = self.data_dir() / "images" / "temp"
        job_dir = (
            temp_root
            / created_at.strftime("%Y")
            / created_at.strftime("%m")
            / created_at.strftime("%d")
            / job_id
        )
        if not job_dir.exists():
            return 0, 0, True

        removed_files = 0
        removed_bytes = 0
        for path in job_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            removed_files += 1
            removed_bytes += stat.st_size
        try:
            shutil.rmtree(job_dir)
        except OSError:
            self.log.warning(
                "retention sweeper could not remove job directory %s",
                job_dir,
                exc_info=True,
            )
            return 0, 0, False

        parent = job_dir.parent
        while parent != temp_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return removed_files, removed_bytes, True

    async def run_pass(self) -> None:
        now = self.utc_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        rows = await self.db_all(
            """
            SELECT job_id, created_at, finished_at, retention_days, images_json
            FROM jobs
            WHERE finished_at IS NOT NULL
            """,
            (),
        )
        removed_files = 0
        removed_bytes = 0
        removed_jobs = 0
        for row in rows:
            expires_at = self.job_effective_expiry(row)
            if expires_at is None or expires_at > now:
                continue
            files, freed, cleaned = await asyncio.to_thread(
                self.remove_job_artifacts,
                row,
            )
            removed_files += files
            removed_bytes += freed
            if not cleaned:
                continue
            removed_jobs += await self.db_exec(
                "DELETE FROM jobs WHERE job_id = ? AND finished_at IS NOT NULL",
                (self._row_value(row, "job_id"),),
            )

        cutoff = now - timedelta(days=self.max_retention_days())
        sweep_filesystem = self.sweep_filesystem_fn or self.sweep_filesystem
        ref_files, ref_bytes = await asyncio.to_thread(
            sweep_filesystem,
            cutoff.timestamp(),
        )
        removed_files += ref_files
        removed_bytes += ref_bytes
        if removed_files:
            self.log.info(
                "retention sweeper removed %d files (%d bytes)",
                removed_files,
                removed_bytes,
            )

        if removed_jobs:
            self.log.info(
                "retention sweeper removed %d job rows",
                removed_jobs,
            )
