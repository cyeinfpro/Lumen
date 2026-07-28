"""SQLite job repository adapter."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .. import payloads, persistence
from ..config import ImageJobSettings
from ..credential_vault import CredentialVault


class SQLiteJobRepository:
    def __init__(
        self,
        settings: ImageJobSettings,
        credential_vault: CredentialVault | None = None,
    ) -> None:
        self.settings = settings
        self.credential_vault = credential_vault or CredentialVault(
            active_key_id=settings.credential_active_key_id,
            master_secret=settings.credential_master_secret.get_secret_value(),
        )
        self._pragmas = persistence.sqlite_tuning_pragmas(settings.sqlite_journal_mode)

    def _open(self) -> sqlite3.Connection:
        return persistence.open_connection(
            self.settings.db_path,
            self._pragmas,
        )

    def _open_readonly(self) -> sqlite3.Connection:
        database_uri = f"{self.settings.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(
            database_uri,
            timeout=5,
            isolation_level=None,
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        return conn

    async def initialize(self) -> None:
        await asyncio.to_thread(
            persistence.init_storage,
            data_dir=self.settings.data_dir,
            refs_dir=self.settings.refs_dir,
            db_path=self.settings.db_path,
            open_conn=self._open,
            auth_hash=payloads.auth_hash,
            credential_vault=self.credential_vault,
        )

    def _one_sync(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> sqlite3.Row | None:
        return persistence.db_one_sync(self._open, sql, tuple(params))

    def _all_sync(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> list[sqlite3.Row]:
        return persistence.db_all_sync(self._open, sql, tuple(params))

    def _execute_sync(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> int:
        return persistence.db_exec_sync(self._open, sql, tuple(params))

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
        conn = self._open_readonly()
        try:
            row = conn.execute("SELECT 1").fetchone()
            return row is not None and int(row[0]) == 1
        except Exception:
            return False
        finally:
            conn.close()

    async def readiness_probe(self) -> bool:
        return await asyncio.to_thread(self._readiness_probe_sync)


@dataclass(frozen=True)
class SQLiteJobHeartbeat:
    repository: SQLiteJobRepository

    async def touch_running(self, job_id: str) -> None:
        await self.repository.execute(
            "UPDATE jobs SET updated_at = ? WHERE job_id = ? AND status = 'running'",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
