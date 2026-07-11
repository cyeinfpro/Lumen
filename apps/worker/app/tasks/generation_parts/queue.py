from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ._facade import GenerationFacade

_g = GenerationFacade()
bind_generation_facade = _g.bind

IMAGE_QUEUE_LOCK_KEY = "generation:image_queue:lock"
IMAGE_QUEUE_ACTIVE_KEY = "generation:image_queue:active"
IMAGE_QUEUE_PROVIDER_LOCK_PREFIX = "generation:image_queue:provider:"
IMAGE_QUEUE_TASK_PROVIDER_PREFIX = "generation:image_queue:task_provider:"
IMAGE_QUEUE_ENQUEUE_DEDUPE_PREFIX = "generation:image_queue:enqueue:"
IMAGE_QUEUE_NOT_BEFORE_PREFIX = "generation:image_queue:not_before:"
IMAGE_QUEUE_AVOID_PREFIX = "generation:image_queue:avoid:"
IMAGE_QUEUE_LANE_CURSOR_KEY = "generation:image_queue:lane_cursor"
IMAGE_INFLIGHT_PREFIX = "generation:image_inflight:"
IMAGE_QUEUE_LOCK_TTL_S = 10
IMAGE_QUEUE_LOCK_WAIT_S = 5.0
IMAGE_QUEUE_SCAN_LIMIT = 100
IMAGE_QUEUE_FAIR_SCAN_LIMIT = 1000
IMAGE_QUEUE_ENQUEUE_DEDUPE_TTL_S = 30
IMAGE_QUEUE_NOT_BEFORE_GRACE_S = 600
IMAGE_PROVIDER_UNAVAILABLE_RETRY_S = 30
IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S = 5.0
PROVIDER_COOLDOWN_LOCAL: dict[str, float] = {}
IMAGE_QUEUE_AVOID_TTL_S = 120
IMAGE_QUEUE_DEFAULT_LANE = "image:interactive:unknown"
IMAGE_QUEUE_LANE_WEIGHTS: dict[str, int] = {
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
IMAGE_QUEUE_LANE_ORDER: tuple[str, ...] = tuple(IMAGE_QUEUE_LANE_WEIGHTS)
IMAGE_QUEUE_LANE_RANK: dict[str, int] = {
    lane: idx for idx, lane in enumerate(IMAGE_QUEUE_LANE_ORDER)
}
IMAGE_GENERATION_CONCURRENCY_SETTING = "image.generation_concurrency"


@dataclass(frozen=True)
class QueuedGenerationCandidate:
    id: str
    queue_lane: str = IMAGE_QUEUE_DEFAULT_LANE
    size_bucket: str | None = None
    cost_class: str | None = None
    created_at: datetime | None = None


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


def image_queue_capacity() -> int:
    return _g._coerce_image_queue_capacity(
        getattr(_g.settings, "image_generation_concurrency", 4)
    )


async def resolve_image_queue_capacity() -> int:
    try:
        raw = await _g.runtime_settings.resolve(
            _g._IMAGE_GENERATION_CONCURRENCY_SETTING
        )
    except Exception as exc:  # noqa: BLE001
        _g.logger.warning("image queue capacity resolve failed err=%s", exc)
        return _g._image_queue_capacity()
    if raw is None:
        return _g._image_queue_capacity()
    return _g._coerce_image_queue_capacity(raw)


def image_provider_lock_key(provider_name: str) -> str:
    return f"{_g._IMAGE_QUEUE_PROVIDER_LOCK_PREFIX}{provider_name}"


def image_provider_active_key(provider_name: str) -> str:
    return f"generation:image_queue:provider_active:{provider_name}"


def image_task_provider_key(task_id: str) -> str:
    return f"{_g._IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{task_id}"


def image_queue_enqueue_dedupe_key(task_id: str) -> str:
    return f"{_g._IMAGE_QUEUE_ENQUEUE_DEDUPE_PREFIX}{task_id}"


def image_queue_not_before_key(task_id: str) -> str:
    return f"{_g._IMAGE_QUEUE_NOT_BEFORE_PREFIX}{task_id}"


def image_queue_avoid_key(task_id: str) -> str:
    return f"{_g._IMAGE_QUEUE_AVOID_PREFIX}{task_id}"


async def avoid_provider_for_task(redis: Any, task_id: str, provider_name: str) -> None:
    if not provider_name:
        return
    try:
        key = _g._image_queue_avoid_key(task_id)
        await redis.sadd(key, provider_name)
        await redis.expire(key, _g._IMAGE_QUEUE_AVOID_TTL_S)
    except Exception:  # noqa: BLE001
        _g.logger.debug("avoid_provider write failed", exc_info=True)


async def get_avoided_providers(redis: Any, task_id: str) -> set[str]:
    try:
        raw = await redis.smembers(_g._image_queue_avoid_key(task_id))
    except Exception:  # noqa: BLE001
        return set()
    return {name for item in raw or [] if (name := _g._redis_text(item))}


async def clear_avoided_providers(redis: Any, task_id: str) -> None:
    with suppress(Exception):
        await redis.delete(_g._image_queue_avoid_key(task_id))


def image_inflight_key(task_id: str) -> str:
    return f"{_g._IMAGE_INFLIGHT_PREFIX}{task_id}"


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


async def inflight_set_fields(redis: Any, task_id: str, fields: dict[str, str]) -> None:
    if not fields:
        return
    payload = {key: value for key, value in fields.items() if value not in (None, "")}
    if not payload:
        return
    payload["updated_at"] = str(int(time.time() * 1000))
    try:
        key = _g._image_inflight_key(task_id)
        await redis.hset(key, mapping=payload)
        await redis.expire(key, _g._LEASE_TTL_S * 4)
    except Exception:  # noqa: BLE001
        _g.logger.debug("image_inflight write failed task=%s", task_id, exc_info=True)


async def inflight_clear(redis: Any, task_id: str) -> None:
    with suppress(Exception):
        await redis.delete(_g._image_inflight_key(task_id))


@asynccontextmanager
async def image_queue_lock(redis: Any) -> AsyncIterator[None]:
    token = _g.new_uuid7()
    deadline = asyncio.get_event_loop().time() + _g._IMAGE_QUEUE_LOCK_WAIT_S
    while True:
        got = await redis.set(
            _g._IMAGE_QUEUE_LOCK_KEY,
            token,
            nx=True,
            ex=_g._IMAGE_QUEUE_LOCK_TTL_S,
        )
        if got:
            break
        if asyncio.get_event_loop().time() >= deadline:
            raise _g.UpstreamError(
                "image queue scheduler busy",
                error_code=_g.EC.LOCAL_QUEUE_FULL.value,
                status_code=None,
            )
        await asyncio.sleep(0.05)

    try:
        yield
    finally:
        try:
            eval_fn = getattr(redis, "eval", None)
            if callable(eval_fn):
                await eval_fn(
                    _g._RELEASE_LEASE_LUA,
                    1,
                    _g._IMAGE_QUEUE_LOCK_KEY,
                    token,
                )
            else:
                current = _g._redis_text(await redis.get(_g._IMAGE_QUEUE_LOCK_KEY))
                if current == token:
                    await redis.delete(_g._IMAGE_QUEUE_LOCK_KEY)
        except Exception:  # noqa: BLE001
            _g.logger.warning("image queue lock release failed", exc_info=True)


async def cleanup_image_queue_active(redis: Any) -> None:
    try:
        await redis.zremrangebyscore(
            _g._IMAGE_QUEUE_ACTIVE_KEY,
            "-inf",
            time.time(),
        )
    except Exception:  # noqa: BLE001
        _g.logger.debug("image queue active cleanup failed", exc_info=True)


async def active_image_provider_names(redis: Any) -> set[str]:
    try:
        raw_names = await redis.zrange(_g._IMAGE_QUEUE_ACTIVE_KEY, 0, -1)
    except Exception as exc:  # noqa: BLE001
        raise _g.UpstreamError(
            "image queue active set unavailable",
            error_code=_g.EC.LOCAL_QUEUE_FULL.value,
            status_code=None,
        ) from exc
    return {name for item in raw_names or [] if (name := _g._redis_text(item))}


async def provider_active_count(redis: Any, provider_name: str) -> int | None:
    key = _g._image_provider_active_key(provider_name)
    try:
        await redis.zremrangebyscore(key, "-inf", time.time())
        count = await redis.zcard(key)
    except Exception as exc:  # noqa: BLE001
        _g.logger.warning(
            "image queue active_count failed provider=%s err=%s",
            provider_name,
            exc,
        )
        return None
    try:
        return int(count or 0)
    except (TypeError, ValueError):
        return 0


async def queued_generation_ids(limit: int) -> list[str]:
    async with _g.SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    _g.select(_g.Generation.id)
                    .where(_g.Generation.status == _g.GenerationStatus.QUEUED.value)
                    .order_by(
                        _g.Generation.created_at.asc(),
                        _g.Generation.id.asc(),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [str(row) for row in rows]


def queue_lane_weight(lane: str | None) -> int:
    if not lane:
        return 1
    return max(1, int(_g._IMAGE_QUEUE_LANE_WEIGHTS.get(lane, 1)))


def queue_lane_sort_key(lane: str) -> tuple[int, int, str]:
    return (
        _g._IMAGE_QUEUE_LANE_RANK.get(
            lane,
            len(_g._IMAGE_QUEUE_LANE_RANK),
        ),
        -_g._queue_lane_weight(lane),
        lane,
    )


def weighted_queue_lane_slots(lanes: list[str]) -> list[str]:
    ordered = sorted(lanes, key=_g._queue_lane_sort_key)
    if not ordered:
        return []
    max_weight = max(_g._queue_lane_weight(lane) for lane in ordered)
    slots: list[str] = []
    for level in range(max_weight):
        for lane in ordered:
            if _g._queue_lane_weight(lane) > level:
                slots.append(lane)
    return slots


def queued_candidate_from_mapping(
    row: Any,
    *,
    default_id: str | None = None,
) -> QueuedGenerationCandidate:
    mapping = getattr(row, "_mapping", None)

    def value(name: str, default: Any = None) -> Any:
        if mapping is not None and name in mapping:
            return mapping[name]
        return getattr(row, name, default)

    generation_id = str(value("id", default_id or ""))
    metadata = _g.generation_queue_metadata(
        upstream_request=value("upstream_request", None),
        action=value("action", None),
        size_requested=value("size_requested", None),
        mask_image_id=value("mask_image_id", None),
        created_at=value("created_at", None),
        upstream_pixels=value("upstream_pixels", None),
    )
    lane = str(metadata.get("queue_lane") or _g._IMAGE_QUEUE_DEFAULT_LANE)
    return _g._QueuedGenerationCandidate(
        id=generation_id,
        queue_lane=lane,
        size_bucket=metadata.get("size_bucket"),
        cost_class=metadata.get("cost_class"),
        created_at=value("created_at", None),
    )


def fallback_queued_candidate(generation_id: str) -> QueuedGenerationCandidate:
    return _g._QueuedGenerationCandidate(id=str(generation_id))


async def queued_generation_candidates(
    limit: int,
) -> list[QueuedGenerationCandidate]:
    ids = await _g._queued_generation_ids(limit)
    if not ids:
        return []
    try:
        async with _g.SessionLocal() as session:
            rows = (
                await session.execute(
                    _g.select(
                        _g.Generation.id,
                        _g.Generation.upstream_request,
                        _g.Generation.action,
                        _g.Generation.size_requested,
                        _g.Generation.mask_image_id,
                        _g.Generation.created_at,
                        _g.Generation.upstream_pixels,
                    ).where(_g.Generation.id.in_(ids))
                )
            ).all()
    except Exception as exc:  # noqa: BLE001
        _g.logger.debug("image queue candidate enrichment failed: %s", exc)
        return [_g._fallback_queued_candidate(generation_id) for generation_id in ids]

    by_id: dict[str, QueuedGenerationCandidate] = {}
    for row in rows:
        candidate = _g._queued_candidate_from_mapping(row)
        if candidate.id:
            by_id[candidate.id] = candidate
    return [
        by_id.get(
            str(generation_id),
            _g._fallback_queued_candidate(str(generation_id)),
        )
        for generation_id in ids
    ]


async def select_ready_generation_ids_by_lane(
    redis: Any,
    ready_by_lane: dict[str, list[QueuedGenerationCandidate]],
    limit: int,
    *,
    advance_cursor: bool = False,
) -> list[str]:
    slots = _g._weighted_queue_lane_slots(list(ready_by_lane))
    if not slots:
        return []
    raw_cursor: str | None = None
    with suppress(Exception):
        raw_cursor = _g._redis_text(await redis.get(_g._IMAGE_QUEUE_LANE_CURSOR_KEY))
    try:
        cursor = int(raw_cursor) if raw_cursor else 0
    except ValueError:
        cursor = 0

    selected: list[str] = []
    remaining = sum(len(candidates) for candidates in ready_by_lane.values())
    while remaining > 0 and len(selected) < limit:
        lane = slots[cursor % len(slots)]
        cursor += 1
        lane_candidates = ready_by_lane.get(lane)
        if not lane_candidates:
            continue
        selected.append(lane_candidates.pop(0).id)
        remaining -= 1

    if advance_cursor:
        with suppress(Exception):
            await redis.set(
                _g._IMAGE_QUEUE_LANE_CURSOR_KEY,
                str(cursor),
                ex=3600,
            )
    return selected


async def advance_image_queue_lane_cursor(redis: Any, steps: int = 1) -> None:
    if steps <= 0:
        return
    with suppress(Exception):
        await redis.incrby(_g._IMAGE_QUEUE_LANE_CURSOR_KEY, int(steps))
        await redis.expire(_g._IMAGE_QUEUE_LANE_CURSOR_KEY, 3600)


async def ready_queued_generation_ids(
    redis: Any,
    limit: int,
    *,
    advance_cursor: bool = False,
) -> list[str]:
    candidates = await _g._queued_generation_candidates(
        max(limit, _g._IMAGE_QUEUE_FAIR_SCAN_LIMIT)
    )
    if not candidates:
        return []
    ready_fifo: list[str] = []
    ready_by_lane: dict[str, list[QueuedGenerationCandidate]] = {}
    now = time.time()
    now_mono = time.monotonic()
    active_members: set[str] = set()
    with suppress(_g.UpstreamError):
        await _g._cleanup_image_queue_active(redis)
    with suppress(_g.UpstreamError):
        active_members = await _g._active_image_provider_names(redis)
    for candidate in candidates:
        queued_id = candidate.id
        if (
            queued_id in active_members
            or _g._dual_race_sentinel_name(queued_id) in active_members
        ):
            continue
        local_until = _g._PROVIDER_COOLDOWN_LOCAL.get(queued_id)
        if local_until is not None:
            if local_until > now_mono:
                continue
            _g._PROVIDER_COOLDOWN_LOCAL.pop(queued_id, None)
        not_before_key = _g._image_queue_not_before_key(queued_id)
        raw_not_before = _g._redis_text(await redis.get(not_before_key))
        if raw_not_before:
            try:
                if float(raw_not_before) > now:
                    continue
            except ValueError:
                with suppress(Exception):
                    await redis.delete(not_before_key)
        ready_fifo.append(queued_id)
        ready_by_lane.setdefault(
            candidate.queue_lane or _g._IMAGE_QUEUE_DEFAULT_LANE,
            [],
        ).append(candidate)
    if not ready_by_lane:
        return []
    if len(ready_by_lane) == 1:
        return ready_fifo[:limit]
    try:
        selected = await _g._select_ready_generation_ids_by_lane(
            redis,
            {lane: list(values) for lane, values in ready_by_lane.items()},
            limit,
            advance_cursor=advance_cursor,
        )
        if selected:
            return selected
    except Exception as exc:  # noqa: BLE001
        _g.logger.warning("image queue weighted lane selection failed err=%s", exc)
    return ready_fifo[:limit]


async def enqueue_generation_once(
    redis: Any,
    task_id: str,
    *,
    defer_by: int | float | None = None,
    job_try: int | None = None,
) -> bool:
    dedupe_key = _g._image_queue_enqueue_dedupe_key(task_id)
    try:
        first = await redis.set(
            dedupe_key,
            "1",
            nx=True,
            ex=_g._IMAGE_QUEUE_ENQUEUE_DEDUPE_TTL_S,
        )
        if not first:
            return False
        kwargs: dict[str, Any] = {}
        if defer_by is not None and defer_by > 0:
            kwargs["_defer_by"] = defer_by
        if job_try is not None:
            kwargs["_job_try"] = job_try
        await redis.enqueue_job("run_generation", task_id, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001
        with suppress(Exception):
            await redis.delete(dedupe_key)
        _g.logger.warning("image queue enqueue failed task=%s err=%s", task_id, exc)
        return False


async def clear_image_queue_enqueue_dedupe(redis: Any, task_id: str) -> None:
    with suppress(Exception):
        await redis.delete(_g._image_queue_enqueue_dedupe_key(task_id))


async def kick_image_queue(redis: Any) -> None:
    capacity = await _g._resolve_image_queue_capacity()
    try:
        ids = await _g._ready_queued_generation_ids(
            redis,
            max(_g._IMAGE_QUEUE_SCAN_LIMIT, capacity * 2),
        )
    except Exception as exc:  # noqa: BLE001
        _g.logger.warning("image queue kick scan failed err=%s", exc)
        return
    for queued_id in ids[: max(1, capacity * 2)]:
        await _g._enqueue_generation_once(redis, queued_id)
