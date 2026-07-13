"""Project Generation/VideoGeneration truth into Canvas execution state."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from arq.cron import cron
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from lumen_core.canvas_models import (
    CanvasAssetRef,
    CanvasDocument,
    CanvasExecutionTask,
    CanvasNodeExecution,
    CanvasNodeSelection,
    CanvasRun,
    CanvasRunEvent,
    CanvasTaskTerminalReceipt,
)
from lumen_core.canvas import (
    canvas_input_snapshot_matches_graph,
    canvas_node_definition_hash,
)
from lumen_core.constants import GenerationStatus, VideoGenerationStatus
from lumen_core.models import Generation, Image, Video, VideoGeneration, new_uuid7

from ..db import SessionLocal, affected_rows

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100
_NONTERMINAL_EXECUTION_STATUSES = (
    "pending",
    "ready",
    "queued",
    "running",
    "reconciling",
    "canceling",
)
_TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "canceled", "expired"})
_SUCCESS_EXECUTION_STATUSES = frozenset({"succeeded", "reused", "skipped"})


@dataclass(frozen=True, slots=True)
class _TaskProjection:
    status: str
    output: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    unresolved_output: bool = False
    task_id: str | None = None
    task_epoch: int = 0


@dataclass(slots=True)
class _ProjectionBatch:
    projections: list[_TaskProjection]
    changed: bool
    unresolved_output: bool
    started_at: datetime | None
    finished_at: datetime | None


def _generation_fingerprint(generation: Generation) -> str | None:
    direct = getattr(generation, "request_fingerprint", None)
    if isinstance(direct, str) and direct:
        return direct
    request = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    value = request.get("request_fingerprint")
    return value if isinstance(value, str) and value else None


def _fingerprint_matches(task: CanvasExecutionTask, real: Any) -> bool:
    expected = getattr(task, "request_fingerprint", None)
    actual = (
        real.request_fingerprint
        if isinstance(getattr(real, "request_fingerprint", None), str)
        else _generation_fingerprint(real)
        if isinstance(real, Generation)
        else None
    )
    return not expected or not actual or expected == actual


def _image_output(task: CanvasExecutionTask, image: Image) -> dict[str, Any]:
    return {
        "type": "image",
        "ordinal": task.ordinal,
        "image_id": image.id,
        "generation_id": task.generation_id,
        "width": image.width,
        "height": image.height,
        "mime": image.mime,
        "sha256": image.sha256,
    }


def _video_output(task: CanvasExecutionTask, video: Video) -> dict[str, Any]:
    return {
        "type": "video",
        "ordinal": task.ordinal,
        "video_id": video.id,
        "video_generation_id": task.video_generation_id,
        "width": video.width,
        "height": video.height,
        "duration_ms": video.duration_ms,
        "mime": video.mime,
        "sha256": video.sha256,
    }


async def _project_generation(
    session: Any,
    task: CanvasExecutionTask,
) -> _TaskProjection | None:
    generation = await session.get(Generation, task.generation_id)
    if generation is None or not _fingerprint_matches(task, generation):
        return None
    status = generation.status
    if status == GenerationStatus.QUEUED.value:
        return _TaskProjection(
            "queued", task_id=generation.id, task_epoch=generation.attempt or 0
        )
    if status == GenerationStatus.RUNNING.value:
        return _TaskProjection(
            "running",
            started_at=generation.started_at,
            task_id=generation.id,
            task_epoch=generation.attempt or 0,
        )
    if status == GenerationStatus.SUCCEEDED.value:
        image = (
            await session.execute(
                select(Image)
                .where(
                    Image.owner_generation_id == generation.id,
                    Image.deleted_at.is_(None),
                )
                .order_by(Image.created_at, Image.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if image is None:
            return _TaskProjection(
                "running",
                started_at=generation.started_at,
                unresolved_output=True,
                task_id=generation.id,
                task_epoch=generation.attempt or 0,
            )
        return _TaskProjection(
            "succeeded",
            output=_image_output(task, image),
            started_at=generation.started_at,
            finished_at=generation.finished_at,
            task_id=generation.id,
            task_epoch=generation.attempt or 0,
        )
    if status == GenerationStatus.CANCELED.value:
        return _TaskProjection(
            "canceled",
            error_code=generation.error_code,
            error_message=generation.error_message,
            started_at=generation.started_at,
            finished_at=generation.finished_at,
            task_id=generation.id,
            task_epoch=generation.attempt or 0,
        )
    if status == GenerationStatus.FAILED.value:
        return _TaskProjection(
            "failed",
            error_code=generation.error_code,
            error_message=generation.error_message,
            started_at=generation.started_at,
            finished_at=generation.finished_at,
            task_id=generation.id,
            task_epoch=generation.attempt or 0,
        )
    return None


async def _project_video_generation(
    session: Any,
    task: CanvasExecutionTask,
) -> _TaskProjection | None:
    generation = await session.get(VideoGeneration, task.video_generation_id)
    if generation is None or not _fingerprint_matches(task, generation):
        return None
    status = generation.status
    if status == VideoGenerationStatus.QUEUED.value:
        return _TaskProjection(
            "queued",
            task_id=generation.id,
            task_epoch=generation.submission_epoch or 0,
        )
    if status in {
        VideoGenerationStatus.SUBMITTING.value,
        VideoGenerationStatus.SUBMIT_UNKNOWN.value,
        VideoGenerationStatus.SUBMITTED.value,
        VideoGenerationStatus.RUNNING.value,
    }:
        return _TaskProjection(
            "running",
            started_at=generation.started_at,
            task_id=generation.id,
            task_epoch=generation.submission_epoch or 0,
        )
    if status == VideoGenerationStatus.SUCCEEDED.value:
        video = (
            await session.execute(
                select(Video)
                .where(
                    Video.owner_generation_id == generation.id,
                    Video.deleted_at.is_(None),
                )
                .order_by(Video.created_at, Video.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if video is None:
            return _TaskProjection(
                "running",
                started_at=generation.started_at,
                unresolved_output=True,
                task_id=generation.id,
                task_epoch=generation.submission_epoch or 0,
            )
        return _TaskProjection(
            "succeeded",
            output=_video_output(task, video),
            started_at=generation.started_at,
            finished_at=generation.finished_at,
            task_id=generation.id,
            task_epoch=generation.submission_epoch or 0,
        )
    terminal_status = {
        VideoGenerationStatus.FAILED.value: "failed",
        VideoGenerationStatus.CANCELED.value: "canceled",
        VideoGenerationStatus.EXPIRED.value: "expired",
    }.get(status)
    if terminal_status is None:
        return None
    return _TaskProjection(
        terminal_status,
        error_code=generation.error_code,
        error_message=generation.error_message,
        started_at=generation.started_at,
        finished_at=generation.finished_at,
        task_id=generation.id,
        task_epoch=generation.submission_epoch or 0,
    )


async def _project_task(
    session: Any,
    task: CanvasExecutionTask,
) -> _TaskProjection | None:
    if task.task_kind == "generation" and task.generation_id:
        return await _project_generation(session, task)
    if task.task_kind == "video_generation" and task.video_generation_id:
        return await _project_video_generation(session, task)
    return None


def _insert_for_session(session: Any, model: Any) -> Any:
    bind = getattr(session, "bind", None)
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    return sqlite_insert(model) if dialect == "sqlite" else pg_insert(model)


def _terminal_fingerprint(projection: _TaskProjection) -> str:
    payload = {
        "status": projection.status,
        "output": projection.output,
        "error_code": projection.error_code,
        "error_message": projection.error_message,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _record_terminal_receipt(
    session: Any,
    task: CanvasExecutionTask,
    projection: _TaskProjection,
) -> bool:
    if projection.task_id is None:
        return True
    fingerprint = _terminal_fingerprint(projection)
    result = await session.execute(
        _insert_for_session(session, CanvasTaskTerminalReceipt)
        .values(
            id=new_uuid7(),
            execution_task_id=task.id,
            task_kind=task.task_kind,
            task_id=projection.task_id,
            task_epoch=projection.task_epoch,
            terminal_status=projection.status,
            terminal_fingerprint=fingerprint,
        )
        .on_conflict_do_nothing(
            index_elements=[
                CanvasTaskTerminalReceipt.task_kind,
                CanvasTaskTerminalReceipt.task_id,
                CanvasTaskTerminalReceipt.task_epoch,
            ]
        )
    )
    if affected_rows(result):
        return True
    existing = (
        await session.execute(
            select(CanvasTaskTerminalReceipt.terminal_fingerprint).where(
                CanvasTaskTerminalReceipt.task_kind == task.task_kind,
                CanvasTaskTerminalReceipt.task_id == projection.task_id,
                CanvasTaskTerminalReceipt.task_epoch == projection.task_epoch,
            )
        )
    ).scalar_one_or_none()
    return existing == fingerprint


def _aggregate_execution_status(statuses: list[str]) -> str | None:
    if not statuses or any(
        status not in _TERMINAL_TASK_STATUSES for status in statuses
    ):
        return None
    succeeded = statuses.count("succeeded")
    if succeeded == len(statuses):
        return "succeeded"
    if succeeded:
        return "partial_failed"
    if all(status == "canceled" for status in statuses):
        return "canceled"
    return "failed"


def _active_execution_status(
    current: str,
    statuses: list[str],
    *,
    unresolved_output: bool,
) -> str:
    if current == "canceling":
        return current
    if unresolved_output:
        return "reconciling"
    if any(status == "running" for status in statuses):
        return "running"
    return "queued"


def _single_run_status(execution_status: str) -> str:
    if execution_status in _SUCCESS_EXECUTION_STATUSES:
        return "succeeded"
    if execution_status == "blocked":
        return "failed"
    if execution_status in {
        "partial_failed",
        "failed",
        "canceled",
        "reconciling",
        "canceling",
        "running",
        "queued",
    }:
        return execution_status
    return "planning"


async def _materialize_asset_refs(
    session: Any,
    execution: CanvasNodeExecution,
    outputs: list[dict[str, Any]],
) -> None:
    existing_rows = (
        await session.execute(
            select(CanvasAssetRef.image_id, CanvasAssetRef.video_id).where(
                CanvasAssetRef.execution_id == execution.id,
                CanvasAssetRef.scope == "execution",
            )
        )
    ).all()
    existing_images = {row.image_id for row in existing_rows if row.image_id}
    existing_videos = {row.video_id for row in existing_rows if row.video_id}
    for output in outputs:
        image_id = output.get("image_id")
        video_id = output.get("video_id")
        if image_id and image_id not in existing_images:
            session.add(
                CanvasAssetRef(
                    id=new_uuid7(),
                    canvas_id=execution.canvas_id,
                    execution_id=execution.id,
                    node_id=execution.node_id,
                    scope="execution",
                    retention_class="history",
                    image_id=image_id,
                )
            )
            existing_images.add(image_id)
        elif video_id and video_id not in existing_videos:
            session.add(
                CanvasAssetRef(
                    id=new_uuid7(),
                    canvas_id=execution.canvas_id,
                    execution_id=execution.id,
                    node_id=execution.node_id,
                    scope="execution",
                    retention_class="history",
                    video_id=video_id,
                )
            )
            existing_videos.add(video_id)


async def _cas_active_output(
    session: Any,
    execution: CanvasNodeExecution,
) -> bool:
    base_revision = int(execution.selection_base_revision or 0)
    values = {
        "execution_id": execution.id,
        "output_index": 0,
        "revision": base_revision + 1,
        "updated_at": datetime.now(timezone.utc),
    }
    result = await session.execute(
        update(CanvasNodeSelection)
        .where(
            CanvasNodeSelection.canvas_id == execution.canvas_id,
            CanvasNodeSelection.node_id == execution.node_id,
            CanvasNodeSelection.revision == base_revision,
            CanvasNodeSelection.locked.is_(False),
        )
        .values(**values)
    )
    if affected_rows(result):
        return True
    if base_revision != 0:
        return False
    result = await session.execute(
        _insert_for_session(session, CanvasNodeSelection)
        .values(
            canvas_id=execution.canvas_id,
            node_id=execution.node_id,
            locked=False,
            **values,
        )
        .on_conflict_do_nothing(
            index_elements=[
                CanvasNodeSelection.canvas_id,
                CanvasNodeSelection.node_id,
            ]
        )
    )
    return bool(affected_rows(result))


def _auto_select_requested(execution: CanvasNodeExecution) -> bool:
    metadata = (execution.config_snapshot_jsonb or {}).get("_canvas", {})
    return not isinstance(metadata, dict) or metadata.get(
        "auto_select_on_success", True
    )


async def _auto_select_is_current(
    session: Any,
    execution: CanvasNodeExecution,
) -> bool:
    if not _auto_select_requested(execution):
        return False
    canvas = (
        await session.execute(
            select(CanvasDocument)
            .where(
                CanvasDocument.id == execution.canvas_id,
                CanvasDocument.user_id == execution.user_id,
                CanvasDocument.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if canvas is None:
        return False
    node = next(
        (
            item
            for item in (canvas.graph_jsonb or {}).get("nodes", [])
            if isinstance(item, dict) and item.get("id") == execution.node_id
        ),
        None,
    )
    if node is None or canvas_node_definition_hash(node) != execution.definition_hash:
        return False
    rows = (
        await session.execute(
            select(CanvasNodeSelection)
            .where(CanvasNodeSelection.canvas_id == execution.canvas_id)
            .with_for_update()
        )
    ).scalars()
    selections = {
        row.node_id: (row.execution_id, int(row.output_index))
        for row in rows
    }
    return canvas_input_snapshot_matches_graph(
        canvas.graph_jsonb,
        node_id=execution.node_id,
        input_snapshot=execution.input_snapshot_jsonb or {},
        selections=selections,
    )


async def _project_execution_tasks(
    session: Any,
    tasks: list[CanvasExecutionTask],
    execution: CanvasNodeExecution,
    *,
    now: datetime,
) -> _ProjectionBatch:
    changed = False
    projections: list[_TaskProjection] = []
    unresolved_output = False
    started_at = execution.started_at
    finished_at = execution.finished_at
    for task in tasks:
        was_terminal = task.status in _TERMINAL_TASK_STATUSES
        projection = await _project_task(session, task)
        if was_terminal:
            if (
                task.status == "succeeded"
                and projection is not None
                and projection.unresolved_output
            ):
                unresolved_output = True
            elif projection is None or projection.status != task.status:
                projection = _TaskProjection(
                    task.status, output=task.output_jsonb or None
                )
            elif not await _record_terminal_receipt(session, task, projection):
                unresolved_output = True
                projection = _TaskProjection(
                    task.status, output=task.output_jsonb or None
                )
            elif task.output_jsonb:
                projection = replace(projection, output=task.output_jsonb)
        elif projection is None:
            projections.append(_TaskProjection(task.status))
            continue
        elif (
            projection.status in _TERMINAL_TASK_STATUSES
            and not await _record_terminal_receipt(session, task, projection)
        ):
            unresolved_output = True
            projections.append(_TaskProjection(task.status))
            continue
        if projection is None:
            projections.append(_TaskProjection(task.status))
            continue
        projections.append(projection)
        unresolved_output = unresolved_output or projection.unresolved_output
        if not was_terminal and task.status != projection.status:
            task.status = projection.status
            task.updated_at = now
            changed = True
        if projection.output is not None and task.output_jsonb != projection.output:
            task.output_jsonb = projection.output
            task.updated_at = now
            changed = True
        if projection.started_at and (
            started_at is None or projection.started_at < started_at
        ):
            started_at = projection.started_at
        if projection.finished_at and (
            finished_at is None or projection.finished_at > finished_at
        ):
            finished_at = projection.finished_at
    return _ProjectionBatch(
        projections,
        changed,
        unresolved_output,
        started_at,
        finished_at,
    )


async def _lock_run(session: Any, run_id: str) -> CanvasRun | None:
    return (
        await session.execute(
            select(CanvasRun).where(CanvasRun.id == run_id).with_for_update()
        )
    ).scalar_one_or_none()


async def _aggregate_single_node_run(
    session: Any,
    run: CanvasRun,
    execution: CanvasNodeExecution,
    *,
    now: datetime,
) -> None:
    rows = (
        await session.execute(
            select(CanvasNodeExecution.id).where(
                CanvasNodeExecution.run_id == execution.run_id
            )
        )
    ).scalars()
    if len(list(rows)) != 1:
        return
    run.status = _single_run_status(execution.status)
    if execution.started_at is not None and run.started_at is None:
        run.started_at = execution.started_at
    if execution.status not in _NONTERMINAL_EXECUTION_STATUSES:
        run.finished_at = execution.finished_at or now
    run.updated_at = now


async def _append_run_event(
    session: Any,
    run: CanvasRun,
    execution: CanvasNodeExecution,
    *,
    event_key: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    existing = (
        await session.execute(
            select(CanvasRunEvent.id).where(
                CanvasRunEvent.run_id == run.id,
                CanvasRunEvent.event_key == event_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    run.last_event_seq = int(run.last_event_seq or 0) + 1
    session.add(
        CanvasRunEvent(
            id=new_uuid7(),
            run_id=run.id,
            seq=run.last_event_seq,
            execution_id=execution.id,
            event_type=event_type,
            event_key=event_key,
            payload_jsonb=payload,
        )
    )


def _first_failure(
    projections: list[_TaskProjection],
) -> tuple[str | None, str | None]:
    for projection in projections:
        if projection.status in {"failed", "expired", "canceled"}:
            return projection.error_code, projection.error_message
    return None, None


async def _reconcile_execution_id(execution_id: str) -> bool:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session, session.begin():
        execution = (
            await session.execute(
                select(CanvasNodeExecution)
                .where(
                    CanvasNodeExecution.id == execution_id,
                    CanvasNodeExecution.status.in_(_NONTERMINAL_EXECUTION_STATUSES),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if execution is None:
            return False
        tasks = list(
            (
                await session.execute(
                    select(CanvasExecutionTask)
                    .where(CanvasExecutionTask.execution_id == execution.id)
                    .order_by(CanvasExecutionTask.ordinal)
                    .with_for_update()
                )
            ).scalars()
        )
        if not tasks:
            return False

        batch = await _project_execution_tasks(
            session,
            tasks,
            execution,
            now=now,
        )
        changed = batch.changed
        projections = batch.projections
        unresolved_output = batch.unresolved_output
        statuses = [projection.status for projection in projections]
        terminal_status = (
            None if unresolved_output else _aggregate_execution_status(statuses)
        )
        next_status = terminal_status or _active_execution_status(
            execution.status,
            statuses,
            unresolved_output=unresolved_output,
        )
        if execution.status != next_status:
            execution.status = next_status
            changed = True
        if batch.started_at is not None and execution.started_at != batch.started_at:
            execution.started_at = batch.started_at
            changed = True
        outputs = [
            projection.output
            for projection in projections
            if projection.status == "succeeded" and projection.output is not None
        ]
        selection_updated = False
        if terminal_status is not None:
            if execution.outputs_jsonb != outputs:
                execution.outputs_jsonb = outputs
                changed = True
            execution.finished_at = batch.finished_at or now
            execution.error_code, execution.error_message = _first_failure(projections)
            await _materialize_asset_refs(session, execution, outputs)
            if outputs and await _auto_select_is_current(session, execution):
                selection_updated = await _cas_active_output(session, execution)
            changed = True
        if not changed:
            execution.updated_at = now
            return False
        execution.updated_at = now

        run = await _lock_run(session, execution.run_id)
        if run is None:
            return True
        await _aggregate_single_node_run(session, run, execution, now=now)
        epoch = int(execution.attempt_epoch or 0)
        event_key = f"execution:{execution.id}:epoch:{epoch}:status:{execution.status}"
        await _append_run_event(
            session,
            run,
            execution,
            event_key=event_key,
            event_type="canvas.execution.status_changed",
            payload={
                "execution_id": execution.id,
                "node_id": execution.node_id,
                "status": execution.status,
                "outputs": outputs if terminal_status is not None else [],
                "selection_updated": selection_updated,
            },
        )
        return True


async def _scan_execution_ids() -> list[str]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(CanvasNodeExecution.id)
                .where(
                    CanvasNodeExecution.status.in_(_NONTERMINAL_EXECUTION_STATUSES),
                    select(CanvasExecutionTask.id)
                    .where(CanvasExecutionTask.execution_id == CanvasNodeExecution.id)
                    .exists(),
                )
                .order_by(CanvasNodeExecution.updated_at, CanvasNodeExecution.id)
                .limit(_BATCH_SIZE)
            )
        ).scalars()
        return list(rows)


async def reconcile_canvas_execution(ctx: dict[str, Any], execution_id: str) -> int:
    """Reconcile one execution; the Redis context is deliberately not authoritative."""
    del ctx
    return int(await _reconcile_execution_id(execution_id))


async def reconcile_canvas_executions(ctx: dict[str, Any]) -> int:
    """Periodically converge active Canvas executions from durable task rows."""
    del ctx
    touched = 0
    for execution_id in await _scan_execution_ids():
        try:
            touched += int(await _reconcile_execution_id(execution_id))
        except Exception:  # noqa: BLE001
            logger.exception(
                "canvas execution reconcile failed execution_id=%s",
                execution_id,
            )
    return touched


cron_jobs = [
    cron(reconcile_canvas_executions, second={10, 40}, run_at_startup=False),
]

__all__ = [
    "cron_jobs",
    "reconcile_canvas_execution",
    "reconcile_canvas_executions",
]
