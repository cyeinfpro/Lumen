from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lumen_core.context_window import is_summary_usable
from lumen_core.models import Conversation

from .common import (
    LoadedSummaryMessages,
    boundary_created_at,
    boundary_id,
    extra_instruction_hash,
    public_summary_result,
    summary_satisfies_request,
)
from .results import SummaryRequest


@dataclass(frozen=True)
class SummaryPlan:
    request: SummaryRequest | None
    immediate_result: dict[str, Any] | None = None
    handled: bool = False


async def build_summary_plan(
    session: Any,
    conv: Conversation,
    boundary: Any,
    settings: Any,
    *,
    force: bool,
    extra_instruction: str | None,
    dry_run: bool,
    trigger: str,
    target_tokens: int,
    input_budget: int,
    summary_timeout_s: float,
    model: str,
    circuit_threshold: int,
    load_messages: Callable[
        [Any, str, str | None, str], Awaitable[LoadedSummaryMessages]
    ],
    load_position: Callable[[Any, str], Awaitable[tuple[datetime, str] | None]],
    boundary_id_fn: Callable[[Any], str | None] = boundary_id,
    boundary_created_at_fn: Callable[[Any], datetime | None] = boundary_created_at,
    extra_instruction_hash_fn: Callable[
        [str | None], str | None
    ] = extra_instruction_hash,
    is_summary_usable_fn: Callable[[dict[str, Any]], bool] = is_summary_usable,
    summary_satisfies_request_fn: Callable[..., bool] = summary_satisfies_request,
    public_summary_result_fn: Callable[..., dict[str, Any]] = public_summary_result,
) -> SummaryPlan:
    conv_id = str(conv.id)
    boundary_key = boundary_id_fn(boundary)
    if not boundary_key:
        return SummaryPlan(None, handled=True)

    existing_summary = (
        conv.summary_jsonb if isinstance(conv.summary_jsonb, dict) else None
    )
    usable_summary = (
        existing_summary
        if existing_summary is not None and is_summary_usable_fn(existing_summary)
        else None
    )
    extra_hash = extra_instruction_hash_fn(extra_instruction)
    if (
        not dry_run
        and not force
        and summary_satisfies_request_fn(usable_summary, boundary, extra_hash)
    ):
        return SummaryPlan(
            None,
            immediate_result=public_summary_result_fn(
                usable_summary,
                created=False,
                status="cached",
            ),
            handled=True,
        )

    previous_summary_text, previous_up_to_id = _previous_summary_state(
        usable_summary,
        force=force,
    )
    loaded = await load_messages(
        session,
        conv_id,
        previous_up_to_id,
        boundary_key,
    )
    boundary_dt = boundary_created_at_fn(boundary)
    if boundary_dt is None:
        position = await load_position(session, boundary_key)
        boundary_dt = position[0] if position is not None else None
    if boundary_dt is None:
        return SummaryPlan(None, handled=True)

    return SummaryPlan(
        SummaryRequest(
            conv_id=conv_id,
            boundary=boundary,
            boundary_id=boundary_key,
            boundary_dt=boundary_dt,
            settings=settings,
            target_tokens=target_tokens,
            input_budget=input_budget,
            summary_timeout_s=summary_timeout_s,
            model=model,
            circuit_threshold=circuit_threshold,
            extra_instruction=extra_instruction,
            extra_hash=extra_hash,
            existing_summary=existing_summary,
            previous_summary_text=previous_summary_text,
            loaded=loaded,
            trigger=trigger,
            force=force,
        )
    )


def _previous_summary_state(
    summary: dict[str, Any] | None,
    *,
    force: bool,
) -> tuple[str | None, str | None]:
    if force or not isinstance(summary, dict):
        return None, None
    text = summary.get("text")
    message_id = summary.get("up_to_message_id")
    return (
        text if isinstance(text, str) else None,
        message_id if isinstance(message_id, str) else None,
    )
