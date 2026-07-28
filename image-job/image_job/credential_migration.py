"""SQLite migration from legacy plaintext job credentials."""

from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Callable

from .credential_vault import CredentialVault, CredentialVaultError


_ACTIVE_CREDENTIAL_STATUSES = frozenset({"queued", "running"})
_CREDENTIAL_CLEANUP_META_KEY = "credential_plaintext_cleanup_pending"


def _ensure_metadata_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS image_job_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def _set_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO image_job_metadata(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _metadata_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM image_job_metadata WHERE key = ?",
        (key,),
    ).fetchone()
    return None if row is None else str(row["value"])


def _credential_envelope_state(row: sqlite3.Row) -> tuple[bool, bool]:
    values = (
        row["auth_ciphertext"],
        row["auth_nonce"],
        row["auth_key_id"],
    )
    present = tuple(value is not None for value in values)
    return any(present), all(present)


def migrate_job_credentials(
    conn: sqlite3.Connection,
    *,
    credential_vault: CredentialVault,
    auth_hash: Callable[[str], str],
) -> None:
    _ensure_metadata_table(conn)
    rows = conn.execute(
        """
        SELECT job_id, auth_hash, upstream_auth_hash, auth_header,
               auth_ciphertext, auth_nonce, auth_key_id, status
        FROM jobs
        """
    ).fetchall()
    savepoint = "jobs_credential_migration"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        if any(row["auth_header"] is not None for row in rows):
            _set_metadata(conn, _CREDENTIAL_CLEANUP_META_KEY, "1")

        for row in rows:
            job_id = str(row["job_id"] or "")
            owner_hash = str(row["auth_hash"] or "")
            status = str(row["status"] or "")
            plaintext = row["auth_header"]
            has_envelope, complete_envelope = _credential_envelope_state(row)

            if status not in _ACTIVE_CREDENTIAL_STATUSES:
                if plaintext is not None or has_envelope:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET auth_header = NULL,
                            auth_ciphertext = NULL,
                            auth_nonce = NULL,
                            auth_key_id = NULL
                        WHERE job_id = ?
                        """,
                        (job_id,),
                    )
                continue

            if has_envelope and not complete_envelope:
                raise CredentialVaultError(
                    f"job {job_id} has an incomplete credential envelope"
                )

            if plaintext is not None:
                authorization = str(plaintext)
                upstream_digest = auth_hash(authorization)
                if complete_envelope:
                    decrypted = credential_vault.decrypt_job_row(row)
                    if not hmac.compare_digest(decrypted, authorization):
                        raise CredentialVaultError(
                            f"job {job_id} plaintext and ciphertext credentials differ"
                        )
                    ciphertext = bytes(row["auth_ciphertext"])
                    nonce = bytes(row["auth_nonce"])
                    key_id = str(row["auth_key_id"])
                else:
                    encrypted = credential_vault.encrypt(
                        authorization,
                        job_id=job_id,
                        owner_hash=owner_hash,
                    )
                    ciphertext = encrypted.ciphertext
                    nonce = encrypted.nonce
                    key_id = encrypted.key_id
                conn.execute(
                    """
                    UPDATE jobs
                    SET upstream_auth_hash = COALESCE(upstream_auth_hash, ?),
                        auth_header = NULL,
                        auth_ciphertext = ?,
                        auth_nonce = ?,
                        auth_key_id = ?
                    WHERE job_id = ?
                    """,
                    (
                        upstream_digest,
                        ciphertext,
                        nonce,
                        key_id,
                        job_id,
                    ),
                )
                continue

            if complete_envelope:
                authorization = credential_vault.decrypt_job_row(row)
                conn.execute(
                    """
                    UPDATE jobs
                    SET upstream_auth_hash = COALESCE(upstream_auth_hash, ?)
                    WHERE job_id = ?
                    """,
                    (auth_hash(authorization), job_id),
                )
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")


def checkpoint_sensitive_migration(conn: sqlite3.Connection) -> None:
    if _metadata_value(conn, _CREDENTIAL_CLEANUP_META_KEY) != "1":
        return
    mode_row = conn.execute("PRAGMA journal_mode").fetchone()
    mode = str(mode_row[0]).lower() if mode_row is not None else ""
    if mode == "wal":
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise sqlite3.OperationalError(
                "credential migration WAL checkpoint remained busy"
            )
    elif mode == "persist":
        _reset_persist_journal(conn)
    _set_metadata(conn, _CREDENTIAL_CLEANUP_META_KEY, "0")
    if mode == "persist":
        _reset_persist_journal(conn)


def _reset_persist_journal(conn: sqlite3.Connection) -> None:
    deleted = conn.execute("PRAGMA journal_mode = DELETE").fetchone()
    deleted_mode = str(deleted[0]).lower() if deleted is not None else ""
    if deleted_mode != "delete":
        raise sqlite3.OperationalError(
            "credential migration could not clear PERSIST journal"
        )
    restored = conn.execute("PRAGMA journal_mode = PERSIST").fetchone()
    restored_mode = str(restored[0]).lower() if restored is not None else ""
    if restored_mode != "persist":
        raise sqlite3.OperationalError(
            "credential migration could not restore PERSIST journal"
        )
