from __future__ import annotations

import pytest
from pydantic import ValidationError

from lumen_core.canvas_schemas import (
    EXECUTABLE_NODE_TYPES,
    GENERATED_OUTPUT_NODE_TYPES,
    IMAGE_EXECUTABLE_NODE_TYPES,
    MAX_CANVAS_FRAMES,
    NODE_OUTPUT_PORTS,
    VIDEO_EXECUTABLE_NODE_TYPES,
    CanvasGraph,
    validate_required_inputs,
)


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
                _node("prompt-merge", "prompt_merge"),
                _node(
                    "image-asset",
                    "image_asset",
                    {"image_id": "00000000-0000-4000-8000-000000000001"},
                ),
                _node(
                    "mask-asset",
                    "mask_asset",
                    {"image_id": "00000000-0000-4000-8000-000000000003"},
                ),
                _node(
                    "video-asset",
                    "video_asset",
                    {"video_id": "00000000-0000-4000-8000-000000000002"},
                ),
                _node("image-gen", "image_generate"),
                _node("image-edit", "image_edit"),
                _node("image-inpaint", "image_inpaint"),
                _node("image-upscale", "image_upscale"),
                _node("video-gen", "video_generate"),
                _node("video-text", "video_text_generate"),
                _node("video-image", "video_image_generate"),
                _node("video-reference", "video_reference_generate"),
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
        "prompt_merge",
        "image_asset",
        "mask_asset",
        "video_asset",
        "image_generate",
        "image_edit",
        "image_inpaint",
        "image_upscale",
        "video_generate",
        "video_text_generate",
        "video_image_generate",
        "video_reference_generate",
        "note",
        "frame",
        "delivery",
    ]
    assert graph.nodes[5].config.size == "1K"
    assert graph.nodes[5].config.quality == "standard"
    assert graph.nodes[8].config.size == "2K"
    assert graph.nodes[8].config.quality == "2k"
    assert graph.nodes[8].config.fast is True
    assert graph.nodes[10].config.mode == "t2v"
    assert graph.nodes[11].config.mode == "i2v"
    assert graph.nodes[12].config.mode == "reference"

    invalid = _node("bad", "image_generate", {"unknown": True})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CanvasGraph.model_validate({"nodes": [invalid]})


def test_new_node_constants_configs_and_duration_contracts() -> None:
    assert IMAGE_EXECUTABLE_NODE_TYPES == {
        "image_generate",
        "image_edit",
        "image_inpaint",
        "image_upscale",
    }
    assert VIDEO_EXECUTABLE_NODE_TYPES == {
        "video_generate",
        "video_text_generate",
        "video_image_generate",
        "video_reference_generate",
    }
    assert EXECUTABLE_NODE_TYPES == GENERATED_OUTPUT_NODE_TYPES

    graph = CanvasGraph.model_validate(
        {
            "nodes": [
                _node(
                    "merge",
                    "prompt_merge",
                    {
                        "separator": " | ",
                        "prefix": "[",
                        "suffix": "]",
                        "trim": False,
                        "dedupe": True,
                    },
                ),
                _node(
                    "video",
                    "video_text_generate",
                    {"duration_s": -1},
                ),
            ]
        }
    )
    assert graph.nodes[0].config.separator == " | "
    assert graph.nodes[1].config.duration_s == -1

    with pytest.raises(ValidationError, match="between 3 and 15"):
        CanvasGraph.model_validate(
            {"nodes": [_node("video", "video_text_generate", {"duration_s": 2})]}
        )
    with pytest.raises(ValidationError, match="Input should be 'i2v'"):
        CanvasGraph.model_validate(
            {
                "nodes": [
                    _node(
                        "video",
                        "video_image_generate",
                        {"mode": "t2v"},
                    )
                ]
            }
        )
    with pytest.raises(ValidationError, match="at most 32 characters"):
        CanvasGraph.model_validate(
            {
                "nodes": [
                    _node(
                        "merge",
                        "prompt_merge",
                        {"separator": "x" * 33},
                    )
                ]
            }
        )
    legacy = CanvasGraph.model_validate(
        {
            "nodes": [
                _node(
                    "image",
                    "image_edit",
                    {"aspect_ratio": "bogus"},
                ),
                _node(
                    "video",
                    "video_text_generate",
                    {"resolution": "bogus", "seed": -2},
                ),
            ]
        }
    )
    assert legacy.nodes[0].config.aspect_ratio == "bogus"
    assert legacy.nodes[1].config.resolution == "bogus"
    assert legacy.nodes[1].config.seed == -2


def test_canvas_graph_limits_frame_count() -> None:
    frames = [
        {
            "id": f"frame-{index}",
            "title": "",
            "position": {"x": 0, "y": 0},
            "size": {"width": 100, "height": 100},
        }
        for index in range(MAX_CANVAS_FRAMES + 1)
    ]
    with pytest.raises(ValidationError, match="at most 1000 items"):
        CanvasGraph.model_validate({"frames": frames})


def test_legacy_v1_nodes_keep_historical_reference_cardinality_readable() -> None:
    image_assets = [
        _node(
            f"image-asset-{index}",
            "image_asset",
            {"image_id": f"image-{index}"},
        )
        for index in range(17)
    ]
    video_assets = [
        _node(
            f"video-asset-{index}",
            "video_asset",
            {"video_id": f"video-{index}"},
        )
        for index in range(4)
    ]
    graph = CanvasGraph.model_validate(
        {
            "nodes": [
                *image_assets,
                *video_assets,
                _node("image-generate", "image_generate"),
                _node("video-generate", "video_generate", {"mode": "reference"}),
            ],
            "edges": [
                *[
                    _edge(
                        f"image-reference-{index}",
                        source["id"],
                        "image",
                        "image-generate",
                        "references",
                        "image",
                        order=index,
                    )
                    for index, source in enumerate(image_assets)
                ],
                *[
                    _edge(
                        f"legacy-video-image-{index}",
                        source["id"],
                        "image",
                        "video-generate",
                        "reference_images",
                        "image",
                        order=index,
                    )
                    for index, source in enumerate(image_assets[:10])
                ],
                *[
                    _edge(
                        f"legacy-video-clip-{index}",
                        source["id"],
                        "video",
                        "video-generate",
                        "reference_videos",
                        "video",
                        order=index,
                    )
                    for index, source in enumerate(video_assets)
                ],
            ],
        }
    )

    assert len(graph.edges) == 31


def test_new_node_ports_required_inputs_and_mask_asset_are_compatible() -> None:
    graph = CanvasGraph.model_validate(
        {
            "nodes": [
                _node("prompt", "prompt", {"text": "repair"}),
                _node("source", "image_asset", {"image_id": "source-image"}),
                _node("mask", "mask_asset", {"image_id": "mask-image"}),
                _node("inpaint", "image_inpaint"),
                _node("video-ref", "video_reference_generate"),
            ],
            "edges": [
                _edge(
                    "prompt-inpaint",
                    "prompt",
                    "text",
                    "inpaint",
                    "prompt",
                    "text",
                ),
                _edge(
                    "source-inpaint",
                    "source",
                    "image",
                    "inpaint",
                    "source",
                    "image",
                ),
                _edge(
                    "mask-inpaint",
                    "mask",
                    "mask",
                    "inpaint",
                    "mask",
                    "mask",
                ),
            ],
        }
    )

    assert validate_required_inputs(graph, "inpaint") == []
    assert validate_required_inputs(graph, "video-ref") == [
        "prompt",
        "reference_images|reference_videos",
    ]


def test_mask_asset_exposes_only_mask_output_and_image_assets_can_feed_masks() -> None:
    assert set(NODE_OUTPUT_PORTS["mask_asset"]) == {"mask"}
    assert NODE_OUTPUT_PORTS["mask_asset"]["mask"].data_type == "mask"

    CanvasGraph.model_validate(
        {
            "nodes": [
                _node("image", "image_asset", {"image_id": "mask-image"}),
                _node("inpaint", "image_inpaint"),
            ],
            "edges": [
                _edge(
                    "image-mask",
                    "image",
                    "image",
                    "inpaint",
                    "mask",
                    "mask",
                )
            ],
        }
    )

    with pytest.raises(ValidationError, match="unknown source handle"):
        CanvasGraph.model_validate(
            {
                "nodes": [
                    _node("mask", "mask_asset", {"image_id": "mask-image"}),
                    _node("inpaint", "image_inpaint"),
                ],
                "edges": [
                    _edge(
                        "missing-image-handle",
                        "mask",
                        "image",
                        "inpaint",
                        "mask",
                        "mask",
                    )
                ],
            }
        )

    with pytest.raises(ValidationError, match="incompatible port types"):
        CanvasGraph.model_validate(
            {
                "nodes": [
                    _node("mask", "mask_asset", {"image_id": "mask-image"}),
                    _node("edit", "image_edit"),
                ],
                "edges": [
                    _edge(
                        "mask-as-source-image",
                        "mask",
                        "mask",
                        "edit",
                        "source",
                        "image",
                    )
                ],
            }
        )


@pytest.mark.parametrize(
    ("source_type", "source_handle", "target_handle", "data_type", "count"),
    [
        ("image_asset", "image", "reference_images", "image", 10),
        ("video_asset", "video", "reference_videos", "video", 4),
    ],
)
def test_reference_video_ports_enforce_downstream_media_limits(
    source_type: str,
    source_handle: str,
    target_handle: str,
    data_type: str,
    count: int,
) -> None:
    nodes = [_node("target", "video_reference_generate")]
    edges = []
    for index in range(count):
        source_id = f"source-{index}"
        config = (
            {"image_id": f"image-{index}"}
            if source_type == "image_asset"
            else {"video_id": f"video-{index}"}
        )
        nodes.append(_node(source_id, source_type, config))
        edges.append(
            _edge(
                f"edge-{index}",
                source_id,
                source_handle,
                "target",
                target_handle,
                data_type,
                order=index,
            )
        )

    with pytest.raises(ValidationError, match="accepts at most"):
        CanvasGraph.model_validate({"nodes": nodes, "edges": edges})


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
