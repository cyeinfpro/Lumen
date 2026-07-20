"""Storyboard image task publication and outbox acknowledgement."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import EV_GEN_QUEUED, task_channel

from ...arq_pool import get_arq_pool
from ...redis_client import get_redis
from ...services.message_submission import publish_message_appended
from ...sse_publish import publish_sse_event
from .common import STORYBOARD_KEYFRAME_PARALLELISM
from .contracts import StoryboardImageTask


logger = logging.getLogger(__name__)


async def publish_storyboard_image_task(
    *,
    db: AsyncSession,
    user_id: str,
    task: StoryboardImageTask,
    enqueue_fn: Callable[..., Awaitable[bool]] | None = None,
    mark_published_fn: Callable[..., Awaitable[None]] | None = None,
) -> None:
    enqueue = enqueue_fn or enqueue_storyboard_image_task
    mark_published = mark_published_fn or mark_storyboard_image_tasks_published
    if await enqueue(user_id=user_id, task=task):
        await mark_published(db, [task])


async def publish_storyboard_image_tasks(
    *,
    db: AsyncSession,
    user_id: str,
    tasks: list[StoryboardImageTask],
    enqueue_fn: Callable[..., Awaitable[bool]] | None = None,
    mark_published_fn: Callable[..., Awaitable[None]] | None = None,
) -> None:
    enqueue = enqueue_fn or enqueue_storyboard_image_task
    mark_published = mark_published_fn or mark_storyboard_image_tasks_published
    semaphore = asyncio.Semaphore(STORYBOARD_KEYFRAME_PARALLELISM)

    async def publish_one(task: StoryboardImageTask) -> bool:
        async with semaphore:
            return await enqueue(user_id=user_id, task=task)

    results = await asyncio.gather(
        *(publish_one(task) for task in tasks),
        return_exceptions=True,
    )
    published = [
        task for task, result in zip(tasks, results, strict=False) if result is True
    ]
    for result in results:
        if isinstance(result, Exception):
            logger.warning("storyboard image task publish failed: %s", result)
    if published:
        await mark_published(db, published)


async def enqueue_storyboard_image_task(
    *,
    user_id: str,
    task: StoryboardImageTask,
    redis_factory: Callable[[], Any] = get_redis,
    pool_factory: Callable[[], Awaitable[Any]] = get_arq_pool,
    publish_message_fn: Callable[..., Awaitable[None]] = publish_message_appended,
    publish_sse_fn: Callable[..., Awaitable[None]] = publish_sse_event,
) -> bool:
    redis = redis_factory()
    try:
        await publish_message_fn(
            redis=redis,
            user_id=user_id,
            conv_id=task.conversation_id,
            message_ids=[task.user_message_id, task.assistant_message_id],
        )
        pool = await pool_factory()
        for payload in task.outbox_payloads:
            enqueue_kwargs: dict[str, Any] = {}
            defer_s = payload.get("defer_s")
            if isinstance(defer_s, (int, float)) and defer_s > 0:
                enqueue_kwargs["_defer_by"] = float(defer_s)
            enqueue_kwargs["_job_id"] = arq_job_id(
                str(payload["kind"]),
                str(payload["task_id"]),
                payload.get("outbox_id"),
            )
            await pool.enqueue_job(
                "run_generation",
                payload["task_id"],
                **enqueue_kwargs,
            )
            event_data: dict[str, Any] = {
                "generation_id": payload["task_id"],
                "message_id": task.assistant_message_id,
                "conversation_id": task.conversation_id,
                "kind": payload["kind"],
            }
            for key in ("trace_id", "source", "action_source"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    event_data[key] = value
            input_images = payload.get("input_images")
            if isinstance(input_images, list):
                event_data["input_images"] = input_images
            await publish_sse_fn(
                redis,
                user_id=user_id,
                channel=task_channel(str(payload["task_id"])),
                event_name=EV_GEN_QUEUED,
                data=event_data,
            )
    except Exception:
        logger.warning(
            "storyboard image task enqueue failed user=%s conv=%s msg=%s",
            user_id,
            task.conversation_id,
            task.assistant_message_id,
            exc_info=True,
        )
        return False
    return True


async def mark_storyboard_image_tasks_published(
    db: AsyncSession,
    tasks: list[StoryboardImageTask],
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> None:
    try:
        now = now_fn()
        for task in tasks:
            for row in task.outbox_rows:
                row.published_at = now
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.warning("storyboard outbox rollback failed", exc_info=True)
        logger.warning("storyboard outbox mark-published failed", exc_info=True)
