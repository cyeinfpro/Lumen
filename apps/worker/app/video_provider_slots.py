"""Redis-backed concurrency slots for video providers."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

MAX_PROVIDER_POLL_DURATION_S = 48 * 60 * 60
VIDEO_PROVIDER_SLOT_STALE_AFTER_S = MAX_PROVIDER_POLL_DURATION_S + 5 * 60
VIDEO_PROVIDER_SLOT_TTL_S = VIDEO_PROVIDER_SLOT_STALE_AFTER_S + 60 * 60
VIDEO_PROVIDER_SLOT_PREFIX = "video:provider_slot:"
VIDEO_PROVIDER_SLOT_LOCK_PREFIX = "video:provider_slot_lock:"

_ACQUIRE_PROVIDER_SLOT_LUA = """
local active_key = KEYS[1]
local exclusive_key = KEYS[2]
local task_id = ARGV[1]
local now = tonumber(ARGV[2])
local cutoff = tonumber(ARGV[3])
local concurrency = tonumber(ARGV[4])
local wants_exclusive = tonumber(ARGV[5])
local ttl = tonumber(ARGV[6])

redis.call('ZREMRANGEBYSCORE', active_key, '-inf', cutoff)
redis.call('ZREMRANGEBYSCORE', exclusive_key, '-inf', cutoff)

local task_active = redis.call('ZSCORE', active_key, task_id)
local task_exclusive = redis.call('ZSCORE', exclusive_key, task_id)
local active_count = redis.call('ZCARD', active_key)
local exclusive_count = redis.call('ZCARD', exclusive_key)

if task_active then
  if wants_exclusive == 1 then
    if active_count ~= 1 or exclusive_count > 1 then
      return 0
    end
    if exclusive_count == 1 and not task_exclusive then
      return 0
    end
  elseif exclusive_count > 0 and not task_exclusive then
    return 0
  end

  redis.call('ZADD', active_key, now, task_id)
  redis.call('EXPIRE', active_key, ttl)
  if wants_exclusive == 1 then
    redis.call('ZADD', exclusive_key, now, task_id)
    redis.call('EXPIRE', exclusive_key, ttl)
  end
  return 1
end

if wants_exclusive == 1 then
  if active_count > 0 or exclusive_count > 0 then
    return 0
  end
elseif exclusive_count > 0 then
  return 0
end

if active_count >= concurrency then
  return 0
end

redis.call('ZADD', active_key, now, task_id)
redis.call('EXPIRE', active_key, ttl)
if wants_exclusive == 1 then
  redis.call('ZADD', exclusive_key, now, task_id)
  redis.call('EXPIRE', exclusive_key, ttl)
end
return 1
"""


async def acquire_provider_slot(
    redis: Any,
    provider_name: str,
    concurrency: int,
    task_id: str,
    *,
    exclusive: bool = False,
) -> bool:
    zkey = f"{VIDEO_PROVIDER_SLOT_PREFIX}{provider_name}"
    exclusive_zkey = f"{zkey}:exclusive"
    now = time.time()
    acquired = await redis.eval(
        _ACQUIRE_PROVIDER_SLOT_LUA,
        2,
        zkey,
        exclusive_zkey,
        task_id,
        now,
        now - VIDEO_PROVIDER_SLOT_STALE_AFTER_S,
        max(1, int(concurrency)),
        1 if exclusive else 0,
        VIDEO_PROVIDER_SLOT_TTL_S,
    )
    return bool(acquired)


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
