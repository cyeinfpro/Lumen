"""Lazy singleton ArqRedis pool for enqueueing worker jobs.

The API must enqueue via arq (not raw XADD) so the Worker's arq functions
(`run_generation` / `run_completion`) actually consume the tasks. See DESIGN §5.x.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import weakref
from dataclasses import dataclass, field

from arq.connections import ArqRedis, RedisSettings, create_pool
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .config import settings


logger = logging.getLogger(__name__)


@dataclass
class _ArqPoolState:
    pool: ArqRedis | None = None
    loop: asyncio.AbstractEventLoop | None = None
    loop_id: int | None = None
    checked_at: float = 0.0
    # Weak values: each lock is only referenced while its loop's callers are
    # running, so a closed loop's lock is reclaimed (and its entry dropped)
    # instead of accumulating forever as loops are created and destroyed.
    locks: weakref.WeakValueDictionary[int, asyncio.Lock] = field(
        default_factory=weakref.WeakValueDictionary
    )


_pool_state = _ArqPoolState()
# Compatibility injection points retained for older loop-recreation tests.
_pool: ArqRedis | None = None
_pool_loop_id: int | None = None


def _lock_for_current_loop() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    lock = _pool_state.locks.get(loop_id)
    if lock is None:
        lock = asyncio.Lock()
        _pool_state.locks[loop_id] = lock
    return lock


_ARQ_MAX_CONNECTIONS = 50
_ARQ_HEALTH_CHECK_INTERVAL_SECONDS = 30.0
# 半开连接下 ping 可能永不返回;健康检查在全局锁内执行,无界等待会把
# 进程内所有入队永久卡死,故 ping 必须带上限超时。
_ARQ_PING_TIMEOUT_SECONDS = 2.0
# create_pool 内部同样执行无界 ping(connect 阶段才有 conn_timeout 限制),
# 且重试预算为 5 × (1s connect + 1s retry_delay)≈10s;上限取 15s 既不打断
# arq 的正常重试,又保证半开场景下全局锁不会永久卡死。
_ARQ_POOL_CREATE_TIMEOUT_SECONDS = 15.0


def _redis_settings() -> RedisSettings:
    # arq parses a redis URL via RedisSettings.from_dsn
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    redis_settings.max_connections = _ARQ_MAX_CONNECTIONS
    # 不能开 retry_on_timeout:入队走 XADD(非幂等),响应超时丢失后 redis-py
    # 会盲目重发,同一任务被入队两次、执行两次;超时直接抛错由调用方兜底。
    return redis_settings


async def _close_pool(pool: ArqRedis) -> None:
    try:
        close = getattr(pool, "aclose", None) or pool.close
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001
        logger.warning("arq redis pool close failed err=%r", exc)


async def _pool_is_healthy(pool: ArqRedis) -> bool:
    try:
        await asyncio.wait_for(pool.ping(), timeout=_ARQ_PING_TIMEOUT_SECONDS)
    except (
        RedisConnectionError,
        RedisTimeoutError,
        RedisError,
        OSError,
        asyncio.TimeoutError,
    ) as exc:
        logger.warning("arq redis pool health check failed; reconnecting err=%r", exc)
        return False
    return True


async def get_arq_pool() -> ArqRedis:
    """Return a process-wide ArqRedis pool (initialized on first call)."""
    async with _lock_for_current_loop():
        if _pool_state.pool is None and _pool is not None:
            _pool_state.pool = _pool
            _pool_state.loop_id = _pool_loop_id
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        loop_marker_mismatch = (
            _pool_state.loop_id is not None and _pool_state.loop_id != loop_id
        )
        if _pool_state.pool is not None and (
            _pool_state.loop is not loop or loop_marker_mismatch
        ):
            old = _pool_state.pool
            _pool_state.pool = None
            _pool_state.loop = None
            _pool_state.loop_id = None
            _pool_state.checked_at = 0.0
            await _close_pool(old)
        if (
            _pool_state.pool is not None
            and loop.time() - _pool_state.checked_at
            >= _ARQ_HEALTH_CHECK_INTERVAL_SECONDS
        ):
            healthy = await _pool_is_healthy(_pool_state.pool)
            _pool_state.checked_at = loop.time()
            if not healthy:
                old = _pool_state.pool
                _pool_state.pool = None
                _pool_state.loop = None
                _pool_state.loop_id = None
                _pool_state.checked_at = 0.0
                await _close_pool(old)
        if _pool_state.pool is None:
            _pool_state.pool = await asyncio.wait_for(
                create_pool(_redis_settings()),
                timeout=_ARQ_POOL_CREATE_TIMEOUT_SECONDS,
            )
            _pool_state.loop = loop
            _pool_state.loop_id = loop_id
            _pool_state.checked_at = loop.time()
        return _pool_state.pool


async def close_arq_pool() -> None:
    """Close the pool on shutdown. Safe to call when not initialized."""
    async with _lock_for_current_loop():
        if _pool_state.pool is not None:
            await _close_pool(_pool_state.pool)
            _pool_state.pool = None
            _pool_state.loop = None
            _pool_state.loop_id = None
            _pool_state.checked_at = 0.0
