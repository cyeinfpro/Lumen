"""Sanitized Agent NDJSON sections for the current-user export archive."""

from __future__ import annotations

import asyncio
import json
import zipfile
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import AgentRun, AgentSession, AgentToolCall, Conversation


_EXPORT_BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class ExportAgentSessionDescriptor:
    id: str
    conversation_id: str
    runtime_version: str
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExportAgentRunDescriptor:
    id: str
    agent_session_id: str
    user_message_id: str
    assistant_message_id: str
    status: str
    execution_epoch: int
    model: str | None
    reasoning_effort: str | None
    turn_count: int
    tool_call_count: int
    usage: dict[str, Any]
    error_code: str | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExportAgentToolCallDescriptor:
    id: str
    agent_run_id: str
    ordinal: int
    name: str
    mode: str | None
    status: str
    arguments: dict[str, Any]
    generation_ids: list[str]
    error_code: str | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class AgentExportStats:
    sessions: int
    runs: int
    tool_calls: int


async def _query_batch_rows(
    db: AsyncSession,
    statement: Any,
) -> Sequence[Any]:
    try:
        rows = (await db.execute(statement)).all()
    finally:
        await db.rollback()
    return rows


async def iter_export_agent_session_batches(
    db: AsyncSession,
    user_id: str,
) -> AsyncIterator[tuple[ExportAgentSessionDescriptor, ...]]:
    last_created_at: datetime | None = None
    last_id: str | None = None
    while True:
        filters = [
            AgentSession.user_id == user_id,
            Conversation.user_id == user_id,
        ]
        if last_created_at is not None and last_id is not None:
            filters.append(
                or_(
                    AgentSession.created_at > last_created_at,
                    and_(
                        AgentSession.created_at == last_created_at,
                        AgentSession.id > last_id,
                    ),
                )
            )
        rows = await _query_batch_rows(
            db,
            select(
                AgentSession.id.label("id"),
                AgentSession.conversation_id.label("conversation_id"),
                AgentSession.runtime_version.label("runtime_version"),
                AgentSession.created_at.label("created_at"),
                AgentSession.updated_at.label("updated_at"),
            )
            .join(Conversation, Conversation.id == AgentSession.conversation_id)
            .where(*filters)
            .order_by(AgentSession.created_at.asc(), AgentSession.id.asc())
            .limit(_EXPORT_BATCH_SIZE),
        )
        if not rows:
            return
        last_created_at = rows[-1].created_at
        last_id = rows[-1].id
        yield tuple(
            ExportAgentSessionDescriptor(
                id=row.id,
                conversation_id=row.conversation_id,
                runtime_version=row.runtime_version,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        )


async def iter_export_agent_run_batches(
    db: AsyncSession,
    user_id: str,
) -> AsyncIterator[tuple[ExportAgentRunDescriptor, ...]]:
    last_created_at: datetime | None = None
    last_id: str | None = None
    while True:
        filters = [AgentRun.user_id == user_id]
        if last_created_at is not None and last_id is not None:
            filters.append(
                or_(
                    AgentRun.created_at > last_created_at,
                    and_(AgentRun.created_at == last_created_at, AgentRun.id > last_id),
                )
            )
        rows = await _query_batch_rows(
            db,
            select(
                AgentRun.id.label("id"),
                AgentRun.agent_session_id.label("agent_session_id"),
                AgentRun.user_message_id.label("user_message_id"),
                AgentRun.assistant_message_id.label("assistant_message_id"),
                AgentRun.status.label("status"),
                AgentRun.execution_epoch.label("execution_epoch"),
                AgentRun.model.label("model"),
                AgentRun.reasoning_effort.label("reasoning_effort"),
                AgentRun.turn_count.label("turn_count"),
                AgentRun.tool_call_count.label("tool_call_count"),
                AgentRun.usage_jsonb.label("usage_jsonb"),
                AgentRun.error_code.label("error_code"),
                AgentRun.created_at.label("created_at"),
                AgentRun.updated_at.label("updated_at"),
            )
            .where(*filters)
            .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
            .limit(_EXPORT_BATCH_SIZE),
        )
        if not rows:
            return
        last_created_at = rows[-1].created_at
        last_id = rows[-1].id
        yield tuple(
            ExportAgentRunDescriptor(
                id=row.id,
                agent_session_id=row.agent_session_id,
                user_message_id=row.user_message_id,
                assistant_message_id=row.assistant_message_id,
                status=row.status,
                execution_epoch=int(row.execution_epoch or 0),
                model=row.model,
                reasoning_effort=row.reasoning_effort,
                turn_count=int(row.turn_count or 0),
                tool_call_count=int(row.tool_call_count or 0),
                usage=row.usage_jsonb if isinstance(row.usage_jsonb, dict) else {},
                error_code=row.error_code,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        )


def _safe_tool_arguments(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "prompt",
        "reference_labels",
        "count",
        "aspect_ratio",
        "quality",
        "render_quality",
        "background",
        "output_format",
    }
    return {key: value[key] for key in allowed if key in value}


def _safe_generation_ids(value: Any) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("generation_ids"), list):
        return []
    return [
        item for item in value["generation_ids"] if isinstance(item, str)
    ][:4]


async def iter_export_agent_tool_call_batches(
    db: AsyncSession,
    user_id: str,
) -> AsyncIterator[tuple[ExportAgentToolCallDescriptor, ...]]:
    last_created_at: datetime | None = None
    last_id: str | None = None
    while True:
        filters = [AgentRun.user_id == user_id]
        if last_created_at is not None and last_id is not None:
            filters.append(
                or_(
                    AgentToolCall.created_at > last_created_at,
                    and_(
                        AgentToolCall.created_at == last_created_at,
                        AgentToolCall.id > last_id,
                    ),
                )
            )
        rows = await _query_batch_rows(
            db,
            select(
                AgentToolCall.id.label("id"),
                AgentToolCall.agent_run_id.label("agent_run_id"),
                AgentToolCall.ordinal.label("ordinal"),
                AgentToolCall.name.label("name"),
                AgentToolCall.mode.label("mode"),
                AgentToolCall.status.label("status"),
                AgentToolCall.arguments_jsonb.label("arguments_jsonb"),
                AgentToolCall.result_jsonb.label("result_jsonb"),
                AgentToolCall.error_code.label("error_code"),
                AgentToolCall.created_at.label("created_at"),
                AgentToolCall.updated_at.label("updated_at"),
            )
            .join(AgentRun, AgentRun.id == AgentToolCall.agent_run_id)
            .where(*filters)
            .order_by(AgentToolCall.created_at.asc(), AgentToolCall.id.asc())
            .limit(_EXPORT_BATCH_SIZE),
        )
        if not rows:
            return
        last_created_at = rows[-1].created_at
        last_id = rows[-1].id
        yield tuple(
            ExportAgentToolCallDescriptor(
                id=row.id,
                agent_run_id=row.agent_run_id,
                ordinal=int(row.ordinal),
                name=row.name,
                mode=row.mode,
                status=row.status,
                arguments=_safe_tool_arguments(row.arguments_jsonb),
                generation_ids=_safe_generation_ids(row.result_jsonb),
                error_code=row.error_code,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _write_ndjson_records(
    output: BinaryIO,
    records: Sequence[dict[str, Any]],
) -> int:
    for record in records:
        await asyncio.to_thread(
            output.write,
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n",
        )
    return len(records)


async def _export_agent_sessions(
    db: AsyncSession,
    archive: zipfile.ZipFile,
    user_id: str,
    batches: Callable[..., AsyncIterator[tuple[ExportAgentSessionDescriptor, ...]]],
) -> int:
    exported = 0
    with archive.open("agent-sessions.ndjson", "w") as output:
        async for rows in batches(db, user_id):
            exported += await _write_ndjson_records(
                output,
                [
                    {
                        "id": row.id,
                        "conversation_id": row.conversation_id,
                        "runtime_version": row.runtime_version,
                        "created_at": _iso(row.created_at),
                        "updated_at": _iso(row.updated_at),
                    }
                    for row in rows
                ],
            )
    return exported


async def _export_agent_runs(
    db: AsyncSession,
    archive: zipfile.ZipFile,
    user_id: str,
    batches: Callable[..., AsyncIterator[tuple[ExportAgentRunDescriptor, ...]]],
) -> int:
    exported = 0
    with archive.open("agent-runs.ndjson", "w") as output:
        async for rows in batches(db, user_id):
            exported += await _write_ndjson_records(
                output,
                [
                    {
                        "id": row.id,
                        "agent_session_id": row.agent_session_id,
                        "user_message_id": row.user_message_id,
                        "assistant_message_id": row.assistant_message_id,
                        "status": row.status,
                        "execution_epoch": row.execution_epoch,
                        "model": row.model,
                        "reasoning_effort": row.reasoning_effort,
                        "turn_count": row.turn_count,
                        "tool_call_count": row.tool_call_count,
                        "usage": row.usage,
                        "error_code": row.error_code,
                        "created_at": _iso(row.created_at),
                        "updated_at": _iso(row.updated_at),
                    }
                    for row in rows
                ],
            )
    return exported


async def _export_agent_tool_calls(
    db: AsyncSession,
    archive: zipfile.ZipFile,
    user_id: str,
    batches: Callable[..., AsyncIterator[tuple[ExportAgentToolCallDescriptor, ...]]],
) -> int:
    exported = 0
    with archive.open("agent-tool-calls.ndjson", "w") as output:
        async for rows in batches(db, user_id):
            exported += await _write_ndjson_records(
                output,
                [
                    {
                        "id": row.id,
                        "agent_run_id": row.agent_run_id,
                        "ordinal": row.ordinal,
                        "name": row.name,
                        "mode": row.mode,
                        "status": row.status,
                        "arguments": row.arguments,
                        "generation_ids": row.generation_ids,
                        "error_code": row.error_code,
                        "created_at": _iso(row.created_at),
                        "updated_at": _iso(row.updated_at),
                    }
                    for row in rows
                ],
            )
    return exported


async def export_agent_data(
    db: AsyncSession,
    archive: zipfile.ZipFile,
    user_id: str,
    *,
    session_batches: Callable[
        ..., AsyncIterator[tuple[ExportAgentSessionDescriptor, ...]]
    ] = iter_export_agent_session_batches,
    run_batches: Callable[
        ..., AsyncIterator[tuple[ExportAgentRunDescriptor, ...]]
    ] = iter_export_agent_run_batches,
    tool_batches: Callable[
        ..., AsyncIterator[tuple[ExportAgentToolCallDescriptor, ...]]
    ] = iter_export_agent_tool_call_batches,
) -> AgentExportStats:
    return AgentExportStats(
        sessions=await _export_agent_sessions(
            db, archive, user_id, session_batches
        ),
        runs=await _export_agent_runs(db, archive, user_id, run_batches),
        tool_calls=await _export_agent_tool_calls(
            db, archive, user_id, tool_batches
        ),
    )


__all__ = [
    "AgentExportStats",
    "ExportAgentRunDescriptor",
    "ExportAgentSessionDescriptor",
    "ExportAgentToolCallDescriptor",
    "export_agent_data",
    "iter_export_agent_run_batches",
    "iter_export_agent_session_batches",
    "iter_export_agent_tool_call_batches",
]
