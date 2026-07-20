"""Keepalive wrapping for prompt enhancement SSE streams."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

logger = logging.getLogger(__name__)

_QueueItem = tuple[str, str | BaseException | None]


async def _pump_source(
    source: AsyncIterator[str],
    queue: asyncio.Queue[_QueueItem],
) -> None:
    try:
        async for chunk in source:
            await queue.put(("chunk", chunk))
        await queue.put(("done", None))
    except BaseException as exc:  # noqa: BLE001
        await queue.put(("error", exc))


async def _cancel_pump(pump_task: asyncio.Task[None]) -> None:
    pump_task.cancel()
    try:
        await pump_task
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("prompt enhance keepalive pump failed during cancellation")


async def _next_item(
    queue: asyncio.Queue[_QueueItem],
    *,
    interval_seconds: float,
) -> _QueueItem | None:
    try:
        return await asyncio.wait_for(queue.get(), timeout=interval_seconds)
    except asyncio.TimeoutError:
        return None


def _chunk_from_item(item: _QueueItem) -> str | None:
    kind, payload = item
    if kind == "chunk":
        if not isinstance(payload, str):
            raise RuntimeError("prompt enhance stream emitted non-text chunk")
        return payload
    if kind == "done":
        return None
    if isinstance(payload, BaseException):
        raise payload
    return None


async def stream_with_keepalive(
    source: AsyncIterator[str],
    *,
    interval_seconds: float,
    keepalive_chunk: str,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
    pump_task = asyncio.create_task(_pump_source(source, queue))
    try:
        yield keepalive_chunk
        while True:
            item = await _next_item(queue, interval_seconds=interval_seconds)
            if item is None:
                yield keepalive_chunk
                continue
            chunk = _chunk_from_item(item)
            if chunk is None:
                return
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        await _cancel_pump(pump_task)
        raise
    finally:
        if not pump_task.done():
            await _cancel_pump(pump_task)
