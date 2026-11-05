"""Completion stream output, including private SVG text isolation."""

from __future__ import annotations

from typing import Any

from lumen_core.constants import EV_COMP_DELTA, EV_COMP_THINKING_DELTA

from ...reconciliation.task_domains import completion_execution_epoch as _completion_execution_epoch
from .execution import CompletionExecution


def completion_event_payload(state: CompletionExecution, **extra: Any) -> dict[str, Any]:
    return {
        "completion_id": state.request.task_id,
        "message_id": state.preparation.message_id,
        "attempt": state.preparation.attempt,
        "attempt_epoch": state.preparation.attempt_epoch,
        "execution_epoch": _completion_execution_epoch(state),
        **extra,
    }


async def publish_completion_thinking(
    state: CompletionExecution,
    text: str,
) -> None:
    if getattr(state.preparation, "artifact_format", None) == "svg":
        return
    if not text:
        return
    if state.streaming.accumulated_thinking.endswith(text):
        return
    state.streaming.accumulated_thinking += text
    await state.ports.events.publish_event(
        state.request.redis,
        state.preparation.user_id,
        state.request.channel,
        EV_COMP_THINKING_DELTA,
        completion_event_payload(state, thinking_delta=text),
    )


async def handle_completion_delta(
    state: CompletionExecution,
    event: dict[str, Any],
    *,
    phase: str,
) -> None:
    delta = event.get("delta") or ""
    if not delta:
        return
    state.streaming.has_partial = True
    state.streaming.accumulated_text += delta
    state.usage.delta_counter += 1
    if state.usage.delta_counter % state.ports.retry._CANCEL_CHECK_EVERY_DELTAS == 0:
        if state.settlement.lease_lost.is_set():
            raise state.ports.retry._LeaseLost(f"lease lost during {phase} stream")
        if await state.ports.retry._is_cancelled(
            state.request.redis, state.request.task_id
        ):
            raise state.ports.retry._TaskCancelled(f"cancelled during {phase} stream")
    if getattr(state.preparation, "artifact_format", None) == "svg":
        # Unvalidated XML never enters ordinary text/thinking SSE or Markdown.
        if len(state.streaming.accumulated_text.encode("utf-8")) > 1024 * 1024:
            state.streaming.accumulated_text = state.streaming.accumulated_text.encode("utf-8")[:1024 * 1024].decode("utf-8", errors="ignore")
        return
    total_len = len(state.streaming.accumulated_text)
    if (
        total_len - state.streaming.flushed_len
        >= state.ports.retry._PG_FLUSH_EVERY_CHARS
    ):
        state.streaming.flushed_len = total_len
        await state.ports.persistence._flush_completion_text(
            state.request.task_id,
            state.streaming.accumulated_text,
            attempt_epoch=state.preparation.attempt_epoch,
        )
    await state.ports.events.publish_event(
        state.request.redis,
        state.preparation.user_id,
        state.request.channel,
        EV_COMP_DELTA,
        completion_event_payload(state, text_delta=delta),
    )
