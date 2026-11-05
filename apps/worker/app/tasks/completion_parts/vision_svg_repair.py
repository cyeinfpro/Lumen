"""Fenced terminal SVG repair from a completed receipt, without model or billing calls."""

from __future__ import annotations

from typing import Any

from lumen_core.constants import CompletionStatus, EV_COMP_SUCCEEDED
from lumen_core.model_entities import Message

from ...completion_checkpoint import completion_has_completed_checkpoint
from ...vision_svg_artifact import is_vision_svg, stage_vision_svg_artifact


async def repair_terminal_svg(state: Any, session: Any, completion: Any) -> bool:
    """Terminal repair is strictly local. The claim already locked and fenced this row."""
    if not is_vision_svg(completion) or completion.status != CompletionStatus.SUCCEEDED.value:
        return False
    request = dict(completion.upstream_request or {})
    if (completion.cancel_requested_at is not None or request.get("artifact_status") not in {"pending", "processing_failed"}
            or not completion_has_completed_checkpoint(completion)):
        return False
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost before local SVG repair")
    await state.ports.retry._raise_if_completion_cancelled(state.request.redis, str(completion.id), "cancelled before SVG repair")
    content = await stage_vision_svg_artifact(session, completion)
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost during local SVG repair")
    await state.ports.retry._raise_if_completion_cancelled(state.request.redis, str(completion.id), "cancelled during SVG repair")
    message = await session.get(Message, completion.message_id)
    if message is not None:
        message.content = {**dict(message.content or {}), **content}
    delivery = state.ports.events._stage_completion_event(
        session, completion.user_id, state.request.channel, EV_COMP_SUCCEEDED,
        {"completion_id": str(completion.id), "message_id": completion.message_id,
         "attempt": int(completion.attempt or 0), "attempt_epoch": int(completion.attempt or 0),
         "execution_epoch": int(completion.execution_epoch or 0), "local_artifact_repair": True, **content})
    await session.commit()
    await state.ports.events._deliver_completion_event(state.request.redis, delivery)
    state.settlement.task_outcome = "succeeded"
    return True
