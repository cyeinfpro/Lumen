"""Completion event staging and delivery adapters."""

from __future__ import annotations

from functools import partial
from typing import Any, Callable

from .. import tool_images


def build_event_hooks(**kwargs: Any) -> tool_images.CompletionEventHooks:
    return tool_images.CompletionEventHooks(**kwargs)


def bind_event_functions(
    hooks: tool_images.CompletionEventHooks,
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    return (
        partial(tool_images._stage_completion_event, hooks=hooks),
        partial(tool_images._publish_completion_event, hooks=hooks),
    )


async def deliver_completion_event(
    redis: Any,
    delivery: tuple[str, str, dict[str, Any]],
    *,
    deliver_staged_events: Callable[..., Any],
) -> None:
    await deliver_staged_events(redis, [delivery])


async def publish_tool_progress(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    tool_call: dict[str, Any],
    tool_calls: list[dict[str, Any]],
    publish_event: Callable[..., Any],
    progress_event: str,
) -> None:
    await publish_event(
        redis,
        user_id,
        channel,
        progress_event,
        {
            "completion_id": task_id,
            "message_id": message_id,
            "attempt": attempt,
            "attempt_epoch": attempt_epoch,
            "stage": "tool_call",
            "tool_call": tool_call,
            "tool_calls": tool_calls,
        },
    )


async def publish_tool_updates(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    tool_tracker: Any,
    updates: list[dict[str, Any]],
    publish_event: Callable[..., Any],
    publish_progress: Callable[..., Any],
    progress_event: str,
) -> None:
    tool_calls = tool_tracker.content()
    if len(updates) > 1:
        await publish_event(
            redis,
            user_id,
            channel,
            progress_event,
            {
                "completion_id": task_id,
                "message_id": message_id,
                "attempt": attempt,
                "attempt_epoch": attempt_epoch,
                "stage": "tool_call",
                "tool_call": updates[-1],
                "tool_call_updates": updates,
                "tool_calls": tool_calls,
            },
        )
        return
    for tool_call in updates:
        await publish_progress(
            redis=redis,
            user_id=user_id,
            channel=channel,
            task_id=task_id,
            message_id=message_id,
            attempt=attempt,
            attempt_epoch=attempt_epoch,
            tool_call=tool_call,
            tool_calls=tool_calls,
        )
