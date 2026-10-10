"""Synthetic graph correctness at 100/500/1000 nodes; no timing threshold."""

import pytest
from lumen_core.canvas_run_plan import compile_run_plan, plan_ready_nodes


def synthetic_graph(size):
    nodes = [
        {
            "id": "prompt",
            "type": "prompt",
            "position": {"x": 0, "y": 0},
            "config": {"text": "synthetic prompt"},
        }
    ]
    edges = []
    for index in range(size - 1):
        identifier = f"g{index}"
        nodes.append(
            {
                "id": identifier,
                "type": "image_generate",
                "position": {"x": index * 300, "y": 0},
                "config": {},
            }
        )
        edges.append(
            {
                "id": f"p{index}",
                "source_node_id": "prompt",
                "source_handle": "text",
                "target_node_id": identifier,
                "target_handle": "prompt",
                "data_type": "text",
            }
        )
        if index:
            edges.append(
                {
                    "id": f"e{index}",
                    "source_node_id": f"g{index - 1}",
                    "source_handle": "image",
                    "target_node_id": identifier,
                    "target_handle": "references",
                    "data_type": "image",
                }
            )
    return {"nodes": nodes, "edges": edges}


@pytest.mark.parametrize("size", [100, 500, 1000])
def test_large_plan_dependency_order_and_failure_propagation(size):
    plan = compile_run_plan(
        synthetic_graph(size),
        tenant_id="synthetic-user",
        canvas_id="synthetic-canvas",
        document_revision=1,
        kind="all",
        target_node_ids=(),
    )
    assert len(plan.steps) == size - 1
    assert plan.steps[0].dependencies == ()
    assert all(
        step.dependencies == (f"g{index - 1}",)
        for index, step in enumerate(plan.steps)
        if index
    )
    decision = plan_ready_nodes(plan, {})
    assert decision.ready == ("g0",)
    failed = plan_ready_nodes(plan, {"g0": "failed"})
    assert len(failed.blocked) == size - 2
    assert failed.finished
