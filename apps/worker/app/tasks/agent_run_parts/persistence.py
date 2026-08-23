"""Epoch-fenced Agent run/message/tool persistence and durable events."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_events import (
    AGENT_RUN_TERMINAL_STATUSES,
    AGENT_TOOL_TERMINAL_STATUSES,
    EV_AGENT_OUTPUT_DELTA,
    EV_AGENT_RUN_CANCELLED,
    EV_AGENT_RUN_FAILED,
    EV_AGENT_RUN_PARTIAL,
    EV_AGENT_RUN_STARTED,
    EV_AGENT_RUN_SUCCEEDED,
    EV_AGENT_TOOL_FAILED,
    AgentRunStatus,
    AgentToolCallStatus,
    agent_channel,
    agent_event_id,
)
from lumen_core.constants import MessageStatus
from lumen_core.model_entities import (
    AgentRun,
    AgentSession,
    AgentToolCall,
    Generation,
    Message,
    OutboxEvent,
)
from lumen_core.schema_models import AgentEventEnvelope

from ...agent_billing import (
    AgentBillingResult,
    release_agent_text_hold,
    settle_agent_text_actual,
    settle_agent_text_unknown,
)
from ...agent_runtime_client import AgentRuntimeEvent
from ...db import SessionLocal
from ...sse_publish import publish_event
from .compaction_checkpoint import (
    build_pi_compaction_checkpoint,
    checkpoint_provider_dispatched,
    checkpoint_provider_response,
)
from .contracts import AGENT_NO_COST_HTTP_STATUSES, AGENT_USAGE_KEYS, AgentClaim


logger = logging.getLogger(__name__)


def _dispatch(run: AgentRun) -> dict[str, Any]:
    return dict(run.dispatch_jsonb) if isinstance(run.dispatch_jsonb, dict) else {}


def _canonical_usage(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    usage: dict[str, int] = {}
    for key in AGENT_USAGE_KEYS:
        raw = source.get(key)
        usage[key] = (
            max(0, int(raw))
            if isinstance(raw, int) and not isinstance(raw, bool)
            else 0
        )
    usage["total_tokens"] = sum(
        usage[key]
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    )
    return usage


def _add_usage(current: Any, incoming: Any) -> dict[str, int]:
    left = _canonical_usage(current)
    right = _canonical_usage(incoming)
    result = {
        key: left[key] + right[key] for key in AGENT_USAGE_KEYS if key != "total_tokens"
    }
    result["total_tokens"] = sum(
        result[key]
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    )
    return result


def _max_usage(current: Any, incoming: Any) -> dict[str, int]:
    left = _canonical_usage(current)
    right = _canonical_usage(incoming)
    result = {
        key: max(left[key], right[key])
        for key in AGENT_USAGE_KEYS
        if key != "total_tokens"
    }
    result["total_tokens"] = sum(
        result[key]
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    )
    return result


def _stage_event(
    db: AsyncSession,
    *,
    run: AgentRun,
    event_name: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run.last_event_seq = int(run.last_event_seq or 0) + 1
    data = AgentEventEnvelope(
        agent_session_id=run.agent_session_id,
        agent_run_id=run.id,
        assistant_message_id=run.assistant_message_id,
        execution_epoch=run.execution_epoch,
        event_seq=run.last_event_seq,
        event_name=event_name,
    ).model_dump(mode="json")
    data["event_id"] = agent_event_id(
        run.id,
        run.execution_epoch,
        run.last_event_seq,
    )
    if extra:
        data.update(extra)
    db.add(
        OutboxEvent(
            kind="sse",
            payload={
                "user_id": run.user_id,
                "channel": agent_channel(run.agent_session_id),
                "event_name": event_name,
                "data": data,
            },
            published_at=None,
        )
    )
    return data


async def publish_agent_event_fast_path(redis: Any, data: dict[str, Any]) -> None:
    user_id = str(data.get("user_id") or "")
    event_data = {key: value for key, value in data.items() if key != "user_id"}
    try:
        await publish_event(
            redis,
            user_id,
            agent_channel(str(event_data["agent_session_id"])),
            str(event_data["event_name"]),
            event_data,
        )
    except Exception:
        logger.warning(
            "agent event fast path failed run=%s event=%s",
            event_data.get("agent_run_id"),
            event_data.get("event_name"),
            exc_info=True,
        )


def _public_event(run: AgentRun, data: dict[str, Any]) -> dict[str, Any]:
    return {"user_id": run.user_id, **data}


async def claim_agent_run(run_id: str) -> tuple[AgentClaim, dict[str, Any] | None]:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        async with db.begin():
            run = (
                await db.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                return AgentClaim("missing", run_id), None
            if run.status in AGENT_RUN_TERMINAL_STATUSES:
                return AgentClaim("terminal", run.id, run.execution_epoch), None
            dispatch = _dispatch(run)
            delivery = str(dispatch.get("runtime_delivery") or "")
            if run.status == AgentRunStatus.RUNNING.value and delivery in {
                "starting",
                "provider_dispatched",
                "provider_response",
                "compaction_ready",
                "terminal_received",
                "unknown",
            }:
                return AgentClaim("result_unknown", run.id, run.execution_epoch), None

            run.execution_epoch = int(run.execution_epoch or 0) + 1
            run.attempt = int(run.attempt or 0) + 1
            run.status = AgentRunStatus.RUNNING.value
            run.started_at = run.started_at or now
            run.finished_at = None
            run.error_code = None
            run.error_message = None
            run.dispatch_jsonb = {
                **dispatch,
                "runtime_delivery": "claimed",
                "execution_epoch": run.execution_epoch,
                "attempt": run.attempt,
                "claimed_at": now.isoformat(),
                "provider_dispatch_count": 0,
                "provider_completed_count": 0,
                "provider_response_statuses": [],
                "last_runtime_seq": 0,
                "trace_id": dispatch.get("trace_id") or secrets.token_hex(16),
            }
            message = (
                await db.execute(
                    select(Message)
                    .where(Message.id == run.assistant_message_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if message is not None:
                message.status = MessageStatus.STREAMING.value
            data = _stage_event(db, run=run, event_name=EV_AGENT_RUN_STARTED)
            public = _public_event(run, data)
            epoch = run.execution_epoch
        return AgentClaim("execute", run_id, epoch), public


async def load_claimed_run(run_id: str, execution_epoch: int) -> AgentRun:
    async with SessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        if (
            run is None
            or run.status != AgentRunStatus.RUNNING.value
            or run.execution_epoch != execution_epoch
        ):
            raise RuntimeError("Agent run claim is no longer current")
        return run


async def update_dispatch_state(
    run_id: str,
    execution_epoch: int,
    *,
    state: str,
    extra: dict[str, Any] | None = None,
) -> bool:
    async with SessionLocal() as db:
        async with db.begin():
            run = (
                await db.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                run is None
                or run.status != AgentRunStatus.RUNNING.value
                or run.execution_epoch != execution_epoch
            ):
                return False
            dispatch = _dispatch(run)
            dispatch["runtime_delivery"] = state
            dispatch[f"{state}_at"] = datetime.now(timezone.utc).isoformat()
            if extra:
                dispatch.update(extra)
            run.dispatch_jsonb = dispatch
            return True


async def _record_runtime_tool_failure(
    db: AsyncSession,
    *,
    run: AgentRun,
    event: AgentRuntimeEvent,
) -> None:
    if event.ordinal is None or not event.tool_call_id:
        return
    existing = (
        await db.execute(
            select(AgentToolCall).where(
                AgentToolCall.agent_run_id == run.id,
                (
                    (AgentToolCall.pi_tool_call_id == event.tool_call_id)
                    | (AgentToolCall.ordinal == event.ordinal)
                ),
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status in AGENT_TOOL_TERMINAL_STATUSES:
        return
    now = datetime.now(timezone.utc)
    status = (
        AgentToolCallStatus.TIMED_OUT.value
        if event.result_unknown is True
        else AgentToolCallStatus.FAILED.value
    )
    error_code = event.error_code or (
        "agent_tool_result_unknown"
        if event.result_unknown is True
        else "agent_tool_failed"
    )
    if existing is None:
        identity = (
            f"{run.id}\n{run.execution_epoch}\n{event.ordinal}\n{event.tool_call_id}"
        ).encode("utf-8")
        request_hash = hashlib.sha256(b"runtime-tool-arguments-unavailable").hexdigest()
        existing = AgentToolCall(
            agent_run_id=run.id,
            capability_id=f"runtime-unacknowledged:{run.execution_epoch}",
            pi_tool_call_id=event.tool_call_id,
            ordinal=event.ordinal,
            execution_epoch=run.execution_epoch,
            name=event.name or "lumen_create_image",
            mode=(
                event.mode
                if event.mode in {"text_to_image", "image_to_image"}
                else None
            ),
            status=status,
            request_hash=request_hash,
            semantic_key=hashlib.sha256(identity).hexdigest(),
            arguments_jsonb={},
            result_jsonb={"accepted": False, "runtime_receipt_only": True},
            generation_count=0,
            error_code=error_code,
            error_message=None,
            started_at=now,
            finished_at=now,
        )
        db.add(existing)
    else:
        existing.status = status
        existing.error_code = error_code
        existing.error_message = None
        existing.finished_at = now
    _stage_event(
        db,
        run=run,
        event_name=EV_AGENT_TOOL_FAILED,
        extra={
            "tool_call_id": existing.id,
            "error_code": error_code,
        },
    )


async def _checkpoint_runtime_started(
    db: AsyncSession,
    run: AgentRun,
    dispatch: dict[str, Any],
    event: AgentRuntimeEvent,
) -> None:
    if not event.runtime_version:
        return
    session = await db.get(AgentSession, run.agent_session_id)
    if session is not None:
        session.runtime_version = event.runtime_version
    dispatch["runtime_version"] = event.runtime_version
    if event.reasoning_effort is not None:
        dispatch["effective_reasoning_effort"] = event.reasoning_effort


async def _checkpoint_pi_compaction(
    db: AsyncSession,
    run: AgentRun,
    dispatch: dict[str, Any],
    event: AgentRuntimeEvent,
) -> None:
    checkpoint = build_pi_compaction_checkpoint(run, event)
    session = await db.get(AgentSession, run.agent_session_id)
    if session is None or session.user_id != run.user_id:
        raise ValueError("Pi compaction session is unavailable")
    boundary_exists = (
        await db.execute(
            select(Message.id).where(
                Message.id == event.first_kept_message_id,
                Message.conversation_id == session.conversation_id,
                Message.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if boundary_exists is None:
        raise ValueError("Pi compaction boundary is unavailable")
    run.usage_jsonb = _add_usage(
        run.usage_jsonb,
        event.usage.model_dump(mode="json"),
    )
    dispatch["runtime_delivery"] = "compaction_ready"
    dispatch["provider_completed_count"] = int(
        dispatch.get("provider_completed_count") or 0
    ) + event.provider_call_count
    dispatch["pi_compaction_count"] = int(dispatch.get("pi_compaction_count") or 0) + 1
    dispatch["pi_compaction_first_kept_message_id"] = event.first_kept_message_id
    dispatch["pi_compaction"] = checkpoint


def _checkpoint_turn(
    run: AgentRun,
    dispatch: dict[str, Any],
    event: AgentRuntimeEvent,
) -> None:
    if event.turn is not None:
        run.turn_count = max(int(run.turn_count or 0), event.turn)
    if (
        event.usage is not None
        and event.usage.total_tokens > 0
        and event.stop_reason not in {"error", "aborted"}
    ):
        dispatch["provider_completed_count"] = int(
            dispatch.get("provider_completed_count") or 0
        ) + 1
    if event.usage is not None:
        run.usage_jsonb = _add_usage(
            run.usage_jsonb,
            event.usage.model_dump(mode="json"),
        )


def _checkpoint_terminal(
    run: AgentRun,
    dispatch: dict[str, Any],
    event: AgentRuntimeEvent,
) -> None:
    dispatch["runtime_delivery"] = "terminal_received"
    if event.usage is not None:
        run.usage_jsonb = _max_usage(
            run.usage_jsonb,
            event.usage.model_dump(mode="json"),
        )
    if event.turn_count is not None:
        run.turn_count = max(int(run.turn_count or 0), event.turn_count)
    if event.tool_call_count is not None:
        run.tool_call_count = max(int(run.tool_call_count or 0), event.tool_call_count)
    if event.provider_dispatch_count is not None:
        dispatch["provider_dispatch_count"] = max(
            int(dispatch.get("provider_dispatch_count") or 0),
            event.provider_dispatch_count,
        )
    if event.provider_completed_count is not None:
        dispatch["provider_completed_count"] = max(
            int(dispatch.get("provider_completed_count") or 0),
            event.provider_completed_count,
        )
    dispatch["runtime_terminal"] = {
        "type": event.type,
        "status": event.status,
        "error_code": event.error_code,
        "turn_count": event.turn_count,
        "tool_call_count": event.tool_call_count,
        "provider_dispatch_count": event.provider_dispatch_count,
        "provider_completed_count": event.provider_completed_count,
        "usage": _canonical_usage(run.usage_jsonb),
    }


async def _apply_runtime_checkpoint(
    db: AsyncSession,
    run: AgentRun,
    dispatch: dict[str, Any],
    event: AgentRuntimeEvent,
) -> None:
    if event.type == "run.started":
        await _checkpoint_runtime_started(db, run, dispatch, event)
    elif event.type == "provider.dispatched":
        checkpoint_provider_dispatched(dispatch)
    elif event.type == "provider.response":
        checkpoint_provider_response(dispatch, event)
    elif event.type == "turn.completed":
        _checkpoint_turn(run, dispatch, event)
    elif event.type == "compaction.completed":
        await _checkpoint_pi_compaction(db, run, dispatch, event)
    elif event.type == "tool.failed":
        await _record_runtime_tool_failure(db, run=run, event=event)
    else:
        _checkpoint_terminal(run, dispatch, event)


async def record_runtime_checkpoint(
    run_id: str,
    execution_epoch: int,
    event: AgentRuntimeEvent,
) -> bool:
    if event.type not in {
        "run.started",
        "provider.dispatched",
        "provider.response",
        "turn.completed",
        "compaction.completed",
        "tool.failed",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }:
        return True
    async with SessionLocal() as db:
        async with db.begin():
            run = (
                await db.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                run is None
                or run.status != AgentRunStatus.RUNNING.value
                or run.execution_epoch != execution_epoch
            ):
                return False
            dispatch = _dispatch(run)
            last_runtime_seq = int(dispatch.get("last_runtime_seq") or 0)
            if event.seq <= last_runtime_seq:
                return True
            await _apply_runtime_checkpoint(db, run, dispatch, event)
            dispatch["last_runtime_seq"] = event.seq
            run.dispatch_jsonb = dispatch
            return True


async def flush_agent_text(
    redis: Any,
    *,
    run_id: str,
    execution_epoch: int,
    text: str,
    delta: str,
) -> bool:
    if not delta:
        return True
    public: dict[str, Any] | None = None
    async with SessionLocal() as db:
        async with db.begin():
            run = (
                await db.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                run is None
                or run.status != AgentRunStatus.RUNNING.value
                or run.execution_epoch != execution_epoch
            ):
                return False
            message = (
                await db.execute(
                    select(Message)
                    .where(Message.id == run.assistant_message_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if message is None:
                return False
            content = dict(message.content) if isinstance(message.content, dict) else {}
            content.update({"source": "agent", "agent_run_id": run.id, "text": text})
            message.content = content
            message.status = MessageStatus.STREAMING.value
            dispatch = _dispatch(run)
            dispatch["runtime_delivery"] = (
                dispatch.get("runtime_delivery") or "streaming"
            )
            dispatch["flushed_text_chars"] = len(text)
            run.dispatch_jsonb = dispatch
            data = _stage_event(
                db,
                run=run,
                event_name=EV_AGENT_OUTPUT_DELTA,
                extra={"text_delta": delta},
            )
            public = _public_event(run, data)
    if public is not None:
        await publish_agent_event_fast_path(redis, public)
    return True


def _generation_ids(generation_rows: list[Generation], tool_id: str) -> list[str]:
    result: list[str] = []
    for generation in generation_rows:
        request = generation.upstream_request
        if isinstance(request, dict) and request.get("agent_tool_call_id") == tool_id:
            result.append(generation.id)
    return result[:4]


def _repair_tools(
    tools: list[AgentToolCall],
    generations: list[Generation],
    *,
    now: datetime,
    unknown: bool,
) -> None:
    for tool in tools:
        if tool.status in AGENT_TOOL_TERMINAL_STATUSES:
            continue
        generation_ids = _generation_ids(generations, tool.id)
        if generation_ids:
            tool.status = AgentToolCallStatus.SUCCEEDED.value
            tool.result_jsonb = {
                "generation_ids": generation_ids,
                "mode": tool.mode,
                "accepted": True,
                "recovered": True,
            }
            tool.generation_count = len(generation_ids)
            tool.error_code = None
        else:
            tool.status = (
                AgentToolCallStatus.TIMED_OUT.value
                if unknown
                else AgentToolCallStatus.FAILED.value
            )
            tool.error_code = (
                "agent_tool_result_unknown" if unknown else "agent_tool_interrupted"
            )
            tool.error_message = None
        tool.finished_at = now


def _tool_projection(tool: AgentToolCall) -> dict[str, Any]:
    result = tool.result_jsonb if isinstance(tool.result_jsonb, dict) else {}
    generation_ids = result.get("generation_ids")
    safe_ids = (
        [value for value in generation_ids if isinstance(value, str)][:4]
        if isinstance(generation_ids, list)
        else []
    )
    return {
        "id": tool.id,
        "name": tool.name,
        "label": "Create image",
        "mode": tool.mode,
        "status": tool.status,
        "generation_ids": safe_ids,
        "generation_count": len(safe_ids),
        **({"error_code": tool.error_code} if tool.error_code else {}),
    }


def _terminal_event_name(status: str) -> str:
    return {
        AgentRunStatus.SUCCEEDED.value: EV_AGENT_RUN_SUCCEEDED,
        AgentRunStatus.PARTIAL.value: EV_AGENT_RUN_PARTIAL,
        AgentRunStatus.CANCELLED.value: EV_AGENT_RUN_CANCELLED,
    }.get(status, EV_AGENT_RUN_FAILED)


async def _settle_billing(
    db: AsyncSession,
    *,
    run: AgentRun,
    knowledge: Literal["actual", "proven_absent", "unknown"],
    usage: dict[str, Any],
    reason: str,
) -> AgentBillingResult:
    if knowledge == "actual":
        return await settle_agent_text_actual(db, run=run, usage=usage)
    if knowledge == "proven_absent":
        return await release_agent_text_hold(db, run=run, reason=reason)
    return await settle_agent_text_unknown(db, run=run, reason=reason)


async def finalize_agent_run(
    redis: Any,
    *,
    run_id: str,
    execution_epoch: int,
    requested_status: Literal["succeeded", "partial", "failed", "cancelled"],
    text: str,
    usage: dict[str, Any],
    turn_count: int,
    runtime_tool_count: int,
    error_code: str | None,
    knowledge: Literal["actual", "proven_absent", "unknown"],
    reason: str,
    used_memory_summary: tuple[dict[str, str], ...] = (),
) -> tuple[str, AgentBillingResult | None, str | None]:
    public: dict[str, Any] | None = None
    conversation_id: str | None = None
    billing_result: AgentBillingResult | None = None
    final_status = requested_status
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        async with db.begin():
            run = (
                await db.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                return "missing", None, None
            if run.status in AGENT_RUN_TERMINAL_STATUSES:
                return run.status, None, None
            if (
                run.status != AgentRunStatus.RUNNING.value
                or run.execution_epoch != execution_epoch
            ):
                return "superseded", None, None
            message = (
                await db.execute(
                    select(Message)
                    .where(Message.id == run.assistant_message_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            tools = list(
                (
                    await db.execute(
                        select(AgentToolCall)
                        .where(AgentToolCall.agent_run_id == run.id)
                        .order_by(AgentToolCall.ordinal.asc())
                    )
                )
                .scalars()
                .all()
            )
            generations = list(
                (
                    await db.execute(
                        select(Generation).where(
                            Generation.message_id == run.assistant_message_id,
                            Generation.user_id == run.user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            _repair_tools(tools, generations, now=now, unknown=knowledge == "unknown")
            has_side_effect = any(
                tool.status == AgentToolCallStatus.SUCCEEDED.value
                and int(tool.generation_count or 0) > 0
                for tool in tools
            )
            if requested_status == "partial":
                final_status = AgentRunStatus.PARTIAL.value
            elif requested_status == "failed" and has_side_effect:
                final_status = AgentRunStatus.PARTIAL.value
            elif requested_status == "cancelled":
                final_status = AgentRunStatus.CANCELLED.value
            elif requested_status == "succeeded":
                final_status = AgentRunStatus.SUCCEEDED.value
            else:
                final_status = AgentRunStatus.FAILED.value
            run.status = final_status
            run.finished_at = now
            run.turn_count = max(int(run.turn_count or 0), max(0, turn_count))
            run.tool_call_count = max(
                int(run.tool_call_count or 0),
                max(0, runtime_tool_count),
                len(tools),
            )
            effective_usage = _max_usage(run.usage_jsonb, usage)
            run.usage_jsonb = effective_usage
            run.error_code = error_code if final_status != "succeeded" else None
            run.error_message = None
            dispatch = _dispatch(run)
            dispatch.update(
                {
                    "runtime_delivery": "finalized",
                    "billing_knowledge": knowledge,
                    "finalized_at": now.isoformat(),
                }
            )
            run.dispatch_jsonb = dispatch
            if message is not None:
                content = (
                    dict(message.content) if isinstance(message.content, dict) else {}
                )
                durable_text = content.get("text")
                durable_text = durable_text if isinstance(durable_text, str) else ""
                if not text or durable_text.startswith(text):
                    final_text = durable_text
                elif text.startswith(durable_text):
                    final_text = text
                else:
                    final_text = (
                        text if len(text) >= len(durable_text) else durable_text
                    )
                projected_tools = [_tool_projection(tool) for tool in tools]
                generation_ids = list(
                    dict.fromkeys(
                        generation_id
                        for tool in projected_tools
                        for generation_id in tool.get("generation_ids", [])
                        if isinstance(generation_id, str)
                    )
                )
                content.update(
                    {
                        "source": "agent",
                        "agent_run_id": run.id,
                        "text": final_text,
                        "tool_calls": projected_tools,
                        "generation_ids": generation_ids,
                    }
                )
                if used_memory_summary:
                    content["used_memory_summary"] = list(used_memory_summary)
                message.content = content
                message.status = {
                    AgentRunStatus.SUCCEEDED.value: MessageStatus.SUCCEEDED.value,
                    AgentRunStatus.PARTIAL.value: MessageStatus.PARTIAL.value,
                    AgentRunStatus.CANCELLED.value: MessageStatus.CANCELED.value,
                }.get(final_status, MessageStatus.FAILED.value)
                conversation_id = message.conversation_id
            billing_result = await _settle_billing(
                db,
                run=run,
                knowledge=knowledge,
                usage=effective_usage,
                reason=reason,
            )
            run.text_hold_micro = 0
            data = _stage_event(
                db,
                run=run,
                event_name=_terminal_event_name(final_status),
                extra={
                    "status": final_status,
                    **({"error_code": error_code} if error_code else {}),
                },
            )
            public = _public_event(run, data)
            if (
                final_status
                in {
                    AgentRunStatus.SUCCEEDED.value,
                    AgentRunStatus.PARTIAL.value,
                }
                and conversation_id is not None
            ):
                memory_event_id = (
                    f"memory-extract:{run.user_message_id}:{run.assistant_message_id}"
                )
                db.add(
                    OutboxEvent(
                        kind="memory_extract",
                        payload={
                            "task_id": run.assistant_message_id,
                            "event_id": memory_event_id,
                            "user_id": run.user_id,
                            "conversation_id": conversation_id,
                            "source_user_message_id": run.user_message_id,
                            "assistant_message_id": run.assistant_message_id,
                            "kind": "memory_extract",
                            "source": "agent_succeeded",
                        },
                        published_at=None,
                    )
                )
    if public is not None:
        await publish_agent_event_fast_path(redis, public)
    return final_status, billing_result, conversation_id


async def reconcile_cancelled_agent_hold(run_id: str) -> bool:
    """Consume a running-run hold after API cancellation advanced the epoch."""
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        async with db.begin():
            run = (
                await db.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one_or_none()
            if run is None or run.status != AgentRunStatus.CANCELLED.value:
                return False
            billing = run.billing_jsonb if isinstance(run.billing_jsonb, dict) else {}
            if billing.get("state") in {
                "settled",
                "settled_unknown",
                "released",
                "not_applicable",
            }:
                return False
            tools = list(
                (
                    await db.execute(
                        select(AgentToolCall)
                        .where(AgentToolCall.agent_run_id == run.id)
                        .order_by(AgentToolCall.ordinal.asc())
                    )
                )
                .scalars()
                .all()
            )
            generations = list(
                (
                    await db.execute(
                        select(Generation).where(
                            Generation.message_id == run.assistant_message_id,
                            Generation.user_id == run.user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            dispatch = _dispatch(run)
            delivery = str(dispatch.get("runtime_delivery") or "")
            usage = run.usage_jsonb if isinstance(run.usage_jsonb, dict) else {}
            has_usage = any(
                isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in usage.values()
            )
            response_statuses = [
                value
                for value in dispatch.get("provider_response_statuses", [])
                if isinstance(value, int) and not isinstance(value, bool)
            ]
            dispatch_count = int(dispatch.get("provider_dispatch_count") or 0)
            completed_count = int(dispatch.get("provider_completed_count") or 0)
            pending = max(0, dispatch_count - completed_count)
            pending_statuses = response_statuses[-pending:] if pending else []
            pending_proven_absent = (
                pending > 0
                and len(pending_statuses) == pending
                and all(
                    value in AGENT_NO_COST_HTTP_STATUSES for value in pending_statuses
                )
            )
            unknown = (pending > 0 and not pending_proven_absent) or (
                dispatch_count == 0 and delivery in {"starting", "unknown"}
            )
            _repair_tools(tools, generations, now=now, unknown=unknown)
            message = (
                await db.execute(
                    select(Message)
                    .where(Message.id == run.assistant_message_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if message is not None:
                content = (
                    dict(message.content) if isinstance(message.content, dict) else {}
                )
                projected = [_tool_projection(tool) for tool in tools]
                content["tool_calls"] = projected
                content["generation_ids"] = list(
                    dict.fromkeys(
                        value
                        for item in projected
                        for value in item.get("generation_ids", [])
                        if isinstance(value, str)
                    )
                )
                message.content = content
            if unknown:
                await settle_agent_text_unknown(
                    db,
                    run=run,
                    reason="cancelled_after_runtime_dispatch",
                )
            elif has_usage:
                await settle_agent_text_actual(db, run=run, usage=usage)
            else:
                await release_agent_text_hold(
                    db,
                    run=run,
                    reason="cancelled_before_runtime_dispatch",
                )
            run.text_hold_micro = 0
            return True


__all__ = [
    "claim_agent_run",
    "finalize_agent_run",
    "flush_agent_text",
    "load_claimed_run",
    "publish_agent_event_fast_path",
    "reconcile_cancelled_agent_hold",
    "record_runtime_checkpoint",
    "update_dispatch_state",
]
