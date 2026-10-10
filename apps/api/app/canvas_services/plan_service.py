"""Bridge existing authenticated resolution snapshots to immutable run plans."""

from __future__ import annotations

from typing import Any

from lumen_core.canvas_run_plan import OutputReference, compile_run_plan


def single_node_run_plan(
    *,
    user_id: str,
    canvas_id: str,
    revision: int,
    graph: dict[str, Any],
    node_id: str,
    resolved_snapshot: dict[str, Any],
) -> dict[str, Any]:
    selected, pinned = {}, {}
    for binding in resolved_snapshot.get("bindings", []):
        execution_id = binding.get("source_execution_id")
        asset = binding.get("asset")
        if not execution_id or not isinstance(asset, dict):
            continue
        kind = "image" if asset.get("image_id") else "video"
        reference = OutputReference(
            node_id=binding["source_node_id"],
            execution_id=execution_id,
            output_index=int(binding.get("output_index") or 0),
            asset_kind=kind,
            asset_id=asset[f"{kind}_id"],
            source_sha256=asset["sha256"],
        )
        if binding.get("binding_mode") == "pinned":
            pinned[binding["edge_id"]] = reference
        else:
            selected[reference.node_id] = reference
    plan = compile_run_plan(
        graph,
        tenant_id=user_id,
        canvas_id=canvas_id,
        document_revision=revision,
        kind="single",
        target_node_ids=(node_id,),
        selected_outputs=selected,
        pinned_outputs=pinned,
    )
    return plan.to_dict()
