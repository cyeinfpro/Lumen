"""Persist changed task progress, coalescing identical snapshots."""

from sqlalchemy import select
from lumen_core.canvas_models import CanvasRun
from lumen_core.canvas_progress import canvas_progress_snapshot
from .run_event_service import append_run_event


async def record_canvas_progress(db, *, execution, owners, emit=True):
    digest, items = canvas_progress_snapshot(owners)
    if not items:
        return False
    run = (
        await db.execute(
            select(CanvasRun)
            .where(
                CanvasRun.id == execution.run_id,
                CanvasRun.user_id == execution.user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        return False
    summary = dict(run.summary_jsonb or {})
    snapshots = dict(summary.get("task_progress", {}))
    if snapshots.get(execution.id) == digest:
        return False
    snapshots[execution.id] = digest
    summary["task_progress"] = snapshots
    run.summary_jsonb = summary
    if not emit:
        return False
    await append_run_event(
        db,
        run=run,
        execution=execution,
        event_type="canvas.execution.progress",
        event_key=f"execution:{execution.id}:progress:{int(run.last_event_seq or 0) + 1}",
        payload={
            "execution_id": execution.id,
            "node_id": execution.node_id,
            "tasks": items,
        },
    )
    return True
