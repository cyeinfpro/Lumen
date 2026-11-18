"""CQRS read-only Canvas projection and explicit background write repair."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas import canvas_input_snapshot_matches_graph, canvas_node_definition_hash
from lumen_core.canvas_models import (
    CanvasAssetRef, CanvasDocument, CanvasExecutionTask,
    CanvasNodeExecution, CanvasNodeSelection, CanvasRun, CanvasRunEvent,
)
from lumen_core.canvas_output_selection import canvas_auto_select_output_index
from lumen_core.canvas_schemas import IMAGE_EXECUTABLE_NODE_TYPES, VIDEO_EXECUTABLE_NODE_TYPES
from lumen_core.constants import GenerationStatus, VideoGenerationStatus
from lumen_core.models import Generation, Image, Video, VideoGeneration, new_uuid7

from .. import runtime as api_runtime  # noqa: F401
from ..arq_pool import get_arq_pool
from ..redis_client import get_redis
from .event_commit import commit_canvas_events
from .graph_resolution import find_node
from .identity_fence import lock_canvas_write_identity
from .progress_events import record_canvas_progress
from .run_event_service import append_run_event

logger = logging.getLogger(__name__)

_ACTIVE_EXECUTION_STATUSES = ("pending", "ready", "queued", "running", "reconciling", "canceling")
_TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "canceled", "expired"})
_TERMINAL_EXECUTION_STATUSES = frozenset({"succeeded", "partial_failed", "failed", "canceled"})
_REPAIR_DUE_KEY_PREFIX = "canvas:repair_due"
_REPAIR_DUE_TTL_SECONDS = 15
_REPAIR_ENQUEUE_TIMEOUT_S = 1.5
_RECONCILE_WORKER_JOB = "reconcile_canvas_execution"
_OVERLAY_INSTALLED_KEY = "canvas_readonly_projection_overlay"


@dataclass(frozen=True, slots=True)
class _ReadProjectionOverlay:
    synthetic_tasks: tuple[CanvasExecutionTask, ...]
    synthetic_selections: tuple[CanvasNodeSelection, ...]
    converged_single_run_ids: frozenset[str]


class _OverlayScalars(list[Any]):
    def all(self) -> list[Any]:
        return list(self)


class _OverlayResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _OverlayScalars:
        return _OverlayScalars(self._rows)

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        if len(self._rows) != 1:
            raise ValueError(f"expected 1 row, got {len(self._rows)}")
        return self._rows[0]


def _request_metadata(row: Any) -> dict[str, Any]:
    value = getattr(row, "upstream_request", None)
    return value if isinstance(value, dict) else {}


def _dialect_is_postgresql(db: AsyncSession) -> bool:
    bind = getattr(db, "bind", None) or getattr(
        getattr(db, "sync_session", None), "bind", None
    )
    return getattr(getattr(bind, "dialect", None), "name", None) == "postgresql"


def _build_missing_task(
    execution: CanvasNodeExecution, *, ordinal: int, real: Any
) -> CanvasExecutionTask:
    is_video = isinstance(real, VideoGeneration)
    task_kind = "video_generation" if is_video else "generation"
    actual_fingerprint = (
        real.request_fingerprint
        if is_video and isinstance(real.request_fingerprint, str)
        else execution.request_fingerprint
    )
    created_at = getattr(real, "created_at", None) or datetime.now(timezone.utc)
    task = CanvasExecutionTask(
        id=new_uuid7(), execution_id=execution.id, ordinal=ordinal, task_kind=task_kind,
        generation_id=None if is_video else real.id,
        video_generation_id=real.id if is_video else None,
        status="queued", idempotency_key=str(real.id),
        request_fingerprint=actual_fingerprint,
        billing_ref_type=task_kind, billing_ref_id=real.id, output_jsonb={},
    )
    if hasattr(task, "created_at"):
        task.created_at = created_at
    if hasattr(task, "updated_at"):
        task.updated_at = getattr(real, "updated_at", None) or created_at
    return task


async def _fetch_missing_owners(
    db: AsyncSession, *, model: Any, user_id: str, missing_ids: tuple[str, ...]
) -> list[Any]:
    filters: list[Any] = [
        model.user_id == user_id,
        model.upstream_request["canvas_execution_id"].as_string().in_(missing_ids),
    ]
    if _dialect_is_postgresql(db):
        filters.insert(1, text("upstream_request ? 'canvas_execution_id'"))
    stmt = select(model).where(*filters).order_by(model.created_at.asc(), model.id.asc())
    return list((await db.execute(stmt)).scalars())


async def _discover_missing_tasks(
    db: AsyncSession, *, user_id: str, missing: dict[str, CanvasNodeExecution]
) -> list[CanvasExecutionTask]:
    if not missing:
        return []
    missing_ids = tuple(missing)
    owners: list[Any] = []
    if any(row.node_type in IMAGE_EXECUTABLE_NODE_TYPES for row in missing.values()):
        owners.extend(
            await _fetch_missing_owners(
                db, model=Generation, user_id=user_id, missing_ids=missing_ids
            )
        )
    if any(row.node_type in VIDEO_EXECUTABLE_NODE_TYPES for row in missing.values()):
        owners.extend(
            await _fetch_missing_owners(
                db, model=VideoGeneration, user_id=user_id, missing_ids=missing_ids
            )
        )
    by_execution: dict[str, list[Any]] = {}
    for real in owners:
        exec_id = _request_metadata(real).get("canvas_execution_id")
        if isinstance(exec_id, str) and exec_id in missing:
            by_execution.setdefault(exec_id, []).append(real)
    created: list[CanvasExecutionTask] = []
    for exec_id, rows in by_execution.items():
        rows.sort(key=lambda row: (row.created_at, row.id))
        for ordinal, real in enumerate(rows):
            created.append(_build_missing_task(missing[exec_id], ordinal=ordinal, real=real))
    return created


async def _repair_missing_links(
    db: AsyncSession, *, user_id: str, executions: list[CanvasNodeExecution]
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
        ).scalars()
    )
    missing = {row.id: row for row in executions if row.id not in linked_ids}
    discovered = await _discover_missing_tasks(db, user_id=user_id, missing=missing)
    for task in discovered:
        db.add(task)
    if discovered:
        await db.flush()
    return bool(discovered)


def _map_generation_row(
    task: CanvasExecutionTask, row: Generation | None, image: Image | None
) -> tuple[str, dict[str, Any] | None, Any]:
    if row is None:
        return task.status, None, None
    if row.status == GenerationStatus.SUCCEEDED.value:
        if image is None:
            return "running", None, row
        output = {
            "type": "image", "image_id": image.id, "generation_id": row.id,
            "width": image.width, "height": image.height, "mime": image.mime, "sha256": image.sha256,
        }
        return "succeeded", output, row
    status = {
        GenerationStatus.QUEUED.value: "queued", GenerationStatus.RUNNING.value: "running",
        GenerationStatus.FAILED.value: "failed", GenerationStatus.CANCELED.value: "canceled",
    }.get(row.status)
    return status or task.status, None, row


def _map_video_row(
    task: CanvasExecutionTask, row: VideoGeneration | None, video: Video | None
) -> tuple[str, dict[str, Any] | None, Any]:
    if row is None:
        return task.status, None, None
    if row.status == VideoGenerationStatus.SUCCEEDED.value:
        if video is None:
            return "running", None, row
        output = {
            "type": "video", "video_id": video.id, "video_generation_id": row.id,
            "width": video.width, "height": video.height, "duration_ms": video.duration_ms,
            "mime": video.mime, "sha256": video.sha256,
        }
        return "succeeded", output, row
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


async def _first_media_by_owner(
    db: AsyncSession, *, model: Any, owner_ids: list[str]
) -> dict[str, Any]:
    if not owner_ids:
        return {}
    rows = (
        await db.execute(
            select(model)
            .where(model.owner_generation_id.in_(owner_ids), model.deleted_at.is_(None))
            .order_by(model.created_at.asc(), model.id.asc())
        )
    ).scalars()
    by_owner: dict[str, Any] = {}
    for row in rows:
        if row.owner_generation_id is not None:
            by_owner.setdefault(row.owner_generation_id, row)
    return by_owner


async def _project_generations_batch(
    db: AsyncSession, tasks: list[CanvasExecutionTask]
) -> dict[int, tuple[str, dict[str, Any] | None, Any]]:
    ids = list(dict.fromkeys(t.generation_id for t in tasks if t.generation_id))
    rows_by_id = (
        {
            r.id: r
            for r in (
                await db.execute(select(Generation).where(Generation.id.in_(ids)))
            ).scalars()
        }
        if ids
        else {}
    )
    succeeded = [
        r.id for r in rows_by_id.values() if r.status == GenerationStatus.SUCCEEDED.value
    ]
    images = await _first_media_by_owner(db, model=Image, owner_ids=succeeded)
    return {
        id(t): _map_generation_row(
            t, rows_by_id.get(t.generation_id or ""), images.get(t.generation_id or "")
        )
        for t in tasks
    }


async def _project_videos_batch(
    db: AsyncSession, tasks: list[CanvasExecutionTask]
) -> dict[int, tuple[str, dict[str, Any] | None, Any]]:
    ids = list(
        dict.fromkeys(t.video_generation_id for t in tasks if t.video_generation_id)
    )
    rows_by_id = (
        {
            r.id: r
            for r in (
                await db.execute(
                    select(VideoGeneration).where(VideoGeneration.id.in_(ids))
                )
            ).scalars()
        }
        if ids
        else {}
    )
    succeeded = [
        r.id
        for r in rows_by_id.values()
        if r.status == VideoGenerationStatus.SUCCEEDED.value
    ]
    videos = await _first_media_by_owner(db, model=Video, owner_ids=succeeded)
    return {
        id(t): _map_video_row(
            t,
            rows_by_id.get(t.video_generation_id or ""),
            videos.get(t.video_generation_id or ""),
        )
        for t in tasks
    }


async def _project_generation(
    db: AsyncSession, task: CanvasExecutionTask
) -> tuple[str, dict[str, Any] | None, Any]:
    return (await _project_generations_batch(db, [task]))[id(task)]


async def _project_video(
    db: AsyncSession, task: CanvasExecutionTask
) -> tuple[str, dict[str, Any] | None, Any]:
    return (await _project_videos_batch(db, [task]))[id(task)]


_DEFAULT_PROJECT_GENERATION = _project_generation
_DEFAULT_PROJECT_VIDEO = _project_video


async def _project_tasks(
    db: AsyncSession, tasks: list[CanvasExecutionTask]
) -> dict[int, tuple[str, dict[str, Any] | None, Any]]:
    results: dict[int, tuple[str, dict[str, Any] | None, Any]] = {}
    gen_tasks = [t for t in tasks if t.task_kind == "generation"]
    if gen_tasks:
        if _project_generation is _DEFAULT_PROJECT_GENERATION:
            results.update(await _project_generations_batch(db, gen_tasks))
        else:
            for task in gen_tasks:
                results[id(task)] = await _project_generation(db, task)
    video_tasks = [t for t in tasks if t.task_kind == "video_generation"]
    if video_tasks:
        if _project_video is _DEFAULT_PROJECT_VIDEO:
            results.update(await _project_videos_batch(db, video_tasks))
        else:
            for task in video_tasks:
                results[id(task)] = await _project_video(db, task)
    return results


def _execution_status(statuses: list[str], current: str) -> str:
    if not statuses:
        return current
    if not all(status in _TERMINAL_TASK_STATUSES for status in statuses):
        if current == "canceling":
            return current
        return "running" if "running" in statuses else "queued"
    succeeded = statuses.count("succeeded")
    if succeeded == len(statuses):
        return "succeeded"
    if succeeded:
        return "partial_failed"
    return "canceled" if all(s == "canceled" for s in statuses) else "failed"


async def _materialize_asset_refs(
    db: AsyncSession, *, execution: CanvasNodeExecution, outputs: list[dict[str, Any]]
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


def _auto_select_enabled(execution: Any, outputs: list[dict[str, Any]]) -> int | None:
    output_index = canvas_auto_select_output_index(
        execution.config_snapshot_jsonb, outputs
    )
    metadata = (execution.config_snapshot_jsonb or {}).get("_canvas", {})
    if output_index is None or not isinstance(metadata, dict):
        return None
    return output_index if metadata.get("auto_select_on_success") else None


def _eligible_auto_select_target(
    *, graph: Any, selection_rows: list[Any], execution: Any, outputs: list[dict[str, Any]]
) -> tuple[int | None, Any | None]:
    output_index = _auto_select_enabled(execution, outputs)
    if output_index is None:
        return None, None
    try:
        current_node = find_node(graph, execution.node_id)
    except Exception:
        return None, None
    if canvas_node_definition_hash(current_node) != execution.definition_hash:
        return None, None
    selections = {r.node_id: (r.execution_id, int(r.output_index)) for r in selection_rows}
    try:
        matches = canvas_input_snapshot_matches_graph(
            graph,
            node_id=execution.node_id,
            input_snapshot=execution.input_snapshot_jsonb or {},
            selections=selections,
        )
    except (RecursionError, TypeError, ValueError):
        return None, None
    if not matches:
        return None, None
    expected_rev = int(execution.selection_base_revision or 0)
    sel = next((r for r in selection_rows if r.node_id == execution.node_id), None)
    if sel is None or sel.locked or int(sel.revision) != expected_rev:
        return None, None
    return output_index, sel


async def _load_canvas_selection_context(
    db: AsyncSession, *, user_id: str, canvas_id: str, lock: bool
) -> tuple[Any | None, list[Any]]:
    canvas_stmt = select(CanvasDocument).where(
        CanvasDocument.id == canvas_id,
        CanvasDocument.user_id == user_id,
        CanvasDocument.deleted_at.is_(None),
    )
    if lock:
        canvas_stmt = canvas_stmt.with_for_update()
    canvas = (await db.execute(canvas_stmt)).scalar_one_or_none()
    if canvas is None:
        return None, []
    sel_stmt = select(CanvasNodeSelection).where(
        CanvasNodeSelection.canvas_id == canvas_id
    )
    if lock:
        sel_stmt = sel_stmt.with_for_update()
    rows = list((await db.execute(sel_stmt)).scalars())
    return canvas.graph_jsonb, rows


def _apply_auto_selection(execution: Any, selection: Any, output_index: int) -> None:
    selection.execution_id = execution.id
    selection.output_index = output_index
    selection.revision = int(execution.selection_base_revision or 0) + 1
    selection.updated_at = datetime.now(timezone.utc)


async def _auto_select(
    db: AsyncSession,
    *,
    user_id: str,
    execution: CanvasNodeExecution,
    outputs: list[dict[str, Any]],
    lock: bool = False,
) -> bool:
    if _auto_select_enabled(execution, outputs) is None:
        return False
    graph, rows = await _load_canvas_selection_context(
        db, user_id=user_id, canvas_id=execution.canvas_id, lock=lock
    )
    if graph is None:
        return False
    out_idx, sel = _eligible_auto_select_target(
        graph=graph, selection_rows=rows, execution=execution, outputs=outputs
    )
    if out_idx is None or sel is None:
        return False
    _apply_auto_selection(execution, sel, out_idx)
    return True


def _update_single_run_status(run: Any, execution: Any, next_status: str) -> None:
    if run.kind != "single":
        return
    valid = {"partial_failed", "failed", "canceled", "running", "reconciling", "canceling", "queued"}
    if next_status in {"succeeded", "reused", "skipped"}:
        run.status = "succeeded"
    elif next_status in valid:
        run.status = next_status
    if next_status not in _ACTIVE_EXECUTION_STATUSES:
        run.finished_at = execution.finished_at or datetime.now(timezone.utc)


def _apply_terminal_execution_fields(
    execution: CanvasNodeExecution,
    *,
    outputs: list[dict[str, Any]],
    real_rows: list[Any],
) -> bool:
    changed = execution.outputs_jsonb != outputs
    if changed:
        execution.outputs_jsonb = outputs
    execution.finished_at = max(
        (r.finished_at for r in real_rows if r is not None and r.finished_at is not None),
        default=datetime.now(timezone.utc),
    )
    failed_statuses = {
        GenerationStatus.FAILED.value,
        VideoGenerationStatus.FAILED.value,
        VideoGenerationStatus.EXPIRED.value,
    }
    failure = next(
        (r for r in real_rows if getattr(r, "status", None) in failed_statuses),
        None,
    )
    execution.error_code = getattr(failure, "error_code", None)
    execution.error_message = getattr(failure, "error_message", None)
    return changed


def _apply_projected_tasks(
    tasks: list[CanvasExecutionTask],
    projected_by_task: dict[int, tuple[str, dict[str, Any] | None, Any]],
) -> tuple[bool, bool, list[dict[str, Any]], list[Any]]:
    changed = False
    terminal_lag = False
    outputs: list[dict[str, Any]] = []
    real_rows: list[Any] = []
    for task in tasks:
        item = projected_by_task.get(id(task))
        if item is None:
            continue
        status, output, real = item
        real_rows.append(real)
        if status in _TERMINAL_TASK_STATUSES and (
            task.status != status or (output is not None and task.output_jsonb != output)
        ):
            terminal_lag = True
        if task.status != status:
            task.status = status
            changed = True
        if output is not None:
            outputs.append(output)
            if task.output_jsonb != output:
                task.output_jsonb = output
                changed = True
    return changed, terminal_lag, outputs, real_rows


async def _record_run_status_change(
    db: AsyncSession,
    *,
    execution: CanvasNodeExecution,
    next_status: str,
    outputs: list[dict[str, Any]],
    selection_updated: bool,
) -> None:
    run = (
        await db.execute(
            select(CanvasRun).where(CanvasRun.id == execution.run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        return
    _update_single_run_status(run, execution, next_status)
    event_key = (
        f"execution:{execution.id}:epoch:{execution.attempt_epoch}:status:{next_status}"
    )
    exists = (
        await db.execute(
            select(CanvasRunEvent.id).where(
                CanvasRunEvent.run_id == run.id, CanvasRunEvent.event_key == event_key
            )
        )
    ).scalar_one_or_none()
    if exists is None:
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


async def _reconcile_execution(
    db: AsyncSession, *, user_id: str, execution: CanvasNodeExecution
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
    projected_by_task = await _project_tasks(db, tasks)
    changed, _lag, outputs, real_rows = _apply_projected_tasks(tasks, projected_by_task)
    next_status = _execution_status([t.status for t in tasks], execution.status)
    if execution.status != next_status:
        execution.status = next_status
        changed = True
    selection_updated = False
    if next_status in _TERMINAL_EXECUTION_STATUSES:
        out_changed = _apply_terminal_execution_fields(
            execution, outputs=outputs, real_rows=real_rows
        )
        refs_added = await _materialize_asset_refs(db, execution=execution, outputs=outputs)
        selection_updated = await _auto_select(
            db, user_id=user_id, execution=execution, outputs=outputs, lock=True
        )
        changed = changed or out_changed or refs_added or selection_updated
    progress = await record_canvas_progress(
        db, execution=execution, owners=real_rows, emit=not changed
    )
    if not (changed or progress):
        return False
    await _record_run_status_change(
        db,
        execution=execution,
        next_status=next_status,
        outputs=outputs,
        selection_updated=selection_updated,
    )
    return True


def _overlay_statement_result(
    statement: Any, result: Any, overlay: _ReadProjectionOverlay
) -> Any:
    descriptions = getattr(statement, "column_descriptions", None)
    if not isinstance(descriptions, list) or len(descriptions) != 1:
        return result
    entity = descriptions[0].get("entity")
    if entity is CanvasExecutionTask and overlay.synthetic_tasks:
        rows = list(result.scalars())
        existing_keys = {(row.execution_id, row.ordinal) for row in rows}
        existing_ids = {row.id for row in rows}
        merged = [
            *rows,
            *(
                r
                for r in overlay.synthetic_tasks
                if r.id not in existing_ids
                and (r.execution_id, r.ordinal) not in existing_keys
            ),
        ]
        merged.sort(key=lambda row: (row.execution_id, row.ordinal))
        return _OverlayResult(merged)
    if entity is CanvasNodeSelection and overlay.synthetic_selections:
        by_node = {row.node_id: row for row in result.scalars()}
        for sel in overlay.synthetic_selections:
            if sel.execution_id is not None:
                by_node[sel.node_id] = sel
        return _OverlayResult(sorted(by_node.values(), key=lambda row: row.node_id))
    if entity is CanvasRun and overlay.converged_single_run_ids:
        rows = list(result.scalars())
        if "status IN" in str(statement):
            rows = [r for r in rows if r.id not in overlay.converged_single_run_ids]
        return _OverlayResult(rows)
    return result


def _install_readonly_overlay(db: AsyncSession, overlay: _ReadProjectionOverlay) -> None:
    if not (
        overlay.synthetic_tasks
        or overlay.synthetic_selections
        or overlay.converged_single_run_ids
    ):
        return
    info = getattr(db, "info", None)
    if isinstance(info, dict):
        info[_OVERLAY_INSTALLED_KEY] = overlay
    if getattr(db, "_lumen_readonly_execute_wrapped", False):
        return
    original_execute = db.execute

    async def _wrapped_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original_execute(statement, *args, **kwargs)
        active = (
            db.info.get(_OVERLAY_INSTALLED_KEY)
            if isinstance(getattr(db, "info", None), dict)
            else overlay
        )
        if not isinstance(active, _ReadProjectionOverlay):
            return result
        return _overlay_statement_result(statement, result, active)

    setattr(db, "_lumen_readonly_execute_wrapped", True)
    setattr(db, "execute", _wrapped_execute)


async def _schedule_background_repair(*, canvas_id: str, execution_ids: list[str]) -> bool:
    if not execution_ids:
        return False

    async def _dispatch() -> bool:
        redis = get_redis()
        claimed = await redis.set(
            f"{_REPAIR_DUE_KEY_PREFIX}:{canvas_id}",
            "1",
            ex=_REPAIR_DUE_TTL_SECONDS,
            nx=True,
        )
        if not claimed:
            return False
        pool = await get_arq_pool()
        for execution_id in execution_ids:
            await pool.enqueue_job(_RECONCILE_WORKER_JOB, execution_id)
        return True

    try:
        return await asyncio.wait_for(_dispatch(), timeout=_REPAIR_ENQUEUE_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "canvas background repair dispatch deferred canvas_id=%s err=%s",
            canvas_id,
            type(exc).__name__,
        )
        return False


async def _converge_single_runs_in_memory(
    db: AsyncSession, terminal_by_run: dict[str, tuple[CanvasNodeExecution, str]]
) -> set[str]:
    if not terminal_by_run:
        return set()
    runs = (
        await db.execute(
            select(CanvasRun).where(CanvasRun.id.in_(tuple(terminal_by_run)))
        )
    ).scalars()
    converged: set[str] = set()
    for run in runs:
        execution, status_val = terminal_by_run[run.id]
        _update_single_run_status(run, execution, status_val)
        if run.kind == "single":
            converged.add(run.id)
    return converged


async def _try_in_memory_auto_select(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    execution: CanvasNodeExecution,
    outputs: list[dict[str, Any]],
    selection_ctx: tuple[Any | None, list[Any]] | None,
) -> tuple[tuple[Any | None, list[Any]] | None, CanvasNodeSelection | None]:
    if _auto_select_enabled(execution, outputs) is None:
        return selection_ctx, None
    if selection_ctx is None:
        selection_ctx = await _load_canvas_selection_context(
            db, user_id=user_id, canvas_id=canvas_id, lock=False
        )
    if selection_ctx[0] is None:
        return selection_ctx, None
    idx, sel = _eligible_auto_select_target(
        graph=selection_ctx[0],
        selection_rows=selection_ctx[1],
        execution=execution,
        outputs=outputs,
    )
    if idx is None or sel is None:
        return selection_ctx, None
    was_null = sel.execution_id is None
    _apply_auto_selection(execution, sel, idx)
    return selection_ctx, (sel if was_null else None)


async def _project_executions_in_memory(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    executions: list[CanvasNodeExecution],
    tasks_by_exec: dict[str, list[CanvasExecutionTask]],
    projected_by_task: dict[int, tuple[str, dict[str, Any] | None, Any]],
    missing_link_exec_ids: set[str],
) -> tuple[list[str], list[CanvasNodeSelection], set[str]]:
    repair_due_ids: list[str] = []
    synthetic_selections: list[CanvasNodeSelection] = []
    terminal_by_run: dict[str, tuple[CanvasNodeExecution, str]] = {}
    selection_ctx: tuple[Any | None, list[Any]] | None = None

    for execution in executions:
        tasks = tasks_by_exec.get(execution.id, [])
        needs_repair = execution.id in missing_link_exec_ids
        if not tasks:
            continue
        _changed, terminal_lag, outputs, real_rows = _apply_projected_tasks(
            tasks, projected_by_task
        )
        needs_repair = needs_repair or terminal_lag
        next_status = _execution_status([t.status for t in tasks], execution.status)
        if next_status in _TERMINAL_EXECUTION_STATUSES:
            if execution.status != next_status or execution.outputs_jsonb != outputs:
                needs_repair = True
            execution.status = next_status
            _apply_terminal_execution_fields(execution, outputs=outputs, real_rows=real_rows)
            terminal_by_run[execution.run_id] = (execution, next_status)
            selection_ctx, new_sel = await _try_in_memory_auto_select(
                db, user_id=user_id, canvas_id=canvas_id,
                execution=execution, outputs=outputs, selection_ctx=selection_ctx,
            )
            if new_sel is not None:
                synthetic_selections.append(new_sel)
        elif execution.status != next_status:
            execution.status = next_status
        if needs_repair:
            repair_due_ids.append(execution.id)

    converged_runs = await _converge_single_runs_in_memory(db, terminal_by_run)
    return repair_due_ids, synthetic_selections, converged_runs


async def project_canvas_executions_readonly(
    db: AsyncSession, *, user_id: str, canvas_id: str, limit: int = 20
) -> int:
    """Compute a lock-free in-memory GET view and schedule async worker repair."""
    if limit <= 0:
        return 0
    sync_session = getattr(db, "sync_session", None)
    if sync_session is not None:
        sync_session.autoflush = False
    executions = list(
        (
            await db.execute(
                select(CanvasNodeExecution)
                .where(
                    CanvasNodeExecution.canvas_id == canvas_id,
                    CanvasNodeExecution.user_id == user_id,
                    CanvasNodeExecution.status.in_(_ACTIVE_EXECUTION_STATUSES),
                )
                .order_by(CanvasNodeExecution.created_at.asc(), CanvasNodeExecution.id.asc())
                .limit(max(limit * 5, 100))
            )
        ).scalars()
    )
    if not executions:
        return 0
    durable_stmt = (
        select(CanvasExecutionTask)
        .where(CanvasExecutionTask.execution_id.in_([r.id for r in executions]))
        .order_by(CanvasExecutionTask.execution_id.asc(), CanvasExecutionTask.ordinal.asc())
    )
    durable_tasks = list((await db.execute(durable_stmt)).scalars())
    tasks_by_exec: dict[str, list[CanvasExecutionTask]] = {}
    for task in durable_tasks:
        tasks_by_exec.setdefault(task.execution_id, []).append(task)
    missing = {row.id: row for row in executions if row.id not in tasks_by_exec}
    synthetic_tasks = await _discover_missing_tasks(db, user_id=user_id, missing=missing)
    for task in synthetic_tasks:
        tasks_by_exec.setdefault(task.execution_id, []).append(task)
    all_tasks = [*durable_tasks, *synthetic_tasks]
    projected_by_task = await _project_tasks(db, all_tasks) if all_tasks else {}
    repair_due_ids, synthetic_selections, converged_runs = await _project_executions_in_memory(
        db, user_id=user_id, canvas_id=canvas_id, executions=executions,
        tasks_by_exec=tasks_by_exec, projected_by_task=projected_by_task,
        missing_link_exec_ids={t.execution_id for t in synthetic_tasks},
    )
    _install_readonly_overlay(
        db,
        _ReadProjectionOverlay(
            tuple(synthetic_tasks), tuple(synthetic_selections), frozenset(converged_runs)
        ),
    )
    due_slice = repair_due_ids[:limit]
    if due_slice:
        await _schedule_background_repair(canvas_id=canvas_id, execution_ids=due_slice)
    return len(due_slice)


def _clear_readonly_overlay(db: AsyncSession) -> None:
    info = getattr(db, "info", None)
    if isinstance(info, dict):
        info.pop(_OVERLAY_INSTALLED_KEY, None)
    sync_session = getattr(db, "sync_session", None)
    if sync_session is not None:
        sync_session.autoflush = True


async def repair_execution_transaction(
    db: AsyncSession, *, user_id: str, canvas_id: str, identifier: str
) -> bool:
    """Explicit locked write-repair transaction for background/worker callers."""
    _clear_readonly_overlay(db)
    try:
        await lock_canvas_write_identity(db, user_id=user_id)
        execution = (
            await db.execute(
                select(CanvasNodeExecution)
                .where(
                    CanvasNodeExecution.id == identifier,
                    CanvasNodeExecution.canvas_id == canvas_id,
                    CanvasNodeExecution.user_id == user_id,
                    CanvasNodeExecution.status.in_(_ACTIVE_EXECUTION_STATUSES),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        changed = False
        if execution is not None:
            linked = await _repair_missing_links(
                db, user_id=user_id, executions=[execution]
            )
            reconciled = await _reconcile_execution(
                db, user_id=user_id, execution=execution
            )
            changed = linked or reconciled
        await commit_canvas_events(db)
        return changed
    except BaseException:
        await db.rollback()
        raise


async def persist_canvas_executions(
    db: AsyncSession, *, user_id: str, canvas_id: str, limit: int = 20
) -> int:
    """Explicit background write-repair entrypoint preserving row-lock ordering."""
    if limit <= 0:
        return 0
    _clear_readonly_overlay(db)
    touched, cursor, page_size = 0, None, min(limit, 100)
    while touched < limit:
        stmt = select(CanvasNodeExecution.id, CanvasNodeExecution.created_at).where(
            CanvasNodeExecution.canvas_id == canvas_id,
            CanvasNodeExecution.user_id == user_id,
            CanvasNodeExecution.status.in_(_ACTIVE_EXECUTION_STATUSES),
        )
        if cursor is not None:
            stmt = stmt.where(
                or_(
                    CanvasNodeExecution.created_at > cursor[1],
                    and_(CanvasNodeExecution.created_at == cursor[1], CanvasNodeExecution.id > cursor[0]),
                )
            )
        rows = (
            await db.execute(
                stmt.order_by(CanvasNodeExecution.created_at, CanvasNodeExecution.id).limit(page_size)
            )
        ).all()
        await commit_canvas_events(db)
        if not rows:
            break
        cursor = rows[-1]
        for identifier, _created_at in rows:
            touched += int(
                await repair_execution_transaction(db, user_id=user_id, canvas_id=canvas_id, identifier=identifier)
            )
            if touched >= limit:
                break
        if len(rows) < page_size:
            break
    return touched


async def repair_canvas_executions(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    limit: int = 20,
    persist: bool = False,
) -> int:
    """Default GET entrypoint is pure read-only; pass persist=True for worker repair."""
    if persist:
        return await persist_canvas_executions(
            db, user_id=user_id, canvas_id=canvas_id, limit=limit
        )
    return await project_canvas_executions_readonly(
        db, user_id=user_id, canvas_id=canvas_id, limit=limit
    )
