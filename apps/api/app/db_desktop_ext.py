"""SQLite connection hooks used by the desktop runtime."""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine


logger = logging.getLogger(__name__)


def configure_sqlite_engine(engine: AsyncEngine) -> None:
    """Install SQLite pragmas and load sqlite-vec when it is available."""

    @event.listens_for(engine.sync_engine, "connect")
    def _configure(dbapi_conn, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA temp_store=MEMORY")
        finally:
            cursor.close()

        try:
            import sqlite_vec

            dbapi_conn.enable_load_extension(True)
            try:
                sqlite_vec.load(dbapi_conn)
            finally:
                dbapi_conn.enable_load_extension(False)
        except Exception as exc:  # noqa: BLE001
            logger.info("sqlite-vec unavailable for this connection: %s", exc)
