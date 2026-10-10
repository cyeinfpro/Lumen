"""Persist coalesced task progress inside the existing reconcile transaction."""

from sqlalchemy import select
from lumen_core.canvas_models import CanvasRun, CanvasRunEvent
from lumen_core.canvas_progress import canvas_progress_snapshot
from lumen_core.model_entities import Generation, VideoGeneration


async def record_canvas_progress(session, *, execution, tasks, emit=True):
    owners = []
    for task in tasks:
        model = Generation if task.task_kind == "generation" else VideoGeneration
        identifier = (
            task.generation_id
            if task.task_kind == "generation"
            else task.video_generation_id
        )
        owner = await session.get(model, identifier) if identifier else None
        if owner is not None and getattr(owner, "user_id", None) == execution.user_id:
            owners.append(owner)
    digest, items = canvas_progress_snapshot(owners)
    if not items:
        return False
    run = (
        await session.execute(
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
    run.last_event_seq = int(run.last_event_seq or 0) + 1
    session.add(
        CanvasRunEvent(
            run_id=run.id,
            seq=run.last_event_seq,
            execution_id=execution.id,
            event_type="canvas.execution.progress",
            event_key=f"execution:{execution.id}:progress:{run.last_event_seq}",
            payload_jsonb={
                "execution_id": execution.id,
                "node_id": execution.node_id,
                "tasks": items,
            },
        )
    )
    return True
