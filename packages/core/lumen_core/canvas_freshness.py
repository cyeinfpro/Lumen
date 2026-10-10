"""Read-only semantic freshness, including transitive follow-active changes."""

from collections.abc import Mapping
from pydantic import ValidationError
from .canvas import (
    canvas_input_snapshot_matches_graph,
    canvas_node_definition_hash,
    propagate_stale,
)
from .canvas_snapshot_index import CanvasSnapshotIndex


def execution_freshness(graph, executions, selections):
    """Return projections only; never mutate execution status or billed work."""
    try:
        index = CanvasSnapshotIndex.build(graph)
    except ValidationError:
        return {
            "execution_freshness": {
                row.id: {"state": "unknown", "reason": "snapshot_unavailable"}
                for row in executions
            },
            "stale_node_ids": [],
        }
    selected = {
        row.node_id: (row.execution_id, int(row.output_index)) for row in selections
    }
    definitions = {
        node_id: canvas_node_definition_hash(node)
        for node_id, node in index.nodes.items()
    }
    states = {
        row.id: freshness_for_execution(row, index, definitions, selected)
        for row in executions
    }
    roots = {
        row.node_id
        for row in executions
        if selected.get(row.node_id, (None, 0))[0] == row.id
        and states[row.id]["state"] == "stale"
    }
    stale = set(propagate_stale(index.graph, roots))
    for row in executions:
        if row.node_id in stale and states[row.id]["state"] == "fresh":
            states[row.id] = {"state": "stale", "reason": "upstream_changed"}
    return {"execution_freshness": states, "stale_node_ids": sorted(stale)}


def freshness_for_execution(row, index, definitions, selections):
    if row.node_id not in definitions:
        return {"state": "stale", "reason": "node_removed"}
    if not row.definition_hash:
        return {"state": "unknown", "reason": "snapshot_unavailable"}
    if row.definition_hash != definitions[row.node_id]:
        return {"state": "stale", "reason": "definition_changed"}
    snapshot = row.input_snapshot_jsonb
    if not isinstance(snapshot, Mapping) or not isinstance(
        snapshot.get("bindings"), list
    ):
        return {"state": "unknown", "reason": "snapshot_unavailable"}
    if not canvas_input_snapshot_matches_graph(
        index.graph,
        node_id=row.node_id,
        input_snapshot=snapshot,
        selections=selections,
        index=index,
    ):
        return {"state": "stale", "reason": "inputs_changed"}
    return {"state": "fresh", "reason": None}
