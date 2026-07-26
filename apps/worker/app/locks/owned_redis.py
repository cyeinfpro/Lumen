"""Token-owned Redis locks with atomic renewal and release."""

from __future__ import annotations

import asyncio
import hashlib
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


# 释放失败时的补偿重试：Lua 是 CAS（GET==token 才 DEL），重放天然幂等，
# 而多等一个 TTL 意味着整条对账/outbox 链路白白停摆一个 TTL。
RELEASE_RETRY_DELAYS_S: tuple[float, ...] = (0.05, 0.2)


def _lock_name(key: str) -> str:
    return key.removeprefix("lock:")


def _token_fingerprint(token: str) -> str:
    """日志用的 token 指纹。

    token 本身是「谁持有这把锁」的凭证，明文进日志等于任何能读日志的人都能
    伪造释放；只打前 12 位 sha256 摘要，既能在多 worker 日志里对上同一次
    持有，又不泄露凭证。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


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
        log.warning(
            "redis lock renew failed key=%s token=%s",
            key,
            _token_fingerprint(token),
            exc_info=True,
        )
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
    holder_task: asyncio.Task[Any],
    log: logging.Logger,
) -> None:
    loop = asyncio.get_running_loop()
    interval_s = max(0.1, ttl_s / 3)
    retry_delay_s = max(0.05, min(1.0, ttl_s / 12))
    expiry_guard_s = max(0.05, min(1.0, ttl_s / 10))
    last_confirmed_at = loop.time()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except TimeoutError:
            pass
        while not stop.is_set():
            renewed = await renew_owned_lock(
                redis,
                key=key,
                token=token,
                ttl_s=ttl_s,
                log=log,
            )
            if renewed is True:
                last_confirmed_at = loop.time()
                break
            if renewed is False:
                log.warning("redis lock ownership lost key=%s", key)
                if not holder_task.done():
                    holder_task.cancel(f"owned redis lock lost: {key}")
                return

            # Redis could not answer, but the last confirmed TTL is still
            # valid. Retry within that window instead of cancelling the holder
            # on a single network timeout.
            remaining_s = last_confirmed_at + ttl_s - loop.time()
            if remaining_s <= expiry_guard_s:
                owned_redis_lock_total.labels(
                    lock=_lock_name(key),
                    outcome="renew_unconfirmed_expiry",
                ).inc()
                log.error(
                    "redis lock renewal remained unconfirmed near expiry "
                    "key=%s remaining_s=%.3f",
                    key,
                    max(0.0, remaining_s),
                )
                if not holder_task.done():
                    holder_task.cancel(f"owned redis lock renewal unconfirmed: {key}")
                return
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=min(retry_delay_s, remaining_s - expiry_guard_s),
                )
                return
            except TimeoutError:
                pass


async def release_owned_lock(
    redis: Any,
    *,
    key: str,
    token: str,
    log: logging.Logger = logger,
) -> bool | None:
    """Release only if ``token`` is still the owner.

    ``None`` means Redis never confirmed the outcome. The lock then stays held
    until its TTL expires, so callers must treat it as "still locked" rather
    than as a successful release.
    """
    attempts = len(RELEASE_RETRY_DELAYS_S) + 1
    for attempt in range(1, attempts + 1):
        try:
            released = await redis.eval(RELEASE_OWNED_LOCK_LUA, 1, key, token)
        except Exception:  # noqa: BLE001
            log.warning(
                "redis lock release failed key=%s token=%s attempt=%d/%d",
                key,
                _token_fingerprint(token),
                attempt,
                attempts,
                exc_info=True,
            )
            if attempt < attempts:
                await asyncio.sleep(RELEASE_RETRY_DELAYS_S[attempt - 1])
                continue
            owned_redis_lock_total.labels(
                lock=_lock_name(key),
                outcome="release_unknown",
            ).inc()
            log.error(
                "redis lock release gave up key=%s token=%s; lock stays held "
                "until ttl expires",
                key,
                _token_fingerprint(token),
            )
            return None
        outcome = "released" if int(released or 0) == 1 else "release_not_owner"
        owned_redis_lock_total.labels(lock=_lock_name(key), outcome=outcome).inc()
        if outcome == "release_not_owner":
            log.warning(
                "redis lock release found another owner key=%s token=%s",
                key,
                _token_fingerprint(token),
            )
        return outcome == "released"
    return None  # pragma: no cover - loop always returns


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
    holder_task = asyncio.current_task()
    if holder_task is None:  # pragma: no cover - async context always has a task
        raise RuntimeError("owned redis lock requires an active asyncio task")
    renewer = asyncio.create_task(
        _renew_owned_lock_loop(
            redis,
            key=key,
            token=token,
            ttl_s=ttl_s,
            stop=stop,
            holder_task=holder_task,
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
            log.warning(
                "redis lock renewer failed key=%s token=%s",
                key,
                _token_fingerprint(token),
                exc_info=True,
            )
        finally:
            # 释放结果不能吞：None = Redis 没确认，锁要卡到 TTL 才自然释放，
            # 下一轮 cron/对账在此期间会被判成 contended 而空转。
            if await release_owned_lock(redis, key=key, token=token, log=log) is None:
                log.error(
                    "redis lock left held key=%s token=%s ttl_s=%s",
                    key,
                    _token_fingerprint(token),
                    ttl_s,
                )
