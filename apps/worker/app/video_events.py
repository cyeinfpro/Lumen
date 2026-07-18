"""Video task event payloads and reliable delivery helpers."""

from __future__ import annotations

import logging
from typing import Any

from lumen_core.constants import task_channel
from lumen_core.models import OutboxEvent, VideoGeneration

from .sse_publish import publish_event


logger = logging.getLogger(__name__)


def video_event_data(
    generation: VideoGeneration,
    **extra: Any,
) -> dict[str, Any]:
    canonical = {
        "video_generation_id": generation.id,
        "kind": "video_generation",
        "status": generation.status,
        "stage": generation.progress_stage,
        "progress_pct": generation.progress_pct,
        "submission_epoch": int(getattr(generation, "submission_epoch", 0) or 0),
        "video_id": extra.pop("video_id", None),
        "error_code": generation.error_code,
        "error_message": generation.error_message,
    }
    canonical.update(
        {key: value for key, value in extra.items() if key not in canonical}
    )
    return canonical


def queue_video_event(
    session: Any,
    generation: VideoGeneration,
    event_name: str,
    **extra: Any,
) -> None:
    session.add(
        OutboxEvent(
            kind="sse",
            payload={
                "user_id": generation.user_id,
                "channel": task_channel(generation.id),
                "event_name": event_name,
                "data": video_event_data(generation, **extra),
            },
            published_at=None,
        )
    )


async def publish_video_event(
    redis: Any,
    generation: VideoGeneration,
    event_name: str,
    **extra: Any,
) -> None:
    await publish_event(
        redis,
        generation.user_id,
        task_channel(generation.id),
        event_name,
        video_event_data(generation, **extra),
    )


async def publish_video_event_after_commit(
    redis: Any,
    generation: VideoGeneration,
    event_name: str,
    **extra: Any,
) -> None:
    try:
        await publish_video_event(redis, generation, event_name, **extra)
    except Exception:
        logger.warning(
            "video post-commit publish failed task=%s event=%s",
            generation.id,
            event_name,
            exc_info=True,
        )
