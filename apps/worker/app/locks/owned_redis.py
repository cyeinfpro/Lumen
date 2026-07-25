"""Token-owned Redis locks with atomic renewal and release."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from prometheus_client import Counter

from .. import observability

logger = logging.getLogger(__name__)

RELEASE_OWNED_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

RENEW_OWNED_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""

owned_redis_lock_total = observability._metric(  # noqa: SLF001
    Counter,
    "lumen_worker_owned_redis_lock_total",
    "Owned Redis lock operations by lock name and outcome.",
    labelnames=("lock", "outcome"),
)


def _lock_name(key: str) -> str:
    return key.removeprefix("lock:")


async def renew_owned_lock(
    redis: Any,
    *,
    key: str,
    token: str,
    ttl_s: int,
    log: logging.Logger = logger,
) -> bool | None:
    """Renew only while ``token`` still owns ``key``.

    ``None`` means Redis could not establish ownership. Callers must not
    interpret that state as successful renewal or permission to reclaim work.
    """
    try:
        renewed = await redis.eval(
            RENEW_OWNED_LOCK_LUA,
            1,
            key,
            token,
            str(ttl_s),
        )
    except Exception:  # noqa: BLE001
        owned_redis_lock_total.labels(
            lock=_lock_name(key),
            outcome="renew_unknown",
        ).inc()
        log.warning("redis lock renew failed key=%s", key, exc_info=True)
        return None
    if int(renewed or 0) != 1:
        owned_redis_lock_total.labels(
            lock=_lock_name(key),
            outcome="ownership_lost",
        ).inc()
        return False
    owned_redis_lock_total.labels(
        lock=_lock_name(key),
        outcome="renewed",
    ).inc()
    return True


async def _renew_owned_lock_loop(
    redis: Any,
    *,
    key: str,
    token: str,
    ttl_s: int,
    stop: asyncio.Event,
    log: logging.Logger,
) -> None:
    interval_s = max(0.1, ttl_s / 3)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except TimeoutError:
            pass
        renewed = await renew_owned_lock(
            redis,
            key=key,
            token=token,
            ttl_s=ttl_s,
            log=log,
        )
        if renewed is False:
            log.warning("redis lock ownership lost key=%s", key)
            return


async def release_owned_lock(
    redis: Any,
    *,
    key: str,
    token: str,
    log: logging.Logger = logger,
) -> bool | None:
    """Release only if ``token`` is still the owner."""
    try:
        released = await redis.eval(RELEASE_OWNED_LOCK_LUA, 1, key, token)
    except Exception:  # noqa: BLE001
        owned_redis_lock_total.labels(
            lock=_lock_name(key),
            outcome="release_unknown",
        ).inc()
        log.warning("redis lock release failed key=%s", key, exc_info=True)
        return None
    outcome = "released" if int(released or 0) == 1 else "release_not_owner"
    owned_redis_lock_total.labels(lock=_lock_name(key), outcome=outcome).inc()
    return outcome == "released"


@asynccontextmanager
async def owned_redis_lock(
    redis: Any,
    *,
    key: str,
    ttl_s: int,
    log: logging.Logger = logger,
) -> AsyncIterator[bool]:
    """Yield whether a token-owned lock was acquired and keep it renewed."""
    token = uuid.uuid4().hex
    acquired = await redis.set(key, token, ex=ttl_s, nx=True)
    if not acquired:
        owned_redis_lock_total.labels(
            lock=_lock_name(key),
            outcome="contended",
        ).inc()
        yield False
        return

    owned_redis_lock_total.labels(
        lock=_lock_name(key),
        outcome="acquired",
    ).inc()
    stop = asyncio.Event()
    renewer = asyncio.create_task(
        _renew_owned_lock_loop(
            redis,
            key=key,
            token=token,
            ttl_s=ttl_s,
            stop=stop,
            log=log,
        )
    )
    try:
        yield True
    finally:
        stop.set()
        renewer.cancel()
        try:
            await renewer
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.warning("redis lock renewer failed key=%s", key, exc_info=True)
        finally:
            await release_owned_lock(redis, key=key, token=token, log=log)
