"""Bounded GET-time repair from durable media task truth."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import (
    CanvasAssetRef,
    CanvasDocument,
    CanvasExecutionTask,
    CanvasNodeExecution,
    CanvasNodeSelection,
    CanvasRun,
    CanvasRunEvent,
)
from lumen_core.canvas import (
    canvas_input_snapshot_matches_graph,
    canvas_node_definition_hash,
)
from lumen_core.canvas_schemas import (
    IMAGE_EXECUTABLE_NODE_TYPES,
    VIDEO_EXECUTABLE_NODE_TYPES,
)
from lumen_core.constants import GenerationStatus, VideoGenerationStatus
from lumen_core.models import Generation, Image, Video, VideoGeneration

from .graph_resolution import find_node
from .run_event_service import append_run_event


_ACTIVE_EXECUTION_STATUSES = (
    "pending",
    "ready",
    "queued",
    "running",
    "reconciling",
    "canceling",
)
_TERMINAL_TASK_STATUSES = {"succeeded", "failed", "canceled", "expired"}


def _request_metadata(row: Any) -> dict[str, Any]:
    value = getattr(row, "upstream_request", None)
    return value if isinstance(value, dict) else {}


async def _repair_missing_links(
    db: AsyncSession,
    *,
    user_id: str,
    executions: list[CanvasNodeExecution],
) -> bool:
    if not executions:
        return False
    execution_ids = [row.id for row in executions]
    linked_ids = set(
        (
            await db.execute(
                select(CanvasExecutionTask.execution_id).where(
                    CanvasExecutionTask.execution_id.in_(execution_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    missing = {row.id: row for row in executions if row.id not in linked_ids}
    if not missing:
        return False
    recent_generations = []
    if any(row.node_type in IMAGE_EXECUTABLE_NODE_TYPES for row in missing.values()):
        recent_generations = (
            (
                await db.execute(
                    select(Generation)
                    .where(Generation.user_id == user_id)
                    .order_by(Generation.created_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    recent_videos = []
    if any(row.node_type in VIDEO_EXECUTABLE_NODE_TYPES for row in missing.values()):
        recent_videos = (
            (
                await db.execute(
                    select(VideoGeneration)
                    .where(VideoGeneration.user_id == user_id)
                    .order_by(VideoGeneration.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    changed = False
    by_execution: dict[str, list[Any]] = {}
    for real in [*recent_generations, *recent_videos]:
        execution_id = _request_metadata(real).get("canvas_execution_id")
        if execution_id in missing:
            by_execution.setdefault(execution_id, []).append(real)
    for execution_id, rows in by_execution.items():
        rows.sort(key=lambda row: (row.created_at, row.id))
        for ordinal, real in enumerate(rows):
            is_video = isinstance(real, VideoGeneration)
            task_kind = "video_generation" if is_video else "generation"
            actual_fingerprint = (
                real.request_fingerprint
                if is_video and isinstance(real.request_fingerprint, str)
                else missing[execution_id].request_fingerprint
            )
            db.add(
                CanvasExecutionTask(
                    execution_id=execution_id,
                    ordinal=ordinal,
                    task_kind=task_kind,
                    generation_id=None if is_video else real.id,
                    video_generation_id=real.id if is_video else None,
                    status="queued",
                    idempotency_key=str(real.id),
                    request_fingerprint=actual_fingerprint,
                    billing_ref_type=task_kind,
                    billing_ref_id=real.id,
                    output_jsonb={},
                )
            )
            changed = True
    if changed:
        await db.flush()
    return changed


async def _project_generation(
    db: AsyncSession,
    task: CanvasExecutionTask,
) -> tuple[str, dict[str, Any] | None, Any]:
    row = await db.get(Generation, task.generation_id)
    if row is None:
        return task.status, None, None
    status = {
        GenerationStatus.QUEUED.value: "queued",
        GenerationStatus.RUNNING.value: "running",
        GenerationStatus.FAILED.value: "failed",
        GenerationStatus.CANCELED.value: "canceled",
    }.get(row.status)
    output = None
    if row.status == GenerationStatus.SUCCEEDED.value:
        image = (
            await db.execute(
                select(Image)
                .where(
                    Image.owner_generation_id == row.id,
                    Image.deleted_at.is_(None),
                )
                .order_by(Image.created_at.asc(), Image.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if image is None:
            return "running", None, row
        status = "succeeded"
        output = {
            "type": "image",
            "image_id": image.id,
            "generation_id": row.id,
            "width": image.width,
            "height": image.height,
            "mime": image.mime,
            "sha256": image.sha256,
        }
    return status or task.status, output, row


async def _project_video(
    db: AsyncSession,
    task: CanvasExecutionTask,
) -> tuple[str, dict[str, Any] | None, Any]:
    row = await db.get(VideoGeneration, task.video_generation_id)
    if row is None:
        return task.status, None, None
    if row.status == VideoGenerationStatus.SUCCEEDED.value:
        video = (
            await db.execute(
                select(Video)
                .where(
                    Video.owner_generation_id == row.id,
                    Video.deleted_at.is_(None),
                )
                .order_by(Video.created_at.asc(), Video.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if video is None:
            return "running", None, row
        return (
            "succeeded",
            {
                "type": "video",
                "video_id": video.id,
                "video_generation_id": row.id,
                "width": video.width,
                "height": video.height,
                "duration_ms": video.duration_ms,
                "mime": video.mime,
                "sha256": video.sha256,
            },
            row,
        )
    status = {
        VideoGenerationStatus.QUEUED.value: "queued",
        VideoGenerationStatus.SUBMITTING.value: "running",
        VideoGenerationStatus.SUBMIT_UNKNOWN.value: "running",
        VideoGenerationStatus.SUBMITTED.value: "running",
        VideoGenerationStatus.RUNNING.value: "running",
        VideoGenerationStatus.FAILED.value: "failed",
        VideoGenerationStatus.CANCELED.value: "canceled",
        VideoGenerationStatus.EXPIRED.value: "expired",
    }.get(row.status)
    return status or task.status, None, row


def _execution_status(statuses: list[str], current: str) -> str:
    if not statuses:
        return current
    terminal = all(status in _TERMINAL_TASK_STATUSES for status in statuses)
    if not terminal:
        if current == "canceling":
            return current
        return "running" if "running" in statuses else "queued"
    succeeded = statuses.count("succeeded")
    if succeeded == len(statuses):
        return "succeeded"
    if succeeded:
        return "partial_failed"
    if all(status == "canceled" for status in statuses):
        return "canceled"
    return "failed"


async def _materialize_asset_refs(
    db: AsyncSession,
    *,
    execution: CanvasNodeExecution,
    outputs: list[dict[str, Any]],
) -> bool:
    existing = (
        await db.execute(
            select(CanvasAssetRef).where(
                CanvasAssetRef.execution_id == execution.id,
                CanvasAssetRef.scope == "execution",
            )
        )
    ).scalars()
    keys = {(row.image_id, row.video_id) for row in existing}
    changed = False
    for output in outputs:
        key = (output.get("image_id"), output.get("video_id"))
        if key in keys:
            continue
        db.add(
            CanvasAssetRef(
                canvas_id=execution.canvas_id,
                execution_id=execution.id,
                node_id=execution.node_id,
                scope="execution",
                retention_class="history",
                image_id=output.get("image_id"),
                video_id=output.get("video_id"),
            )
        )
        keys.add(key)
        changed = True
    return changed


async def _auto_select(
    db: AsyncSession,
    *,
    user_id: str,
    execution: CanvasNodeExecution,
    outputs: list[dict[str, Any]],
) -> bool:
    if not outputs:
        return False
    metadata = execution.config_snapshot_jsonb.get("_canvas", {})
    if not isinstance(metadata, dict) or not metadata.get("auto_select_on_success"):
        return False
    canvas = (
        await db.execute(
            select(CanvasDocument)
            .where(
                CanvasDocument.id == execution.canvas_id,
                CanvasDocument.user_id == user_id,
                CanvasDocument.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if canvas is None:
        return False
    graph = canvas.graph_jsonb
    try:
        current_node = find_node(graph, execution.node_id)
    except Exception:
        return False
    current_definition_hash = canvas_node_definition_hash(current_node)
    if current_definition_hash != execution.definition_hash:
        return False
    selection_rows = list(
        (
            await db.execute(
                select(CanvasNodeSelection)
                .where(CanvasNodeSelection.canvas_id == execution.canvas_id)
                .with_for_update()
            )
        ).scalars()
    )
    selections = {
        row.node_id: (row.execution_id, int(row.output_index)) for row in selection_rows
    }
    try:
        snapshot_matches = canvas_input_snapshot_matches_graph(
            graph,
            node_id=execution.node_id,
            input_snapshot=execution.input_snapshot_jsonb or {},
            selections=selections,
        )
    except (RecursionError, TypeError, ValueError):
        return False
    if not snapshot_matches:
        return False
    expected_revision = int(execution.selection_base_revision or 0)
    selection = next(
        (row for row in selection_rows if row.node_id == execution.node_id),
        None,
    )
    if (
        selection is None
        or selection.locked
        or int(selection.revision) != expected_revision
    ):
        return False
    selection.execution_id = execution.id
    selection.output_index = 0
    selection.revision = expected_revision + 1
    selection.updated_at = datetime.now(timezone.utc)
    return True


async def _reconcile_execution(
    db: AsyncSession,
    *,
    user_id: str,
    execution: CanvasNodeExecution,
) -> bool:
    tasks = list(
        (
            await db.execute(
                select(CanvasExecutionTask)
                .where(CanvasExecutionTask.execution_id == execution.id)
                .order_by(CanvasExecutionTask.ordinal.asc())
                .with_for_update()
            )
        ).scalars()
    )
    if not tasks:
        return False
    changed = False
    outputs: list[dict[str, Any]] = []
    real_rows: list[Any] = []
    for task in tasks:
        if task.task_kind == "generation":
            status, output, real = await _project_generation(db, task)
        elif task.task_kind == "video_generation":
            status, output, real = await _project_video(db, task)
        else:
            continue
        real_rows.append(real)
        if task.status != status:
            task.status = status
            changed = True
        if output is not None:
            outputs.append(output)
            if task.output_jsonb != output:
                task.output_jsonb = output
                changed = True
    statuses = [task.status for task in tasks]
    next_status = _execution_status(statuses, execution.status)
    if execution.status != next_status:
        execution.status = next_status
        changed = True
    if next_status in {"succeeded", "partial_failed", "failed", "canceled"}:
        if execution.outputs_jsonb != outputs:
            execution.outputs_jsonb = outputs
            changed = True
        execution.finished_at = max(
            (
                row.finished_at
                for row in real_rows
                if row is not None and row.finished_at is not None
            ),
            default=datetime.now(timezone.utc),
        )
        failure = next(
            (
                row
                for row in real_rows
                if row is not None
                and getattr(row, "status", None)
                in {
                    GenerationStatus.FAILED.value,
                    VideoGenerationStatus.FAILED.value,
                    VideoGenerationStatus.EXPIRED.value,
                }
            ),
            None,
        )
        execution.error_code = getattr(failure, "error_code", None)
        execution.error_message = getattr(failure, "error_message", None)
        asset_refs_added = await _materialize_asset_refs(
            db,
            execution=execution,
            outputs=outputs,
        )
        selection_updated = await _auto_select(
            db,
            user_id=user_id,
            execution=execution,
            outputs=outputs,
        )
        changed = changed or asset_refs_added or selection_updated
    else:
        selection_updated = False
    if not changed:
        return False
    run = (
        await db.execute(
            select(CanvasRun).where(CanvasRun.id == execution.run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is not None:
        run.status = (
            "succeeded"
            if next_status in {"succeeded", "reused", "skipped"}
            else next_status
            if next_status
            in {
                "partial_failed",
                "failed",
                "canceled",
                "running",
                "reconciling",
                "canceling",
                "queued",
            }
            else run.status
        )
        if next_status not in _ACTIVE_EXECUTION_STATUSES:
            run.finished_at = execution.finished_at or datetime.now(timezone.utc)
        event_key = (
            f"execution:{execution.id}:epoch:{execution.attempt_epoch}:"
            f"status:{next_status}"
        )
        event_exists = (
            await db.execute(
                select(CanvasRunEvent.id).where(
                    CanvasRunEvent.run_id == run.id,
                    CanvasRunEvent.event_key == event_key,
                )
            )
        ).scalar_one_or_none()
        if event_exists is None:
            await append_run_event(
                db,
                run=run,
                execution=execution,
                event_type="canvas.execution.status_changed",
                event_key=event_key,
                payload={
                    "execution_id": execution.id,
                    "node_id": execution.node_id,
                    "status": next_status,
                    "outputs": outputs,
                    "selection_updated": selection_updated,
                },
            )
    return True


async def repair_canvas_executions(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    limit: int = 20,
) -> int:
    executions = list(
        (
            await db.execute(
                select(CanvasNodeExecution)
                .where(
                    CanvasNodeExecution.canvas_id == canvas_id,
                    CanvasNodeExecution.user_id == user_id,
                    CanvasNodeExecution.status.in_(_ACTIVE_EXECUTION_STATUSES),
                )
                .order_by(CanvasNodeExecution.updated_at.asc())
                .limit(limit)
                .with_for_update()
            )
        ).scalars()
    )
    changed = await _repair_missing_links(
        db,
        user_id=user_id,
        executions=executions,
    )
    touched = 0
    for execution in executions:
        if await _reconcile_execution(
            db,
            user_id=user_id,
            execution=execution,
        ):
            touched += 1
    if changed or touched:
        await db.commit()
    return touched
