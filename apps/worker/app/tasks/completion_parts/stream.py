"""Completion stream cancellation and abort helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from lumen_core.chat_tools import ToolStatus
from lumen_core.constants import GenerationErrorCode as EC

from ...upstream import UpstreamCancelled, UpstreamError
from .tool_state import _CompletionToolTracker


_REASONING_DELTA_EVENT_TYPES = {
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.reasoning_summary.delta",
}


class _TaskCancelled(RuntimeError):
    """User cancellation signal handled as a terminal completion outcome."""


class _ToolIdleTimeout(RuntimeError):
    """No upstream events arrived while a tool call was active."""


class _LeaseLost(UpstreamCancelled):
    """Lease renewer gave up; this worker must stop before another attempt runs."""


@dataclass(frozen=True)
class CancellationCheckHooks:
    cancel_check_errors_total: Any
    logger: logging.Logger


def _extract_reasoning_delta(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    if event_type in _REASONING_DELTA_EVENT_TYPES:
        for key in ("delta", "text", "summary"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
    if event_type == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "reasoning":
            return _extract_reasoning_text_from_item(item)
    return ""


def _extract_reasoning_text_from_item(item: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("summary_text", "text"):
        value = item.get(key)
        if isinstance(value, str) and value:
            chunks.append(value)
    summary = item.get("summary")
    if isinstance(summary, str) and summary:
        chunks.append(summary)
    elif isinstance(summary, list):
        for part in summary:
            if isinstance(part, str) and part:
                chunks.append(part)
            elif isinstance(part, dict):
                for key in ("text", "summary_text"):
                    value = part.get(key)
                    if isinstance(value, str) and value:
                        chunks.append(value)
                        break
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                value = part.get("text")
                if isinstance(value, str) and value:
                    chunks.append(value)
    return "\n".join(chunks)


def _extract_reasoning_text_from_response(
    response: dict[str, Any] | None,
) -> str:
    if not isinstance(response, dict):
        return ""
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    chunks: list[str] = []
    for item in output:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            text = _extract_reasoning_text_from_item(item)
            if text:
                chunks.append(text)
    return "\n\n".join(chunks)


async def _is_cancelled(
    redis: Any,
    task_id: str,
    *,
    hooks: CancellationCheckHooks,
) -> bool:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            value = await redis.get(f"task:{task_id}:cancel")
            return bool(value)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(0.05 * (attempt + 1))
    hooks.cancel_check_errors_total.inc()
    hooks.logger.warning(
        "completion cancel check failed closed task=%s err=%s",
        task_id,
        last_exc,
    )
    return True


async def _raise_if_completion_cancelled(
    redis: Any,
    task_id: str,
    reason: str,
    *,
    is_cancelled: Callable[[Any, str], Awaitable[bool]],
) -> None:
    if await is_cancelled(redis, task_id):
        raise _TaskCancelled(reason)


def _raise_for_terminal_response_event(
    ev_type: str,
    resp: dict[str, Any],
    event_error: Any = None,
) -> None:
    _ = event_error
    terminal_status = (
        ToolStatus.CANCELLED.value
        if ev_type in {"response.cancelled", "response.canceled"}
        else ToolStatus.FAILED.value
    )
    error_code = (
        EC.CANCELLED.value
        if terminal_status == ToolStatus.CANCELLED.value
        else EC.BAD_RESPONSE.value
    )
    if terminal_status == ToolStatus.CANCELLED.value:
        raise _TaskCancelled("upstream response cancelled")
    raise UpstreamError(
        f"upstream {ev_type}",
        error_code=error_code,
        status_code=200,
        payload=resp or None,
    )


async def _watch_completion_cancel(
    redis: Any,
    task_id: str,
    *,
    cancel_requested: asyncio.Event,
    stop_requested: asyncio.Event,
    poll_interval_s: float,
    is_cancelled: Callable[[Any, str], Awaitable[bool]],
) -> None:
    while not stop_requested.is_set() and not cancel_requested.is_set():
        if await is_cancelled(redis, task_id):
            cancel_requested.set()
            return
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=poll_interval_s)
        except TimeoutError:
            continue


async def _next_completion_stream_event(
    stream: Any,
    *,
    cancel_requested: asyncio.Event,
    lease_lost: asyncio.Event,
    idle_timeout_s: float | None = None,
) -> dict[str, Any]:
    next_task = asyncio.create_task(anext(stream))
    cancel_task = asyncio.create_task(cancel_requested.wait())
    lease_task = asyncio.create_task(lease_lost.wait())
    timeout_task = (
        asyncio.create_task(asyncio.sleep(idle_timeout_s))
        if idle_timeout_s is not None and idle_timeout_s > 0
        else None
    )
    wait_for_tasks = {next_task, cancel_task, lease_task}
    if timeout_task is not None:
        wait_for_tasks.add(timeout_task)
    try:
        done, _pending = await asyncio.wait(
            wait_for_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if next_task in done:
            return await next_task

        next_task.cancel()
        with suppress(BaseException):
            await next_task
        with suppress(Exception):
            await stream.aclose()

        if cancel_requested.is_set():
            raise _TaskCancelled("cancelled during stream")
        if lease_lost.is_set():
            raise _LeaseLost("lease lost during stream")
        if timeout_task is not None and timeout_task in done:
            raise _ToolIdleTimeout("tool call idle timeout")
        raise UpstreamError(
            "completion stream aborted",
            error_code=EC.TEXT_STREAM_INTERRUPTED.value,
            status_code=None,
        )
    finally:
        for task in (cancel_task, lease_task, timeout_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(BaseException):
                    await task


async def _iter_completion_stream_with_abort(
    stream: Any,
    *,
    cancel_requested: asyncio.Event,
    lease_lost: asyncio.Event,
    tool_tracker: _CompletionToolTracker,
    tool_idle_timeout_s: float,
    next_event: Callable[..., Awaitable[dict[str, Any]]],
) -> Any:
    try:
        while True:
            try:
                yield await next_event(
                    stream,
                    cancel_requested=cancel_requested,
                    lease_lost=lease_lost,
                    idle_timeout_s=tool_tracker.idle_timeout_remaining(
                        tool_idle_timeout_s
                    ),
                )
            except StopAsyncIteration:
                break
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            with suppress(Exception):
                await close()
