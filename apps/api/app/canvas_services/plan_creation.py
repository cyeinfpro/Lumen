"""Persist immutable batch plans and pending steps before any task admission."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from lumen_core.canvas import canonical_hash, canvas_node_definition_hash
from lumen_core.canvas_models import (
    CanvasAssetRef,
    CanvasNodeExecution,
    CanvasNodeSelection,
    CanvasRun,
)
from ..idempotency.advisory import lock_user_key
from .core_adapter import validated_graph
from .document_service import get_owned_canvas
from .errors import canvas_http
from .identity_fence import lock_canvas_write_identity
from .plan_preview import preview_run_plan
from .run_event_service import append_run_event
from .event_commit import commit_canvas_events
from .version_service import create_version


async def selection_revisions(db, *, canvas_id, node_ids):
    rows = list(
        (
            await db.execute(
                select(CanvasNodeSelection)
                .where(
                    CanvasNodeSelection.canvas_id == canvas_id,
                    CanvasNodeSelection.node_id.in_(node_ids),
                )
                .order_by(CanvasNodeSelection.node_id)
                .with_for_update()
            )
        ).scalars()
    )
    revisions = {row.node_id: int(row.revision) for row in rows}
    for node_id in sorted(set(node_ids) - revisions.keys()):
        db.add(
            CanvasNodeSelection(
                canvas_id=canvas_id,
                node_id=node_id,
                output_index=0,
                revision=0,
                locked=False,
            )
        )
        revisions[node_id] = 0
    await db.flush()
    return revisions


def new_plan_execution(
    *,
    run,
    node,
    step,
    sequence,
    selection_revision,
    auto_select,
    attempt=0,
    retry_of=None,
):
    config = dict(node["config"])
    if step.effective_model:
        config["model"] = step.effective_model
    key = canonical_hash(
        {"run_id": run.id, "node_id": step.node_id, "attempt": attempt}
    )
    outputs = []
    if step.reuse:
        ref = step.reuse
        outputs = [
            {
                "type": ref.asset_kind,
                f"{ref.asset_kind}_id": ref.asset_id,
                "sha256": ref.source_sha256,
            }
        ]
    return CanvasNodeExecution(
        canvas_id=run.canvas_id,
        run_id=run.id,
        user_id=run.user_id,
        node_id=step.node_id,
        node_type=node["type"],
        node_schema_version=int(node.get("schema_version") or 1),
        sequence=sequence,
        attempt=attempt,
        attempt_epoch=int(run.run_epoch),
        status="reused" if step.reuse else "pending",
        definition_hash=canvas_node_definition_hash(node),
        input_hash=canonical_hash({"pending_plan": run.id, "node": step.node_id}),
        execution_fingerprint=key,
        submission_idempotency_key=f"cp:{key}",
        request_fingerprint=key,
        config_snapshot_jsonb={
            **config,
            "_canvas": {
                "auto_select_on_success": auto_select,
                "planned_output_ordinal": step.output_index,
                "selection_base_revision": selection_revision,
            },
        },
        input_snapshot_jsonb={},
        model_snapshot_jsonb={
            "model": step.effective_model,
            "capability_version": step.capability_version,
        },
        pricing_snapshot_jsonb={
            "source": "plan_preview",
            "estimated_cost_micro": step.estimated_cost_micro,
        },
        processor_version="canvas-api-v1",
        outputs_jsonb=outputs,
        selection_base_revision=selection_revision,
        retry_of_execution_id=retry_of,
        reused_from_execution_id=step.reuse.execution_id if step.reuse else None,
        finished_at=datetime.now(timezone.utc) if step.reuse else None,
    )


async def start_run_plan(db, *, user, canvas_id, body, header_idempotency_key):
    if header_idempotency_key != body.idempotency_key:
        raise canvas_http(
            "idempotency_key_mismatch", "Idempotency-Key must match the body", 422
        )
    fingerprint = canonical_hash(
        {"canvas_id": canvas_id, "request": body.model_dump(mode="json")}
    )
    await lock_user_key(db, "canvas-submission", user.id, body.idempotency_key)
    await lock_canvas_write_identity(db, user_id=user.id)
    existing = (
        await db.execute(
            select(CanvasRun).where(
                CanvasRun.user_id == user.id,
                CanvasRun.idempotency_key == body.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.canvas_id != canvas_id
            or existing.request_fingerprint != fingerprint
        ):
            raise canvas_http(
                "idempotency_conflict", "idempotency key already used", 409
            )
        return existing
    canvas = await get_owned_canvas(db, user_id=user.id, canvas_id=canvas_id, lock=True)
    plan = await preview_run_plan(db, user=user, canvas=canvas, body=body)
    if body.plan_hash != plan.plan_hash:
        raise canvas_http(
            "canvas_plan_changed", "preview the updated plan before running", 409
        )
    version = await create_version(
        db, canvas=canvas, user_id=user.id, kind="run", reuse_exact=True
    )
    revisions = await selection_revisions(
        db, canvas_id=canvas_id, node_ids=[step.node_id for step in plan.steps]
    )
    run = CanvasRun(
        canvas_id=canvas_id,
        version_id=version.id,
        user_id=user.id,
        kind=plan.kind,
        status="queued",
        failure_policy=plan.failure_policy,
        run_epoch=0,
        last_event_seq=0,
        target_node_ids=list(plan.target_node_ids),
        idempotency_key=body.idempotency_key,
        request_fingerprint=fingerprint,
        budget_micro=body.budget_micro,
        reserved_micro=0,
        spent_micro=0,
        estimated_cost_micro=plan.estimated_cost_micro,
        summary_jsonb={
            "run_plan": plan.to_dict(),
            "document_revision": body.document_revision,
            "plan_admitted_micro": 0,
            "auto_select_on_success": body.auto_select_on_success,
        },
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.flush()
    nodes = {node["id"]: node for node in validated_graph(canvas.graph_jsonb)["nodes"]}
    for index, step in enumerate(plan.steps):
        execution = new_plan_execution(
            run=run,
            node=nodes[step.node_id],
            step=step,
            sequence=index,
            selection_revision=revisions[step.node_id],
            auto_select=body.auto_select_on_success,
        )
        db.add(execution)
        await db.flush()
        if step.reuse:
            db.add(
                CanvasAssetRef(
                    canvas_id=canvas_id,
                    execution_id=execution.id,
                    node_id=step.node_id,
                    scope="execution",
                    retention_class="history",
                    image_id=step.reuse.asset_id
                    if step.reuse.asset_kind == "image"
                    else None,
                    video_id=step.reuse.asset_id
                    if step.reuse.asset_kind == "video"
                    else None,
                )
            )
    await append_run_event(
        db,
        run=run,
        execution=None,
        event_type="canvas.run.queued",
        event_key=f"run:{run.id}:epoch:0:queued",
        payload={"status": "queued", "plan_hash": plan.plan_hash},
    )
    await commit_canvas_events(db)
    return run
