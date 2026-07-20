"""Generation queue state cleanup shared by routes and services."""

from __future__ import annotations

from typing import Any


_IMAGE_QUEUE_ACTIVE_KEY = "generation:image_queue:active"
_IMAGE_QUEUE_TASK_PROVIDER_PREFIX = "generation:image_queue:task_provider:"
_IMAGE_QUEUE_PROVIDER_ACTIVE_PREFIX = "generation:image_queue:provider_active:"
_DUAL_RACE_SENTINEL_PREFIX = "__dr:"


def _task_provider_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{task_id}"


def _provider_active_key(provider_name: str) -> str:
    return f"{_IMAGE_QUEUE_PROVIDER_ACTIVE_PREFIX}{provider_name}"


def _redis_text(value: object) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return None


async def release_generation_queue_state(redis: Any, task_id: str) -> None:
    """Remove a generation from global/provider active sets and clear its lease."""
    task_provider_key = _task_provider_key(task_id)
    provider_name = _redis_text(await redis.get(task_provider_key))
    pipeline_fn = getattr(redis, "pipeline", None)
    pipeline = pipeline_fn(transaction=False) if callable(pipeline_fn) else None

    async def zrem(key: str, member: str) -> None:
        if pipeline is not None:
            pipeline.zrem(key, member)
        else:
            await redis.zrem(key, member)

    async def delete(key: str) -> None:
        if pipeline is not None:
            pipeline.delete(key)
        else:
            await redis.delete(key)

    if provider_name:
        if provider_name.startswith(_DUAL_RACE_SENTINEL_PREFIX):
            await zrem(_IMAGE_QUEUE_ACTIVE_KEY, provider_name)
        else:
            await zrem(_IMAGE_QUEUE_ACTIVE_KEY, task_id)
            await zrem(_provider_active_key(provider_name), task_id)
    else:
        await zrem(_IMAGE_QUEUE_ACTIVE_KEY, task_id)
    await delete(task_provider_key)
    await delete(f"task:{task_id}:lease")
    if pipeline is not None:
        await pipeline.execute()
