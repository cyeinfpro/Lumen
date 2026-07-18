"""Database and queue helpers used while deleting a conversation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import MemoryExtractionRun

from ..db import affected_rows


async def conversation_wallet_exists(db: AsyncSession, user_id: str) -> bool:
    wallet = await billing_core.get_wallet(db, user_id, lock=False, create=False)
    return wallet is not None


async def cancel_conversation_memory_extractions(
    db: AsyncSession,
    *,
    conv_id: str,
    user_id: str,
    canceled_at: datetime,
) -> int:
    result = await db.execute(
        update(MemoryExtractionRun)
        .where(
            MemoryExtractionRun.conversation_id == conv_id,
            MemoryExtractionRun.user_id == user_id,
            MemoryExtractionRun.status.in_(("pending", "running", "retryable")),
        )
        .values(
            status="canceled",
            owner=None,
            lease_expires_at=None,
            canceled_at=canceled_at,
            cancel_reason="conversation_deleted",
            fence=MemoryExtractionRun.fence + 1,
            updated_at=canceled_at,
        )
    )
    return affected_rows(result)


async def release_conversation_generation_queue_state(
    redis: Any,
    task_id: str,
) -> None:
    from ..routes.tasks import _release_generation_queue_state

    await _release_generation_queue_state(redis, task_id)


__all__ = [
    "cancel_conversation_memory_extractions",
    "conversation_wallet_exists",
    "release_conversation_generation_queue_state",
]
