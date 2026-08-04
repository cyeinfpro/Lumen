"""Package-local SQLite, job state, reference, and retention helpers."""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .credential_migration import (
    checkpoint_sensitive_migration,
    migrate_job_credentials,
)
from .credential_vault import CredentialVault
from .durable_files import durable_mkdir
from .persistence_parts import common as _persistence_common
from .persistence_parts.references import (
    ReferencePersistenceFacade as ReferencePersistenceFacade,
)
from .persistence_parts.retention import RetentionFacade as RetentionFacade
from .retention_scan_schema import ensure_retention_scan_schema


DbAll = _persistence_common.DbAll
DbExec = _persistence_common.DbExec
EnqueueJob = _persistence_common.EnqueueJob
_initial_retention_expiry = _persistence_common.initial_retention_expiry
_parse_finite_float = _persistence_common.parse_finite_float
_parse_utc_datetime = _persistence_common.parse_utc_datetime
_reject_json_constant = _persistence_common.reject_json_constant
_strict_json_loads = _persistence_common.strict_json_loads
_terminal_retention_expiry = _persistence_common.terminal_retention_expiry


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
TERMINAL_JOB_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "cancelled",
        "cancel_requested",
        "uncertain",
    }
)
_SQLITE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
REFERENCE_TIMESTAMP_NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_REFERENCE_TIMESTAMP_COLUMN_SQL = (
    "strftime('%Y-%m-%dT%H:%M:%fZ', created_at)"
)


def _sqlite_identifier(value: str) -> str:
    if _SQLITE_IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"invalid SQLite identifier: {value!r}")
    return f'"{value}"'


def sqlite_tuning_pragmas(journal_mode: str) -> tuple[str, ...]:
    mode = journal_mode if journal_mode in ALLOWED_SQLITE_JOURNAL_MODES else "WAL"
    return (
        f"PRAGMA journal_mode = {mode}",
        "PRAGMA secure_delete = ON",
        "PRAGMA synchronous = FULL",
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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS refs_created_datetime_idx
                ON refs(julianday(created_at))
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS refs_token_ext_idx
                ON refs(token, ext)
            """
        )


def _normalize_refs_created_at(conn: sqlite3.Connection) -> None:
    normalized = _REFERENCE_TIMESTAMP_COLUMN_SQL
    conn.execute(
        f"""
        UPDATE refs
        SET created_at = {normalized}
        WHERE {normalized} IS NOT NULL
          AND created_at <> {normalized}
        """  # nosec B608 - normalized is a package-owned SQL expression.
    )


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
        _normalize_refs_created_at(conn)
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
    durable_mkdir(data_dir)
    durable_mkdir(refs_dir)
    durable_mkdir(db_path.parent)
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
        ensure_retention_scan_schema(conn)
        _ensure_column(conn, "jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "jobs", "execution_token", "TEXT")
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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS jobs_active_updated_idx
                ON jobs(status, updated_at, job_id)
                WHERE status IN ('queued', 'running')
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

    async def mark_running(self, job_id: str) -> str | None:
        now = self.now_iso()
        execution_token = secrets.token_urlsafe(24)
        changed = await self.db_exec(
            "UPDATE jobs SET status = 'running', "
            "started_at = COALESCE(started_at, ?), "
            "updated_at = ?, attempts = attempts + 1, execution_token = ? "
            "WHERE job_id = ? AND status = 'queued' "
            "AND execution_token IS NULL "
            "AND auth_ciphertext IS NOT NULL "
            "AND auth_nonce IS NOT NULL "
            "AND auth_key_id IS NOT NULL",
            (now, now, execution_token, job_id),
        )
        if changed != 1:
            return None
        return execution_token

    async def touch_running(self, job_id: str, execution_token: str) -> bool:
        changed = await self.db_exec(
            """
            UPDATE jobs
            SET updated_at = ?
            WHERE job_id = ?
              AND status = 'running'
              AND execution_token = ?
            """,
            (self.now_iso(), job_id, execution_token),
        )
        return changed == 1

    async def mark_succeeded(
        self,
        job_id: str,
        *,
        execution_token: str,
        upstream_status: int,
        elapsed_ms: int,
        images: list[dict[str, Any]],
        endpoint_used: str | None = None,
    ) -> bool:
        now = self.now_iso()
        retention_expires_at = _terminal_retention_expiry(
            now,
            job_ttl_days=self.job_ttl_days(),
            images=images,
        )
        changed = await self.db_exec(
            """
            UPDATE jobs
            SET status = 'succeeded',
                auth_header = NULL,
                auth_ciphertext = NULL,
                auth_nonce = NULL,
                auth_key_id = NULL,
                execution_token = NULL,
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
              AND status = 'running'
              AND execution_token = ?
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
                execution_token,
            ),
        )
        return changed == 1

    async def mark_failed(
        self,
        job_id: str,
        *,
        execution_token: str,
        error: str,
        upstream_status: int | None = None,
        upstream_body: Any | None = None,
        elapsed_ms: int | None = None,
        error_class: str | None = None,
        endpoint_used: str | None = None,
        retryable: bool = False,
        retry_suppressed: bool = False,
        outcome_uncertain: bool = False,
    ) -> bool:
        now = self.now_iso()
        terminal_status = "uncertain" if outcome_uncertain else "failed"
        retention_expires_at = _terminal_retention_expiry(
            now,
            job_ttl_days=self.job_ttl_days(),
        )
        changed = await self.db_exec(
            """
            UPDATE jobs
            SET status = ?,
                auth_header = NULL,
                auth_ciphertext = NULL,
                auth_nonce = NULL,
                auth_key_id = NULL,
                execution_token = NULL,
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
              AND status = 'running'
              AND execution_token = ?
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
                execution_token,
            ),
        )
        return changed == 1

    async def mark_cancelled(
        self,
        job_id: str,
        *,
        execution_token: str | None = None,
        error: str = "cancelled",
    ) -> bool:
        now = self.now_iso()
        after_dispatch = execution_token is not None
        terminal_status = "cancel_requested" if after_dispatch else "cancelled"
        retention_expires_at = _terminal_retention_expiry(
            now,
            job_ttl_days=self.job_ttl_days(),
        )
        state_predicate = (
            "status = 'running' AND execution_token = ?"
            if after_dispatch
            else "status = 'queued' AND execution_token IS NULL"
        )
        params: tuple[Any, ...] = (
            terminal_status,
            now,
            now,
            error,
            int(after_dispatch),
            int(after_dispatch),
            retention_expires_at,
            retention_expires_at,
            job_id,
        )
        if execution_token is not None:
            params = (*params, execution_token)
        changed = await self.db_exec(
            f"""
            UPDATE jobs
            SET status = ?,
                auth_header = NULL,
                auth_ciphertext = NULL,
                auth_nonce = NULL,
                auth_key_id = NULL,
                execution_token = NULL,
                finished_at = ?,
                updated_at = ?,
                error = ?,
                retryable = 0,
                retry_suppressed = ?,
                outcome_uncertain = ?,
                retention_expires_at = CASE
                    WHEN retention_expires_at IS NULL
                         OR retention_expires_at > ?
                    THEN ?
                    ELSE retention_expires_at
                END
            WHERE job_id = ? AND {state_predicate}
            """,  # nosec B608 - predicate is selected from fixed literals.
            params,
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
                    execution_token = NULL,
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
                    execution_token = NULL,
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
                execution_token = NULL,
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
        elif row["status"] in {"failed", "uncertain", "cancel_requested"}:
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
