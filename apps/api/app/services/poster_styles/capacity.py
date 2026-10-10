"""Distributed capacity leases for poster-style tagging."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator
from lumen_core.capacity_leases import maintained_capacity_lease


logger = logging.getLogger(__name__)


_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
end
return 0
"""

_RENEW_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("EXPIRE", KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


class PosterTaggingCapacityUnavailable(RuntimeError):
    """No distributed tagging slot became available before the deadline."""


@dataclass
class RedisCapacityLeaseHandle:
    redis: Any
    key: str
    owner_token: str
    ttl_seconds: int
    _released: bool = False

    async def renew(self) -> bool:
        if self._released:
            return False
        result = await self.redis.eval(
            _RENEW_SCRIPT,
            1,
            self.key,
            self.owner_token,
            str(self.ttl_seconds),
        )
        return bool(result)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self.redis.eval(
            _RELEASE_SCRIPT,
            1,
            self.key,
            self.owner_token,
        )


class RedisCapacityLease:
    """A small fixed-slot lease shared by every API/worker process."""

    def __init__(
        self,
        redis: Any,
        *,
        limit: int,
        ttl_seconds: int = 60,
        wait_timeout_seconds: float = 15.0,
        retry_interval_seconds: float = 0.05,
        key_prefix: str = "lumen:poster-tagging:capacity",
    ) -> None:
        if limit <= 0:
            raise ValueError("poster tagging capacity limit must be positive")
        if ttl_seconds <= 0:
            raise ValueError("poster tagging capacity TTL must be positive")
        self.redis = redis
        self.limit = limit
        self.ttl_seconds = ttl_seconds
        self.wait_timeout_seconds = max(0.0, wait_timeout_seconds)
        self.retry_interval_seconds = max(0.01, retry_interval_seconds)
        self.key_prefix = key_prefix.rstrip(":")

    async def try_acquire(
        self,
        *,
        owner_token: str | None = None,
    ) -> RedisCapacityLeaseHandle | None:
        token = owner_token or secrets.token_urlsafe(24)
        for slot in range(self.limit):
            key = f"{self.key_prefix}:{slot}"
            acquired = await self.redis.set(
                key,
                token,
                nx=True,
                ex=self.ttl_seconds,
            )
            if acquired:
                return RedisCapacityLeaseHandle(
                    redis=self.redis,
                    key=key,
                    owner_token=token,
                    ttl_seconds=self.ttl_seconds,
                )
        return None

    async def acquire(self) -> RedisCapacityLeaseHandle:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.wait_timeout_seconds
        while True:
            lease = await self.try_acquire()
            if lease is not None:
                return lease
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise PosterTaggingCapacityUnavailable(
                    "poster tagging distributed capacity unavailable"
                )
            await asyncio.sleep(min(self.retry_interval_seconds, remaining))

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        lease = await self.acquire()
        holder_task = asyncio.current_task()
        # Shared guard uses bounded renewal RPCs and a confirmed-TTL deadline.
        # Exceptions/false ownership fail closed, before the safety margin ends.
        async with maintained_capacity_lease(
            lease, ttl_seconds=self.ttl_seconds
        ) as guard:
            await (
                guard.assert_owned()
            )  # Confirm even a delayed acquire acknowledgement.

            async def interrupt_on_loss():
                await guard.wait_lost()
                if holder_task is not None:
                    holder_task.cancel()

            watcher = asyncio.create_task(
                interrupt_on_loss(), name="media-capacity-loss"
            )
            try:
                yield
                await guard.assert_owned()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)


__all__ = [
    "PosterTaggingCapacityUnavailable",
    "RedisCapacityLease",
    "RedisCapacityLeaseHandle",
]
