"""Restart-safe plan admission using SQL fencing and the existing task outbox.

No provider is called here. A pending step, its durable task, billing admission,
outbox rows and run budget counter commit together. A crash before commit leaves
it pending; a crash after commit leaves it queued and cannot create another task.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace

from sqlalchemy import select
from lumen_core.canvas import canvas_execution_fingerprint, canvas_input_hash
from lumen_core.canvas_models import (
    CanvasExecutionTask,
    CanvasNodeExecution,
    CanvasRun,
    CanvasVersion,
)
from lumen_core.canvas_run_plan import plan_ready_nodes, restore_run_plan

from ..services.active_user import lock_active_user_snapshot
from .document_service import get_owned_canvas
from .errors import canvas_http
from .execution_service import PreparedNodeExecution, dispatch_prepared_execution
from .graph_resolution import resolve_node
from .plan_dispatch_graph import frozen_dispatch_graph, verify_frozen_bindings
from .plan_pricing import quote_plan_node
from .run_event_service import append_run_event
from .event_commit import commit_canvas_events


async def latest_plan_executions(db, run_id):
    rows = (
        await db.execute(
            select(CanvasNodeExecution)
            .where(
                CanvasNodeExecution.run_id == run_id,
            )
            .order_by(CanvasNodeExecution.node_id, CanvasNodeExecution.attempt)
        )
    ).scalars()
    return {row.node_id: row for row in rows}


async def record_step_status(db, run, execution, status, *, error_code=None):
    execution.status = status
    execution.error_code = error_code
    if status in {"failed", "blocked", "canceled"}:
        execution.finished_at = datetime.now(timezone.utc)
    await append_run_event(
        db,
        run=run,
        execution=execution,
        event_type="canvas.execution.status_changed",
        event_key=f"execution:{execution.id}:epoch:{execution.attempt_epoch}:status:{status}",
        payload={
            "execution_id": execution.id,
            "node_id": execution.node_id,
            "status": status,
        },
    )


async def finish_plan_if_terminal(db, run, executions, decision):
    if not decision.finished:
        return False
    statuses = {row.status for row in executions.values()}
    if statuses <= {"succeeded", "reused", "skipped"}:
        status = "succeeded"
    elif statuses & {"succeeded", "reused", "partial_failed"}:
        status = "partial_failed"
    elif statuses <= {"canceled", "blocked", "skipped"} and run.cancel_requested_at:
        status = "canceled"
    else:
        status = "failed"
    if run.status != status:
        run.status = status
        run.finished_at = datetime.now(timezone.utc)
        await append_run_event(
            db,
            run=run,
            execution=None,
            event_type="canvas.run.status_changed",
            event_key=f"run:{run.id}:epoch:{run.run_epoch}:status:{status}",
            payload={"status": status},
        )
    await commit_canvas_events(db)
    return True


async def prepare_plan_step(db, *, run, canvas, user, plan, executions, execution):
    version = (
        await db.execute(
            select(CanvasVersion).where(
                CanvasVersion.id == run.version_id,
                CanvasVersion.user_id == run.user_id,
                CanvasVersion.canvas_id == run.canvas_id,
            )
        )
    ).scalar_one()
    graph = frozen_dispatch_graph(plan, version.graph_jsonb, executions)
    resolved = await resolve_node(
        db, user=user, canvas_id=canvas.id, graph=graph, node_id=execution.node_id
    )
    # Resolution is pinned, but preserve the authored binding semantics for
    # semantic-stale comparison and the existing selection CAS.
    modes = {edge["id"]: edge["binding_mode"] for edge in version.graph_jsonb["edges"]}
    snapshot = {
        **resolved.snapshot,
        "bindings": [
            {
                **binding,
                "binding_mode": modes.get(binding["edge_id"], binding["binding_mode"]),
            }
            for binding in resolved.snapshot.get("bindings", [])
        ],
    }
    resolved = replace(resolved, snapshot=snapshot)
    verify_frozen_bindings(plan, resolved.snapshot)
    step = next(item for item in plan.steps if item.node_id == execution.node_id)
    quote = await quote_plan_node(db, user=user, graph=graph, node=resolved.node)
    if (
        quote.model != step.effective_model
        or quote.capability_version != step.capability_version
        or quote.estimated_cost_micro != step.estimated_cost_micro
    ):
        raise canvas_http(
            "canvas_plan_price_changed", "plan price or model capability changed", 409
        )
    summary = dict(run.summary_jsonb)
    admitted = int(summary.get("plan_admitted_micro", 0))
    if admitted + quote.estimated_cost_micro > int(run.budget_micro):
        raise canvas_http(
            "canvas_plan_budget_exhausted", "run admission budget is exhausted", 409
        )
    summary["plan_admitted_micro"] = admitted + quote.estimated_cost_micro
    run.summary_jsonb = summary
    run.status = "running"
    execution.input_snapshot_jsonb = resolved.snapshot
    execution.input_hash = canvas_input_hash(resolved.snapshot)
    execution.execution_fingerprint = canvas_execution_fingerprint(
        definition_hash=execution.definition_hash,
        input_hash=execution.input_hash,
        node_schema_version=execution.node_schema_version,
        effective_model=quote.model,
        effective_provider_capability={"version": quote.capability_version},
        processor_version=execution.processor_version,
    )
    execution.started_at = datetime.now(timezone.utc)
    await record_step_status(db, run, execution, "queued")
    return PreparedNodeExecution(
        canvas=canvas,
        resolved=resolved,
        run=run,
        execution=execution,
        node_type=execution.node_type,
        submission_key=execution.submission_idempotency_key,
        request_fingerprint=execution.request_fingerprint,
    )


async def dispatch_plan_once(db, *, run_id):
    run = (
        await db.execute(select(CanvasRun).where(CanvasRun.id == run_id))
    ).scalar_one_or_none()
    if (
        run is None
        or run.kind == "single"
        or run.status not in {"queued", "running", "reconciling", "canceling"}
    ):
        return False
    if not isinstance((run.summary_jsonb or {}).get("run_plan"), dict):
        return False
    identity = await lock_active_user_snapshot(
        db, run.user_id, "wallet", session_id=None
    )
    canvas = await get_owned_canvas(
        db, user_id=run.user_id, canvas_id=run.canvas_id, lock=True
    )
    # Only undispatched rows are locked before the run. Never invert the worker's
    # execution -> run lock order for a task that may already be running.
    pending = list(
        (
            await db.execute(
                select(CanvasNodeExecution)
                .where(
                    CanvasNodeExecution.run_id == run_id,
                    CanvasNodeExecution.status.in_(("pending", "ready")),
                )
                .order_by(CanvasNodeExecution.sequence)
                .with_for_update(skip_locked=True)
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
    if run.status not in {"queued", "running", "reconciling", "canceling"}:
        return False
    plan = restore_run_plan(
        run.summary_jsonb["run_plan"], tenant_id=run.user_id, canvas_id=run.canvas_id
    )
    executions = await latest_plan_executions(db, run.id)
    decision = plan_ready_nodes(
        plan, {key: row.status for key, row in executions.items()}
    )
    locked = {row.node_id: row for row in pending if executions.get(row.node_id) is row}
    if run.cancel_requested_at is not None:
        for row in locked.values():
            await record_step_status(db, run, row, "canceled")
    else:
        for node_id in decision.blocked:
            if node_id in locked:
                await record_step_status(
                    db,
                    run,
                    locked[node_id],
                    "blocked",
                    error_code="canvas_dependency_failed",
                )
    decision = plan_ready_nodes(
        plan, {key: row.status for key, row in executions.items()}
    )
    if await finish_plan_if_terminal(db, run, executions, decision):
        return True
    ready = next(
        (locked[node_id] for node_id in decision.ready if node_id in locked), None
    )
    if ready is None or run.cancel_requested_at is not None:
        await commit_canvas_events(db)
        return False
    execution_id = ready.id
    try:
        prepared = await prepare_plan_step(
            db,
            run=run,
            canvas=canvas,
            user=identity.user,
            plan=plan,
            executions=executions,
            execution=ready,
        )
        await dispatch_prepared_execution(
            db, user=identity.user, active_user_snapshot=identity, prepared=prepared
        )
    except Exception:
        await db.rollback()
        # If commit actually succeeded but its acknowledgement was lost, the
        # reloaded queued status/task fence below prevents a false failure/retry.
        await fail_unadmitted_step(db, execution_id=execution_id)
        raise
    return True


async def fail_unadmitted_step(db, *, execution_id):
    execution = (
        await db.execute(
            select(CanvasNodeExecution)
            .where(
                CanvasNodeExecution.id == execution_id,
                CanvasNodeExecution.status.in_(("pending", "ready")),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if execution is None:
        return
    existing_task = (
        await db.execute(
            select(CanvasExecutionTask.id)
            .where(
                CanvasExecutionTask.execution_id == execution_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_task is not None:
        return
    run = (
        await db.execute(
            select(CanvasRun).where(CanvasRun.id == execution.run_id).with_for_update()
        )
    ).scalar_one()
    await record_step_status(
        db, run, execution, "failed", error_code="canvas_plan_admission_failed"
    )
    execution.error_message = (
        "Task admission failed before a provider request; review the plan."
    )
    await commit_canvas_events(db)
