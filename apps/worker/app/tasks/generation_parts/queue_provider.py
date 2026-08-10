"""Provider-lane reservation decisions for the generation scheduler."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from .lease import LEASE_TTL_S
from .queue import (
    IMAGE_QUEUE_ACTIVE_KEY,
    IMAGE_QUEUE_LOCK_KEY,
    IMAGE_QUEUE_NOT_BEFORE_GRACE_S,
    IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S,
    dual_race_sentinel_name,
    image_queue_not_before_key,
    image_task_provider_key,
    provider_active_count,
    redis_text,
)
from .services import RunGenerationDeps


ReserveProviderSlot = Callable[..., Awaitable[bool]]
ReservationKey = Callable[[str], str]
ReservationTtl = Callable[..., int]
logger = logging.getLogger(__name__)


RESERVE_DUAL_RACE_SLOT_LUA = """
local task_provider_key = KEYS[1]
local global_zset = KEYS[2]
local not_before_key = KEYS[3]
local lock_key = KEYS[4]
local reservation_key = KEYS[5]

local lock_token = ARGV[1]
local sentinel = ARGV[2]
local expiry = tonumber(ARGV[3])
local task_provider_ttl = tonumber(ARGV[4])
local reservation_ttl = tonumber(ARGV[5])

if redis.call('GET', lock_key) ~= lock_token then
  return -1
end
if redis.call('EXISTS', task_provider_key) == 1 then
  return 0
end

redis.call('SET', task_provider_key, sentinel, 'EX', task_provider_ttl)
redis.call('SET', reservation_key, lock_token, 'EX', reservation_ttl)
redis.call('ZADD', global_zset, expiry, sentinel)
redis.call('DEL', not_before_key)
return 1
"""


async def reserve_dual_race_slot(
    lock: Any,
    *,
    task_id: str,
    expiry: float,
    active_count: int,
    capacity: int,
    services: RunGenerationDeps,
    reservation_key: ReservationKey,
    reservation_ttl: ReservationTtl,
) -> Any | None:
    from ...provider_pool import ResolvedProvider

    sentinel = dual_race_sentinel_name(task_id)
    ok = await lock.eval_fenced(
        RESERVE_DUAL_RACE_SLOT_LUA,
        5,
        image_task_provider_key(task_id),
        IMAGE_QUEUE_ACTIVE_KEY,
        image_queue_not_before_key(task_id),
        IMAGE_QUEUE_LOCK_KEY,
        reservation_key(task_id),
        lock.token,
        sentinel,
        str(expiry),
        str(LEASE_TTL_S),
        str(reservation_ttl(services=services)),
        lost_result=-1,
    )
    if int(ok or 0) != 1:
        return None
    logger.info(
        "image queue admitted task=%s mode=dual_race active=%d/%d",
        task_id,
        active_count + 1,
        capacity,
    )
    return ResolvedProvider(name=sentinel, base_url="", api_key="")


async def reserve_from_provider_candidates(
    redis: Any,
    lock: Any,
    *,
    task_id: str,
    providers: list[Any],
    now: float,
    expiry: float,
    active_count: int,
    capacity: int,
    services: RunGenerationDeps,
    reserve_provider_slot: ReserveProviderSlot,
) -> tuple[Any | None, bool]:
    active_count_failed = False
    for provider in providers:
        provider_name = redis_text(getattr(provider, "name", ""))
        if not provider_name:
            continue
        concurrency = max(
            1,
            int(getattr(provider, "image_concurrency", 1) or 1),
        )
        current = await provider_active_count(
            redis,
            provider_name,
            lock=lock,
            services=services,
        )
        if current is None:
            active_count_failed = True
            continue
        if current >= concurrency:
            continue
        admitted = await reserve_provider_slot(
            lock,
            task_id=task_id,
            provider_name=provider_name,
            concurrency=concurrency,
            capacity=capacity,
            now=now,
            expiry=expiry,
            services=services,
        )
        if not admitted:
            continue
        logger.info(
            "image queue admitted task=%s provider=%s "
            "provider_active=%d/%d global_active=%d/%d",
            task_id,
            provider_name,
            current + 1,
            concurrency,
            active_count + 1,
            capacity,
        )
        return provider, active_count_failed
    return None, active_count_failed


async def defer_after_active_count_failure(
    lock: Any,
    *,
    task_id: str,
    services: RunGenerationDeps,
) -> None:
    cooldown = IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S
    redis_set_ok = False
    try:
        redis_set_ok = await lock.set_if_owner(
            image_queue_not_before_key(task_id),
            str(time.time() + cooldown),
            cooldown + IMAGE_QUEUE_NOT_BEFORE_GRACE_S,
        )
    except Exception:  # noqa: BLE001
        pass
    services.queue.provider_cooldowns[task_id] = time.monotonic() + cooldown
    logger.warning(
        "image queue deferred task=%s after provider active count failure "
        "cooldown=%.1fs redis_set=%s",
        task_id,
        cooldown,
        redis_set_ok,
    )


__all__ = [
    "RESERVE_DUAL_RACE_SLOT_LUA",
    "defer_after_active_count_failure",
    "reserve_dual_race_slot",
    "reserve_from_provider_candidates",
]
