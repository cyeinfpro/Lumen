"""Serialize Canvas runs, executions, selections, and media URLs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import (
    CanvasExecutionTask,
    CanvasNodeExecution,
    CanvasNodeSelection,
    CanvasRun,
    CanvasRunEvent,
)
from lumen_core.models import VideoGeneration

from .document_service import get_owned_canvas
from .document_service import document_dict
from .errors import canvas_http
from .version_service import selection_dict


_ACTIVE_RUN_STATUSES = (
    "planning",
    "queued",
    "running",
    "paused",
    "reconciling",
    "canceling",
)


def output_dict(output: dict[str, Any]) -> dict[str, Any]:
    item = dict(output)
    image_id = item.get("image_id")
    video_id = item.get("video_id")
    if isinstance(image_id, str) and image_id:
        item["url"] = f"/api/images/{image_id}/binary"
        item["preview_url"] = f"/api/images/{image_id}/variants/preview1024"
    if isinstance(video_id, str) and video_id:
        item["url"] = f"/api/videos/{video_id}/binary"
        item["poster_url"] = f"/api/videos/{video_id}/poster"
    return item


def _elapsed_ms(row: Any) -> int | None:
    started_at = (
        getattr(row, "started_at", None)
        or getattr(row, "submit_started_at", None)
        or getattr(row, "created_at", None)
    )
    if not isinstance(started_at, datetime):
        return None
    finished_at = getattr(row, "finished_at", None)
    if not isinstance(finished_at, datetime):
        finished_at = datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    else:
        started_at = started_at.astimezone(timezone.utc)
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    else:
        finished_at = finished_at.astimezone(timezone.utc)
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def execution_task_dict(
    task: CanvasExecutionTask,
    owner: Any | None,
) -> dict[str, Any]:
    source = owner or task
    progress_pct = getattr(source, "progress_pct", None)
    if not isinstance(progress_pct, int):
        progress_pct = None
    return {
        "id": task.id,
        "kind": task.task_kind,
        "status": getattr(source, "status", task.status),
        "progress_stage": getattr(source, "progress_stage", task.status),
        "progress_pct": progress_pct,
        "generation_id": task.generation_id,
        "completion_id": task.completion_id,
        "video_generation_id": task.video_generation_id,
        "model": getattr(source, "model", None),
        "provider_name": getattr(source, "provider_name", None),
        "provider_kind": getattr(source, "provider_kind", None),
        "action": getattr(source, "action", None),
        "duration_s": getattr(source, "duration_s", None),
        "resolution": getattr(source, "resolution", None),
        "aspect_ratio": getattr(source, "aspect_ratio", None),
        "size_requested": getattr(source, "size_requested", None),
        "generate_audio": getattr(source, "generate_audio", None),
        "attempt": getattr(source, "attempt", None),
        "elapsed_ms": _elapsed_ms(source),
        "error_code": getattr(source, "error_code", None),
        "error_message": getattr(source, "error_message", None),
        "created_at": getattr(source, "created_at", task.created_at),
        "updated_at": getattr(source, "updated_at", task.updated_at),
        "started_at": getattr(source, "started_at", None),
        "submit_started_at": getattr(source, "submit_started_at", None),
        "submitted_at": getattr(source, "submitted_at", None),
        "finished_at": getattr(source, "finished_at", None),
    }


async def execution_tasks_by_execution(
    db: AsyncSession,
    executions: list[CanvasNodeExecution],
) -> dict[str, list[dict[str, Any]]]:
    execution_ids = [row.id for row in executions]
    if not execution_ids:
        return {}
    tasks = list(
        (
            await db.execute(
                select(CanvasExecutionTask)
                .where(CanvasExecutionTask.execution_id.in_(execution_ids))
                .order_by(
                    CanvasExecutionTask.execution_id.asc(),
                    CanvasExecutionTask.ordinal.asc(),
                )
            )
        ).scalars()
    )
    if not tasks:
        return {}

    video_generations: dict[str, VideoGeneration] = {}
    video_generation_ids = [
        row.video_generation_id for row in tasks if row.video_generation_id
    ]
    if video_generation_ids:
        rows = (
            await db.execute(
                select(VideoGeneration).where(
                    VideoGeneration.id.in_(video_generation_ids)
                )
            )
        ).scalars()
        video_generations = {row.id: row for row in rows}

    details: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        owner = video_generations.get(task.video_generation_id or "")
        details.setdefault(task.execution_id, []).append(
            execution_task_dict(task, owner)
        )
    return details


def execution_dict(
    row: CanvasNodeExecution,
    tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "node_id": row.node_id,
        "node_type": row.node_type,
        "status": row.status,
        "attempt": int(row.attempt),
        "execution_fingerprint": row.execution_fingerprint,
        "outputs": [
            output_dict(item)
            for item in (row.outputs_jsonb or [])
            if isinstance(item, dict)
        ],
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "tasks": tasks or [],
    }


def run_dict(row: CanvasRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "canvas_id": row.canvas_id,
        "version_id": row.version_id,
        "parent_run_id": row.parent_run_id,
        "kind": row.kind,
        "status": row.status,
        "target_node_ids": list(row.target_node_ids or []),
        "last_event_seq": int(row.last_event_seq),
        "estimated_cost_micro": int(row.estimated_cost_micro),
        "actual_cost_micro": row.actual_cost_micro,
        "summary": row.summary_jsonb or {},
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def run_event_dict(row: CanvasRunEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "seq": int(row.seq),
        "execution_id": row.execution_id,
        "event_type": row.event_type,
        "event_key": row.event_key,
        "payload": row.payload_jsonb or {},
        "created_at": row.created_at,
    }


async def serialize_submission(
    db: AsyncSession,
    *,
    run: CanvasRun,
    execution: CanvasNodeExecution,
) -> dict[str, Any]:
    await db.refresh(run)
    await db.refresh(execution)
    tasks = await execution_tasks_by_execution(db, [execution])
    return {
        "run": run_dict(run),
        "execution": execution_dict(execution, tasks.get(execution.id)),
    }


async def canvas_projections(
    db: AsyncSession,
    *,
    canvas_id: str,
    execution_limit: int = 50,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selections = list(
        (
            await db.execute(
                select(CanvasNodeSelection)
                .where(
                    CanvasNodeSelection.canvas_id == canvas_id,
                    CanvasNodeSelection.execution_id.is_not(None),
                )
                .order_by(CanvasNodeSelection.node_id.asc())
            )
        ).scalars()
    )
    executions = list(
        (
            await db.execute(
                select(CanvasNodeExecution)
                .where(CanvasNodeExecution.canvas_id == canvas_id)
                .order_by(
                    CanvasNodeExecution.created_at.desc(),
                    CanvasNodeExecution.id.desc(),
                )
                .limit(execution_limit)
            )
        ).scalars()
    )
    execution_ids = {row.id for row in executions}
    pinned_execution_ids = [
        edge["pinned_execution_id"]
        for edge in (graph or {}).get("edges", [])
        if isinstance(edge, dict)
        and edge.get("binding_mode") == "pinned"
        and isinstance(edge.get("pinned_execution_id"), str)
    ]
    projected_execution_ids = list(
        dict.fromkeys(
            [
                row.execution_id
                for row in selections
                if row.execution_id is not None
                and row.execution_id not in execution_ids
            ]
            + [
                execution_id
                for execution_id in pinned_execution_ids
                if execution_id not in execution_ids
            ]
        )
    )
    if projected_execution_ids:
        projected_executions = list(
            (
                await db.execute(
                    select(CanvasNodeExecution).where(
                        CanvasNodeExecution.canvas_id == canvas_id,
                        CanvasNodeExecution.id.in_(projected_execution_ids),
                    )
                )
            ).scalars()
        )
        projected_by_id = {row.id: row for row in projected_executions}
        executions.extend(
            projected_by_id[execution_id]
            for execution_id in projected_execution_ids
            if execution_id in projected_by_id
        )
    runs = list(
        (
            await db.execute(
                select(CanvasRun)
                .where(
                    CanvasRun.canvas_id == canvas_id,
                    CanvasRun.status.in_(_ACTIVE_RUN_STATUSES),
                )
                .order_by(CanvasRun.created_at.desc())
                .limit(20)
            )
        ).scalars()
    )
    tasks = await execution_tasks_by_execution(db, executions)
    return {
        "selections": [selection_dict(row) for row in selections],
        "recent_executions": [
            execution_dict(row, tasks.get(row.id)) for row in executions
        ],
        "active_runs": [run_dict(row) for row in runs],
    }


async def serialize_canvas_document(
    db: AsyncSession,
    *,
    canvas: Any,
) -> dict[str, Any]:
    return {
        **document_dict(canvas),
        **await canvas_projections(
            db,
            canvas_id=canvas.id,
            graph=canvas.graph_jsonb,
        ),
    }


async def list_runs(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    await get_owned_canvas(db, user_id=user_id, canvas_id=canvas_id)
    rows = list(
        (
            await db.execute(
                select(CanvasRun)
                .where(
                    CanvasRun.canvas_id == canvas_id,
                    CanvasRun.user_id == user_id,
                )
                .order_by(CanvasRun.created_at.desc())
                .limit(limit)
            )
        ).scalars()
    )
    return [run_dict(row) for row in rows]


async def get_run_detail(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    run_id: str,
) -> dict[str, Any]:
    await get_owned_canvas(db, user_id=user_id, canvas_id=canvas_id)
    run = (
        await db.execute(
            select(CanvasRun).where(
                CanvasRun.id == run_id,
                CanvasRun.canvas_id == canvas_id,
                CanvasRun.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise canvas_http("not_found", "canvas run not found", 404)
    executions = list(
        (
            await db.execute(
                select(CanvasNodeExecution)
                .where(CanvasNodeExecution.run_id == run.id)
                .order_by(CanvasNodeExecution.sequence.asc())
            )
        ).scalars()
    )
    tasks = await execution_tasks_by_execution(db, executions)
    return {
        **run_dict(run),
        "executions": [
            execution_dict(row, tasks.get(row.id)) for row in executions
        ],
    }


async def list_run_events(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    run_id: str,
    after_seq: int,
    limit: int,
) -> list[dict[str, Any]]:
    await get_owned_canvas(db, user_id=user_id, canvas_id=canvas_id)
    run = (
        await db.execute(
            select(CanvasRun.id).where(
                CanvasRun.id == run_id,
                CanvasRun.canvas_id == canvas_id,
                CanvasRun.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise canvas_http("not_found", "canvas run not found", 404)
    rows = list(
        (
            await db.execute(
                select(CanvasRunEvent)
                .where(
                    CanvasRunEvent.run_id == run_id,
                    CanvasRunEvent.seq > after_seq,
                )
                .order_by(CanvasRunEvent.seq.asc())
                .limit(limit)
            )
        ).scalars()
    )
    return [run_event_dict(row) for row in rows]
