"""Public helpers and export inventory for canvas schemas."""

from __future__ import annotations

import math
import sys
from typing import Any


def validate_required_inputs(graph: Any, node_id: str) -> list[str]:
    """Return execution-required input handles that are currently unresolved."""
    schemas = sys.modules["lumen_core.canvas_schemas"]
    node = next((item for item in graph.nodes if item.id == node_id), None)
    if node is None:
        raise KeyError(node_id)
    connected = {
        edge.target_handle for edge in graph.edges if edge.target_node_id == node_id
    }
    missing = [
        handle
        for handle, spec in schemas.NODE_INPUT_PORTS[node.type].items()
        if spec.required_for_execution and handle not in connected
    ]
    if node.type == "video_generate" and node.config.mode == "i2v":
        if "first_frame" not in connected:
            missing.append("first_frame")
    if node.type == "video_generate" and node.config.mode == "reference":
        if not connected & {"reference_images", "reference_videos"}:
            missing.append("reference_images|reference_videos")
    if node.type == "video_reference_generate":
        if not connected & {"reference_images", "reference_videos"}:
            missing.append("reference_images|reference_videos")
    return missing


def ensure_finite_number(value: float) -> float:
    """Guard callers constructing mutation payloads manually."""
    if not math.isfinite(value):
        raise ValueError("canvas coordinates and dimensions must be finite")
    return value


EXPORTS = (
    "AddEdgeOperation",
    "AddFrameOperation",
    "AddNodeOperation",
    "CANVAS_NODE_ADAPTER",
    "CANVAS_OPERATION_ADAPTER",
    "CANVAS_OPERATIONS_ADAPTER",
    "CanvasBindingMode",
    "CanvasConfigChange",
    "CanvasDataType",
    "CanvasDocumentSettings",
    "CanvasEdge",
    "CanvasFrame",
    "CanvasGraph",
    "CanvasNodeDefinition",
    "CanvasNodeType",
    "CanvasOperation",
    "CanvasPortSpec",
    "EXECUTABLE_NODE_TYPES",
    "GENERATED_OUTPUT_NODE_TYPES",
    "GRAPH_SCHEMA_VERSION",
    "ImageAssetNode",
    "ImageEditNode",
    "ImageGenerateNode",
    "ImageInpaintNode",
    "ImageUpscaleNode",
    "IMAGE_EXECUTABLE_NODE_TYPES",
    "MAX_CANVAS_EDGES",
    "MAX_CANVAS_FRAMES",
    "MAX_CANVAS_GRAPH_BYTES",
    "MAX_CANVAS_NODES",
    "MaskAssetNode",
    "MoveNodesOperation",
    "NODE_INPUT_PORTS",
    "NODE_OUTPUT_PORTS",
    "PromptMergeNode",
    "PromptNode",
    "RemoveEdgesOperation",
    "RemoveFrameOperation",
    "RemoveNodesOperation",
    "ResizeNodeOperation",
    "UpdateDocumentSettingsOperation",
    "UpdateEdgeOperation",
    "UpdateFrameOperation",
    "UpdateNodeConfigOperation",
    "UpdateNodeMetaOperation",
    "VideoAssetNode",
    "VideoGenerateNode",
    "VideoImageGenerateNode",
    "VideoReferenceGenerateNode",
    "VideoTextGenerateNode",
    "VIDEO_EXECUTABLE_NODE_TYPES",
    "ensure_finite_number",
    "validate_required_inputs",
)
