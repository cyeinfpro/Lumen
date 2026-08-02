from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NotRequired, TypedDict, Unpack

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


class _SummaryPlanArgs(TypedDict):
    force: bool
    extra_instruction: str | None
    dry_run: bool
    trigger: str
    target_tokens: int
    input_budget: int
    summary_timeout_s: float
    model: str
    circuit_threshold: int
    load_messages: Callable[
        [Any, str, str | None, str], Awaitable[LoadedSummaryMessages]
    ]
    load_position: Callable[[Any, str], Awaitable[tuple[datetime, str] | None]]
    boundary_id_fn: NotRequired[Callable[[Any], str | None]]
    boundary_created_at_fn: NotRequired[Callable[[Any], datetime | None]]
    extra_instruction_hash_fn: NotRequired[Callable[[str | None], str | None]]
    is_summary_usable_fn: NotRequired[Callable[[dict[str, Any]], bool]]
    summary_satisfies_request_fn: NotRequired[Callable[..., bool]]
    public_summary_result_fn: NotRequired[Callable[..., dict[str, Any]]]


@dataclass(frozen=True)
class _SummaryPlanOptions:
    force: bool
    extra_instruction: str | None
    dry_run: bool
    trigger: str
    target_tokens: int
    input_budget: int
    summary_timeout_s: float
    model: str
    circuit_threshold: int
    load_messages: Callable[
        [Any, str, str | None, str], Awaitable[LoadedSummaryMessages]
    ]
    load_position: Callable[[Any, str], Awaitable[tuple[datetime, str] | None]]
    boundary_id_fn: Callable[[Any], str | None] = boundary_id
    boundary_created_at_fn: Callable[[Any], datetime | None] = boundary_created_at
    extra_instruction_hash_fn: Callable[[str | None], str | None] = (
        extra_instruction_hash
    )
    is_summary_usable_fn: Callable[[dict[str, Any]], bool] = is_summary_usable
    summary_satisfies_request_fn: Callable[..., bool] = summary_satisfies_request
    public_summary_result_fn: Callable[..., dict[str, Any]] = public_summary_result


async def build_summary_plan(
    session: Any,
    conv: Conversation,
    boundary: Any,
    settings: Any,
    **kwargs: Unpack[_SummaryPlanArgs],
) -> SummaryPlan:
    options = _SummaryPlanOptions(**kwargs)
    conv_id = str(conv.id)
    boundary_key = options.boundary_id_fn(boundary)
    if not boundary_key:
        return SummaryPlan(None, handled=True)

    existing_summary = (
        conv.summary_jsonb if isinstance(conv.summary_jsonb, dict) else None
    )
    usable_summary = (
        existing_summary
        if existing_summary is not None
        and options.is_summary_usable_fn(existing_summary)
        else None
    )
    extra_hash = options.extra_instruction_hash_fn(options.extra_instruction)
    if (
        not options.dry_run
        and not options.force
        and options.summary_satisfies_request_fn(
            usable_summary,
            boundary,
            extra_hash,
        )
    ):
        return SummaryPlan(
            None,
            immediate_result=options.public_summary_result_fn(
                usable_summary,
                created=False,
                status="cached",
            ),
            handled=True,
        )

    previous_summary_text, previous_up_to_id = _previous_summary_state(
        usable_summary,
        force=options.force,
    )
    loaded = await options.load_messages(
        session,
        conv_id,
        previous_up_to_id,
        boundary_key,
    )
    boundary_dt = options.boundary_created_at_fn(boundary)
    if boundary_dt is None:
        position = await options.load_position(session, boundary_key)
        boundary_dt = position[0] if position is not None else None
    if boundary_dt is None:
        return SummaryPlan(None, handled=True)

    return SummaryPlan(
        SummaryRequest(
            conv_id=conv_id,
            user_id=str(conv.user_id),
            boundary=boundary,
            boundary_id=boundary_key,
            boundary_dt=boundary_dt,
            settings=settings,
            target_tokens=options.target_tokens,
            input_budget=options.input_budget,
            summary_timeout_s=options.summary_timeout_s,
            model=options.model,
            circuit_threshold=options.circuit_threshold,
            extra_instruction=options.extra_instruction,
            extra_hash=extra_hash,
            existing_summary=existing_summary,
            previous_summary_text=previous_summary_text,
            loaded=loaded,
            trigger=options.trigger,
            force=options.force,
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
