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
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Generation.id, Generation.status).where(
                        Generation.id.in_([task_id for _, task_id in sentinel_task_ids])
                    )
                )
            ).all()
        )
    status_by_id = {row.id: row.status for row in rows}
    cleared = 0
    for sentinel_name, task_id in sentinel_task_ids:
        status = status_by_id.get(task_id)
        if status not in terminal:
            continue
        try:
            await redis.zrem(IMAGE_QUEUE_ACTIVE_KEY, sentinel_name)
            await redis.delete(f"{IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{task_id}")
            await redis.delete(f"task:{task_id}:lease")
        except Exception:  # noqa: BLE001
            log.warning(
                "reconcile clear sentinel failed task=%s",
                task_id,
                exc_info=True,
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
