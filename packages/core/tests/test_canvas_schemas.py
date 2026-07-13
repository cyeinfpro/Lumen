from __future__ import annotations

import pytest
from pydantic import ValidationError

from lumen_core.canvas_schemas import CanvasGraph, validate_required_inputs


def _node(
    node_id: str,
    node_type: str,
    config: dict | None = None,
    *,
    x: float = 0,
) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "schema_version": 1,
        "title": node_id,
        "position": {"x": x, "y": 0},
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


def test_v1_node_catalog_parses_with_strict_configs() -> None:
    graph = CanvasGraph.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                _node("prompt", "prompt", {"text": "hello"}),
                _node(
                    "image-asset",
                    "image_asset",
                    {"image_id": "00000000-0000-4000-8000-000000000001"},
                ),
                _node(
                    "video-asset",
                    "video_asset",
                    {"video_id": "00000000-0000-4000-8000-000000000002"},
                ),
                _node("image-gen", "image_generate"),
                _node("video-gen", "video_generate"),
                _node("note", "note", {"text": "review"}),
                _node("frame", "frame"),
                _node("delivery", "delivery"),
            ],
            "edges": [],
            "frames": [],
            "settings": {"snap_to_grid": True, "grid_size": 16},
        }
    )

    assert [node.type for node in graph.nodes] == [
        "prompt",
        "image_asset",
        "video_asset",
        "image_generate",
        "video_generate",
        "note",
        "frame",
        "delivery",
    ]
    assert graph.nodes[3].config.size == "1K"
    assert graph.nodes[3].config.quality == "standard"

    invalid = _node("bad", "image_generate", {"unknown": True})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CanvasGraph.model_validate({"nodes": [invalid]})


def test_graph_validates_typed_ports_cardinality_order_and_pinned_binding() -> None:
    nodes = [
        _node("prompt-a", "prompt", {"text": "a"}),
        _node("prompt-b", "prompt", {"text": "b"}),
        _node("asset-a", "image_asset", {"image_id": "image-a"}),
        _node("asset-b", "image_generate"),
        _node("generate", "image_generate"),
    ]

    valid = CanvasGraph.model_validate(
        {
            "nodes": nodes,
            "edges": [
                _edge(
                    "prompt-edge",
                    "prompt-a",
                    "text",
                    "generate",
                    "prompt",
                    "text",
                ),
                _edge(
                    "ref-a",
                    "asset-a",
                    "image",
                    "generate",
                    "references",
                    "image",
                    order=0,
                ),
                _edge(
                    "ref-b",
                    "asset-b",
                    "image",
                    "generate",
                    "references",
                    "image",
                    order=1,
                    binding_mode="pinned",
                    pinned_execution_id="00000000-0000-4000-8000-000000000111",
                    pinned_output_index=0,
                ),
            ],
        }
    )
    assert len(valid.edges) == 3

    with pytest.raises(ValidationError, match="accepts at most 1"):
        CanvasGraph.model_validate(
            {
                "nodes": nodes,
                "edges": [
                    _edge(
                        "prompt-a-edge",
                        "prompt-a",
                        "text",
                        "generate",
                        "prompt",
                        "text",
                    ),
                    _edge(
                        "prompt-b-edge",
                        "prompt-b",
                        "text",
                        "generate",
                        "prompt",
                        "text",
                    ),
                ],
            }
        )

    with pytest.raises(ValidationError, match="incompatible port types"):
        CanvasGraph.model_validate(
            {
                "nodes": nodes,
                "edges": [
                    _edge(
                        "wrong-type",
                        "prompt-a",
                        "text",
                        "generate",
                        "references",
                        "text",
                    )
                ],
            }
        )

    with pytest.raises(ValidationError, match="requires order"):
        CanvasGraph.model_validate(
            {
                "nodes": nodes,
                "edges": [
                    _edge(
                        "unordered-a",
                        "asset-a",
                        "image",
                        "generate",
                        "references",
                        "image",
                    ),
                    _edge(
                        "unordered-b",
                        "asset-b",
                        "image",
                        "generate",
                        "references",
                        "image",
                    ),
                ],
            }
        )

    with pytest.raises(ValidationError, match="require execution id"):
        CanvasGraph.model_validate(
            {
                "nodes": nodes,
                "edges": [
                    _edge(
                        "bad-pin",
                        "asset-a",
                        "image",
                        "generate",
                        "references",
                        "image",
                        binding_mode="pinned",
                    )
                ],
            }
        )

    with pytest.raises(ValidationError, match="only generated outputs"):
        CanvasGraph.model_validate(
            {
                "nodes": nodes,
                "edges": [
                    _edge(
                        "asset-pin",
                        "asset-a",
                        "image",
                        "generate",
                        "references",
                        "image",
                        binding_mode="pinned",
                        pinned_execution_id="00000000-0000-4000-8000-000000000111",
                        pinned_output_index=0,
                    )
                ],
            }
        )


def test_graph_rejects_cycles_and_incompatible_video_modes() -> None:
    nodes = [
        _node("image-a", "image_generate"),
        _node("image-b", "image_generate"),
    ]
    with pytest.raises(ValidationError, match="acyclic"):
        CanvasGraph.model_validate(
            {
                "nodes": nodes,
                "edges": [
                    _edge(
                        "a-to-b",
                        "image-a",
                        "image",
                        "image-b",
                        "references",
                        "image",
                    ),
                    _edge(
                        "b-to-a",
                        "image-b",
                        "image",
                        "image-a",
                        "references",
                        "image",
                    ),
                ],
            }
        )

    with pytest.raises(ValidationError, match="t2v nodes cannot"):
        CanvasGraph.model_validate(
            {
                "nodes": [
                    _node(
                        "asset",
                        "image_asset",
                        {"image_id": "00000000-0000-4000-8000-000000000001"},
                    ),
                    _node("video", "video_generate", {"mode": "t2v"}),
                ],
                "edges": [
                    _edge(
                        "frame-edge",
                        "asset",
                        "image",
                        "video",
                        "first_frame",
                        "image",
                    )
                ],
            }
        )


def test_required_input_validation_is_separate_from_draft_graph_validation() -> None:
    graph = CanvasGraph.model_validate(
        {
            "nodes": [
                _node("image", "image_generate"),
                _node("video", "video_generate", {"mode": "i2v"}),
            ]
        }
    )

    assert validate_required_inputs(graph, "image") == ["prompt"]
    assert validate_required_inputs(graph, "video") == ["prompt", "first_frame"]


def test_frontend_v1_default_configs_are_accepted_as_incomplete_drafts() -> None:
    graph = CanvasGraph.model_validate(
        {
            "nodes": [
                _node(
                    "image-asset",
                    "image_asset",
                    {"image_id": "", "display_name": ""},
                ),
                _node(
                    "video-asset",
                    "video_asset",
                    {"video_id": "", "display_name": ""},
                ),
                _node(
                    "image",
                    "image_generate",
                    {
                        "aspect_ratio": "1:1",
                        "size": "1K",
                        "quality": "standard",
                        "count": 1,
                        "fast": True,
                        "output_format": "webp",
                    },
                ),
                _node(
                    "video",
                    "video_generate",
                    {
                        "mode": "i2v",
                        "model": "",
                        "duration_s": 5,
                        "resolution": "720p",
                        "aspect_ratio": "16:9",
                        "generate_audio": False,
                        "seed": None,
                        "watermark": False,
                    },
                ),
                _node(
                    "frame",
                    "frame",
                    {"label": "新画框", "hidden_in_run": False},
                ),
                _node(
                    "delivery",
                    "delivery",
                    {"set_as_thumbnail": True},
                ),
            ]
        }
    )

    assert graph.nodes[0].config.image_id == ""
    assert graph.nodes[3].config.duration_s == 5
