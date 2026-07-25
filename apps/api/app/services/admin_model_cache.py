"""TTL cache shared by provider mutations and the admin model catalog route."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.schemas import AdminModelsOut


_CACHE_TTL_S = 60.0


@dataclass
class _AdminModelCacheState:
    cache: tuple[float, AdminModelsOut] | None = None
    lock: asyncio.Lock | None = None


_admin_model_cache_state = _AdminModelCacheState()


def _cache_lock() -> asyncio.Lock:
    if _admin_model_cache_state.lock is None:
        _admin_model_cache_state.lock = asyncio.Lock()
    return _admin_model_cache_state.lock


async def get_cached_admin_models(
    db: AsyncSession,
    builder: Callable[[AsyncSession], Awaitable[AdminModelsOut]],
) -> AdminModelsOut:
    now = time.monotonic()
    cached = _admin_model_cache_state.cache
    if cached is not None and cached[0] > now:
        return cached[1]

    async with _cache_lock():
        cached = _admin_model_cache_state.cache
        if cached is not None and cached[0] > now:
            return cached[1]
        data = await builder(db)
        _admin_model_cache_state.cache = (now + _CACHE_TTL_S, data)
        return data


def invalidate_admin_models_cache() -> None:
    _admin_model_cache_state.cache = None
