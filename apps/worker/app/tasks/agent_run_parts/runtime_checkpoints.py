"""Runtime event checkpoint ingestion for epoch-fenced Agent runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_events import (
    AGENT_FILE_TOOLS,
    AGENT_FIRST_PARTY_TOOLS,
    AGENT_TOOL_TERMINAL_STATUSES,
    AGENT_TOOL_WEB_SEARCH,
    EV_AGENT_TOOL_FAILED,
    EV_AGENT_TOOL_STARTED,
    EV_AGENT_TOOL_SUCCEEDED,
    AgentRunStatus,
    AgentToolCallStatus,
)
from lumen_core.agent_protocol_safety import agent_text_boundary_error
from lumen_core.model_entities import (
    AgentProviderCall,
    AgentRun,
    AgentSession,
    AgentToolCall,
    Message,
)

from ...agent_runtime_client import AgentRuntimeEvent
from .compaction_checkpoint import (
    build_pi_compaction_checkpoint,
    checkpoint_provider_dispatched,
    checkpoint_provider_response,
)
from .contracts import AGENT_USAGE_KEYS


@dataclass(frozen=True, slots=True)
class RuntimeCheckpointDependencies:
    session_factory: Any
    stage_event: Callable[..., dict[str, Any]]


def dispatch_snapshot(run: AgentRun) -> dict[str, Any]:
    return dict(run.dispatch_jsonb) if isinstance(run.dispatch_jsonb, dict) else {}


def canonical_usage(value: Any) -> dict[str, int]:
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


def add_usage(current: Any, incoming: Any) -> dict[str, int]:
    left = canonical_usage(current)
    right = canonical_usage(incoming)
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


def max_usage(current: Any, incoming: Any) -> dict[str, int]:
    left = canonical_usage(current)
    right = canonical_usage(incoming)
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


_RUNTIME_LOCAL_TOOLS = frozenset({AGENT_TOOL_WEB_SEARCH, *AGENT_FILE_TOOLS})
_RUNTIME_TOOL_MODES = frozenset({"web_search", "file_list", "file_read", "file_search"})


def _runtime_tool_identity(
    run: AgentRun,
    event: AgentRuntimeEvent,
) -> tuple[str, str, dict[str, Any]]:
    arguments = dict(event.arguments or {})
    encoded = json.dumps(
        arguments,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    request_hash = hashlib.sha256(encoded).hexdigest()
    identity = (
        f"{run.id}\n{run.execution_epoch}\n{event.ordinal}\n"
        f"{event.tool_call_id}\n{event.name}\n{request_hash}"
    ).encode("utf-8")
    return request_hash, hashlib.sha256(identity).hexdigest(), arguments


async def _runtime_tool_row(
    db: AsyncSession,
    *,
    run: AgentRun,
    event: AgentRuntimeEvent,
) -> AgentToolCall | None:
    if (
        event.ordinal is None
        or not event.tool_call_id
        or event.name not in _RUNTIME_LOCAL_TOOLS
    ):
        return None
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
    if existing is not None:
        return existing
    request_hash, semantic_key, arguments = _runtime_tool_identity(run, event)
    existing = AgentToolCall(
        agent_run_id=run.id,
        capability_id=f"runtime-local:{run.execution_epoch}",
        pi_tool_call_id=event.tool_call_id,
        ordinal=event.ordinal,
        execution_epoch=run.execution_epoch,
        name=event.name,
        mode=event.mode if event.mode in _RUNTIME_TOOL_MODES else None,
        status=AgentToolCallStatus.RUNNING.value,
        request_hash=request_hash,
        semantic_key=semantic_key,
        arguments_jsonb=arguments,
        result_jsonb={},
        generation_count=0,
        error_code=None,
        error_message=None,
        started_at=datetime.now(timezone.utc),
        finished_at=None,
    )
    db.add(existing)
    await db.flush()
    return existing


async def _record_runtime_local_tool(
    db: AsyncSession,
    *,
    run: AgentRun,
    event: AgentRuntimeEvent,
    stage_event: Callable[..., dict[str, Any]],
) -> None:
    if event.name not in _RUNTIME_LOCAL_TOOLS:
        return
    tool = await _runtime_tool_row(db, run=run, event=event)
    if tool is None:
        return
    if event.type == "tool.started":
        stage_event(
            db,
            run=run,
            event_name=EV_AGENT_TOOL_STARTED,
            extra={"tool_call_id": tool.id},
        )
        return
    if event.type != "tool.succeeded" or tool.status in AGENT_TOOL_TERMINAL_STATUSES:
        return
    tool.status = AgentToolCallStatus.SUCCEEDED.value
    tool.mode = event.mode if event.mode in _RUNTIME_TOOL_MODES else tool.mode
    tool.result_jsonb = {
        "receipt_version": 1,
        "runtime_local": True,
        "history_text": (event.result_text or "")[:20_000],
    }
    tool.error_code = None
    tool.error_message = None
    tool.finished_at = datetime.now(timezone.utc)
    stage_event(
        db,
        run=run,
        event_name=EV_AGENT_TOOL_SUCCEEDED,
        extra={"tool_call_id": tool.id},
    )


async def _record_runtime_tool_failure(
    db: AsyncSession,
    *,
    run: AgentRun,
    event: AgentRuntimeEvent,
    stage_event: Callable[..., dict[str, Any]],
) -> None:
    if (
        event.ordinal is None
        or not event.tool_call_id
        or event.name not in AGENT_FIRST_PARTY_TOOLS
    ):
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
    failure_receipt = {
        "receipt_version": 1,
        "status": status,
        "http_status": 504 if event.result_unknown is True else 409,
        "error_code": error_code,
        "runtime_receipt_only": True,
        "history_text": (event.result_text or "")[:20_000],
    }
    if existing is None:
        request_hash, semantic_key, arguments = _runtime_tool_identity(run, event)
        existing = AgentToolCall(
            agent_run_id=run.id,
            capability_id=f"runtime-unacknowledged:{run.execution_epoch}",
            pi_tool_call_id=event.tool_call_id,
            ordinal=event.ordinal,
            execution_epoch=run.execution_epoch,
            name=event.name,
            mode=(
                event.mode
                if event.mode
                in {"text_to_image", "image_to_image", *_RUNTIME_TOOL_MODES}
                else None
            ),
            status=status,
            request_hash=request_hash,
            semantic_key=semantic_key,
            arguments_jsonb=arguments,
            result_jsonb=failure_receipt,
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
        existing.result_jsonb = failure_receipt
        existing.finished_at = now
    stage_event(
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
    session = await db.get(AgentSession, run.agent_session_id)
    if session is None or session.user_id != run.user_id:
        raise ValueError("Pi compaction session is unavailable")
    run.usage_jsonb = add_usage(
        run.usage_jsonb,
        event.usage.model_dump(mode="json"),
    )
    dispatch["provider_completed_count"] = (
        int(dispatch.get("provider_completed_count") or 0) + event.provider_call_count
    )
    dispatch["pi_compaction_count"] = int(dispatch.get("pi_compaction_count") or 0) + 1
    violation = agent_text_boundary_error(event.summary or "")
    if violation is not None:
        dispatch["runtime_delivery"] = "compaction_quarantined"
        dispatch["pi_compaction_quarantine"] = {
            "event_seq": event.seq,
            "error_code": violation,
        }
        if session.active_pi_compaction_run_id == run.id:
            session.active_pi_compaction_run_id = None
            session.active_pi_compaction_schema_version = None
            session.active_pi_compaction_event_seq = None
        return
    checkpoint = build_pi_compaction_checkpoint(run, event)
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
    dispatch["runtime_delivery"] = "compaction_ready"
    dispatch["pi_compaction_first_kept_message_id"] = event.first_kept_message_id
    dispatch["pi_compaction"] = checkpoint
    session.active_pi_compaction_run_id = run.id
    session.active_pi_compaction_schema_version = event.checkpoint_version
    session.active_pi_compaction_event_seq = event.seq


def _checkpoint_turn(
    run: AgentRun,
    dispatch: dict[str, Any],
    event: AgentRuntimeEvent,
) -> None:
    if event.turn is not None:
        run.turn_count = max(int(run.turn_count or 0), event.turn)
    if event.usage is not None and event.stop_reason not in {"error", "aborted"}:
        dispatch["provider_completed_count"] = max(
            int(dispatch.get("provider_completed_count") or 0),
            event.dispatch_ordinal or event.turn or 0,
        )
    if event.usage is not None:
        run.usage_jsonb = add_usage(
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
        run.usage_jsonb = max_usage(
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
        "usage": canonical_usage(run.usage_jsonb),
    }


async def _checkpoint_runtime_activity(
    db: AsyncSession,
    run: AgentRun,
    dispatch: dict[str, Any],
    event: AgentRuntimeEvent,
) -> None:
    if event.type == "run.heartbeat":
        dispatch["runtime_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        return
    await _checkpoint_runtime_started(db, run, dispatch, event)


async def _provider_call_for_event(
    db: AsyncSession,
    run: AgentRun,
    ordinal: int | None,
) -> AgentProviderCall | None:
    if ordinal is None:
        return None
    return (
        await db.execute(
            select(AgentProviderCall)
            .where(
                AgentProviderCall.agent_run_id == run.id,
                AgentProviderCall.execution_epoch == run.execution_epoch,
                AgentProviderCall.dispatch_ordinal == ordinal,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _checkpoint_provider_call(
    db: AsyncSession,
    run: AgentRun,
    event: AgentRuntimeEvent,
) -> None:
    if event.type in {"run.completed", "run.failed", "run.cancelled"}:
        pending = list(
            (
                await db.execute(
                    select(AgentProviderCall)
                    .where(
                        AgentProviderCall.agent_run_id == run.id,
                        AgentProviderCall.execution_epoch == run.execution_epoch,
                        AgentProviderCall.result_state == "pending",
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        terminal_exact = (
            event.usage_evidence == "exact"
            and event.provider_dispatch_count == event.provider_completed_count
            and event.usage is not None
        )
        covered = [row.dispatch_ordinal for row in pending]
        for row in pending:
            if terminal_exact:
                row.delivery_state = "completed"
                row.result_state = "exact"
                row.exact_usage_jsonb = {
                    "aggregate": event.usage.model_dump(mode="json"),
                    "covered_ordinals": covered,
                }
                row.uncertainty_reason = None
            else:
                row.delivery_state = (
                    "cancelled" if event.type == "run.cancelled" else "unknown"
                )
                row.result_state = "unknown"
                row.uncertainty_reason = (
                    event.error_code or "terminal_without_exact_usage"
                )
            row.evidence_event_seq = event.seq
        return
    if event.type == "compaction.completed":
        pending = list(
            (
                await db.execute(
                    select(AgentProviderCall)
                    .where(
                        AgentProviderCall.agent_run_id == run.id,
                        AgentProviderCall.execution_epoch == run.execution_epoch,
                        AgentProviderCall.result_state == "pending",
                    )
                    .order_by(AgentProviderCall.dispatch_ordinal.asc())
                    .limit(event.provider_call_count or 0)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        covered = [row.dispatch_ordinal for row in pending]
        exact = event.usage_evidence == "exact"
        for row in pending:
            row.delivery_state = "completed"
            row.result_state = "exact" if exact else "unknown"
            row.exact_usage_jsonb = (
                {
                    "aggregate": (
                        event.usage.model_dump(mode="json") if event.usage else {}
                    ),
                    "covered_ordinals": covered,
                }
                if exact
                else {}
            )
            row.uncertainty_reason = None if exact else "provider_usage_missing"
            row.evidence_event_seq = event.seq
        return
    ordinal = event.dispatch_ordinal
    if ordinal is None and event.type in {"provider.dispatched", "provider.response"}:
        ordinal = event.turn
    row = await _provider_call_for_event(db, run, ordinal)
    if row is None:
        return
    row.evidence_event_seq = max(int(row.evidence_event_seq or 0), event.seq)
    if event.type == "provider.dispatched":
        row.delivery_state = "dispatched"
    elif event.type == "provider.response":
        row.delivery_state = "responded"
        row.response_status = event.status if isinstance(event.status, int) else None
        if event.no_charge_receipt is True:
            row.result_state = "missing"
    elif event.type == "turn.completed":
        row.delivery_state = "completed"
        exact = event.usage_evidence == "exact"
        row.result_state = "exact" if exact else "unknown"
        row.exact_usage_jsonb = (
            event.usage.model_dump(mode="json") if exact and event.usage else {}
        )
        row.uncertainty_reason = None if exact else "provider_usage_missing"


async def _apply_runtime_checkpoint(
    db: AsyncSession,
    run: AgentRun,
    dispatch: dict[str, Any],
    event: AgentRuntimeEvent,
    dependencies: RuntimeCheckpointDependencies,
) -> None:
    await _checkpoint_provider_call(db, run, event)
    if event.type in {"run.started", "run.heartbeat"}:
        await _checkpoint_runtime_activity(db, run, dispatch, event)
    elif event.type == "provider.dispatched":
        checkpoint_provider_dispatched(dispatch, event)
    elif event.type == "provider.response":
        checkpoint_provider_response(dispatch, event)
    elif event.type == "turn.completed":
        _checkpoint_turn(run, dispatch, event)
    elif event.type == "compaction.completed":
        await _checkpoint_pi_compaction(db, run, dispatch, event)
    elif event.type in {"tool.started", "tool.succeeded", "tool.failed"}:
        if event.type == "tool.failed":
            await _record_runtime_tool_failure(
                db,
                run=run,
                event=event,
                stage_event=dependencies.stage_event,
            )
        else:
            await _record_runtime_local_tool(
                db,
                run=run,
                event=event,
                stage_event=dependencies.stage_event,
            )
    else:
        _checkpoint_terminal(run, dispatch, event)


async def record_runtime_checkpoint(
    dependencies: RuntimeCheckpointDependencies,
    run_id: str,
    execution_epoch: int,
    event: AgentRuntimeEvent,
) -> bool:
    if event.type not in {
        "run.started",
        "run.heartbeat",
        "provider.dispatched",
        "provider.response",
        "turn.completed",
        "compaction.completed",
        "tool.started",
        "tool.succeeded",
        "tool.failed",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }:
        return True
    async with dependencies.session_factory() as db:
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
            dispatch = dispatch_snapshot(run)
            last_runtime_seq = int(dispatch.get("last_runtime_seq") or 0)
            if event.seq <= last_runtime_seq:
                return True
            await _apply_runtime_checkpoint(db, run, dispatch, event, dependencies)
            dispatch["last_runtime_seq"] = event.seq
            run.dispatch_jsonb = dispatch
            return True


__all__ = [
    "RuntimeCheckpointDependencies",
    "add_usage",
    "canonical_usage",
    "dispatch_snapshot",
    "max_usage",
    "record_runtime_checkpoint",
]
