"""TTL cache shared by provider mutations and the admin model catalog route."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.schemas import AdminModelsOut


_CACHE_TTL_S = 60.0
_CACHE_LOCK = asyncio.Lock()
_CACHE: tuple[float, AdminModelsOut] | None = None


async def get_cached_admin_models(
    db: AsyncSession,
    builder: Callable[[AsyncSession], Awaitable[AdminModelsOut]],
) -> AdminModelsOut:
    global _CACHE
    now = time.monotonic()
    cached = _CACHE
    if cached is not None and cached[0] > now:
        return cached[1]

    async with _CACHE_LOCK:
        cached = _CACHE
        if cached is not None and cached[0] > now:
            return cached[1]
        data = await builder(db)
        _CACHE = (now + _CACHE_TTL_S, data)
        return data


def invalidate_admin_models_cache() -> None:
    global _CACHE
    _CACHE = None
