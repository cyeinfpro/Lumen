"""Bind a plan to exact output candidates, never mutable current selections."""

from copy import deepcopy

from lumen_core.canvas import canonical_hash
from .errors import canvas_http


def frozen_dispatch_graph(plan, graph, executions):
    if canonical_hash(graph) != plan.graph_hash:
        raise canvas_http(
            "canvas_plan_snapshot_changed",
            "run snapshot failed its integrity check",
            409,
        )
    frozen = deepcopy(graph)
    bindings = dict(plan.bindings)
    steps = {step.node_id: step for step in plan.steps}
    for edge in frozen["edges"]:
        ref = bindings.get(edge["id"])
        if ref is not None:
            edge.update(
                binding_mode="pinned",
                pinned_execution_id=ref.execution_id,
                pinned_output_index=ref.output_index,
            )
            continue
        source = edge["source_node_id"]
        if edge["binding_mode"] == "pinned" or source not in steps:
            continue
        owner = executions.get(source)
        if owner is not None and owner.status in {
            "succeeded",
            "partial_failed",
            "reused",
        }:
            edge.update(
                binding_mode="pinned",
                pinned_execution_id=owner.id,
                pinned_output_index=steps[source].output_index,
            )
    for node in frozen["nodes"]:
        step = steps.get(node["id"])
        if step and step.effective_model:
            node["config"]["model"] = step.effective_model
    return frozen


def verify_frozen_bindings(plan, snapshot):
    expected = dict(plan.bindings)
    for binding in snapshot.get("bindings", []):
        ref = expected.get(binding.get("edge_id"))
        if ref is None:
            continue
        asset = binding.get("asset") or {}
        if (
            binding.get("source_execution_id") != ref.execution_id
            or binding.get("output_index") != ref.output_index
            or asset.get(f"{ref.asset_kind}_id") != ref.asset_id
            or asset.get("sha256") != ref.source_sha256
        ):
            raise canvas_http(
                "canvas_plan_asset_changed",
                "a planned asset is no longer available",
                409,
            )
