"""Idempotent cleanup for terminal image-queue sentinels."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from lumen_core.constants import GenerationStatus
from lumen_core.models import Generation

logger = logging.getLogger(__name__)

DUAL_RACE_SENTINEL_PREFIX = "__dr:"
IMAGE_QUEUE_ACTIVE_KEY = "generation:image_queue:active"
IMAGE_QUEUE_TASK_PROVIDER_PREFIX = "generation:image_queue:task_provider:"
_IMAGE_QUEUE_RESERVATION_PREFIX = "generation:image_queue:reservation:"
_CLEAR_TERMINAL_SENTINEL_LUA = """
local current_provider = redis.call('GET', KEYS[2])
if current_provider and current_provider ~= ARGV[1] then
  return 0
end
redis.call('ZREM', KEYS[1], ARGV[1])
if current_provider == ARGV[1] then
  redis.call('DEL', KEYS[2])
end
redis.call('DEL', KEYS[3])
redis.call('DEL', KEYS[4])
return 1
"""


async def _clear_terminal_sentinel(
    redis: Any,
    *,
    sentinel_name: str,
    task_id: str,
) -> bool:
    eval_fn = getattr(redis, "eval", None)
    if not callable(eval_fn):
        return False
    result = await eval_fn(
        _CLEAR_TERMINAL_SENTINEL_LUA,
        4,
        IMAGE_QUEUE_ACTIVE_KEY,
        f"{IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{task_id}",
        f"task:{task_id}:lease",
        f"{_IMAGE_QUEUE_RESERVATION_PREFIX}{task_id}",
        sentinel_name,
    )
    return int(result or 0) == 1


async def cleanup_terminal_sentinels(
    redis: Any,
    *,
    session_factory: Any,
    log: logging.Logger = logger,
) -> None:
    """Remove only dual-race sentinels whose database task is terminal."""
    try:
        raw_names = await redis.zrange(IMAGE_QUEUE_ACTIVE_KEY, 0, -1)
    except Exception:  # noqa: BLE001
        return
    sentinel_task_ids: list[tuple[str, str]] = []
    for raw in raw_names or []:
        name = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if name.startswith(DUAL_RACE_SENTINEL_PREFIX):
            sentinel_task_ids.append((name, name[len(DUAL_RACE_SENTINEL_PREFIX) :]))
    if not sentinel_task_ids:
        return
    terminal = {
        GenerationStatus.SUCCEEDED.value,
        GenerationStatus.FAILED.value,
        GenerationStatus.CANCELED.value,
    }
    cleared = 0
    for sentinel_name, task_id in sentinel_task_ids:
        async with session_factory() as session:
            generation = (
                await session.execute(
                    select(Generation).where(Generation.id == task_id).with_for_update()
                )
            ).scalar_one_or_none()
            status = getattr(generation, "status", None)
            if status not in terminal:
                continue
            try:
                removed = await _clear_terminal_sentinel(
                    redis,
                    sentinel_name=sentinel_name,
                    task_id=task_id,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "reconcile clear sentinel failed task=%s",
                    task_id,
                    exc_info=True,
                )
                continue
            if not removed:
                log.info(
                    "reconcile kept sentinel after ownership changed task=%s",
                    task_id,
                )
                continue
        cleared += 1
        log.info(
            "reconcile cleared terminal sentinel task=%s status=%s",
            task_id,
            status,
        )
    if cleared:
        log.info("reconcile cleared %d terminal sentinel(s)", cleared)
