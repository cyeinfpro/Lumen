"""Post-commit storyboard delivery and outbox acknowledgement."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ...redis_client import get_redis
from ...services.storyboard import tasks as storyboard_tasks
from ...services.storyboard.common import storyboard_channel
from ...services.storyboard.contracts import StoryboardImageTask
from ...sse_publish import publish_sse_event


logger = logging.getLogger(__name__)


async def publish_storyboard_event(
    user_id: str,
    run_id: str,
    event_name: str,
    data: dict[str, object],
) -> None:
    try:
        await publish_sse_event(
            get_redis(),
            user_id=user_id,
            channel=storyboard_channel(run_id),
            event_name=event_name,
            data={"storyboard_id": run_id, **data},
        )
    except Exception:
        logger.warning(
            "storyboard SSE publish failed user=%s run=%s event=%s",
            user_id,
            run_id,
            event_name,
            exc_info=True,
        )


async def publish_storyboard_image_task(
    *,
    db: AsyncSession,
    user_id: str,
    task: StoryboardImageTask,
) -> None:
    await storyboard_tasks.publish_storyboard_image_task(
        db=db,
        user_id=user_id,
        task=task,
    )


async def publish_storyboard_image_tasks(
    *,
    db: AsyncSession,
    user_id: str,
    tasks: list[StoryboardImageTask],
) -> None:
    await storyboard_tasks.publish_storyboard_image_tasks(
        db=db,
        user_id=user_id,
        tasks=tasks,
    )


enqueue_storyboard_image_task = storyboard_tasks.enqueue_storyboard_image_task
mark_storyboard_image_tasks_published = (
    storyboard_tasks.mark_storyboard_image_tasks_published
)
