"""Lifecycle-owned runtime resources for poster tagging."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .capacity import RedisCapacityLease


_ClientFactory = Callable[..., httpx.AsyncClient]


class PosterTaggingHttpClientPool:
    """Reuse one client per effective proxy instead of one per provider attempt."""

    def __init__(
        self,
        *,
        client_factory: _ClientFactory = httpx.AsyncClient,
    ) -> None:
        self._client_factory = client_factory
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    async def client_for(self, proxy_url: str | None) -> httpx.AsyncClient:
        key = proxy_url or ""
        client = self._clients.get(key)
        if client is not None:
            return client
        async with self._lock:
            client = self._clients.get(key)
            if client is None:
                kwargs: dict[str, Any] = {
                    "timeout": httpx.Timeout(
                        connect=10.0,
                        read=25.0,
                        write=25.0,
                        pool=10.0,
                    ),
                }
                if proxy_url:
                    kwargs["proxy"] = proxy_url
                client = self._client_factory(**kwargs)
                self._clients[key] = client
        return client

    async def aclose(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.aclose()


@dataclass(slots=True)
class PosterTaggingRuntime:
    http_clients: PosterTaggingHttpClientPool
    capacity: RedisCapacityLease

    async def aclose(self) -> None:
        await self.http_clients.aclose()


def build_poster_tagging_runtime(
    redis: Any,
    *,
    concurrency: int,
) -> PosterTaggingRuntime:
    return PosterTaggingRuntime(
        http_clients=PosterTaggingHttpClientPool(),
        capacity=RedisCapacityLease(redis, limit=concurrency),
    )


__all__ = [
    "PosterTaggingHttpClientPool",
    "PosterTaggingRuntime",
    "build_poster_tagging_runtime",
]
