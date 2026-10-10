"""Prevent a new batch from bypassing active/uncertain task-owner recovery."""
from sqlalchemy import or_, select
from lumen_core.canvas_models import CanvasNodeExecution
from .errors import canvas_http
from .run_serialization import execution_tasks_by_execution

ACTIVE = ("pending", "ready", "queued", "running", "reconciling", "canceling")


async def require_plan_nodes_available(db, *, user_id, canvas_id, node_ids):
    if not node_ids:
        return
    # Include every unresolved execution, not just the newest UI projection.
    # A later execution must not conceal an older lost provider acknowledgement.
    rows = list((await db.execute(
        select(CanvasNodeExecution).where(
            CanvasNodeExecution.user_id == user_id,
            CanvasNodeExecution.canvas_id == canvas_id,
            CanvasNodeExecution.node_id.in_(node_ids),
            or_(
                CanvasNodeExecution.status.in_(ACTIVE),
                CanvasNodeExecution.status.notin_(("succeeded", "reused", "skipped")),
            ),
        )
    )).scalars())
    active = next((row for row in rows if row.status in ACTIVE), None)
    if active is not None:
        raise canvas_http(
            "canvas_execution_active", "query the existing node execution first", 409,
            node_id=active.node_id, execution_id=active.id, run_id=active.run_id,
        )
    tasks = await execution_tasks_by_execution(db, rows)
    for row in rows:
        for task in tasks.get(row.id, []):
            recovery = task.get("recovery") or {}
            if recovery.get("state") in {
                "submission_unknown", "unavailable", "running", "queued",
                "reconciling", "saving_artifact", "cancel_requested",
            } or not recovery:
                raise canvas_http(
                    "canvas_execution_unknown",
                    "existing task outcome requires reconciliation before a new plan",
                    409, node_id=row.node_id, execution_id=row.id, run_id=row.run_id,
                )
