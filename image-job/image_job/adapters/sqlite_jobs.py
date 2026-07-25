"""SQLite job repository adapter."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from typing import Any

import job_persistence
import payload_helpers

from ..config import ImageJobSettings


class SQLiteJobRepository:
    def __init__(self, settings: ImageJobSettings) -> None:
        self.settings = settings
        self._pragmas = job_persistence.sqlite_tuning_pragmas(
            settings.sqlite_journal_mode
        )

    def _open(self) -> sqlite3.Connection:
        return job_persistence.open_connection(
            self.settings.db_path,
            self._pragmas,
        )

    async def initialize(self) -> None:
        await asyncio.to_thread(
            job_persistence.init_storage,
            data_dir=self.settings.data_dir,
            refs_dir=self.settings.refs_dir,
            db_path=self.settings.db_path,
            open_conn=self._open,
            auth_hash=payload_helpers.auth_hash,
        )

    def _one_sync(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> sqlite3.Row | None:
        return job_persistence.db_one_sync(self._open, sql, tuple(params))

    def _all_sync(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> list[sqlite3.Row]:
        return job_persistence.db_all_sync(self._open, sql, tuple(params))

    def _execute_sync(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> int:
        return job_persistence.db_exec_sync(self._open, sql, tuple(params))

    async def one(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._one_sync, sql, params)

    async def all(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._all_sync, sql, params)

    async def execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> int:
        return await asyncio.to_thread(self._execute_sync, sql, params)

    def _readiness_probe_sync(self) -> bool:
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS image_job_readiness_probe (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    checked_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO image_job_readiness_probe(singleton, checked_at)
                VALUES (1, CURRENT_TIMESTAMP)
                ON CONFLICT(singleton)
                DO UPDATE SET checked_at = excluded.checked_at
                """
            )
            row = conn.execute(
                "SELECT checked_at FROM image_job_readiness_probe WHERE singleton = 1"
            ).fetchone()
            conn.execute("COMMIT")
            return row is not None
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            return False
        finally:
            conn.close()

    async def readiness_probe(self) -> bool:
        return await asyncio.to_thread(self._readiness_probe_sync)
