"""Durable fair cursor for bounded reference-row cleanup."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .retention_scan_schema import (
    begin_immediate,
    ensure_retention_scan_schema,
    rollback,
)


_MAX_ROW_CURSOR_SCOPES = 16
_STALE_ROW_CURSOR_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class ReferenceRowCandidate:
    rowid: int
    created_at: Any
    created_jd: float
    auth_hash: str
    sha256: str
    token: str
    ext: str
    generation: int


@dataclass(frozen=True)
class DurableReferenceRowCursor:
    scope: str
    now: Callable[[], float] = time.time

    def _prune(self, conn: sqlite3.Connection, current_time: float) -> None:
        rows = conn.execute(
            """
            SELECT scope, updated_at
            FROM retention_row_cursors
            WHERE scope <> ?
            ORDER BY updated_at DESC, scope ASC
            """,
            (self.scope,),
        ).fetchall()
        stale_before = current_time - _STALE_ROW_CURSOR_SECONDS
        stale = {
            str(row[0])
            for row in rows
            if float(row[1]) < stale_before
        }
        keep_other = max(0, _MAX_ROW_CURSOR_SCOPES - 1)
        stale.update(str(row[0]) for row in rows[keep_other:])
        if not stale:
            return
        placeholders = ", ".join("?" for _scope in stale)
        conn.execute(
            f"""
            DELETE FROM retention_row_cursors
            WHERE scope IN ({placeholders})
            """,  # nosec B608
            tuple(sorted(stale)),
        )

    def _state(
        self,
        conn: sqlite3.Connection,
        current_time: float,
    ) -> tuple[int, float | None, int | None]:
        row = conn.execute(
            """
            SELECT generation, cursor_created_jd, cursor_rowid
            FROM retention_row_cursors
            WHERE scope = ?
            """,
            (self.scope,),
        ).fetchone()
        if row is not None:
            return (
                int(row[0]),
                None if row[1] is None else float(row[1]),
                None if row[2] is None else int(row[2]),
            )
        conn.execute(
            """
            INSERT INTO retention_row_cursors (
                scope, generation, cursor_created_jd,
                cursor_rowid, updated_at
            ) VALUES (?, 0, NULL, NULL, ?)
            """,
            (self.scope, current_time),
        )
        return 0, None, None

    @staticmethod
    def _select_after(
        conn: sqlite3.Connection,
        cutoff_iso: str,
        cursor_jd: float | None,
        cursor_rowid: int | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT rowid, created_at, julianday(created_at) AS created_jd,
                   auth_hash, sha256, token, ext
            FROM refs
            WHERE julianday(created_at) < julianday(?)
              AND (
                    ? IS NULL
                    OR julianday(created_at) > ?
                    OR (
                        julianday(created_at) = ?
                        AND rowid > ?
                    )
              )
            ORDER BY julianday(created_at) ASC, rowid ASC
            LIMIT ?
            """,
            (
                cutoff_iso,
                cursor_jd,
                cursor_jd,
                cursor_jd,
                cursor_rowid,
                limit,
            ),
        ).fetchall()

    @staticmethod
    def _select_head(
        conn: sqlite3.Connection,
        cutoff_iso: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT rowid, created_at, julianday(created_at) AS created_jd,
                   auth_hash, sha256, token, ext
            FROM refs
            WHERE julianday(created_at) < julianday(?)
            ORDER BY julianday(created_at) ASC, rowid ASC
            LIMIT ?
            """,
            (cutoff_iso, limit),
        ).fetchall()

    def _store_claim(
        self,
        conn: sqlite3.Connection,
        generation: int,
        last_created_jd: float,
        last_rowid: int,
        current_time: float,
    ) -> None:
        conn.execute(
            """
            UPDATE retention_row_cursors
            SET generation = ?, cursor_created_jd = ?,
                cursor_rowid = ?, updated_at = ?
            WHERE scope = ?
            """,
            (
                generation,
                last_created_jd,
                last_rowid,
                current_time,
                self.scope,
            ),
        )

    def claim(
        self,
        conn: sqlite3.Connection,
        cutoff_iso: str,
        limit: int,
    ) -> tuple[ReferenceRowCandidate, ...]:
        if limit <= 0:
            raise ValueError("reference row claim limit must be positive")
        ensure_retention_scan_schema(conn)
        current_time = self.now()
        begin_immediate(conn)
        try:
            self._prune(conn, current_time)
            generation, cursor_jd, cursor_rowid = self._state(
                conn,
                current_time,
            )
            rows = self._select_after(
                conn,
                cutoff_iso,
                cursor_jd,
                cursor_rowid,
                limit,
            )
            if not rows and cursor_jd is not None:
                rows = self._select_head(conn, cutoff_iso, limit)
                if rows:
                    generation += 1
            if rows:
                last = rows[-1]
                self._store_claim(
                    conn,
                    generation,
                    float(last[2]),
                    int(last[0]),
                    current_time,
                )
            else:
                conn.execute(
                    """
                    UPDATE retention_row_cursors
                    SET updated_at = ?
                    WHERE scope = ?
                    """,
                    (current_time, self.scope),
                )
            conn.execute("COMMIT")
        except BaseException:
            rollback(conn)
            raise
        return tuple(
            ReferenceRowCandidate(
                rowid=int(row[0]),
                created_at=row[1],
                created_jd=float(row[2]),
                auth_hash=str(row[3]),
                sha256=str(row[4]),
                token=str(row[5]),
                ext=str(row[6]),
                generation=generation,
            )
            for row in rows
        )
