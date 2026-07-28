from __future__ import annotations

import logging
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import select

from lumen_core.constants import GenerationErrorCode as EC, GenerationStatus
from lumen_core.models import Generation
from lumen_core.queue_metadata import generation_queue_metadata

from ...provider_runtime.errors import UpstreamError
from ...generation_dispatch import (
    DispatchIdentity,
    enqueue_generation_dispatch,
)
from . import queue_lock as _queue_lock
from .runtime_contracts import GENERATION_LEASE_TTL_S
from .services import RunGenerationDeps

DELETE_IMAGE_QUEUE_KEY_IF_OWNER_LUA = _queue_lock.DELETE_IMAGE_QUEUE_KEY_IF_OWNER_LUA
ImageQueueLockLease = _queue_lock.ImageQueueLockLease
ImageQueueLockLost = _queue_lock.ImageQueueLockLost
RENEW_IMAGE_QUEUE_LOCK_LUA = _queue_lock.RENEW_IMAGE_QUEUE_LOCK_LUA
SET_IMAGE_QUEUE_VALUE_IF_OWNER_LUA = _queue_lock.SET_IMAGE_QUEUE_VALUE_IF_OWNER_LUA
image_queue_lock = _queue_lock.image_queue_lock


IMAGE_QUEUE_LOCK_KEY = "generation:image_queue:lock"
IMAGE_QUEUE_ACTIVE_KEY = "generation:image_queue:active"
IMAGE_QUEUE_PROVIDER_LOCK_PREFIX = "generation:image_queue:provider:"
IMAGE_QUEUE_TASK_PROVIDER_PREFIX = "generation:image_queue:task_provider:"
IMAGE_QUEUE_NOT_BEFORE_PREFIX = "generation:image_queue:not_before:"
IMAGE_QUEUE_AVOID_PREFIX = "generation:image_queue:avoid:"
IMAGE_QUEUE_LANE_CURSOR_KEY = "generation:image_queue:lane_cursor"
IMAGE_INFLIGHT_PREFIX = "generation:image_inflight:"
IMAGE_QUEUE_LOCK_TTL_S = 10
IMAGE_QUEUE_LOCK_WAIT_S = 5.0
IMAGE_QUEUE_FAIR_SCAN_LIMIT = 1000
IMAGE_QUEUE_NOT_BEFORE_GRACE_S = 600
IMAGE_PROVIDER_UNAVAILABLE_RETRY_S = 30
IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S = 5.0
DUAL_RACE_SENTINEL_PREFIX = "__dr:"

logger = logging.getLogger(__name__)


IMAGE_QUEUE_AVOID_TTL_S = 120
IMAGE_QUEUE_DEFAULT_LANE = "image:interactive:unknown"
IMAGE_QUEUE_LANE_WEIGHTS = MappingProxyType(
    {
        "image:interactive:small": 8,
        "image:interactive:medium": 5,
        "image:interactive:large": 3,
        "image:interactive:edit": 4,
        "image:interactive:mask_edit": 5,
        "image:interactive:unknown": 3,
        "image:workflow:small": 3,
        "image:workflow:medium": 2,
        "image:workflow:large": 1,
        "image:workflow:edit": 1,
        "image:workflow:mask_edit": 1,
        "image:workflow:unknown": 1,
    }
)
IMAGE_QUEUE_LANE_ORDER: tuple[str, ...] = tuple(IMAGE_QUEUE_LANE_WEIGHTS)
IMAGE_QUEUE_LANE_RANK = MappingProxyType(
    {lane: idx for idx, lane in enumerate(IMAGE_QUEUE_LANE_ORDER)}
)
IMAGE_GENERATION_CONCURRENCY_SETTING = "image.generation_concurrency"

CLEANUP_IMAGE_QUEUE_ACTIVE_LUA = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then
  return -1
end
return redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
"""

CLEANUP_IMAGE_QUEUE_PROVIDER_LUA = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then
  return -1
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
return redis.call('ZCARD', KEYS[1])
"""

ADVANCE_IMAGE_QUEUE_CURSOR_LUA = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then
  return -1
end
local value = redis.call('INCRBY', KEYS[1], tonumber(ARGV[2]))
redis.call('EXPIRE', KEYS[1], 3600)
return value
"""

CLEAR_STALE_IMAGE_QUEUE_RESERVATION_LUA = """
if redis.call('GET', KEYS[4]) ~= ARGV[1] then
  return -1
end
if redis.call('GET', KEYS[3]) ~= ARGV[2] then
  return 0
end
redis.call('ZREM', KEYS[1], ARGV[3])
redis.call('ZREM', KEYS[2], ARGV[4])
redis.call('DEL', KEYS[3])
return 1
"""


@dataclass(frozen=True)
class QueuedGenerationCandidate:
    id: str
    queue_lane: str = IMAGE_QUEUE_DEFAULT_LANE
    size_bucket: str | None = None
    cost_class: str | None = None
    created_at: datetime | None = None
    attempt: int = 0


def redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def coerce_image_queue_capacity(raw: Any) -> int:
    try:
        return max(1, min(32, int(raw)))
    except (TypeError, ValueError):
        return 4


def image_queue_capacity(*, services: RunGenerationDeps) -> int:
    return services.queue.configured_capacity()


async def resolve_image_queue_capacity(*, services: RunGenerationDeps) -> int:
    try:
        return await services.queue.resolve_capacity()
    except Exception as exc:  # noqa: BLE001
        logger.warning("image queue capacity resolve failed err=%s", exc)
        return image_queue_capacity(services=services)


def image_provider_lock_key(provider_name: str) -> str:
    return f"{IMAGE_QUEUE_PROVIDER_LOCK_PREFIX}{provider_name}"


def image_provider_active_key(provider_name: str) -> str:
    return f"generation:image_queue:provider_active:{provider_name}"


def image_task_provider_key(task_id: str) -> str:
    return f"{IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{task_id}"


def image_queue_not_before_key(task_id: str) -> str:
    return f"{IMAGE_QUEUE_NOT_BEFORE_PREFIX}{task_id}"


def image_queue_avoid_key(task_id: str) -> str:
    return f"{IMAGE_QUEUE_AVOID_PREFIX}{task_id}"


async def avoid_provider_for_task(
    redis: Any, task_id: str, provider_name: str, *, services: RunGenerationDeps
) -> None:
    if not provider_name:
        return
    try:
        key = image_queue_avoid_key(task_id)
        await redis.sadd(key, provider_name)
        await redis.expire(key, IMAGE_QUEUE_AVOID_TTL_S)
    except Exception:  # noqa: BLE001
        logger.debug("avoid_provider write failed", exc_info=True)


async def get_avoided_providers(
    redis: Any, task_id: str, *, services: RunGenerationDeps
) -> set[str]:
    try:
        raw = await redis.smembers(image_queue_avoid_key(task_id))
    except Exception:  # noqa: BLE001
        return set()
    return {name for item in raw or [] if (name := redis_text(item))}


async def clear_avoided_providers(
    redis: Any, task_id: str, *, services: RunGenerationDeps
) -> None:
    with suppress(Exception):
        await redis.delete(image_queue_avoid_key(task_id))


def image_inflight_key(task_id: str) -> str:
    return f"{IMAGE_INFLIGHT_PREFIX}{task_id}"


def classify_inflight_lane(route: str | None, endpoint: str | None) -> str:
    route_value = (route or "").lower()
    endpoint_value = (endpoint or "").lower()
    if route_value.startswith("image2"):
        return "lane_a"
    if route_value.startswith("responses"):
        return "lane_b"
    if route_value == "image_jobs":
        if endpoint_value.endswith(":generations"):
            return "lane_a"
        if endpoint_value.endswith(":responses"):
            return "lane_b"
    return "lane_a"


async def inflight_set_fields(
    redis: Any, task_id: str, fields: dict[str, str], *, services: RunGenerationDeps
) -> None:
    if not fields:
        return
    payload = {key: value for key, value in fields.items() if value not in (None, "")}
    if not payload:
        return
    payload["updated_at"] = str(int(time.time() * 1000))
    try:
        key = image_inflight_key(task_id)
        await redis.hset(key, mapping=payload)
        await redis.expire(key, GENERATION_LEASE_TTL_S * 4)
    except Exception:  # noqa: BLE001
        logger.debug("image_inflight write failed task=%s", task_id, exc_info=True)


async def inflight_clear(
    redis: Any, task_id: str, *, services: RunGenerationDeps
) -> None:
    with suppress(Exception):
        await redis.delete(image_inflight_key(task_id))


async def cleanup_image_queue_active(
    redis: Any,
    *,
    lock: ImageQueueLockLease | None = None,
    services: RunGenerationDeps,
) -> None:
    try:
        if lock is None:
            await redis.zremrangebyscore(
                IMAGE_QUEUE_ACTIVE_KEY,
                "-inf",
                time.time(),
            )
        else:
            await lock.eval_fenced(
                CLEANUP_IMAGE_QUEUE_ACTIVE_LUA,
                2,
                IMAGE_QUEUE_ACTIVE_KEY,
                IMAGE_QUEUE_LOCK_KEY,
                lock.token,
                str(time.time()),
                lost_result=-1,
            )
    except ImageQueueLockLost:
        raise
    except Exception:  # noqa: BLE001
        logger.debug("image queue active cleanup failed", exc_info=True)


async def active_image_provider_names(
    redis: Any, *, services: RunGenerationDeps
) -> set[str]:
    try:
        raw_names = await redis.zrange(IMAGE_QUEUE_ACTIVE_KEY, 0, -1)
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(
            "image queue active set unavailable",
            error_code=EC.LOCAL_QUEUE_FULL.value,
            status_code=None,
        ) from exc
    return {name for item in raw_names or [] if (name := redis_text(item))}


async def provider_active_count(
    redis: Any,
    provider_name: str,
    *,
    lock: ImageQueueLockLease | None = None,
    services: RunGenerationDeps,
) -> int | None:
    key = image_provider_active_key(provider_name)
    try:
        if lock is None:
            await redis.zremrangebyscore(key, "-inf", time.time())
            count = await redis.zcard(key)
        else:
            count = await lock.eval_fenced(
                CLEANUP_IMAGE_QUEUE_PROVIDER_LUA,
                2,
                key,
                IMAGE_QUEUE_LOCK_KEY,
                lock.token,
                str(time.time()),
                lost_result=-1,
                lose_on_error=False,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "image queue active_count failed provider=%s err=%s",
            provider_name,
            exc,
        )
        return None
    try:
        return int(count or 0)
    except (TypeError, ValueError):
        return 0


async def clear_stale_image_queue_reservation(
    redis: Any,
    lock: ImageQueueLockLease,
    *,
    task_id: str,
    provider_name: str,
    services: RunGenerationDeps,
) -> bool:
    """Clear one stale reservation only while this lock token still owns the fence."""
    provider_zset = image_provider_active_key(provider_name)
    active_member = provider_name if is_dual_race_sentinel(provider_name) else task_id
    result = await lock.eval_fenced(
        CLEAR_STALE_IMAGE_QUEUE_RESERVATION_LUA,
        4,
        provider_zset,
        IMAGE_QUEUE_ACTIVE_KEY,
        image_task_provider_key(task_id),
        IMAGE_QUEUE_LOCK_KEY,
        lock.token,
        provider_name,
        task_id,
        active_member,
        lost_result=-1,
    )
    return int(result or 0) == 1


async def queued_generation_ids(
    limit: int, *, services: RunGenerationDeps
) -> list[str]:
    async with services.store.session() as session:
        rows = (
            (
                await session.execute(
                    select(Generation.id)
                    .where(Generation.status == GenerationStatus.QUEUED.value)
                    .order_by(
                        Generation.created_at.asc(),
                        Generation.id.asc(),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [str(row) for row in rows]


def queue_lane_weight(lane: str | None, *, services: RunGenerationDeps) -> int:
    if not lane:
        return 1
    return max(1, int(IMAGE_QUEUE_LANE_WEIGHTS.get(lane, 1)))


def queue_lane_sort_key(
    lane: str, *, services: RunGenerationDeps
) -> tuple[int, int, str]:
    return (
        IMAGE_QUEUE_LANE_RANK.get(
            lane,
            len(IMAGE_QUEUE_LANE_RANK),
        ),
        -queue_lane_weight(lane, services=services),
        lane,
    )


def weighted_queue_lane_slots(
    lanes: list[str], *, services: RunGenerationDeps
) -> list[str]:
    ordered = sorted(
        lanes,
        key=lambda lane: queue_lane_sort_key(lane, services=services),
    )
    if not ordered:
        return []
    max_weight = max(queue_lane_weight(lane, services=services) for lane in ordered)
    slots: list[str] = []
    for level in range(max_weight):
        for lane in ordered:
            if queue_lane_weight(lane, services=services) > level:
                slots.append(lane)
    return slots


def queued_candidate_from_mapping(
    row: Any,
    *,
    default_id: str | None = None,
    services: RunGenerationDeps,
) -> QueuedGenerationCandidate:
    mapping = getattr(row, "_mapping", None)

    def value(name: str, default: Any = None) -> Any:
        if mapping is not None and name in mapping:
            return mapping[name]
        return getattr(row, name, default)

    generation_id = str(value("id", default_id or ""))
    metadata = generation_queue_metadata(
        upstream_request=value("upstream_request", None),
        action=value("action", None),
        size_requested=value("size_requested", None),
        mask_image_id=value("mask_image_id", None),
        created_at=value("created_at", None),
        upstream_pixels=value("upstream_pixels", None),
    )
    lane = str(metadata.get("queue_lane") or IMAGE_QUEUE_DEFAULT_LANE)
    return QueuedGenerationCandidate(
        id=generation_id,
        queue_lane=lane,
        size_bucket=metadata.get("size_bucket"),
        cost_class=metadata.get("cost_class"),
        created_at=value("created_at", None),
        attempt=int(value("attempt", 0) or 0),
    )


def fallback_queued_candidate(
    generation_id: str, *, services: RunGenerationDeps
) -> QueuedGenerationCandidate:
    return QueuedGenerationCandidate(id=str(generation_id))


async def queued_generation_candidates(
    limit: int,
    services: RunGenerationDeps,
) -> list[QueuedGenerationCandidate]:
    ids = await queued_generation_ids(limit, services=services)
    if not ids:
        return []
    try:
        async with services.store.session() as session:
            rows = (
                await session.execute(
                    select(
                        Generation.id,
                        Generation.upstream_request,
                        Generation.action,
                        Generation.size_requested,
                        Generation.mask_image_id,
                        Generation.created_at,
                        Generation.upstream_pixels,
                        Generation.attempt,
                    ).where(Generation.id.in_(ids))
                )
            ).all()
    except Exception as exc:  # noqa: BLE001
        logger.debug("image queue candidate enrichment failed: %s", exc)
        return [
            fallback_queued_candidate(generation_id, services=services)
            for generation_id in ids
        ]

    by_id: dict[str, QueuedGenerationCandidate] = {}
    for row in rows:
        candidate = queued_candidate_from_mapping(row, services=services)
        if candidate.id:
            by_id[candidate.id] = candidate
    return [
        by_id.get(
            str(generation_id),
            fallback_queued_candidate(str(generation_id), services=services),
        )
        for generation_id in ids
    ]


async def select_ready_generation_ids_by_lane(
    redis: Any,
    ready_by_lane: dict[str, list[QueuedGenerationCandidate]],
    limit: int,
    *,
    advance_cursor: bool = False,
    services: RunGenerationDeps,
) -> list[str]:
    slots = weighted_queue_lane_slots(list(ready_by_lane), services=services)
    if not slots:
        return []
    raw_cursor: str | None = None
    with suppress(Exception):
        raw_cursor = redis_text(await redis.get(IMAGE_QUEUE_LANE_CURSOR_KEY))
    try:
        cursor = int(raw_cursor) if raw_cursor else 0
    except ValueError:
        cursor = 0

    lane_queues = {
        lane: deque(candidates) for lane, candidates in ready_by_lane.items()
    }
    selected: list[str] = []
    remaining = sum(len(candidates) for candidates in lane_queues.values())
    while remaining > 0 and len(selected) < limit:
        lane = slots[cursor % len(slots)]
        cursor += 1
        lane_candidates = lane_queues.get(lane)
        if not lane_candidates:
            continue
        selected.append(lane_candidates.popleft().id)
        remaining -= 1

    if advance_cursor:
        with suppress(Exception):
            await redis.set(
                IMAGE_QUEUE_LANE_CURSOR_KEY,
                str(cursor),
                ex=3600,
            )
    return selected


async def advance_image_queue_lane_cursor(
    redis: Any,
    steps: int = 1,
    *,
    lock: ImageQueueLockLease | None = None,
    services: RunGenerationDeps,
) -> None:
    if steps <= 0:
        return
    if lock is not None:
        await lock.eval_fenced(
            ADVANCE_IMAGE_QUEUE_CURSOR_LUA,
            2,
            IMAGE_QUEUE_LANE_CURSOR_KEY,
            IMAGE_QUEUE_LOCK_KEY,
            lock.token,
            int(steps),
            lost_result=-1,
        )
        return
    with suppress(Exception):
        await redis.incrby(IMAGE_QUEUE_LANE_CURSOR_KEY, int(steps))
        await redis.expire(IMAGE_QUEUE_LANE_CURSOR_KEY, 3600)


async def ready_queued_generation_ids(
    redis: Any,
    limit: int,
    *,
    advance_cursor: bool = False,
    lock: ImageQueueLockLease | None = None,
    services: RunGenerationDeps,
) -> list[str]:
    candidates = await ready_queued_generation_candidates(
        redis,
        limit,
        advance_cursor=advance_cursor,
        lock=lock,
        services=services,
    )
    return [candidate.id for candidate in candidates]


async def _batch_not_before_values(
    redis: Any,
    candidates: list[QueuedGenerationCandidate],
) -> list[Any]:
    keys = [image_queue_not_before_key(candidate.id) for candidate in candidates]
    if not keys:
        return []
    mget = getattr(redis, "mget", None)
    if callable(mget):
        return list(await mget(keys))
    return [await redis.get(key) for key in keys]


async def ready_queued_generation_candidates(
    redis: Any,
    limit: int,
    *,
    advance_cursor: bool = False,
    lock: ImageQueueLockLease | None = None,
    services: RunGenerationDeps,
) -> list[QueuedGenerationCandidate]:
    scan_limit = min(
        IMAGE_QUEUE_FAIR_SCAN_LIMIT,
        max(limit, limit * 4),
    )
    candidates = await queued_generation_candidates(
        scan_limit,
        services,
    )
    if not candidates:
        return []
    ready_fifo: list[QueuedGenerationCandidate] = []
    ready_by_lane: dict[str, list[QueuedGenerationCandidate]] = {}
    now = time.time()
    now_mono = time.monotonic()
    active_members: set[str] = set()
    with suppress(UpstreamError):
        await cleanup_image_queue_active(redis, lock=lock, services=services)
    with suppress(UpstreamError):
        active_members = await active_image_provider_names(
            redis,
            services=services,
        )
    not_before_values = await _batch_not_before_values(redis, candidates)
    for candidate, raw_not_before_value in zip(
        candidates,
        not_before_values,
        strict=True,
    ):
        queued_id = candidate.id
        if (
            queued_id in active_members
            or dual_race_sentinel_name(queued_id) in active_members
        ):
            continue
        local_until = services.queue.provider_cooldowns.get(queued_id)
        if local_until is not None:
            if local_until > now_mono:
                continue
            services.queue.provider_cooldowns.pop(queued_id, None)
        not_before_key = image_queue_not_before_key(queued_id)
        raw_not_before = redis_text(raw_not_before_value)
        if raw_not_before:
            try:
                if float(raw_not_before) > now:
                    continue
            except ValueError:
                if lock is not None:
                    await lock.delete_if_owner(not_before_key)
                else:
                    with suppress(Exception):
                        await redis.delete(not_before_key)
        ready_fifo.append(candidate)
        ready_by_lane.setdefault(
            candidate.queue_lane or IMAGE_QUEUE_DEFAULT_LANE,
            [],
        ).append(candidate)
    if not ready_by_lane:
        return []
    if len(ready_by_lane) == 1:
        return ready_fifo[:limit]
    try:
        selected = await select_ready_generation_ids_by_lane(
            redis,
            {lane: list(values) for lane, values in ready_by_lane.items()},
            limit,
            advance_cursor=advance_cursor,
            services=services,
        )
        if selected:
            by_id = {candidate.id: candidate for candidate in ready_fifo}
            return [by_id[task_id] for task_id in selected]
    except Exception as exc:  # noqa: BLE001
        logger.warning("image queue weighted lane selection failed err=%s", exc)
    return ready_fifo[:limit]


async def enqueue_generation_once(
    redis: Any,
    task_id: str,
    *,
    attempt: int = 1,
    defer_by: int | float | None = None,
    job_try: int | None = None,
    replace_dispatch: DispatchIdentity | None = None,
    services: RunGenerationDeps,
) -> bool:
    try:
        result = await enqueue_generation_dispatch(
            redis,
            task_id=task_id,
            attempt=attempt,
            defer_by=defer_by,
            job_try=job_try,
            replace=replace_dispatch,
        )
        return result.accepted
    except Exception as exc:  # noqa: BLE001
        logger.warning("image queue enqueue failed task=%s err=%s", task_id, exc)
        return False


async def kick_image_queue(redis: Any, *, services: RunGenerationDeps) -> None:
    capacity = await resolve_image_queue_capacity(services=services)
    try:
        candidates = await ready_queued_generation_candidates(
            redis,
            max(1, capacity * 2),
            services=services,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("image queue kick scan failed err=%s", exc)
        return
    for candidate in candidates[: max(1, capacity * 2)]:
        await enqueue_generation_once(
            redis,
            candidate.id,
            attempt=candidate.attempt + 1,
            services=services,
        )


def dual_race_sentinel_name(task_id: str) -> str:
    return f"{DUAL_RACE_SENTINEL_PREFIX}{task_id}"


def is_dual_race_sentinel(name: str | None) -> bool:
    return bool(name and name.startswith(DUAL_RACE_SENTINEL_PREFIX))
