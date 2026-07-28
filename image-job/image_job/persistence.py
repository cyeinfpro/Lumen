"""Package-local SQLite, job state, reference, and retention helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .credential_migration import (
    checkpoint_sensitive_migration,
    migrate_job_credentials,
)
from .credential_vault import CredentialVault
from .retention_walk import (
    TraversalBudget,
    iter_child_dirs,
    new_traversal_budget,
    sweep_tree_bounded,
)


ALLOWED_SQLITE_JOURNAL_MODES = frozenset(
    {
        "WAL",
        "DELETE",
        "TRUNCATE",
        "PERSIST",
        "MEMORY",
        "OFF",
    }
)
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


def _parse_utc_datetime(value: Any) -> datetime | None:
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


def _initial_retention_expiry(created_at: str, retention_days: Any) -> str:
    created = _parse_utc_datetime(created_at)
    if created is None:
        raise ValueError("created_at must be a valid datetime")
    try:
        days = max(1, int(retention_days))
    except (TypeError, ValueError):
        days = 1
    return (created + timedelta(days=days)).isoformat()


def _terminal_retention_expiry(
    finished_at: str,
    *,
    job_ttl_days: int,
    images: list[dict[str, Any]] | None = None,
) -> str:
    finished = _parse_utc_datetime(finished_at)
    if finished is None:
        raise ValueError("finished_at must be a valid datetime")
    expiries = [finished + timedelta(days=max(1, int(job_ttl_days)))]
    for image in images or ():
        expires_at = _parse_utc_datetime(image.get("expires_at"))
        if expires_at is not None:
            expiries.append(expires_at)
    return min(expiries).isoformat()


def sqlite_tuning_pragmas(journal_mode: str) -> tuple[str, ...]:
    mode = journal_mode if journal_mode in ALLOWED_SQLITE_JOURNAL_MODES else "WAL"
    return (
        f"PRAGMA journal_mode = {mode}",
        "PRAGMA secure_delete = ON",
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
_REFS_REQUIRED_COLUMNS = frozenset(
    {
        "sha256",
        "token",
        "ext",
        "size",
        "created_at",
    }
)


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
    credential_vault: CredentialVault,
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
                auth_ciphertext BLOB,
                auth_nonce BLOB,
                auth_key_id TEXT,
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
                retention_expires_at TEXT,
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
            CREATE INDEX IF NOT EXISTS jobs_finished_job_idx
                ON jobs(finished_at, job_id)
                WHERE finished_at IS NOT NULL;
            """
        )
        _ensure_refs_auth_schema(conn)
        _ensure_column(conn, "jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "jobs", "error_class", "TEXT")
        _ensure_column(conn, "jobs", "endpoint_used", "TEXT")
        _ensure_column(conn, "jobs", "upstream_auth_hash", "TEXT")
        _ensure_column(conn, "jobs", "auth_ciphertext", "BLOB")
        _ensure_column(conn, "jobs", "auth_nonce", "BLOB")
        _ensure_column(conn, "jobs", "auth_key_id", "TEXT")
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
        _ensure_column(conn, "jobs", "retention_expires_at", "TEXT")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS jobs_retention_expiry_idx
                ON jobs(retention_expires_at, job_id)
                WHERE retention_expires_at IS NOT NULL
            """
        )
        # H-19：提交请求的 request_id 必须落库，否则异步 worker 真正执行时
        # ContextVar 早已不在，两段日志无法关联。
        _ensure_column(conn, "jobs", "request_id", "TEXT")
        migrate_job_credentials(
            conn,
            credential_vault=credential_vault,
            auth_hash=auth_hash,
        )
        conn.execute("DROP INDEX IF EXISTS jobs_auth_idempotency_idx")
        conn.execute(
            """
            CREATE UNIQUE INDEX jobs_auth_idempotency_idx
                ON jobs(auth_hash, upstream_auth_hash, idempotency_key)
                WHERE idempotency_key IS NOT NULL
            """
        )
        checkpoint_sensitive_migration(conn)
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
    credential_vault: CredentialVault
    json_dump: Callable[[Any], str]
    upstream_base_url: Callable[[], str]
    upstream_idempotency_guaranteed: Callable[[], bool]
    error_class_internal: Callable[[], str]
    error_class_network: Callable[[], str]
    job_ttl_days: Callable[[], int]
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
        request_id: str | None = None,
    ) -> None:
        now = self.now_iso()
        retention_expires_at = _initial_retention_expiry(
            now,
            payload["retention_days"],
        )
        owner_auth = owner_auth_header or auth_header
        owner_hash = self.auth_hash(owner_auth)
        encrypted = self.credential_vault.encrypt(
            auth_header,
            job_id=job_id,
            owner_hash=owner_hash,
        )
        await self.db_exec(
            """
            INSERT INTO jobs (
                job_id, auth_hash, upstream_auth_hash,
                auth_ciphertext, auth_nonce, auth_key_id,
                idempotency_key, request_hash, request_type, endpoint,
                payload_json, status, relay_url, retention_days,
                created_at, updated_at, request_id, retention_expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                owner_hash,
                self.auth_hash(auth_header),
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.key_id,
                idempotency_key,
                payload_hash,
                payload["request_type"],
                payload["endpoint"],
                self.json_dump(payload),
                self.upstream_base_url(),
                payload["retention_days"],
                now,
                now,
                request_id or None,
                retention_expires_at,
            ),
        )

    @staticmethod
    def row_has_credential(row: sqlite3.Row) -> bool:
        try:
            return all(
                row[key] is not None
                for key in ("auth_ciphertext", "auth_nonce", "auth_key_id")
            )
        except (IndexError, KeyError, TypeError):
            return False

    async def ensure_queued_job_scheduled(self, row: sqlite3.Row) -> None:
        if row["status"] != "queued" or not self.row_has_credential(row):
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
            "WHERE job_id = ? AND status = 'queued' "
            "AND auth_ciphertext IS NOT NULL "
            "AND auth_nonce IS NOT NULL "
            "AND auth_key_id IS NOT NULL",
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
        retention_expires_at = _terminal_retention_expiry(
            now,
            job_ttl_days=self.job_ttl_days(),
            images=images,
        )
        await self.db_exec(
            """
            UPDATE jobs
            SET status = 'succeeded',
                auth_header = NULL,
                auth_ciphertext = NULL,
                auth_nonce = NULL,
                auth_key_id = NULL,
                finished_at = ?, updated_at = ?, elapsed_ms = ?,
                upstream_status = ?, image_count = ?, images_json = ?,
                error = NULL, upstream_body = NULL, error_class = NULL,
                retryable = 0, retry_suppressed = 0, outcome_uncertain = 0,
                retention_expires_at = CASE
                    WHEN retention_expires_at IS NULL
                         OR retention_expires_at > ?
                    THEN ?
                    ELSE retention_expires_at
                END,
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
                retention_expires_at,
                retention_expires_at,
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
        retention_expires_at = _terminal_retention_expiry(
            now,
            job_ttl_days=self.job_ttl_days(),
        )
        await self.db_exec(
            """
            UPDATE jobs
            SET status = ?,
                auth_header = NULL,
                auth_ciphertext = NULL,
                auth_nonce = NULL,
                auth_key_id = NULL,
                finished_at = ?, updated_at = ?,
                elapsed_ms = ?, upstream_status = ?, error = ?,
                upstream_body = ?, error_class = ?, retryable = ?,
                retry_suppressed = ?, outcome_uncertain = ?,
                retention_expires_at = CASE
                    WHEN retention_expires_at IS NULL
                         OR retention_expires_at > ?
                    THEN ?
                    ELSE retention_expires_at
                END,
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
                retention_expires_at,
                retention_expires_at,
                endpoint_used,
                job_id,
            ),
        )

    async def mark_cancelled(self, job_id: str, *, error: str = "cancelled") -> bool:
        now = self.now_iso()
        retention_expires_at = _terminal_retention_expiry(
            now,
            job_ttl_days=self.job_ttl_days(),
        )
        changed = await self.db_exec(
            """
            UPDATE jobs
            SET status = 'cancelled',
                auth_header = NULL,
                auth_ciphertext = NULL,
                auth_nonce = NULL,
                auth_key_id = NULL,
                finished_at = ?,
                updated_at = ?,
                error = ?,
                retryable = 0,
                retry_suppressed = 0,
                outcome_uncertain = 0,
                retention_expires_at = CASE
                    WHEN retention_expires_at IS NULL
                         OR retention_expires_at > ?
                    THEN ?
                    ELSE retention_expires_at
                END
            WHERE job_id = ? AND status = 'queued'
            """,
            (
                now,
                now,
                error,
                retention_expires_at,
                retention_expires_at,
                job_id,
            ),
        )
        return changed == 1

    async def fail_interrupted_running_jobs(self) -> None:
        now = self.now_iso()
        retention_expires_at = _terminal_retention_expiry(
            now,
            job_ttl_days=self.job_ttl_days(),
        )
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
                  AND auth_ciphertext IS NOT NULL
                  AND auth_nonce IS NOT NULL
                  AND auth_key_id IS NOT NULL
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
                    auth_ciphertext = NULL,
                    auth_nonce = NULL,
                    auth_key_id = NULL,
                    finished_at = ?,
                    updated_at = ?,
                    error = 'image job worker restarted while the upstream result was unresolved',
                    error_class = ?,
                    retryable = 1,
                    retry_suppressed = 1,
                    outcome_uncertain = 1,
                    retention_expires_at = CASE
                        WHEN retention_expires_at IS NULL
                             OR retention_expires_at > ?
                        THEN ?
                        ELSE retention_expires_at
                    END
                WHERE status = 'running'
                  AND auth_ciphertext IS NOT NULL
                  AND auth_nonce IS NOT NULL
                  AND auth_key_id IS NOT NULL
                """,
                (
                    now,
                    now,
                    self.error_class_network(),
                    retention_expires_at,
                    retention_expires_at,
                ),
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
            SET status = 'failed',
                auth_header = NULL,
                auth_ciphertext = NULL,
                auth_nonce = NULL,
                auth_key_id = NULL,
                finished_at = ?, updated_at = ?,
                error = 'image job worker restarted; no auth header to retry',
                error_class = ?, retryable = 0, retry_suppressed = 0,
                outcome_uncertain = 0,
                retention_expires_at = CASE
                    WHEN retention_expires_at IS NULL
                         OR retention_expires_at > ?
                    THEN ?
                    ELSE retention_expires_at
                END
            WHERE status = 'running'
            """,
            (
                now,
                now,
                self.error_class_internal(),
                retention_expires_at,
                retention_expires_at,
            ),
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
    finished_row_batch_size: int = 256
    ref_delete_batch_size: int = 512
    ref_scan_max_entries: int = 2_000
    job_dir_scan_max_entries: int = 2_000
    orphan_scan_max_entries: int = 2_000
    orphan_scan_max_partitions: int = 32
    orphan_scan_max_job_dirs: int = 256
    scan_time_budget_s: float = 1.0
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        integer_budgets = {
            "finished_row_batch_size": self.finished_row_batch_size,
            "ref_delete_batch_size": self.ref_delete_batch_size,
            "ref_scan_max_entries": self.ref_scan_max_entries,
            "job_dir_scan_max_entries": self.job_dir_scan_max_entries,
            "orphan_scan_max_entries": self.orphan_scan_max_entries,
            "orphan_scan_max_partitions": self.orphan_scan_max_partitions,
            "orphan_scan_max_job_dirs": self.orphan_scan_max_job_dirs,
        }
        for name, value in integer_budgets.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.scan_time_budget_s <= 0:
            raise ValueError("scan_time_budget_s must be positive")

    def _new_budget(self, max_entries: int) -> TraversalBudget:
        return new_traversal_budget(
            max_entries,
            time_budget_s=self.scan_time_budget_s,
            monotonic=self.monotonic,
        )

    def sweep_dir(self, base: Path, cutoff_ts: float) -> tuple[int, int]:
        if not base.is_dir():
            return 0, 0
        removed_files, removed_bytes, _complete = sweep_tree_bounded(
            base,
            self._new_budget(self.ref_scan_max_entries),
            cutoff_ts=cutoff_ts,
            remove_directory=False,
        )
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
                """
                DELETE FROM refs
                WHERE rowid IN (
                    SELECT rowid
                    FROM refs
                    WHERE created_at < ?
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT ?
                )
                """,
                (cutoff_iso, self.ref_delete_batch_size),
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
        return _parse_utc_datetime(value)

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

    def remove_job_artifacts(
        self,
        row: Any,
        budget: TraversalBudget | None = None,
    ) -> tuple[int, int, bool]:
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
        return self.remove_job_dir(job_dir, temp_root, budget=budget)

    def remove_job_dir(
        self,
        job_dir: Path,
        temp_root: Path,
        *,
        budget: TraversalBudget | None = None,
    ) -> tuple[int, int, bool]:
        try:
            job_dir.relative_to(temp_root)
        except ValueError:
            return 0, 0, False
        try:
            root_info = job_dir.lstat()
        except FileNotFoundError:
            return 0, 0, True
        except OSError:
            return 0, 0, False
        if not stat.S_ISDIR(root_info.st_mode):
            return 0, 0, False

        traversal_budget = budget or self._new_budget(
            self.job_dir_scan_max_entries
        )
        removed_files, removed_bytes, cleaned = sweep_tree_bounded(
            job_dir,
            traversal_budget,
            cutoff_ts=None,
            remove_directory=True,
        )
        if not cleaned:
            if not traversal_budget.exhausted:
                self.log.warning(
                    "retention sweeper could not remove job directory %s",
                    job_dir,
                )
            return removed_files, removed_bytes, False

        parent = job_dir.parent
        while parent != temp_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return removed_files, removed_bytes, True

    def orphan_job_dir_candidates_sync(
        self,
        cutoff_ts: float,
    ) -> tuple[Path, ...]:
        temp_root = self.data_dir() / "images" / "temp"
        if not temp_root.is_dir():
            return ()

        budget = self._new_budget(self.orphan_scan_max_entries)
        partitions_scanned = 0
        job_dirs_scanned = 0
        candidates: list[Path] = []
        for year_dir in iter_child_dirs(temp_root, budget):
            for month_dir in iter_child_dirs(year_dir, budget):
                for day_dir in iter_child_dirs(month_dir, budget):
                    partitions_scanned += 1
                    for job_dir in iter_child_dirs(day_dir, budget):
                        job_dirs_scanned += 1
                        old_directory = False
                        try:
                            info = job_dir.lstat()
                        except OSError:
                            pass
                        else:
                            old_directory = (
                                stat.S_ISDIR(info.st_mode)
                                and info.st_mtime < cutoff_ts
                            )
                        if old_directory:
                            candidates.append(job_dir)
                        if (
                            budget.exhausted
                            or job_dirs_scanned >= self.orphan_scan_max_job_dirs
                        ):
                            return tuple(candidates)
                    if (
                        budget.exhausted
                        or partitions_scanned >= self.orphan_scan_max_partitions
                    ):
                        return tuple(candidates)
                if budget.exhausted:
                    return tuple(candidates)
            if budget.exhausted:
                return tuple(candidates)
        return tuple(candidates)

    def sweep_orphan_job_dirs_sync(
        self,
        known_job_ids: set[str],
        cutoff_ts: float,
        candidates: tuple[Path, ...] | None = None,
    ) -> tuple[int, int]:
        """删掉 images/temp 下已经没有 jobs 行对应的产物目录。

        H-17：run_pass 的主循环只顺着 `finished_at IS NOT NULL` 的行删产物，
        所以「行被删了但目录删除当时失败」「DB 被重置/回滚」「任务永远停在
        queued/running 从未 finished」留下的目录没有任何人负责，磁盘只增不减。

        故意不按文件 mtime 扫（那会误删仍在保留期内、只是内容旧的活任务产物，
        见 test_retention_pass_uses_each_job_expiry_not_file_mtime），而是先按
        「jobs 表里还有没有这个 job_id」判定孤儿，再用目录 mtime 早于 cutoff
        做二次确认，避免删掉刚建目录还没来得及插行的竞态窗口。
        """
        temp_root = self.data_dir() / "images" / "temp"
        job_dirs = (
            self.orphan_job_dir_candidates_sync(cutoff_ts)
            if candidates is None
            else candidates
        )
        cleanup_budget = self._new_budget(self.job_dir_scan_max_entries)
        removed_files = 0
        removed_bytes = 0
        for job_dir in job_dirs:
            if not cleanup_budget.available():
                break
            if job_dir.name in known_job_ids:
                continue
            try:
                info = job_dir.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_mtime >= cutoff_ts
                ):
                    continue
            except OSError:
                continue
            files, freed, _cleaned = self.remove_job_dir(
                job_dir,
                temp_root,
                budget=cleanup_budget,
            )
            removed_files += files
            removed_bytes += freed
            if cleanup_budget.exhausted:
                break
        return removed_files, removed_bytes

    async def sweep_orphan_job_dirs(self, cutoff_ts: float) -> tuple[int, int]:
        candidates = await asyncio.to_thread(
            self.orphan_job_dir_candidates_sync,
            cutoff_ts,
        )
        candidate_ids = sorted({path.name for path in candidates})
        known: set[str] = set()
        for offset in range(0, len(candidate_ids), 500):
            batch = candidate_ids[offset : offset + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = await self.db_all(
                f"""
                SELECT job_id
                FROM jobs
                WHERE job_id IN ({placeholders})
                ORDER BY job_id ASC
                LIMIT ?
                """,  # nosec B608 - placeholders are generated, not user input.
                (*batch, len(batch)),
            )
            known.update(
                str(self._row_value(row, "job_id"))
                for row in rows
                if self._row_value(row, "job_id") is not None
            )
        return await asyncio.to_thread(
            self.sweep_orphan_job_dirs_sync,
            known,
            cutoff_ts,
            candidates,
        )

    async def run_pass(self) -> None:
        now = self.utc_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        missing_expiry_rows = await self.db_all(
            """
            SELECT job_id, created_at, finished_at, retention_days, images_json
            FROM jobs
            WHERE finished_at IS NOT NULL
              AND retention_expires_at IS NULL
            ORDER BY finished_at ASC, job_id ASC
            LIMIT ?
            """,
            (self.finished_row_batch_size,),
        )
        for row in missing_expiry_rows:
            expires_at = self.job_effective_expiry(row)
            if expires_at is None:
                continue
            await self.db_exec(
                """
                UPDATE jobs
                SET retention_expires_at = ?
                WHERE job_id = ?
                  AND finished_at IS NOT NULL
                  AND retention_expires_at IS NULL
                """,
                (
                    expires_at.isoformat(),
                    self._row_value(row, "job_id"),
                ),
            )

        rows = await self.db_all(
            """
            SELECT job_id, created_at, finished_at, retention_days, images_json
            FROM jobs
            WHERE retention_expires_at IS NOT NULL
              AND retention_expires_at <= ?
            ORDER BY retention_expires_at ASC, job_id ASC
            LIMIT ?
            """,
            (now.isoformat(), self.finished_row_batch_size),
        )
        removed_files = 0
        removed_bytes = 0
        removed_jobs = 0
        job_dir_budget = self._new_budget(self.job_dir_scan_max_entries)
        for row in rows:
            if not job_dir_budget.available():
                break
            expires_at = self.job_effective_expiry(row)
            if expires_at is None or expires_at > now:
                continue
            await self.db_exec(
                """
                UPDATE jobs
                SET auth_header = NULL,
                    auth_ciphertext = NULL,
                    auth_nonce = NULL,
                    auth_key_id = NULL
                WHERE job_id = ?
                """,
                (self._row_value(row, "job_id"),),
            )
            files, freed, cleaned = await asyncio.to_thread(
                self.remove_job_artifacts,
                row,
                job_dir_budget,
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
        orphan_files, orphan_bytes = await self.sweep_orphan_job_dirs(
            cutoff.timestamp()
        )
        removed_files += orphan_files
        removed_bytes += orphan_bytes
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
