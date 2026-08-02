"""异步 Redis 客户端（供 API / SSE / rate-limit 使用）。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .config import settings

_REDIS_RETRY_DELAYS = (0.05, 0.2, 0.5)

# 响应丢失后重发仍安全的命令:只读命令,以及 DELETE/UNLINK 这类第二次执行
# 不改变任何状态的严格幂等命令。写命令(INCR/SET NX/PUBLISH/EVAL 等)若在
# 响应丢失时盲目重试,服务端已执行的命令会被再执行一次,造成计数翻倍、锁
# 误判、消息重复。
_REDIS_RETRY_SAFE_COMMANDS = frozenset(
    {
        "GET", "MGET", "GETRANGE", "STRLEN",
        "EXISTS", "TTL", "PTTL", "TYPE", "KEYS", "DBSIZE",
        "HGET", "HMGET", "HGETALL", "HLEN", "HSTRLEN", "HEXISTS",
        "SCARD", "SMEMBERS", "SISMEMBER", "SINTER", "SUNION", "SDIFF",
        "LRANGE", "LLEN", "LINDEX",
        "ZRANGE", "ZCARD", "ZSCORE", "ZCOUNT",
        "SCAN", "HSCAN", "SSCAN", "ZSCAN",
        "DELETE", "UNLINK", "PING", "ECHO", "TIME", "INFO", "DUMP", "OBJECT",
    }
)


class ReconnectingRedis(redis.Redis):
    async def execute_command(self, *args, **options):  # type: ignore[no-untyped-def]
        command = args[0]
        if isinstance(command, bytes):
            command = command.decode("ascii")
        retry_safe = command in _REDIS_RETRY_SAFE_COMMANDS
        attempts = len(_REDIS_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                return await super().execute_command(*args, **options)
            except (RedisConnectionError, RedisTimeoutError):
                if attempt == attempts - 1 or not retry_safe:
                    # 错误可能发生在响应阶段,命令已被服务端执行;非幂等命令
                    # 不能重发,直接抛错,由调用方自行兜底。
                    raise
                await self.connection_pool.disconnect(inuse_connections=False)
                await asyncio.sleep(_REDIS_RETRY_DELAYS[attempt])
        raise AssertionError("unreachable redis retry state")

    async def aclose(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        try:
            await super().aclose(*args, **kwargs)
        finally:
            for loop, client in list(_redis_state.by_loop.items()):
                if client is self:
                    _redis_state.by_loop.pop(loop, None)
            if _redis_state.client is self:
                _redis_state.client = None
                _redis_state.loop = None


@dataclass
class _RedisState:
    client: "ReconnectingRedis | None" = None
    loop: asyncio.AbstractEventLoop | None = None
    by_loop: WeakKeyDictionary[asyncio.AbstractEventLoop, "ReconnectingRedis"] = field(
        default_factory=WeakKeyDictionary
    )
    pid: int = field(default_factory=os.getpid)


_redis_state = _RedisState()


def _current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _new_redis() -> ReconnectingRedis:
    # 不能开 retry_on_timeout:它会让 redis-py 对任意命令(含 INCR/SET NX)
    # 在响应超时后盲目重发一次,与 execute_command 里修复的重复执行问题同类。
    # 重连重试统一由 execute_command 的幂等命令白名单控制。
    return ReconnectingRedis.from_url(
        settings.redis_url,
        decode_responses=True,
        health_check_interval=30,
        socket_keepalive=True,
    )


def _reset_after_fork() -> None:
    pid = os.getpid()
    if _redis_state.pid == pid:
        return
    _redis_state.by_loop.clear()
    _redis_state.client = None
    _redis_state.loop = None
    _redis_state.pid = pid


def get_redis() -> ReconnectingRedis:
    _reset_after_fork()
    loop = _current_loop()
    if loop is None:
        if _redis_state.client is None:
            _redis_state.client = _new_redis()
            _redis_state.loop = None
        return _redis_state.client

    client = _redis_state.by_loop.get(loop)
    if client is None:
        # New event loop: drop the global pointer to a client that belonged to a
        # different (potentially closed) loop so it can be GC'd instead of
        # piling up connections forever. WeakKeyDictionary already handles the
        # per-loop cache; this just stops `_redis` from pinning a stale client.
        if (
            _redis_state.client is not None
            and _redis_state.loop is not None
            and _redis_state.loop is not loop
        ):
            _redis_state.client = None
            _redis_state.loop = None
        client = _new_redis()
        _redis_state.by_loop[loop] = client
    _redis_state.client = client
    _redis_state.loop = loop
    return client


async def close_redis() -> None:
    _reset_after_fork()
    loop = _current_loop()
    if loop is not None:
        client = _redis_state.by_loop.pop(loop, None)
        if client is not None:
            await client.aclose()
            if _redis_state.client is client:
                _redis_state.client = None
                _redis_state.loop = None
            return
    if _redis_state.client is not None and _redis_state.loop is None:
        client = _redis_state.client
        _redis_state.client = None
        _redis_state.loop = None
        await client.aclose()
