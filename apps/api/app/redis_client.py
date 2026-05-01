"""异步 Redis 客户端（供 API / SSE / rate-limit 使用）。"""

from __future__ import annotations

import asyncio
import os
from weakref import WeakKeyDictionary

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .config import settings

_redis: "ReconnectingRedis | None" = None
_redis_loop: asyncio.AbstractEventLoop | None = None
_redis_by_loop: WeakKeyDictionary[asyncio.AbstractEventLoop, "ReconnectingRedis"] = (
    WeakKeyDictionary()
)
_redis_pid = os.getpid()
_REDIS_RETRY_DELAYS = (0.05, 0.2, 0.5)


class ReconnectingRedis(redis.Redis):
    async def execute_command(self, *args, **options):  # type: ignore[no-untyped-def]
        attempts = len(_REDIS_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                return await super().execute_command(*args, **options)
            except (RedisConnectionError, RedisTimeoutError):
                if attempt == attempts - 1:
                    raise
                await self.connection_pool.disconnect(inuse_connections=False)
                await asyncio.sleep(_REDIS_RETRY_DELAYS[attempt])
        raise AssertionError("unreachable redis retry state")

    async def aclose(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        global _redis, _redis_loop
        try:
            await super().aclose(*args, **kwargs)
        finally:
            for loop, client in list(_redis_by_loop.items()):
                if client is self:
                    _redis_by_loop.pop(loop, None)
            if _redis is self:
                _redis = None
                _redis_loop = None


def _current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _new_redis() -> ReconnectingRedis:
    return ReconnectingRedis.from_url(
        settings.redis_url,
        decode_responses=True,
        health_check_interval=30,
        retry_on_timeout=True,
        socket_keepalive=True,
    )


def _reset_after_fork() -> None:
    global _redis, _redis_loop, _redis_pid
    pid = os.getpid()
    if _redis_pid == pid:
        return
    _redis_by_loop.clear()
    _redis = None
    _redis_loop = None
    _redis_pid = pid


def get_redis() -> ReconnectingRedis:
    global _redis, _redis_loop
    _reset_after_fork()
    loop = _current_loop()
    if loop is None:
        if _redis is None:
            _redis = _new_redis()
            _redis_loop = None
        return _redis

    client = _redis_by_loop.get(loop)
    if client is None:
        # New event loop: drop the global pointer to a client that belonged to a
        # different (potentially closed) loop so it can be GC'd instead of
        # piling up connections forever. WeakKeyDictionary already handles the
        # per-loop cache; this just stops `_redis` from pinning a stale client.
        if _redis is not None and _redis_loop is not None and _redis_loop is not loop:
            _redis = None
            _redis_loop = None
        client = _new_redis()
        _redis_by_loop[loop] = client
    _redis = client
    _redis_loop = loop
    return client


async def close_redis() -> None:
    global _redis, _redis_loop
    _reset_after_fork()
    loop = _current_loop()
    if loop is not None:
        client = _redis_by_loop.pop(loop, None)
        if client is not None:
            await client.aclose()
            if _redis is client:
                _redis = None
                _redis_loop = None
            return
    if _redis is not None and _redis_loop is None:
        client = _redis
        _redis = None
        _redis_loop = None
        await client.aclose()
