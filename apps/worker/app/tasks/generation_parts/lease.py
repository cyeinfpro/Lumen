from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from ._facade import GenerationFacade

_g = GenerationFacade()
bind_generation_facade = _g.bind

LEASE_TTL_S = 60
LEASE_RENEW_S = 30

RELEASE_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

RENEW_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""

ACQUIRE_LUA = """
local v = redis.call('INCR', KEYS[1])
if v <= tonumber(ARGV[1]) then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
  return 1
end
redis.call('DECR', KEYS[1])
return 0
"""

RELEASE_LUA = """
local v = tonumber(redis.call('GET', KEYS[1]) or '0')
if v <= 1 then
  redis.call('DEL', KEYS[1])
  return 0
end
return redis.call('DECR', KEYS[1])
"""

IMAGE_SEMAPHORE_KEY_TTL_S = max(LEASE_TTL_S * 4, 120)


async def is_cancelled(redis: Any, task_id: str) -> bool:
    try:
        value = await redis.get(f"task:{task_id}:cancel")
    except Exception as exc:  # noqa: BLE001
        _g.logger.warning(
            "generation cancel check failed closed task=%s err=%s", task_id, exc
        )
        return True
    return bool(value)


async def acquire_lease(redis: Any, task_id: str, worker_token: str) -> None:
    ok = await redis.set(
        f"task:{task_id}:lease",
        worker_token,
        ex=_g._LEASE_TTL_S,
        nx=True,
    )
    if not ok:
        raise _g._LeaseLost(f"lease already held task={task_id}")


async def release_lease(redis: Any, task_id: str, worker_token: str) -> None:
    try:
        eval_fn = getattr(redis, "eval", None)
        if callable(eval_fn):
            await eval_fn(
                _g._RELEASE_LEASE_LUA,
                1,
                f"task:{task_id}:lease",
                worker_token,
            )
            return
        _g.logger.warning(
            "generation lease release skipped without atomic CAS task=%s", task_id
        )
    except Exception:  # noqa: BLE001
        _g.logger.debug(
            "generation lease release failed task=%s", task_id, exc_info=True
        )


async def lease_renewer(
    redis: Any,
    task_id: str,
    worker_token: str,
    lease_lost: asyncio.Event | None = None,
    *,
    extra_lease_keys: list[str] | None = None,
    image_provider_name: str | None = None,
) -> None:
    """Renew the worker lease and image queue ownership in lock-step."""
    consecutive_failures = 0
    try:
        while True:
            await asyncio.sleep(_g._LEASE_RENEW_S)
            try:
                renewed = await redis.eval(
                    _g._RENEW_LEASE_LUA,
                    1,
                    f"task:{task_id}:lease",
                    worker_token,
                    _g._LEASE_TTL_S,
                )
                if int(renewed or 0) == 0:
                    if lease_lost is not None:
                        lease_lost.set()
                    _g.logger.warning(
                        "generation lease ownership lost task=%s worker=%s",
                        task_id,
                        worker_token,
                    )
                    return
                for key in extra_lease_keys or []:
                    await redis.expire(key, _g._LEASE_TTL_S)
                with suppress(Exception):
                    await redis.expire(
                        _g._image_inflight_key(task_id),
                        _g._LEASE_TTL_S * 4,
                    )
                if image_provider_name:
                    new_expiry = time.time() + _g._LEASE_TTL_S
                    if _g._is_dual_race_sentinel(image_provider_name):
                        await redis.zadd(
                            _g._IMAGE_QUEUE_ACTIVE_KEY,
                            {image_provider_name: new_expiry},
                        )
                    else:
                        await redis.zadd(
                            _g._IMAGE_QUEUE_ACTIVE_KEY,
                            {task_id: new_expiry},
                        )
                        await redis.zadd(
                            _g._image_provider_active_key(image_provider_name),
                            {task_id: new_expiry},
                        )
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                _g.logger.warning(
                    "lease renew failed task=%s err=%s streak=%d",
                    task_id,
                    exc,
                    consecutive_failures,
                )
                if consecutive_failures >= 3:
                    if lease_lost is not None:
                        lease_lost.set()
                    _g.logger.error(
                        "lease renewer giving up task=%s failures=%d",
                        task_id,
                        consecutive_failures,
                    )
                    return
    except asyncio.CancelledError:
        raise


async def cancel_renewer_task(renewer: asyncio.Task[None] | None) -> None:
    if renewer is None:
        return
    renewer.cancel()
    try:
        await renewer
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        _g.logger.debug("generation lease renewer cancellation failed", exc_info=True)


class RedisSemaphore:
    """Lua-atomic concurrency semaphore with bounded waiting."""

    def __init__(
        self,
        redis: Any,
        key: str,
        capacity: int,
        wait_s: float = 60.0,
        on_wait_start: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.redis = redis
        self.key = key
        self.capacity = capacity
        self.wait_s = wait_s
        self.on_wait_start = on_wait_start
        self._acquired = False

    async def __aenter__(self) -> RedisSemaphore:
        loop_until = asyncio.get_event_loop().time() + self.wait_s
        notified = False
        while True:
            try:
                got = await self.redis.eval(
                    _g._ACQUIRE_LUA,
                    1,
                    self.key,
                    self.capacity,
                    _g._IMAGE_SEMAPHORE_KEY_TTL_S,
                )
            except Exception as exc:  # noqa: BLE001
                raise _g.UpstreamError(
                    "local concurrency semaphore unavailable",
                    error_code=_g.EC.LOCAL_QUEUE_FULL.value,
                    status_code=None,
                ) from exc
            if int(got or 0) == 1:
                self._acquired = True
                return self
            if not notified and self.on_wait_start is not None:
                notified = True
                try:
                    await self.on_wait_start()
                except Exception:  # noqa: BLE001
                    _g.logger.debug("sem on_wait_start callback failed", exc_info=True)
            if asyncio.get_event_loop().time() >= loop_until:
                raise _g.UpstreamError(
                    "local concurrency wait exhausted",
                    error_code=_g.EC.LOCAL_QUEUE_FULL.value,
                    status_code=None,
                )
            await asyncio.sleep(0.5)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self._acquired:
            return
        try:
            await self.redis.eval(_g._RELEASE_LUA, 1, self.key)
        except Exception as release_exc:  # noqa: BLE001
            _g.logger.warning(
                "redis sem release failed key=%s err=%s",
                self.key,
                release_exc,
            )
