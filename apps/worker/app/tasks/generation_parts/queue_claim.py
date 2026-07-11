from __future__ import annotations

import time
from contextlib import suppress
from typing import Any

from ._facade import GenerationFacade

_g = GenerationFacade()
bind_generation_facade = _g.bind

RESERVE_IMAGE_SLOT_LUA = """
local provider_zset = KEYS[1]
local global_zset = KEYS[2]
local task_provider_key = KEYS[3]
local not_before_key = KEYS[4]

local now = tonumber(ARGV[1])
local expiry = tonumber(ARGV[2])
local task_id = ARGV[3]
local provider_name = ARGV[4]
local provider_cap = tonumber(ARGV[5])
local global_cap = tonumber(ARGV[6])
local task_provider_ttl = tonumber(ARGV[7])
local provider_zset_ttl = tonumber(ARGV[8])

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
redis.call('ZADD', global_zset, expiry, task_id)
redis.call('DEL', not_before_key)
return 1
"""

DUAL_RACE_SENTINEL_PREFIX = "__dr:"


def dual_race_sentinel_name(task_id: str) -> str:
    return f"{_g._DUAL_RACE_SENTINEL_PREFIX}{task_id}"


def is_dual_race_sentinel(name: str | None) -> bool:
    return bool(name and name.startswith(_g._DUAL_RACE_SENTINEL_PREFIX))


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
) -> Any | None:
    """Reserve one global image slot for a task admitted by fair scheduling."""
    from ...provider_pool import ResolvedProvider, get_pool

    capacity = await _g._resolve_image_queue_capacity()
    async with _g._image_queue_lock(redis):
        await _g._cleanup_image_queue_active(redis)
        active_members = await _g._active_image_provider_names(redis)

        existing_provider = _g._redis_text(
            await redis.get(_g._image_task_provider_key(task_id))
        )
        if existing_provider:
            if _g._is_dual_race_sentinel(existing_provider):
                if existing_provider in active_members:
                    return None
                with suppress(Exception):
                    await redis.delete(_g._image_task_provider_key(task_id))
                with suppress(Exception):
                    await redis.zrem(
                        _g._IMAGE_QUEUE_ACTIVE_KEY,
                        existing_provider,
                    )
                _g.logger.info(
                    "image queue cleared stale dual_race sentinel task=%s",
                    task_id,
                )
            else:
                provider_zset = _g._image_provider_active_key(existing_provider)
                still_admitted = False
                with suppress(Exception):
                    score = await redis.zscore(provider_zset, task_id)
                    still_admitted = score is not None and float(score) > time.time()
                if still_admitted and task_id in active_members:
                    return None
                with suppress(Exception):
                    await redis.zrem(provider_zset, task_id)
                with suppress(Exception):
                    await redis.delete(_g._image_task_provider_key(task_id))
                with suppress(Exception):
                    await redis.zrem(_g._IMAGE_QUEUE_ACTIVE_KEY, task_id)
                _g.logger.info(
                    "image queue cleared stale self-lock task=%s provider=%s",
                    task_id,
                    existing_provider,
                )

        if len(active_members) >= capacity:
            return None

        fair_window = max(1, capacity - len(active_members))
        queued_ids = await _g._ready_queued_generation_ids(redis, fair_window)
        if task_id not in queued_ids:
            return None
        fair_rank = queued_ids.index(task_id)

        now = time.time()
        expiry = now + _g._LEASE_TTL_S

        if dual_race:
            sentinel = _g._dual_race_sentinel_name(task_id)
            await redis.set(
                _g._image_task_provider_key(task_id),
                sentinel,
                ex=_g._LEASE_TTL_S,
            )
            await redis.zadd(
                _g._IMAGE_QUEUE_ACTIVE_KEY,
                {sentinel: expiry},
            )
            await redis.delete(_g._image_queue_not_before_key(task_id))
            await _g._advance_image_queue_lane_cursor(redis, fair_rank + 1)
            _g.logger.info(
                "image queue admitted task=%s mode=dual_race active=%d/%d",
                task_id,
                len(active_members) + 1,
                capacity,
            )
            return ResolvedProvider(name=sentinel, base_url="", api_key="")

        if provider_override is not None:
            providers = [provider_override]
        else:
            pool = await get_pool()
            try:
                providers = await pool.select(
                    route="image",
                    task_id=task_id,
                    endpoint_kind=endpoint_kind,
                    acquire_inflight=False,
                    requires_mask=requires_mask,
                    queue_lane=queue_lane,
                    size_bucket=size_bucket,
                    cost_class=cost_class,
                )
            except TypeError as exc:
                msg = str(exc)
                if (
                    "size_bucket" not in msg
                    and "cost_class" not in msg
                    and "queue_lane" not in msg
                    and "requires_mask" not in msg
                    and "acquire_inflight" not in msg
                    and "endpoint_kind" not in msg
                    and "task_id" not in msg
                ):
                    raise
                try:
                    providers = await pool.select(
                        route="image",
                        task_id=task_id,
                        endpoint_kind=endpoint_kind,
                    )
                except TypeError:
                    try:
                        providers = await pool.select(
                            route="image",
                            task_id=task_id,
                        )
                    except TypeError:
                        try:
                            providers = await pool.select(
                                route="image",
                                endpoint_kind=endpoint_kind,
                            )
                        except TypeError:
                            providers = await pool.select(route="image")
        if not providers:
            return None

        avoided = await _g._get_avoided_providers(redis, task_id)
        if avoided:
            filtered = [
                provider
                for provider in providers
                if _g._redis_text(getattr(provider, "name", "")) not in avoided
            ]
            if filtered:
                providers = filtered
            else:
                _g.logger.info(
                    "image queue avoid set fully overlaps providers, "
                    "ignoring avoid for task=%s avoided=%s",
                    task_id,
                    sorted(avoided),
                )
                with suppress(Exception):
                    await redis.delete(_g._image_queue_avoid_key(task_id))
        if not providers:
            return None

        active_count_failed = False
        for provider in providers:
            provider_name = _g._redis_text(getattr(provider, "name", ""))
            if not provider_name:
                continue
            concurrency = max(
                1,
                int(getattr(provider, "image_concurrency", 1) or 1),
            )
            provider_zset = _g._image_provider_active_key(provider_name)
            current = await _g._provider_active_count(redis, provider_name)
            if current is None:
                active_count_failed = True
                continue
            if current >= concurrency:
                continue
            try:
                eval_fn = getattr(redis, "eval", None)
                if callable(eval_fn):
                    ok = await eval_fn(
                        _g._RESERVE_IMAGE_SLOT_LUA,
                        4,
                        provider_zset,
                        _g._IMAGE_QUEUE_ACTIVE_KEY,
                        _g._image_task_provider_key(task_id),
                        _g._image_queue_not_before_key(task_id),
                        str(now),
                        str(expiry),
                        task_id,
                        provider_name,
                        str(concurrency),
                        str(capacity),
                        str(_g._LEASE_TTL_S),
                        str(_g._LEASE_TTL_S * 4),
                    )
                    if int(ok or 0) != 1:
                        continue
                else:
                    _g.logger.warning(
                        "image queue reserve using non-atomic fallback path task=%s",
                        task_id,
                    )
                    await redis.zadd(provider_zset, {task_id: expiry})
                    await redis.expire(provider_zset, _g._LEASE_TTL_S * 4)
                    await redis.set(
                        _g._image_task_provider_key(task_id),
                        provider_name,
                        ex=_g._LEASE_TTL_S,
                    )
                    await redis.zadd(
                        _g._IMAGE_QUEUE_ACTIVE_KEY,
                        {task_id: expiry},
                    )
                    await redis.delete(_g._image_queue_not_before_key(task_id))
            except Exception:
                with suppress(Exception):
                    await redis.zrem(provider_zset, task_id)
                with suppress(Exception):
                    await redis.delete(_g._image_task_provider_key(task_id))
                with suppress(Exception):
                    await redis.zrem(_g._IMAGE_QUEUE_ACTIVE_KEY, task_id)
                raise
            _g.logger.info(
                "image queue admitted task=%s provider=%s "
                "provider_active=%d/%d global_active=%d/%d",
                task_id,
                provider_name,
                current + 1,
                concurrency,
                len(active_members) + 1,
                capacity,
            )
            await _g._advance_image_queue_lane_cursor(redis, fair_rank + 1)
            return provider
        if active_count_failed:
            cooldown = _g._IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S
            redis_set_ok = False
            try:
                await redis.set(
                    _g._image_queue_not_before_key(task_id),
                    str(time.time() + cooldown),
                    ex=int(cooldown + _g._IMAGE_QUEUE_NOT_BEFORE_GRACE_S),
                )
                redis_set_ok = True
            except Exception:  # noqa: BLE001
                pass
            _g._PROVIDER_COOLDOWN_LOCAL[task_id] = time.monotonic() + cooldown
            _g.logger.warning(
                "image queue deferred task=%s after provider active count failure "
                "cooldown=%.1fs redis_set=%s",
                task_id,
                cooldown,
                redis_set_ok,
            )
    return None


async def release_image_queue_slot(
    redis: Any,
    *,
    task_id: str,
    provider_name: str | None,
) -> None:
    if not provider_name:
        return
    task_provider_key = _g._image_task_provider_key(task_id)
    if _g._is_dual_race_sentinel(provider_name):
        try:
            await redis.zrem(_g._IMAGE_QUEUE_ACTIVE_KEY, provider_name)
            await redis.delete(task_provider_key)
        except Exception:  # noqa: BLE001
            _g.logger.warning(
                "dual_race release failed task=%s sentinel=%s",
                task_id,
                provider_name,
                exc_info=True,
            )
        await _g._kick_image_queue(redis)
        return
    provider_zset = _g._image_provider_active_key(provider_name)
    try:
        await redis.zrem(provider_zset, task_id)
        await redis.zrem(_g._IMAGE_QUEUE_ACTIVE_KEY, task_id)
        await redis.delete(task_provider_key)
        with suppress(Exception):
            legacy = _g._image_provider_lock_key(provider_name)
            owner = _g._redis_text(await redis.get(legacy))
            if owner == task_id:
                await redis.delete(legacy)
    except Exception:  # noqa: BLE001
        _g.logger.warning(
            "image queue release failed task=%s provider=%s",
            task_id,
            provider_name,
            exc_info=True,
        )
    await _g._kick_image_queue(redis)


async def release_generation_runtime_resources(
    redis: Any,
    *,
    task_id: str,
    lease_token: str,
    provider_name: str | None,
    clear_avoided_providers: bool,
) -> None:
    try:
        await _g._release_image_queue_slot(
            redis,
            task_id=task_id,
            provider_name=provider_name,
        )
    except Exception:  # noqa: BLE001
        _g.logger.warning(
            "generation image queue release failed task=%s provider=%s",
            task_id,
            provider_name,
            exc_info=True,
        )
    try:
        await _g._inflight_clear(redis, task_id)
    except Exception:  # noqa: BLE001
        _g.logger.warning(
            "generation inflight cleanup failed task=%s",
            task_id,
            exc_info=True,
        )
    if clear_avoided_providers:
        try:
            await _g._clear_avoided_providers(redis, task_id)
        except Exception:  # noqa: BLE001
            _g.logger.warning(
                "generation avoid-set cleanup failed task=%s",
                task_id,
                exc_info=True,
            )
    try:
        await _g._release_lease(redis, task_id, lease_token)
    except Exception:  # noqa: BLE001
        _g.logger.warning(
            "generation lease release failed task=%s",
            task_id,
            exc_info=True,
        )
