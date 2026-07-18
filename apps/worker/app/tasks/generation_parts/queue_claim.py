from __future__ import annotations

import inspect
import time
from contextlib import suppress
from contextvars import ContextVar
from typing import Any

from redis.exceptions import WatchError

from ._facade import GenerationFacade
from .queue import (
    ImageQueueLockLost,
    clear_stale_image_queue_reservation,
)

_g = GenerationFacade()
bind_generation_facade = _g.bind

RESERVE_IMAGE_SLOT_LUA = """
local provider_zset = KEYS[1]
local global_zset = KEYS[2]
local task_provider_key = KEYS[3]
local not_before_key = KEYS[4]
local lock_key = KEYS[5]
local cursor_key = KEYS[6]
local reservation_key = KEYS[7]

local now = tonumber(ARGV[1])
local expiry = tonumber(ARGV[2])
local task_id = ARGV[3]
local provider_name = ARGV[4]
local provider_cap = tonumber(ARGV[5])
local global_cap = tonumber(ARGV[6])
local task_provider_ttl = tonumber(ARGV[7])
local provider_zset_ttl = tonumber(ARGV[8])
local lock_token = ARGV[9]
local cursor_steps = tonumber(ARGV[10])
local reservation_ttl = tonumber(ARGV[11])

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
if cursor_steps > 0 then
  redis.call('INCRBY', cursor_key, cursor_steps)
  redis.call('EXPIRE', cursor_key, 3600)
end
return 1
"""

RESERVE_DUAL_RACE_SLOT_LUA = """
local task_provider_key = KEYS[1]
local global_zset = KEYS[2]
local not_before_key = KEYS[3]
local cursor_key = KEYS[4]
local lock_key = KEYS[5]
local reservation_key = KEYS[6]

local lock_token = ARGV[1]
local sentinel = ARGV[2]
local expiry = tonumber(ARGV[3])
local task_provider_ttl = tonumber(ARGV[4])
local cursor_steps = tonumber(ARGV[5])
local reservation_ttl = tonumber(ARGV[6])

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
if cursor_steps > 0 then
  redis.call('INCRBY', cursor_key, cursor_steps)
  redis.call('EXPIRE', cursor_key, 3600)
end
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

local owns_reservation = reservation_token ~= '' and redis.call('GET', reservation_key) == reservation_token
local owns_lease = lease_token ~= '' and redis.call('GET', task_lease_key) == lease_token
if not owns_reservation and not owns_lease then
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

DUAL_RACE_SENTINEL_PREFIX = "__dr:"
_IMAGE_QUEUE_RESERVATION_TOKEN_PREFIX = "generation:image_queue:reservation:"
_IMAGE_QUEUE_RESERVATION_TOKENS: ContextVar[dict[str, str]] = ContextVar(
    "image_queue_reservation_tokens",
    default={},
)
_POOL_SELECT_COMPAT_ARGUMENTS = (
    "size_bucket",
    "cost_class",
    "queue_lane",
    "requires_mask",
    "acquire_inflight",
    "endpoint_kind",
    "task_id",
)


def dual_race_sentinel_name(task_id: str) -> str:
    return f"{_g._DUAL_RACE_SENTINEL_PREFIX}{task_id}"


def is_dual_race_sentinel(name: str | None) -> bool:
    return bool(name and name.startswith(_g._DUAL_RACE_SENTINEL_PREFIX))


def _image_queue_reservation_token_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_RESERVATION_TOKEN_PREFIX}{task_id}"


def _remember_image_queue_reservation_token(task_id: str, token: str) -> None:
    tokens = dict(_IMAGE_QUEUE_RESERVATION_TOKENS.get())
    tokens[task_id] = token
    _IMAGE_QUEUE_RESERVATION_TOKENS.set(tokens)


def _forget_image_queue_reservation_token(task_id: str, token: str) -> None:
    tokens = _IMAGE_QUEUE_RESERVATION_TOKENS.get()
    if tokens.get(task_id) != token:
        return
    remaining = dict(tokens)
    remaining.pop(task_id, None)
    _IMAGE_QUEUE_RESERVATION_TOKENS.set(remaining)


def _current_image_queue_reservation_token(task_id: str) -> str | None:
    return _IMAGE_QUEUE_RESERVATION_TOKENS.get().get(task_id)


def _image_queue_reservation_token_ttl() -> int:
    max_runtime = max(0.0, float(getattr(_g, "_RUN_GENERATION_TIMEOUT_S", 0.0)))
    return max(
        int(_g._LEASE_TTL_S * 4),
        int(max_runtime + _g._LEASE_TTL_S * 2),
    )


async def _ready_queue_rank(
    redis: Any,
    lock: Any,
    *,
    task_id: str,
    fair_window: int,
) -> int | None:
    try:
        queued_ids = await _g._ready_queued_generation_ids(
            redis,
            fair_window,
            lock=lock,
        )
    except TypeError as exc:
        if "lock" not in str(exc):
            raise
        queued_ids = await _g._ready_queued_generation_ids(redis, fair_window)
    return queued_ids.index(task_id) if task_id in queued_ids else None


async def _select_queue_provider_candidates(
    *,
    task_id: str,
    endpoint_kind: str | None,
    requires_mask: bool,
    provider_override: Any | None,
    queue_lane: str | None,
    size_bucket: str | None,
    cost_class: str | None,
) -> list[Any]:
    if provider_override is not None:
        return [provider_override]

    from ...provider_pool import get_pool

    pool = await get_pool()
    try:
        return await pool.select(
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
        if not any(name in str(exc) for name in _POOL_SELECT_COMPAT_ARGUMENTS):
            raise

    fallback_kwargs = (
        {"task_id": task_id, "endpoint_kind": endpoint_kind},
        {"task_id": task_id},
        {"endpoint_kind": endpoint_kind},
    )
    for kwargs in fallback_kwargs:
        try:
            return await pool.select(route="image", **kwargs)
        except TypeError:
            continue
    return await pool.select(route="image")


async def _filter_avoided_queue_providers(
    redis: Any,
    lock: Any,
    *,
    task_id: str,
    providers: list[Any],
) -> list[Any]:
    if not providers:
        return providers
    avoided = await _g._get_avoided_providers(redis, task_id)
    if not avoided:
        return providers
    filtered = [
        provider
        for provider in providers
        if _g._redis_text(getattr(provider, "name", "")) not in avoided
    ]
    if filtered:
        return filtered
    _g.logger.info(
        "image queue avoid set fully overlaps providers, "
        "ignoring avoid for task=%s avoided=%s",
        task_id,
        sorted(avoided),
    )
    with suppress(Exception):
        await lock.delete_if_owner(_g._image_queue_avoid_key(task_id))
    return providers


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
    from ...provider_pool import ResolvedProvider

    capacity = await _g._resolve_image_queue_capacity()
    async with _g._image_queue_lock(redis) as lock:
        lock.require_atomic_writes()
        await lock.assert_owner()
        await _g._cleanup_image_queue_active(redis, lock=lock)
        active_members = await _g._active_image_provider_names(redis)

        existing_provider = _g._redis_text(
            await redis.get(_g._image_task_provider_key(task_id))
        )
        if existing_provider:
            if _g._is_dual_race_sentinel(existing_provider):
                if existing_provider in active_members:
                    return None
                cleared = await clear_stale_image_queue_reservation(
                    redis,
                    lock,
                    task_id=task_id,
                    provider_name=existing_provider,
                )
                if cleared:
                    await lock.delete_if_owner(
                        _image_queue_reservation_token_key(task_id)
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
                cleared = await clear_stale_image_queue_reservation(
                    redis,
                    lock,
                    task_id=task_id,
                    provider_name=existing_provider,
                )
                if cleared:
                    await lock.delete_if_owner(
                        _image_queue_reservation_token_key(task_id)
                    )
                _g.logger.info(
                    "image queue cleared stale self-lock task=%s provider=%s",
                    task_id,
                    existing_provider,
                )

        if len(active_members) >= capacity:
            return None

        fair_window = max(1, capacity - len(active_members))
        fair_rank = await _ready_queue_rank(
            redis,
            lock,
            task_id=task_id,
            fair_window=fair_window,
        )
        if fair_rank is None:
            return None

        now = time.time()
        expiry = now + _g._LEASE_TTL_S

        if dual_race:
            sentinel = _g._dual_race_sentinel_name(task_id)
            ok = await lock.eval_fenced(
                RESERVE_DUAL_RACE_SLOT_LUA,
                6,
                _g._image_task_provider_key(task_id),
                _g._IMAGE_QUEUE_ACTIVE_KEY,
                _g._image_queue_not_before_key(task_id),
                _g._IMAGE_QUEUE_LANE_CURSOR_KEY,
                _g._IMAGE_QUEUE_LOCK_KEY,
                _image_queue_reservation_token_key(task_id),
                lock.token,
                sentinel,
                str(expiry),
                str(_g._LEASE_TTL_S),
                str(fair_rank + 1),
                str(_image_queue_reservation_token_ttl()),
                lost_result=-1,
            )
            if int(ok or 0) != 1:
                return None
            _remember_image_queue_reservation_token(task_id, lock.token)
            _g.logger.info(
                "image queue admitted task=%s mode=dual_race active=%d/%d",
                task_id,
                len(active_members) + 1,
                capacity,
            )
            return ResolvedProvider(name=sentinel, base_url="", api_key="")

        providers = await _select_queue_provider_candidates(
            task_id=task_id,
            endpoint_kind=endpoint_kind,
            requires_mask=requires_mask,
            provider_override=provider_override,
            queue_lane=queue_lane,
            size_bucket=size_bucket,
            cost_class=cost_class,
        )
        providers = await _filter_avoided_queue_providers(
            redis,
            lock,
            task_id=task_id,
            providers=providers,
        )
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
            current = await _g._provider_active_count(
                redis,
                provider_name,
                lock=lock,
            )
            if current is None:
                active_count_failed = True
                continue
            if current >= concurrency:
                continue
            try:
                ok = await lock.eval_fenced(
                    _g._RESERVE_IMAGE_SLOT_LUA,
                    7,
                    provider_zset,
                    _g._IMAGE_QUEUE_ACTIVE_KEY,
                    _g._image_task_provider_key(task_id),
                    _g._image_queue_not_before_key(task_id),
                    _g._IMAGE_QUEUE_LOCK_KEY,
                    _g._IMAGE_QUEUE_LANE_CURSOR_KEY,
                    _image_queue_reservation_token_key(task_id),
                    str(now),
                    str(expiry),
                    task_id,
                    provider_name,
                    str(concurrency),
                    str(capacity),
                    str(_g._LEASE_TTL_S),
                    str(_g._LEASE_TTL_S * 4),
                    lock.token,
                    str(fair_rank + 1),
                    str(_image_queue_reservation_token_ttl()),
                    lost_result=-1,
                )
            except ImageQueueLockLost:
                raise
            if int(ok or 0) != 1:
                continue
            _remember_image_queue_reservation_token(task_id, lock.token)
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
            return provider
        if active_count_failed:
            cooldown = _g._IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S
            redis_set_ok = False
            try:
                redis_set_ok = await lock.set_if_owner(
                    _g._image_queue_not_before_key(task_id),
                    str(time.time() + cooldown),
                    cooldown + _g._IMAGE_QUEUE_NOT_BEFORE_GRACE_S,
                )
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
    lease_token: str | None = None,
    reservation_token: str | None = None,
) -> None:
    if not provider_name:
        return

    reservation_token = reservation_token or (
        _current_image_queue_reservation_token(task_id)
    )
    if reservation_token or lease_token:
        released = False
        try:
            released = await _release_image_queue_slot_fenced(
                redis,
                task_id=task_id,
                provider_name=provider_name,
                reservation_token=reservation_token,
                lease_token=lease_token,
            )
        except Exception:  # noqa: BLE001
            _g.logger.warning(
                "fenced image queue release failed task=%s provider=%s",
                task_id,
                provider_name,
                exc_info=True,
            )
        if not released:
            _g.logger.info(
                "image queue release skipped after reservation owner changed "
                "task=%s provider=%s",
                task_id,
                provider_name,
            )
        elif reservation_token:
            _forget_image_queue_reservation_token(task_id, reservation_token)
        with suppress(Exception):
            await _g._kick_image_queue(redis)
        return

    task_provider_key = _g._image_task_provider_key(task_id)
    try:
        current_reservation_token = _g._redis_text(
            await redis.get(_image_queue_reservation_token_key(task_id))
        )
    except Exception:  # noqa: BLE001
        _g.logger.warning(
            "legacy image queue release skipped after reservation token read "
            "failed task=%s provider=%s",
            task_id,
            provider_name,
            exc_info=True,
        )
        return
    if current_reservation_token:
        _g.logger.info(
            "legacy image queue release skipped for tokenized reservation "
            "task=%s provider=%s",
            task_id,
            provider_name,
        )
        with suppress(Exception):
            await _g._kick_image_queue(redis)
        return

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


async def _release_image_queue_slot_fenced(
    redis: Any,
    *,
    task_id: str,
    provider_name: str,
    reservation_token: str | None,
    lease_token: str | None,
) -> bool:
    """Release only while this task still owns its reservation or worker lease."""
    provider_zset = _g._image_provider_active_key(provider_name)
    active_member = (
        provider_name if _g._is_dual_race_sentinel(provider_name) else task_id
    )
    task_provider_key = _g._image_task_provider_key(task_id)
    task_lease_key = f"task:{task_id}:lease"
    reservation_key = _image_queue_reservation_token_key(task_id)
    legacy_provider_lock_key = _g._image_provider_lock_key(provider_name)
    eval_fn = getattr(redis, "eval", None)
    if callable(eval_fn):
        result = await eval_fn(
            RELEASE_IMAGE_QUEUE_SLOT_LUA,
            6,
            provider_zset,
            _g._IMAGE_QUEUE_ACTIVE_KEY,
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
        _g.logger.warning(
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
            current_lease = _g._redis_text(await pipe.get(task_lease_key))
            current_provider = _g._redis_text(await pipe.get(task_provider_key))
            current_reservation = _g._redis_text(await pipe.get(reservation_key))
            owns_reservation = bool(
                reservation_token and current_reservation == reservation_token
            )
            owns_lease = bool(lease_token and current_lease == lease_token)
            if (
                not owns_reservation and not owns_lease
            ) or current_provider != provider_name:
                return False
            legacy_owner = _g._redis_text(await pipe.get(legacy_provider_lock_key))
            pipe.multi()
            pipe.zrem(provider_zset, task_id)
            pipe.zrem(_g._IMAGE_QUEUE_ACTIVE_KEY, active_member)
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
            _g.logger.warning(
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


def _release_slot_fencing_keyword(release_fn: Any) -> str | None:
    """Keep tests and older injected facades that predate token-aware release working."""
    try:
        parameters = inspect.signature(release_fn).parameters.values()
    except (TypeError, ValueError):
        return "lease_token"
    if any(parameter.name == "lease_token" for parameter in parameters):
        return "lease_token"
    if any(parameter.name == "reservation_token" for parameter in parameters):
        return "reservation_token"
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return "lease_token"
    return None


async def release_generation_runtime_resources(
    redis: Any,
    *,
    task_id: str,
    lease_token: str,
    provider_name: str | None,
    clear_avoided_providers: bool,
) -> None:
    try:
        release_fn = _g._release_image_queue_slot
        release_kwargs: dict[str, Any] = {
            "task_id": task_id,
            "provider_name": provider_name,
        }
        fencing_keyword = _release_slot_fencing_keyword(release_fn)
        if fencing_keyword:
            release_kwargs[fencing_keyword] = lease_token
        await release_fn(redis, **release_kwargs)
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
