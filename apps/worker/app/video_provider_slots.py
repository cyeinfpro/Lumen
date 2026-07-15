"""Redis-backed concurrency slots for video providers."""

from __future__ import annotations

import logging
import time
from typing import Any

from lumen_core.models import new_uuid7

logger = logging.getLogger(__name__)

MAX_PROVIDER_POLL_DURATION_S = 48 * 60 * 60
VIDEO_PROVIDER_SLOT_STALE_AFTER_S = MAX_PROVIDER_POLL_DURATION_S + 5 * 60
VIDEO_PROVIDER_SLOT_TTL_S = VIDEO_PROVIDER_SLOT_STALE_AFTER_S + 60 * 60
VIDEO_PROVIDER_SLOT_PREFIX = "video:provider_slot:"
VIDEO_PROVIDER_SLOT_LOCK_PREFIX = "video:provider_slot_lock:"


async def acquire_provider_slot(
    redis: Any,
    provider_name: str,
    concurrency: int,
    task_id: str,
    *,
    exclusive: bool = False,
) -> bool:
    lock_key = f"{VIDEO_PROVIDER_SLOT_LOCK_PREFIX}{provider_name}"
    lock_token = f"{task_id}:{new_uuid7()}"
    ok = await redis.set(lock_key, lock_token, ex=10, nx=True)
    if not ok:
        return False
    zkey = f"{VIDEO_PROVIDER_SLOT_PREFIX}{provider_name}"
    exclusive_zkey = f"{zkey}:exclusive"
    try:
        cutoff = time.time() - VIDEO_PROVIDER_SLOT_STALE_AFTER_S
        await redis.zremrangebyscore(zkey, 0, cutoff)
        await redis.zremrangebyscore(exclusive_zkey, 0, cutoff)
        task_active = await redis.zscore(zkey, task_id) is not None
        task_exclusive = await redis.zscore(exclusive_zkey, task_id) is not None
        exclusive_count = int(await redis.zcard(exclusive_zkey) or 0)
        if task_active:
            active = int(await redis.zcard(zkey) or 0)
            if exclusive and (active != 1 or exclusive_count not in {0, 1}):
                return False
            if not exclusive and exclusive_count and not task_exclusive:
                return False
            await redis.zadd(zkey, {task_id: time.time()})
            await redis.expire(zkey, VIDEO_PROVIDER_SLOT_TTL_S)
            if exclusive:
                await redis.zadd(exclusive_zkey, {task_id: time.time()})
                await redis.expire(exclusive_zkey, VIDEO_PROVIDER_SLOT_TTL_S)
            return True
        active = await redis.zcard(zkey)
        if exclusive:
            if int(active or 0) > 0 or exclusive_count > 0:
                return False
        elif exclusive_count > 0:
            return False
        if int(active or 0) >= max(1, int(concurrency)):
            return False
        await redis.zadd(zkey, {task_id: time.time()})
        await redis.expire(zkey, VIDEO_PROVIDER_SLOT_TTL_S)
        if exclusive:
            await redis.zadd(exclusive_zkey, {task_id: time.time()})
            await redis.expire(exclusive_zkey, VIDEO_PROVIDER_SLOT_TTL_S)
        return True
    finally:
        await release_slot_lock(redis, lock_key, lock_token)


async def release_slot_lock(redis: Any, lock_key: str, token: str) -> None:
    lua = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    try:
        await redis.eval(lua, 1, lock_key, token)
    except Exception:
        logger.debug("video provider slot lock release failed key=%s", lock_key)


async def release_provider_slot(
    redis: Any,
    provider_name: str,
    task_id: str,
) -> None:
    try:
        zkey = f"{VIDEO_PROVIDER_SLOT_PREFIX}{provider_name}"
        await redis.zrem(zkey, task_id)
        await redis.zrem(f"{zkey}:exclusive", task_id)
    except Exception:
        logger.warning(
            "video provider slot release failed provider=%s task=%s",
            provider_name,
            task_id,
            exc_info=True,
        )


def provider_submit_concurrency(provider: Any, generation: Any) -> int:
    configured = max(1, int(getattr(provider, "concurrency", 1) or 1))
    provider_kind = str(getattr(provider, "kind", "") or "").strip().lower()
    resolution = str(getattr(generation, "resolution", "") or "").strip().lower()
    if provider_kind == "volcano" and resolution == "4k":
        return 1
    return configured


def provider_submit_is_exclusive(provider: Any, generation: Any) -> bool:
    provider_kind = str(getattr(provider, "kind", "") or "").strip().lower()
    resolution = str(getattr(generation, "resolution", "") or "").strip().lower()
    return provider_kind == "volcano" and resolution == "4k"


__all__ = [
    "MAX_PROVIDER_POLL_DURATION_S",
    "VIDEO_PROVIDER_SLOT_LOCK_PREFIX",
    "VIDEO_PROVIDER_SLOT_PREFIX",
    "VIDEO_PROVIDER_SLOT_STALE_AFTER_S",
    "VIDEO_PROVIDER_SLOT_TTL_S",
    "acquire_provider_slot",
    "provider_submit_concurrency",
    "provider_submit_is_exclusive",
    "release_provider_slot",
    "release_slot_lock",
]
