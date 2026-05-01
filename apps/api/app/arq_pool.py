"""Lazy singleton ArqRedis pool for enqueueing worker jobs.

The API must enqueue via arq (not raw XADD) so the Worker's arq functions
(`run_generation` / `run_completion`) actually consume the tasks. See DESIGN §5.x.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

from arq.connections import ArqRedis, RedisSettings, create_pool
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .config import settings


logger = logging.getLogger(__name__)

_pool: ArqRedis | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None
_pool_loop_id: int | None = None
_pool_checked_at: float = 0.0

_ARQ_MAX_CONNECTIONS = 50
_ARQ_HEALTH_CHECK_INTERVAL_SECONDS = 30.0


def _redis_settings() -> RedisSettings:
    # arq parses a redis URL via RedisSettings.from_dsn
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    redis_settings.max_connections = _ARQ_MAX_CONNECTIONS
    redis_settings.retry_on_timeout = True
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
        await pool.ping()
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
    global _pool, _pool_loop, _pool_loop_id, _pool_checked_at
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    loop_marker_mismatch = _pool_loop_id is not None and _pool_loop_id != loop_id
    if _pool is not None and (_pool_loop is not loop or loop_marker_mismatch):
        old = _pool
        _pool = None
        _pool_loop = None
        _pool_loop_id = None
        _pool_checked_at = 0.0
        await _close_pool(old)
    if (
        _pool is not None
        and loop.time() - _pool_checked_at >= _ARQ_HEALTH_CHECK_INTERVAL_SECONDS
    ):
        healthy = await _pool_is_healthy(_pool)
        _pool_checked_at = loop.time()
        if not healthy:
            old = _pool
            _pool = None
            _pool_loop = None
            _pool_loop_id = None
            _pool_checked_at = 0.0
            await _close_pool(old)
    if _pool is None:
        _pool = await create_pool(_redis_settings())
        _pool_loop = loop
        _pool_loop_id = loop_id
        _pool_checked_at = loop.time()
    return _pool


async def close_arq_pool() -> None:
    """Close the pool on shutdown. Safe to call when not initialized."""
    global _pool, _pool_loop, _pool_loop_id, _pool_checked_at
    if _pool is not None:
        await _close_pool(_pool)
        _pool = None
        _pool_loop = None
        _pool_loop_id = None
        _pool_checked_at = 0.0
