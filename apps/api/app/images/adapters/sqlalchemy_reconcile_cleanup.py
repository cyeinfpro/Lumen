"""Database guard for stale reconcile publish cleanup."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen_core.model_entities.media_workflows import Image


@asynccontextmanager
async def reconcile_publish_cleanup_guard(
    session_factory: async_sessionmaker[AsyncSession],
    image_id: str,
    *,
    stale_fence: int,
) -> AsyncIterator[bool]:
    """Block takeover while deciding whether a stale owner may delete."""
    if stale_fence <= 0:
        raise ValueError("stale reconcile fence must be positive")
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    select(Image).where(Image.id == image_id).with_for_update()
                )
            ).scalar_one_or_none()
            yield row is None or (
                row.deleted_at is not None
                or int(row.reconcile_fence or 0) == stale_fence
            )
