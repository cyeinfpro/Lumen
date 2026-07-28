"""TTL cache shared by provider mutations and the admin model catalog route."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.schemas import AdminModelsOut


_CACHE_TTL_S = 60.0


@dataclass
class AdminModelCache:
    cache: tuple[float, AdminModelsOut] | None = None
    lock: asyncio.Lock | None = None

    def _lock(self) -> asyncio.Lock:
        if self.lock is None:
            self.lock = asyncio.Lock()
        return self.lock

    async def get(
        self,
        db: AsyncSession,
        builder: Callable[[AsyncSession], Awaitable[AdminModelsOut]],
    ) -> AdminModelsOut:
        now = time.monotonic()
        cached = self.cache
        if cached is not None and cached[0] > now:
            return cached[1]

        async with self._lock():
            cached = self.cache
            if cached is not None and cached[0] > now:
                return cached[1]
            data = await builder(db)
            self.cache = (now + _CACHE_TTL_S, data)
            return data

    def invalidate(self) -> None:
        self.cache = None


def admin_model_cache_from_request(request: Request) -> AdminModelCache:
    runtime = getattr(request.app.state, "runtime", None)
    getter = getattr(runtime, "admin_models", None)
    if not callable(getter):
        raise RuntimeError("API runtime admin model cache is unavailable")
    cache = getter()
    if not isinstance(cache, AdminModelCache):
        raise RuntimeError("API runtime admin model cache has invalid type")
    return cache


__all__ = ["AdminModelCache", "admin_model_cache_from_request"]
