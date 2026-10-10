"""Explicit failed-step repair without replaying successes or uncertain submits."""

from datetime import datetime, timezone
from sqlalchemy import select
from lumen_core.canvas import canonical_hash
from lumen_core.canvas_models import CanvasNodeExecution, CanvasRun, CanvasVersion
from lumen_core.canvas_run_plan import restore_run_plan
from lumen_core.canvas_plan_repair import repair_plan_closure
from ..idempotency.advisory import lock_user_key
from .document_service import get_owned_canvas
from .errors import canvas_http
from .identity_fence import lock_canvas_write_identity
from .plan_creation import new_plan_execution, selection_revisions
from .plan_dispatch import latest_plan_executions
from .run_event_service import append_run_event
from .plan_retry_guard import require_confirmed_failed_tasks
from .event_commit import commit_canvas_events


async def retry_failed_plan_steps(
    db, *, user, canvas_id, run_id, body, header_idempotency_key
):
    if body.idempotency_key != header_idempotency_key:
        raise canvas_http(
            "idempotency_key_mismatch", "Idempotency-Key must match the body", 422
        )
    await lock_user_key(db, "canvas_plan_retry", user.id, body.idempotency_key)
    await lock_canvas_write_identity(db, user_id=user.id)
    await get_owned_canvas(db, user_id=user.id, canvas_id=canvas_id, lock=True)
    run = (
        await db.execute(
            select(CanvasRun).where(
                CanvasRun.id == run_id,
                CanvasRun.user_id == user.id,
                CanvasRun.canvas_id == canvas_id,
            )
        )
    ).scalar_one_or_none()
    if run is None or run.kind == "single":
        raise canvas_http("not_found", "batch run not found", 404)
    # Match the dispatcher/worker lock order: undispatched/failed executions first.
    list(
        (
            await db.execute(
                select(CanvasNodeExecution)
                .where(
                    CanvasNodeExecution.run_id == run_id,
                    CanvasNodeExecution.status.in_(("failed", "blocked")),
                )
                .order_by(CanvasNodeExecution.id)
                .with_for_update()
            )
        ).scalars()
    )
    run = (
        await db.execute(
            select(CanvasRun)
            .where(CanvasRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    summary = dict(run.summary_jsonb)
    requests = dict(summary.get("repair_requests", {}))
    key = canonical_hash({"key": body.idempotency_key})
    fingerprint = canonical_hash(body.model_dump(mode="json"))
    if key in requests:
        if requests[key] != fingerprint:
            raise canvas_http("idempotency_conflict", "repair key already used", 409)
        return run
    if len(requests) >= 100 or run.cancel_requested_at is not None:
        raise canvas_http(
            "canvas_plan_repair_unavailable", "this run cannot be repaired", 409
        )
    plan = restore_run_plan(summary["run_plan"], tenant_id=user.id, canvas_id=canvas_id)
    latest = await latest_plan_executions(db, run_id)
    selected = set(body.execution_ids)
    failed = {row.id: row for row in latest.values() if row.status == "failed"}
    if len(selected) != len(body.execution_ids) or not selected <= failed.keys():
        raise canvas_http(
            "canvas_plan_retry_unsafe",
            "only the latest confirmed failed steps may be retried",
            409,
        )
    await require_confirmed_failed_tasks(
        db,
        user_id=user.id,
        executions=[failed[identifier] for identifier in sorted(selected)],
    )
    node_ids = {failed[identifier].node_id for identifier in selected}
    restart, missing = repair_plan_closure(
        plan,
        {node_id: row.status for node_id, row in latest.items()},
        node_ids,
    )
    if missing:
        raise canvas_http(
            "canvas_plan_repair_incomplete",
            "fail-fast repair must resolve the remaining failed branches",
            409,
            missing_node_ids=list(missing),
        )
    steps = {step.node_id: step for step in plan.steps}
    required = sum(steps[node_id].estimated_cost_micro or 0 for node_id in node_ids)
    available = (
        run.budget_micro
        + body.additional_budget_micro
        - int(summary.get("plan_admitted_micro", 0))
    )
    still_pending = sum(
        steps[node_id].estimated_cost_micro or 0
        for node_id, row in latest.items()
        if row.status in {"pending", "ready"}
        or (row.status == "blocked" and node_id in restart)
    )
    if run.budget_micro + body.additional_budget_micro > 2**63 - 1:
        raise canvas_http(
            "canvas_plan_budget_invalid", "admission budget is too large", 422
        )
    if required + still_pending > available:
        raise canvas_http(
            "canvas_plan_budget_insufficient",
            "repair requires additional admission budget",
            422,
        )
    version = await db.get(CanvasVersion, run.version_id)
    if version is None or version.user_id != user.id or version.canvas_id != canvas_id:
        raise canvas_http(
            "canvas_plan_snapshot_missing", "run snapshot is unavailable", 409
        )
    nodes = {node["id"]: node for node in version.graph_jsonb["nodes"]}
    revisions = await selection_revisions(db, canvas_id=canvas_id, node_ids=restart)
    run.run_epoch += 1
    for node_id in sorted(restart):
        old = latest[node_id]
        db.add(
            new_plan_execution(
                run=run,
                node=nodes[node_id],
                step=steps[node_id],
                sequence=old.sequence,
                selection_revision=revisions[node_id],
                auto_select=bool(summary.get("auto_select_on_success", True)),
                attempt=old.attempt + 1,
                retry_of=old.id,
            )
        )
    requests[key] = fingerprint
    summary["repair_requests"] = requests
    run.summary_jsonb = summary
    run.budget_micro += body.additional_budget_micro
    run.estimated_cost_micro += required
    run.status = "queued"
    run.finished_at = None
    run.updated_at = datetime.now(timezone.utc)
    await append_run_event(
        db,
        run=run,
        execution=None,
        event_type="canvas.run.queued",
        event_key=f"run:{run.id}:epoch:{run.run_epoch}:repair",
        payload={"status": "queued", "repaired_node_ids": sorted(node_ids)},
    )
    await commit_canvas_events(db)
    return run
