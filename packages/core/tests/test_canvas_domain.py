from __future__ import annotations

import unicodedata

import pytest

from lumen_core.canvas import (
    CanvasPreconditionFailedError,
    apply_canvas_mutation,
    canonical_hash,
    canonical_json_dumps,
    canvas_input_snapshot_matches_graph,
    propagate_stale,
    stale_nodes_for_selection_change,
    topological_node_ids,
)
from lumen_core.canvas_schemas import CanvasGraph


def _node(node_id: str, node_type: str, config: dict | None = None) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "schema_version": 1,
        "title": node_id,
        "position": {"x": 0, "y": 0},
        "config": config or {},
        "ui": {},
    }


def _edge(
    edge_id: str,
    source: str,
    source_handle: str,
    target: str,
    target_handle: str,
    data_type: str,
    **extra: object,
) -> dict:
    return {
        "id": edge_id,
        "source_node_id": source,
        "source_handle": source_handle,
        "target_node_id": target,
        "target_handle": target_handle,
        "data_type": data_type,
        **extra,
    }


def _workflow_graph() -> CanvasGraph:
    return CanvasGraph.model_validate(
        {
            "nodes": [
                _node("prompt", "prompt", {"text": "first", "locked": False}),
                _node("image", "image_generate"),
                _node("video", "video_generate", {"mode": "i2v"}),
                _node("delivery", "delivery"),
            ],
            "edges": [
                _edge(
                    "prompt-image",
                    "prompt",
                    "text",
                    "image",
                    "prompt",
                    "text",
                ),
                _edge(
                    "prompt-video",
                    "prompt",
                    "text",
                    "video",
                    "prompt",
                    "text",
                ),
                _edge(
                    "image-video",
                    "image",
                    "image",
                    "video",
                    "first_frame",
                    "image",
                ),
                _edge(
                    "video-delivery",
                    "video",
                    "video",
                    "delivery",
                    "videos",
                    "video",
                    binding_mode="pinned",
                    pinned_execution_id="00000000-0000-4000-8000-000000000900",
                    pinned_output_index=0,
                ),
            ],
        }
    )


def test_canonical_json_normalizes_keys_unicode_numbers_and_negative_zero() -> None:
    composed = "é"
    decomposed = unicodedata.normalize("NFD", composed)
    left = {"b": -0.0, "a": [1.0, decomposed]}
    right = {"a": [1, composed], "b": 0}

    assert canonical_json_dumps(left) == '{"a":[1,"é"],"b":0}'
    assert canonical_hash(left) == canonical_hash(right)

    with pytest.raises(ValueError, match="NaN"):
        canonical_hash({"bad": float("nan")})


def test_topology_and_stale_propagation_stop_at_pinned_edges() -> None:
    graph = _workflow_graph()

    assert topological_node_ids(graph) == ("prompt", "image", "video", "delivery")
    assert propagate_stale(graph, ["prompt"]) == ("prompt", "image", "video")
    assert stale_nodes_for_selection_change(graph, "image") == ("video",)


def test_mutation_updates_config_and_returns_transitive_stale_nodes() -> None:
    graph = _workflow_graph()
    result = apply_canvas_mutation(
        graph,
        [
            {
                "op": "update_node_config",
                "node_id": "prompt",
                "config": {"text": "second", "locked": False},
            },
            {
                "op": "move_nodes",
                "items": [{"node_id": "image", "x": 240, "y": 180}],
            },
        ],
    )

    assert result.graph.nodes[0].config.text == "second"
    assert result.graph.nodes[1].position.x == 240
    assert result.stale_node_ids == ("prompt", "image", "video")
    assert result.changed_node_ids == ("image", "prompt")


def test_config_path_precondition_and_remove_node_edge_set_are_deterministic() -> None:
    graph = _workflow_graph()
    current_hash = canonical_hash("first")
    changed = apply_canvas_mutation(
        graph,
        [
            {
                "op": "update_node_config",
                "node_id": "prompt",
                "changes": [
                    {
                        "path": "text",
                        "before_hash": current_hash,
                        "value": "updated",
                    }
                ],
            }
        ],
    )
    assert changed.graph.nodes[0].config.text == "updated"

    with pytest.raises(CanvasPreconditionFailedError, match="no longer matches"):
        apply_canvas_mutation(
            graph,
            [
                {
                    "op": "update_node_config",
                    "node_id": "prompt",
                    "changes": [
                        {
                            "path": "text",
                            "before_hash": canonical_hash("wrong"),
                            "value": "updated",
                        }
                    ],
                }
            ],
        )

    with pytest.raises(CanvasPreconditionFailedError, match="exactly match"):
        apply_canvas_mutation(
            graph,
            [
                {
                    "op": "remove_nodes",
                    "node_ids": ["image"],
                    "edge_ids": ["prompt-image"],
                }
            ],
        )

    removed = apply_canvas_mutation(
        graph,
        [
            {
                "op": "remove_nodes",
                "node_ids": ["image"],
                "edge_ids": ["prompt-image", "image-video"],
            }
        ],
    )
    assert [node.id for node in removed.graph.nodes] == [
        "prompt",
        "video",
        "delivery",
    ]
    assert removed.stale_node_ids == ("video",)


def test_mutation_batch_only_validates_the_final_materialized_graph() -> None:
    graph = CanvasGraph.model_validate(
        {
            "nodes": [
                _node("image-a", "image_generate"),
                _node("image-b", "image_generate"),
            ],
            "edges": [
                _edge(
                    "a-to-b",
                    "image-a",
                    "image",
                    "image-b",
                    "references",
                    "image",
                )
            ],
        }
    )

    result = apply_canvas_mutation(
        graph,
        [
            {
                "op": "add_edge",
                "edge": _edge(
                    "b-to-a",
                    "image-b",
                    "image",
                    "image-a",
                    "references",
                    "image",
                ),
            },
            {"op": "remove_edges", "edge_ids": ["a-to-b"]},
        ],
    )

    assert [edge.id for edge in result.graph.edges] == ["b-to-a"]


def test_input_snapshot_match_ignores_layout_but_detects_input_changes() -> None:
    graph = _workflow_graph()
    snapshot = {
        "prompt": "first",
        "bindings": [
            {
                "edge_id": "image-video",
                "source_node_id": "image",
                "target_handle": "first_frame",
                "role": None,
                "order": 0,
                "binding_mode": "follow_active",
                "source_execution_id": "execution-image",
                "output_index": 0,
                "asset": {"image_id": "image-output"},
            },
            {
                "edge_id": "prompt-video",
                "source_node_id": "prompt",
                "target_handle": "prompt",
                "role": None,
                "order": 0,
                "binding_mode": "follow_active",
                "text": "first",
            },
        ],
    }
    moved = apply_canvas_mutation(
        graph,
        [
            {
                "op": "move_nodes",
                "items": [{"node_id": "video", "x": 900, "y": 240}],
            }
        ],
    ).graph
    selections = {"image": ("execution-image", 0)}

    assert canvas_input_snapshot_matches_graph(
        moved,
        node_id="video",
        input_snapshot=snapshot,
        selections=selections,
    )

    changed_prompt = apply_canvas_mutation(
        moved,
        [
            {
                "op": "update_node_config",
                "node_id": "prompt",
                "config": {"text": "changed", "locked": False},
            }
        ],
    ).graph
    assert not canvas_input_snapshot_matches_graph(
        changed_prompt,
        node_id="video",
        input_snapshot=snapshot,
        selections=selections,
    )
    assert not canvas_input_snapshot_matches_graph(
        moved,
        node_id="video",
        input_snapshot=snapshot,
        selections={"image": ("another-execution", 0)},
    )
