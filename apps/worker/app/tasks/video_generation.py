"""Public ARQ and cron entrypoints for video generation."""

from __future__ import annotations

from typing import Any

from arq.cron import cron

from .video_generation_parts.default_runtime import DEFAULT_VIDEO_GENERATION_RUNTIME
from .video_generation_parts.runtime import VideoGenerationRuntime


class _CronJobs(tuple[Any, ...]):
    """Immutable schedules that still compose with legacy list-based schedules."""

    def __radd__(self, other: list[Any]) -> list[Any]:
        return [*other, *self]


def _runtime(ctx: dict[str, Any]) -> VideoGenerationRuntime:
    runtime = ctx.get("video_generation_runtime", DEFAULT_VIDEO_GENERATION_RUNTIME)
    if not isinstance(runtime, VideoGenerationRuntime):
        raise TypeError(
            "ctx['video_generation_runtime'] must be VideoGenerationRuntime"
        )
    return runtime


async def run_video_generation(ctx: dict[str, Any], task_id: str) -> None:
    await _runtime(ctx).run_submission(ctx, task_id)


async def run_video_poll(ctx: dict[str, Any], task_id: str) -> None:
    await _runtime(ctx).run_poll(ctx, task_id)


async def reconcile_video_tasks(ctx: dict[str, Any]) -> int:
    return await _runtime(ctx).reconcile(ctx)


cron_jobs = _CronJobs(
    (cron(reconcile_video_tasks, second={15, 45}, run_at_startup=False),)
)

__all__ = [
    "cron_jobs",
    "reconcile_video_tasks",
    "run_video_generation",
    "run_video_poll",
]
