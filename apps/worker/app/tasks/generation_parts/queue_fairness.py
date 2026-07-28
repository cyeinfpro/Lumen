"""Fair-order and stale-reservation decisions for generation admission."""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable

from .queue import (
    clear_stale_image_queue_reservation,
    image_provider_active_key,
    image_task_provider_key,
    is_dual_race_sentinel,
    redis_text,
)
from .services import RunGenerationDeps


ReadyCandidates = Callable[..., Awaitable[list[str]]]
ReservationKey = Callable[[str], str]
logger = logging.getLogger(__name__)


async def ready_queue_rank(
    redis: Any,
    lock: Any,
    *,
    task_id: str,
    fair_window: int,
    services: RunGenerationDeps,
    read_ready_candidates: ReadyCandidates,
) -> int | None:
    try:
        queued_ids = await read_ready_candidates(
            redis,
            fair_window,
            lock=lock,
            services=services,
        )
    except TypeError as exc:
        if "lock" not in str(exc):
            raise
        queued_ids = await read_ready_candidates(
            redis,
            fair_window,
            services=services,
        )
    return queued_ids.index(task_id) if task_id in queued_ids else None


async def existing_reservation_blocks_admission(
    redis: Any,
    lock: Any,
    *,
    task_id: str,
    active_members: set[str],
    services: RunGenerationDeps,
    reservation_key: ReservationKey,
) -> bool:
    provider_name = redis_text(await redis.get(image_task_provider_key(task_id)))
    if not provider_name:
        return False
    if is_dual_race_sentinel(provider_name):
        if provider_name in active_members:
            return True
        await clear_stale_reservation(
            redis,
            lock,
            task_id=task_id,
            provider_name=provider_name,
            services=services,
            reservation_key=reservation_key,
        )
        logger.info(
            "image queue cleared stale dual_race sentinel task=%s",
            task_id,
        )
        return False
    provider_zset = image_provider_active_key(provider_name)
    still_admitted = False
    with suppress(Exception):
        score = await redis.zscore(provider_zset, task_id)
        still_admitted = score is not None and float(score) > time.time()
    if still_admitted and task_id in active_members:
        return True
    await clear_stale_reservation(
        redis,
        lock,
        task_id=task_id,
        provider_name=provider_name,
        services=services,
        reservation_key=reservation_key,
    )
    logger.info(
        "image queue cleared stale self-lock task=%s provider=%s",
        task_id,
        provider_name,
    )
    return False


async def clear_stale_reservation(
    redis: Any,
    lock: Any,
    *,
    task_id: str,
    provider_name: str,
    services: RunGenerationDeps,
    reservation_key: ReservationKey,
) -> None:
    cleared = await clear_stale_image_queue_reservation(
        redis,
        lock,
        task_id=task_id,
        provider_name=provider_name,
        services=services,
    )
    if cleared:
        await lock.delete_if_owner(reservation_key(task_id))


__all__ = [
    "clear_stale_reservation",
    "existing_reservation_blocks_admission",
    "ready_queue_rank",
]
