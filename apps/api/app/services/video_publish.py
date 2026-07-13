"""Best-effort publication for newly queued video generations."""

from __future__ import annotations

import logging
from typing import Any

from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    EV_VIDEO_QUEUED,
    VideoGenerationStage,
    VideoGenerationStatus,
    task_channel,
)

from ..arq_pool import get_arq_pool
from ..observability import task_publish_errors_total
from ..redis_client import get_redis
from ..sse_publish import publish_sse_event

logger = logging.getLogger(__name__)


async def publish_video_queued(payload: dict[str, Any]) -> None:
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "run_video_generation",
            payload["task_id"],
            _job_id=arq_job_id(
                "video_generation",
                payload["task_id"],
                payload.get("outbox_id"),
            ),
        )
        await publish_sse_event(
            get_redis(),
            user_id=payload["user_id"],
            channel=task_channel(payload["task_id"]),
            event_name=EV_VIDEO_QUEUED,
            data={
                "video_generation_id": payload["task_id"],
                "kind": "video_generation",
                "status": VideoGenerationStatus.QUEUED.value,
                "stage": VideoGenerationStage.QUEUED.value,
                "progress_pct": 0,
                "submission_epoch": 0,
                "video_id": None,
                "error_code": None,
            },
        )
    except Exception:
        task_publish_errors_total.labels(kind="video_generation").inc()
        logger.warning(
            "best-effort video queued publish failed task_id=%s",
            payload.get("task_id"),
            exc_info=True,
        )


__all__ = ["publish_video_queued"]
