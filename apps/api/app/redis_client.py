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
from .runtime.slot import RuntimeSlot

_REDIS_RETRY_DELAYS = (0.05, 0.2, 0.5)
_REDIS_CONNECT_TIMEOUT_S = 5.0
_REDIS_COMMAND_TIMEOUT_S = 5.0

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
            state = _active_redis_state()
            for loop, client in list(state.by_loop.items()):
                if client is self:
                    state.by_loop.pop(loop, None)
            if state.client is self:
                state.client = None
                state.loop = None


@dataclass
class RedisClientState:
    client: "ReconnectingRedis | None" = None
    loop: asyncio.AbstractEventLoop | None = None
    by_loop: WeakKeyDictionary[asyncio.AbstractEventLoop, "ReconnectingRedis"] = field(
        default_factory=WeakKeyDictionary
    )
    pid: int = field(default_factory=os.getpid)


_RedisState = RedisClientState
_REDIS_STATE_SLOT: RuntimeSlot[RedisClientState] = RuntimeSlot(
    "api-redis-client-state",
    default_factory=RedisClientState,
)


class _RedisStateView:
    @property
    def client(self) -> "ReconnectingRedis | None":
        return _REDIS_STATE_SLOT.current().client

    @client.setter
    def client(self, value: "ReconnectingRedis | None") -> None:
        _REDIS_STATE_SLOT.current().client = value

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return _REDIS_STATE_SLOT.current().loop

    @loop.setter
    def loop(self, value: asyncio.AbstractEventLoop | None) -> None:
        _REDIS_STATE_SLOT.current().loop = value

    @property
    def by_loop(
        self,
    ) -> WeakKeyDictionary[asyncio.AbstractEventLoop, "ReconnectingRedis"]:
        return _REDIS_STATE_SLOT.current().by_loop

    @property
    def pid(self) -> int:
        return _REDIS_STATE_SLOT.current().pid

    @pid.setter
    def pid(self, value: int) -> None:
        _REDIS_STATE_SLOT.current().pid = value


_redis_state: RedisClientState | _RedisStateView = _RedisStateView()


def _active_redis_state(state: RedisClientState | None = None) -> RedisClientState:
    if state is not None:
        return state
    if isinstance(_redis_state, RedisClientState):
        return _redis_state
    return _REDIS_STATE_SLOT.current()


def configure_redis_state(
    state: RedisClientState | None = None,
) -> RedisClientState:
    active = state or RedisClientState()
    _REDIS_STATE_SLOT.install_default(active)
    return active


def reset_redis_state(state: RedisClientState | None = None) -> None:
    active = state or _REDIS_STATE_SLOT.current_or_none()
    if active is None:
        return
    active.by_loop.clear()
    active.client = None
    active.loop = None
    _REDIS_STATE_SLOT.clear_default(active)


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
        socket_connect_timeout=_REDIS_CONNECT_TIMEOUT_S,
        socket_timeout=_REDIS_COMMAND_TIMEOUT_S,
    )


def _reset_after_fork(state: RedisClientState) -> None:
    pid = os.getpid()
    if state.pid == pid:
        return
    state.by_loop.clear()
    state.client = None
    state.loop = None
    state.pid = pid


def get_redis_for_state(state: RedisClientState | None = None) -> ReconnectingRedis:
    active = _active_redis_state(state)
    _reset_after_fork(active)
    loop = _current_loop()
    if loop is None:
        if active.client is None:
            active.client = _new_redis()
            active.loop = None
        return active.client

    client = active.by_loop.get(loop)
    if client is None:
        if (
            active.client is not None
            and active.loop is not None
            and active.loop is not loop
        ):
            active.client = None
            active.loop = None
        client = _new_redis()
        active.by_loop[loop] = client
    active.client = client
    active.loop = loop
    return client


def get_redis() -> ReconnectingRedis:
    return get_redis_for_state()


async def close_redis(state: RedisClientState | None = None) -> None:
    active = _active_redis_state(state)
    _reset_after_fork(active)
    loop = _current_loop()
    if loop is not None:
        client = active.by_loop.pop(loop, None)
        if client is not None:
            await client.aclose()
            if active.client is client:
                active.client = None
                active.loop = None
            return
    if active.client is not None and active.loop is None:
        client = active.client
        active.client = None
        active.loop = None
        await client.aclose()
