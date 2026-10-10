"""Canonical task-owner fence for paid-task repair, beyond Canvas projection."""

from sqlalchemy import select
from lumen_core.canvas_models import CanvasExecutionTask
from lumen_core.model_entities import Generation, VideoGeneration
from lumen_core.task_outcome_knowledge import task_outcome_unknown
from .errors import canvas_http


async def require_confirmed_failed_tasks(db, *, user_id, executions):
    identifiers = {row.id for row in executions}
    tasks = list(
        (
            await db.execute(
                select(CanvasExecutionTask)
                .where(
                    CanvasExecutionTask.execution_id.in_(identifiers),
                )
                .order_by(CanvasExecutionTask.id)
                .with_for_update()
            )
        ).scalars()
    )
    by_execution = {identifier: [] for identifier in identifiers}
    for task in tasks:
        by_execution[task.execution_id].append(task)
    for execution in executions:
        linked = by_execution[execution.id]
        if not linked and execution.error_code == "canvas_plan_admission_failed":
            continue
        if not linked or execution.outputs_jsonb:
            raise canvas_http(
                "canvas_plan_retry_unsafe", "task outcome requires reconciliation", 409
            )
        for task in linked:
            await require_terminal_owner(db, user_id=user_id, task=task)


async def require_terminal_owner(db, *, user_id, task):
    if task.task_kind == "generation":
        model, identifier = Generation, task.generation_id
    elif task.task_kind == "video_generation":
        model, identifier = VideoGeneration, task.video_generation_id
    else:
        raise canvas_http(
            "canvas_plan_retry_unsafe", "task kind cannot be repaired", 409
        )
    owner = (
        await db.execute(
            select(model)
            .where(
                model.id == identifier,
                model.user_id == user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        owner is None
        or owner.status not in {"failed", "canceled", "expired"}
        or task_outcome_unknown(owner, task_kind=task.task_kind)
        or task.output_jsonb
    ):
        raise canvas_http(
            "canvas_plan_retry_unsafe",
            "only confirmed failed task owners may be regenerated",
            409,
        )
