from __future__ import annotations

import asyncio
import inspect
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from redis.exceptions import WatchError

from .queue_candidate import (
    filter_avoided_providers,
    select_provider_candidates,
)
from .queue_fairness import (
    existing_reservation_blocks_admission,
    ready_queue_rank,
)
from .queue_provider import (
    RESERVE_DUAL_RACE_SLOT_LUA,
    defer_after_active_count_failure,
    reserve_dual_race_slot,
    reserve_from_provider_candidates,
)
from .queue import (
    IMAGE_QUEUE_ACTIVE_KEY,
    IMAGE_QUEUE_LOCK_KEY,
    ImageQueueLockLost,
    active_image_provider_names,
    cleanup_image_queue_active,
    clear_avoided_providers as clear_task_avoided_providers,
    dual_race_sentinel_name,
    image_provider_active_key,
    image_provider_lock_key,
    image_queue_lock,
    image_queue_not_before_key,
    image_task_provider_key,
    inflight_clear,
    is_dual_race_sentinel,
    kick_image_queue,
    ready_queued_generation_ids,
    redis_text,
    resolve_image_queue_capacity,
)
from .lease import LEASE_TTL_S, release_lease
from .runtime_contracts import GENERATION_RUN_TIMEOUT_S
from .services import RunGenerationDeps


RESERVE_IMAGE_SLOT_LUA = """
local provider_zset = KEYS[1]
local global_zset = KEYS[2]
local task_provider_key = KEYS[3]
local not_before_key = KEYS[4]
local lock_key = KEYS[5]
local reservation_key = KEYS[6]

local now = tonumber(ARGV[1])
local expiry = tonumber(ARGV[2])
local task_id = ARGV[3]
local provider_name = ARGV[4]
local provider_cap = tonumber(ARGV[5])
local global_cap = tonumber(ARGV[6])
local task_provider_ttl = tonumber(ARGV[7])
local provider_zset_ttl = tonumber(ARGV[8])
local lock_token = ARGV[9]
local reservation_ttl = tonumber(ARGV[10])

if redis.call('GET', lock_key) ~= lock_token then
  return -1
end

redis.call('ZREMRANGEBYSCORE', provider_zset, '-inf', now)
redis.call('ZREMRANGEBYSCORE', global_zset, '-inf', now)

if redis.call('ZCARD', provider_zset) >= provider_cap then
  return 0
end
if redis.call('ZCARD', global_zset) >= global_cap then
  return 0
end

redis.call('ZADD', provider_zset, expiry, task_id)
redis.call('EXPIRE', provider_zset, provider_zset_ttl)
redis.call('SET', task_provider_key, provider_name, 'EX', task_provider_ttl)
redis.call('SET', reservation_key, lock_token, 'EX', reservation_ttl)
redis.call('ZADD', global_zset, expiry, task_id)
redis.call('DEL', not_before_key)
return 1
"""

RELEASE_IMAGE_QUEUE_SLOT_LUA = """
local provider_zset = KEYS[1]
local global_zset = KEYS[2]
local task_provider_key = KEYS[3]
local task_lease_key = KEYS[4]
local reservation_key = KEYS[5]
local legacy_provider_lock_key = KEYS[6]

local reservation_token = ARGV[1]
local lease_token = ARGV[2]
local expected_provider = ARGV[3]
local task_id = ARGV[4]
local active_member = ARGV[5]

local current_reservation = redis.call('GET', reservation_key)
if current_reservation then
  if reservation_token == '' or current_reservation ~= reservation_token then
    return 0
  end
elseif lease_token == '' or redis.call('GET', task_lease_key) ~= lease_token then
  return 0
end
if redis.call('GET', task_provider_key) ~= expected_provider then
  return 0
end

redis.call('ZREM', provider_zset, task_id)
redis.call('ZREM', global_zset, active_member)
redis.call('DEL', task_provider_key)
redis.call('DEL', reservation_key)
if redis.call('GET', legacy_provider_lock_key) == task_id then
  redis.call('DEL', legacy_provider_lock_key)
end
return 1
"""

_IMAGE_QUEUE_RESERVATION_TOKEN_PREFIX = "generation:image_queue:reservation:"
logger = logging.getLogger(__name__)


def _image_queue_reservation_token_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_RESERVATION_TOKEN_PREFIX}{task_id}"


def _image_queue_reservation_token_ttl(*, services: RunGenerationDeps) -> int:
    max_runtime = max(0.0, GENERATION_RUN_TIMEOUT_S)
    return max(
        int(LEASE_TTL_S * 4),
        int(max_runtime + LEASE_TTL_S * 2),
    )


async def image_queue_reservation_token(
    redis: Any,
    task_id: str,
) -> str | None:
    try:
        return redis_text(
            await redis.get(_image_queue_reservation_token_key(task_id))
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "image queue reservation token read failed task=%s",
            task_id,
            exc_info=True,
        )
        return None


async def _reserve_provider_slot(
    lock: Any,
    *,
    task_id: str,
    provider_name: str,
    concurrency: int,
    capacity: int,
    now: float,
    expiry: float,
    services: RunGenerationDeps,
) -> bool:
    try:
        ok = await lock.eval_fenced(
            RESERVE_IMAGE_SLOT_LUA,
            6,
            image_provider_active_key(provider_name),
            IMAGE_QUEUE_ACTIVE_KEY,
            image_task_provider_key(task_id),
            image_queue_not_before_key(task_id),
            IMAGE_QUEUE_LOCK_KEY,
            _image_queue_reservation_token_key(task_id),
            str(now),
            str(expiry),
            task_id,
            provider_name,
            str(concurrency),
            str(capacity),
            str(LEASE_TTL_S),
            str(LEASE_TTL_S * 4),
            lock.token,
            str(_image_queue_reservation_token_ttl(services=services)),
            lost_result=-1,
        )
    except ImageQueueLockLost:
        raise
    if int(ok or 0) != 1:
        return False
    return True


async def reserve_image_queue_slot(
    redis: Any,
    task_id: str,
    *,
    dual_race: bool = False,
    endpoint_kind: str | None = None,
    requires_mask: bool = False,
    provider_override: Any | None = None,
    queue_lane: str | None = None,
    size_bucket: str | None = None,
    cost_class: str | None = None,
    services: RunGenerationDeps,
) -> Any | None:
    """Reserve one global image slot for a task admitted by strict FIFO.

    The provider branch delegates its atomic ``_RESERVE_IMAGE_SLOT_LUA`` call
    to ``_reserve_provider_slot``, which uses ``lock.eval_fenced(`` with
    ``lost_result=-1``.
    """
    capacity = await resolve_image_queue_capacity(services=services)
    async with image_queue_lock(redis) as lock:
        lock.require_atomic_writes()
        await lock.assert_owner()
        await cleanup_image_queue_active(redis, lock=lock, services=services)
        active_members = await active_image_provider_names(
            redis,
            services=services,
        )
        if await existing_reservation_blocks_admission(
            redis,
            lock,
            task_id=task_id,
            active_members=active_members,
            services=services,
            reservation_key=_image_queue_reservation_token_key,
        ):
            return None
        if len(active_members) >= capacity:
            return None
        fifo_window = max(1, capacity - len(active_members))
        if (
            await ready_queue_rank(
                redis,
                lock,
                task_id=task_id,
                fifo_window=fifo_window,
                services=services,
                read_ready_candidates=ready_queued_generation_ids,
            )
            is None
        ):
            return None

        now = time.time()
        expiry = now + LEASE_TTL_S
        if dual_race:
            provider = await reserve_dual_race_slot(
                lock,
                task_id=task_id,
                expiry=expiry,
                active_count=len(active_members),
                capacity=capacity,
                services=services,
                reservation_key=_image_queue_reservation_token_key,
                reservation_ttl=_image_queue_reservation_token_ttl,
            )
            if provider is not None:
                return provider
        else:
            providers = await select_provider_candidates(
                task_id=task_id,
                endpoint_kind=endpoint_kind,
                requires_mask=requires_mask,
                provider_override=provider_override,
                queue_lane=queue_lane,
                size_bucket=size_bucket,
                cost_class=cost_class,
                services=services,
            )
            providers = await filter_avoided_providers(
                redis,
                lock,
                task_id=task_id,
                providers=providers,
                services=services,
            )
            if providers:
                (
                    provider,
                    active_count_failed,
                ) = await reserve_from_provider_candidates(
                    redis,
                    lock,
                    task_id=task_id,
                    providers=providers,
                    now=now,
                    expiry=expiry,
                    active_count=len(active_members),
                    capacity=capacity,
                    services=services,
                    reserve_provider_slot=_reserve_provider_slot,
                )
                if provider is not None:
                    return provider
                if active_count_failed:
                    await defer_after_active_count_failure(
                        lock,
                        task_id=task_id,
                        services=services,
                    )
    return None


async def release_image_queue_slot(
    redis: Any,
    *,
    task_id: str,
    provider_name: str | None,
    lease_token: str | None = None,
    reservation_token: str | None = None,
    services: RunGenerationDeps,
) -> None:
    if not provider_name:
        return

    if reservation_token or lease_token:
        released = False
        try:
            released = await _release_image_queue_slot_fenced(
                redis,
                task_id=task_id,
                provider_name=provider_name,
                reservation_token=reservation_token,
                lease_token=lease_token,
                services=services,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "fenced image queue release failed task=%s provider=%s",
                task_id,
                provider_name,
                exc_info=True,
            )
        if not released:
            logger.info(
                "image queue release skipped after reservation owner changed "
                "task=%s provider=%s",
                task_id,
                provider_name,
            )
        with suppress(Exception):
            await kick_image_queue(redis, services=services)
        return

    task_provider_key = image_task_provider_key(task_id)
    try:
        current_reservation_token = redis_text(
            await redis.get(_image_queue_reservation_token_key(task_id))
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "legacy image queue release skipped after reservation token read "
            "failed task=%s provider=%s",
            task_id,
            provider_name,
            exc_info=True,
        )
        return
    if current_reservation_token:
        logger.info(
            "legacy image queue release skipped for tokenized reservation "
            "task=%s provider=%s",
            task_id,
            provider_name,
        )
        with suppress(Exception):
            await kick_image_queue(redis, services=services)
        return

    if is_dual_race_sentinel(provider_name):
        try:
            await redis.zrem(IMAGE_QUEUE_ACTIVE_KEY, provider_name)
            await redis.delete(task_provider_key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "dual_race release failed task=%s sentinel=%s",
                task_id,
                provider_name,
                exc_info=True,
            )
        await kick_image_queue(redis, services=services)
        return
    provider_zset = image_provider_active_key(provider_name)
    try:
        await redis.zrem(provider_zset, task_id)
        await redis.zrem(IMAGE_QUEUE_ACTIVE_KEY, task_id)
        await redis.delete(task_provider_key)
        with suppress(Exception):
            legacy = image_provider_lock_key(provider_name)
            owner = redis_text(await redis.get(legacy))
            if owner == task_id:
                await redis.delete(legacy)
    except Exception:  # noqa: BLE001
        logger.warning(
            "image queue release failed task=%s provider=%s",
            task_id,
            provider_name,
            exc_info=True,
        )
    await kick_image_queue(redis, services=services)


async def _release_image_queue_slot_fenced(
    redis: Any,
    *,
    task_id: str,
    provider_name: str,
    reservation_token: str | None,
    lease_token: str | None,
    services: RunGenerationDeps,
) -> bool:
    """Release only while this task still owns its reservation or worker lease."""
    provider_zset = image_provider_active_key(provider_name)
    active_member = provider_name if is_dual_race_sentinel(provider_name) else task_id
    task_provider_key = image_task_provider_key(task_id)
    task_lease_key = f"task:{task_id}:lease"
    reservation_key = _image_queue_reservation_token_key(task_id)
    legacy_provider_lock_key = image_provider_lock_key(provider_name)
    eval_fn = getattr(redis, "eval", None)
    if callable(eval_fn):
        result = await eval_fn(
            RELEASE_IMAGE_QUEUE_SLOT_LUA,
            6,
            provider_zset,
            IMAGE_QUEUE_ACTIVE_KEY,
            task_provider_key,
            task_lease_key,
            reservation_key,
            legacy_provider_lock_key,
            reservation_token or "",
            lease_token or "",
            provider_name,
            task_id,
            active_member,
        )
        return int(result or 0) == 1

    pipeline_factory = getattr(redis, "pipeline", None)
    if not callable(pipeline_factory):
        logger.warning(
            "fenced image queue release skipped without atomic CAS task=%s provider=%s",
            task_id,
            provider_name,
        )
        return False

    for attempt in range(3):
        pipe: Any | None = None
        try:
            pipe = pipeline_factory(transaction=True)
            watch = getattr(pipe, "watch", None)
            if not callable(watch):
                return False
            for key in (
                task_lease_key,
                task_provider_key,
                reservation_key,
                legacy_provider_lock_key,
            ):
                await watch(key)
            current_lease = redis_text(await pipe.get(task_lease_key))
            current_provider = redis_text(await pipe.get(task_provider_key))
            current_reservation = redis_text(await pipe.get(reservation_key))
            owns_slot = (
                bool(
                    reservation_token
                    and current_reservation == reservation_token
                )
                if current_reservation
                else bool(lease_token and current_lease == lease_token)
            )
            if not owns_slot or current_provider != provider_name:
                return False
            legacy_owner = redis_text(await pipe.get(legacy_provider_lock_key))
            pipe.multi()
            pipe.zrem(provider_zset, task_id)
            pipe.zrem(IMAGE_QUEUE_ACTIVE_KEY, active_member)
            pipe.delete(task_provider_key)
            pipe.delete(reservation_key)
            if legacy_owner == task_id:
                pipe.delete(legacy_provider_lock_key)
            await pipe.execute()
            return True
        except WatchError:
            if attempt >= 2:
                return False
        except Exception:
            logger.warning(
                "fenced image queue WATCH release failed task=%s provider=%s",
                task_id,
                provider_name,
                exc_info=True,
            )
            return False
        finally:
            if pipe is not None:
                reset = getattr(pipe, "reset", None)
                if callable(reset):
                    with suppress(Exception):
                        result = reset()
                        if inspect.isawaitable(result):
                            await result
    return False


def _release_slot_fencing_kwargs(
    release_fn: Any,
    *,
    lease_token: str,
    reservation_token: str | None,
) -> dict[str, str]:
    """Keep older adapters that predate token-aware release working."""
    try:
        parameters = inspect.signature(release_fn).parameters
    except (TypeError, ValueError):
        return {"lease_token": lease_token}
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    result: dict[str, str] = {}
    if accepts_kwargs or "lease_token" in parameters:
        result["lease_token"] = lease_token
    if reservation_token and (
        accepts_kwargs or "reservation_token" in parameters
    ):
        result["reservation_token"] = reservation_token
    return result


async def release_generation_runtime_resources(
    redis: Any,
    *,
    task_id: str,
    lease_token: str,
    provider_name: str | None,
    reservation_token: str | None = None,
    clear_avoided_providers: bool,
    services: RunGenerationDeps,
) -> None:
    try:
        release_fn = release_image_queue_slot
        release_kwargs: dict[str, Any] = {
            "task_id": task_id,
            "provider_name": provider_name,
            "services": services,
        }
        release_kwargs.update(
            _release_slot_fencing_kwargs(
                release_fn,
                lease_token=lease_token,
                reservation_token=reservation_token,
            )
        )
        await release_fn(redis, **release_kwargs)
    except Exception:  # noqa: BLE001
        logger.warning(
            "generation image queue release failed task=%s provider=%s",
            task_id,
            provider_name,
            exc_info=True,
        )
    try:
        await inflight_clear(redis, task_id, services=services)
    except Exception:  # noqa: BLE001
        logger.warning(
            "generation inflight cleanup failed task=%s",
            task_id,
            exc_info=True,
        )
    if clear_avoided_providers:
        try:
            await clear_task_avoided_providers(
                redis,
                task_id,
                services=services,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "generation avoid-set cleanup failed task=%s",
                task_id,
                exc_info=True,
            )
    try:
        await release_lease(redis, task_id, lease_token)
    except Exception:  # noqa: BLE001
        logger.warning(
            "generation lease release failed task=%s",
            task_id,
            exc_info=True,
        )


@dataclass(slots=True)
class GenerationResourceLease:
    services: RunGenerationDeps
    redis: Any
    task_id: str
    lease_token: str
    provider_name: str | None
    clear_avoided_providers: bool
    reservation_token: str | None = None
    release_resources: Any | None = None
    _closed: bool = False
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def close(self) -> bool:
        async with self._close_lock:
            if self._closed:
                return False
            self._closed = True
        release = self.release_resources or release_generation_runtime_resources
        await release(
            self.redis,
            task_id=self.task_id,
            lease_token=self.lease_token,
            provider_name=self.provider_name,
            reservation_token=self.reservation_token,
            clear_avoided_providers=self.clear_avoided_providers,
            services=self.services,
        )
        return True


__all__ = [
    "GenerationResourceLease",
    "RELEASE_IMAGE_QUEUE_SLOT_LUA",
    "RESERVE_DUAL_RACE_SLOT_LUA",
    "RESERVE_IMAGE_SLOT_LUA",
    "dual_race_sentinel_name",
    "image_queue_reservation_token",
    "release_generation_runtime_resources",
    "release_image_queue_slot",
    "reserve_image_queue_slot",
]
