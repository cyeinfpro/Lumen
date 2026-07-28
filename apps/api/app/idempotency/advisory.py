"""PostgreSQL transaction-scoped advisory locks for idempotent submissions."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


def advisory_lock_key(namespace: str, user_id: str, key: str) -> int:
    """Map a namespaced user key to a stable PostgreSQL signed bigint."""

    payload = json.dumps(
        [namespace, user_id, key],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return int.from_bytes(
        hashlib.sha256(payload).digest()[:8],
        "big",
        signed=True,
    )


async def lock_user_key(
    db: AsyncSession,
    namespace: str,
    user_id: str,
    key: str,
) -> None:
    """Serialize a user-scoped key on PostgreSQL; other dialects are no-op."""

    connection = await db.connection()
    if connection.dialect.name != "postgresql":
        return
    lock_id = advisory_lock_key(namespace, user_id, key)
    await db.execute(select(func.pg_advisory_xact_lock(lock_id)))
