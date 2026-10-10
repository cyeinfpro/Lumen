"""Compile authoritative, priced plans without admitting generation tasks."""

from __future__ import annotations

from lumen_core.canvas_run_plan import (
    PlanPolicy,
    PlanStepOptions,
    RunPlan,
    compile_run_plan,
)
from .core_adapter import validated_graph
from .errors import canvas_http
from .plan_pricing import cached_plan_quote, quote_plan_node
from .plan_preflight import validate_plan_inputs
from .plan_asset_preflight import validate_plan_assets
from .plan_references import plan_output_references
from .plan_overlap_guard import require_plan_nodes_available


async def preview_run_plan(db, *, user, canvas, body) -> RunPlan:
    if getattr(user, "account_mode", "wallet") != "wallet":
        raise canvas_http(
            "canvas_plan_pricing_unavailable", "batch plans require wallet pricing", 422
        )
    if canvas.revision != body.document_revision:
        raise canvas_http(
            "canvas_revision_conflict", "canvas has changed; refresh the plan", 409
        )
    graph = validated_graph(canvas.graph_jsonb)
    selected, pinned, reuse = await plan_output_references(
        db,
        user_id=user.id,
        canvas_id=canvas.id,
        graph=graph,
        reuse_outputs=body.reuse_outputs,
    )
    common = dict(
        tenant_id=user.id,
        canvas_id=canvas.id,
        document_revision=body.document_revision,
        kind=body.kind,
        target_node_ids=tuple(body.target_node_ids),
        selected_outputs=selected,
        pinned_outputs=pinned,
        reuse_outputs=reuse,
        policy=PlanPolicy(body.budget_micro, body.failure_policy),
    )
    try:
        skeleton = compile_run_plan(graph, **common)
    except ValueError as exc:
        raise canvas_http("canvas_plan_invalid", str(exc), 422) from exc
    await require_plan_nodes_available(
        db, user_id=user.id, canvas_id=canvas.id,
        node_ids=[step.node_id for step in skeleton.steps if step.reuse is None],
    )
    validate_plan_inputs(graph, skeleton)
    await validate_plan_assets(db, user_id=user.id, graph=graph, plan=skeleton)
    quote_graph = {
        **graph,
        "_canvas_bound_outputs": dict(skeleton.bindings),
        "_canvas_reused_outputs": {
            step.node_id: step.reuse for step in skeleton.steps if step.reuse
        },
    }
    nodes = {node["id"]: node for node in graph["nodes"]}
    consumed = {dep for step in skeleton.steps for dep in step.dependencies}
    options, estimates, quotes = {}, {}, {}
    planned = {step.node_id for step in skeleton.steps}
    if not set(body.output_indices) <= planned:
        raise canvas_http(
            "canvas_plan_output_invalid", "output choice is outside the plan", 422
        )
    for step in skeleton.steps:
        node = nodes[step.node_id]
        count = int(node["config"].get("count") or 1)
        index = body.output_indices.get(step.node_id, 0)
        if step.reuse is not None:
            if index != 0:
                raise canvas_http(
                    "canvas_plan_output_invalid", "reused output uses index zero", 422
                )
            continue
        if (
            step.node_id in consumed
            and count > 1
            and step.node_id not in body.output_indices
        ):
            raise canvas_http(
                "canvas_plan_output_required",
                "choose which generated candidate feeds downstream nodes",
                422,
                node_id=step.node_id,
            )
        if type(index) is not int or not 0 <= index < count:
            raise canvas_http(
                "canvas_plan_output_invalid", "invalid generated output index", 422
            )
        quote = await cached_plan_quote(
            db,
            user=user,
            graph=quote_graph,
            node=node,
            cache=quotes,
            quote_fn=quote_plan_node,
        )
        estimates[step.node_id] = quote.estimated_cost_micro
        options[step.node_id] = PlanStepOptions(
            quote.model, quote.capability_version, index
        )
    plan = compile_run_plan(graph, estimates=estimates, step_options=options, **common)
    if (
        plan.estimated_cost_micro is None
        or plan.estimated_cost_micro > body.budget_micro
    ):
        raise canvas_http(
            "canvas_plan_budget_insufficient",
            "plan admission estimate exceeds the supplied budget",
            422,
            estimated_cost_micro=plan.estimated_cost_micro,
        )
    return plan
