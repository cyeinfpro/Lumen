"""SQLite schema and path encoding for durable retention scans."""

from __future__ import annotations

import os
import sqlite3


def ensure_retention_scan_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS retention_scan_roots (
            scope TEXT PRIMARY KEY,
            root_path TEXT NOT NULL,
            root_device INTEGER NOT NULL,
            root_inode INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            work_sequence INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS retention_scan_directories (
            scope TEXT NOT NULL,
            generation INTEGER NOT NULL,
            relative_path BLOB NOT NULL,
            depth INTEGER NOT NULL,
            device INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            scan_offset INTEGER NOT NULL DEFAULT 0,
            attempt_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (scope, generation, relative_path)
        );
        CREATE INDEX IF NOT EXISTS retention_scan_directories_work_idx
            ON retention_scan_directories(scope, generation, depth DESC);
        CREATE TABLE IF NOT EXISTS retention_scan_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            generation INTEGER NOT NULL,
            parent_path BLOB NOT NULL,
            parent_device INTEGER NOT NULL,
            parent_inode INTEGER NOT NULL,
            name BLOB NOT NULL,
            depth INTEGER NOT NULL,
            attempt_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(scope, generation, parent_path, name)
        );
        CREATE INDEX IF NOT EXISTS retention_scan_entries_work_idx
            ON retention_scan_entries(
                scope, generation, attempt_order, entry_id
            );
        CREATE TABLE IF NOT EXISTS retention_scan_candidates (
            candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            root_device INTEGER NOT NULL,
            root_inode INTEGER NOT NULL,
            relative_path BLOB NOT NULL,
            device INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            mode INTEGER NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            ctime_ns INTEGER NOT NULL,
            eligible_mtime_ns INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            attempt_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(
                scope, root_device, root_inode,
                relative_path, device, inode
            )
        );
        CREATE INDEX IF NOT EXISTS retention_scan_candidates_work_idx
            ON retention_scan_candidates(
                scope, root_device, root_inode,
                attempt_order, candidate_id
            );
        CREATE TABLE IF NOT EXISTS retention_row_cursors (
            scope TEXT PRIMARY KEY,
            generation INTEGER NOT NULL DEFAULT 0,
            cursor_created_jd REAL,
            cursor_rowid INTEGER,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS retention_row_cursors_updated_idx
            ON retention_row_cursors(updated_at, scope);
        """
    )
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(retention_scan_candidates)"
        )
    }
    if "eligible_mtime_ns" not in columns:
        conn.execute(
            """
            ALTER TABLE retention_scan_candidates
            ADD COLUMN eligible_mtime_ns INTEGER NOT NULL DEFAULT 0
            """
        )
    directory_columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(retention_scan_directories)"
        )
    }
    if "attempt_order" not in directory_columns:
        conn.execute(
            """
            ALTER TABLE retention_scan_directories
            ADD COLUMN attempt_order INTEGER NOT NULL DEFAULT 0
            """
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS retention_scan_directories_fair_idx
        ON retention_scan_directories(
            scope, generation, depth DESC, attempt_order, relative_path
        )
        """
    )
    conn.execute(
        """
        UPDATE retention_scan_candidates
        SET eligible_mtime_ns = mtime_ns
        WHERE eligible_mtime_ns = 0
        """
    )


def encode_scan_parts(parts: tuple[str, ...]) -> bytes:
    encoded = tuple(os.fsencode(part) for part in parts)
    if any(not part or b"\x00" in part for part in encoded):
        raise ValueError("invalid retention scan path")
    return b"\x00".join(encoded)


def decode_scan_parts(raw: bytes) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(os.fsdecode(part) for part in raw.split(b"\x00"))


def begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")
