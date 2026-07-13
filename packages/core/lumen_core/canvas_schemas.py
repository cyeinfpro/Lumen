"""Strict V1 graph and mutation schemas for the infinite canvas."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .constants import MAX_PROMPT_CHARS


GRAPH_SCHEMA_VERSION = 1
OPERATION_SCHEMA_VERSION = 1
MAX_CANVAS_NODES = 1_000
MAX_CANVAS_EDGES = 3_000
MAX_CANVAS_GRAPH_BYTES = 5 * 1024 * 1024
MAX_CANVAS_NODE_CONFIG_BYTES = 64 * 1024
MAX_CANVAS_GROUP_DEPTH = 4

_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,47}$")
_CONFIG_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")

CanvasDataType = Literal["text", "image", "video", "mask"]
CanvasBindingMode = Literal["follow_active", "pinned"]
CanvasNodeType = Literal[
    "prompt",
    "image_asset",
    "video_asset",
    "image_generate",
    "video_generate",
    "note",
    "frame",
    "delivery",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_entity_id(value: str) -> str:
    if not _ENTITY_ID_RE.fullmatch(value):
        raise ValueError(
            "IDs must be 1-64 ASCII letters, digits, '.', '_', ':', or '-'"
        )
    return value


def _validate_handle(value: str) -> str:
    if not _HANDLE_RE.fullmatch(value):
        raise ValueError("handle must be a 1-48 character stable identifier")
    return value


class CanvasPosition(_StrictModel):
    x: float = Field(allow_inf_nan=False, ge=-10_000_000, le=10_000_000)
    y: float = Field(allow_inf_nan=False, ge=-10_000_000, le=10_000_000)


class CanvasSize(_StrictModel):
    width: float = Field(allow_inf_nan=False, ge=40, le=10_000)
    height: float = Field(allow_inf_nan=False, ge=40, le=10_000)


class CanvasCrop(_StrictModel):
    x: float = Field(default=0, allow_inf_nan=False, ge=0, le=1)
    y: float = Field(default=0, allow_inf_nan=False, ge=0, le=1)
    width: float = Field(default=1, allow_inf_nan=False, gt=0, le=1)
    height: float = Field(default=1, allow_inf_nan=False, gt=0, le=1)

    @model_validator(mode="after")
    def stay_inside_source(self) -> "CanvasCrop":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("crop must stay inside the source asset")
        return self


class CanvasNodeUI(_StrictModel):
    collapsed: bool = False
    color_tag: str | None = Field(default=None, max_length=32)


class PromptNodeConfig(_StrictModel):
    text: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    locked: bool = False


class ImageAssetNodeConfig(_StrictModel):
    image_id: str = Field(default="", max_length=36)
    display_name: str | None = Field(default=None, max_length=255)
    crop: CanvasCrop | None = None


class VideoAssetNodeConfig(_StrictModel):
    video_id: str = Field(default="", max_length=36)
    display_name: str | None = Field(default=None, max_length=255)


class ImageGenerateNodeConfig(_StrictModel):
    model: str | None = Field(default=None, max_length=128)
    aspect_ratio: str = Field(default="1:1", min_length=1, max_length=16)
    size: Literal["1K", "2K", "4K", "1k", "2k", "4k"] = "1K"
    quality: Literal["standard", "high", "1k", "2k", "4k"] = "standard"
    size_mode: Literal["auto", "fixed"] = "auto"
    fixed_size: str | None = Field(default=None, max_length=32)
    render_quality: Literal["auto", "low", "medium", "high"] = "high"
    count: int = Field(default=1, ge=1, le=10)
    fast: bool | None = None
    output_format: Literal["png", "jpeg", "webp"] | None = "webp"
    output_compression: int | None = Field(default=None, ge=0, le=100)
    background: Literal["auto", "opaque", "transparent"] = "auto"
    moderation: Literal["auto", "low"] = "low"

    @model_validator(mode="after")
    def normalize_transparent_output(self) -> "ImageGenerateNodeConfig":
        if self.background == "transparent":
            self.output_format = "png"
            self.output_compression = None
        return self


class VideoGenerateNodeConfig(_StrictModel):
    mode: Literal["t2v", "i2v", "reference"] = "t2v"
    model: str | None = Field(default=None, max_length=64)
    duration_s: int = Field(default=5, ge=3, le=15)
    resolution: str = Field(default="720p", min_length=1, max_length=16)
    aspect_ratio: str = Field(default="16:9", min_length=1, max_length=16)
    generate_audio: bool = True
    seed: int | None = None
    watermark: bool = False


class NoteNodeConfig(_StrictModel):
    text: str = Field(default="", max_length=20_000)
    tags: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            clean = tag.strip()
            if not clean or len(clean) > 32:
                raise ValueError("note tags must contain 1-32 characters")
            if clean not in seen:
                normalized.append(clean)
                seen.add(clean)
        return normalized


class FrameNodeConfig(_StrictModel):
    label: str = Field(default="新画框", max_length=255)
    collapsed: bool = False
    hidden_in_run: bool = False
    runnable_scope: bool = True


class DeliveryNodeConfig(_StrictModel):
    set_as_thumbnail: bool = True
    thumbnail_source_node_id: str | None = None

    @field_validator("thumbnail_source_node_id")
    @classmethod
    def validate_thumbnail_source(cls, value: str | None) -> str | None:
        return _validate_entity_id(value) if value is not None else value


class CanvasNodeBase(_StrictModel):
    id: str
    schema_version: Literal[1] = 1
    title: str = Field(default="", max_length=255)
    position: CanvasPosition
    size: CanvasSize | None = None
    parent_group_id: str | None = None
    ui: CanvasNodeUI = Field(default_factory=CanvasNodeUI)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_entity_id(value)

    @field_validator("parent_group_id")
    @classmethod
    def validate_parent_id(cls, value: str | None) -> str | None:
        return _validate_entity_id(value) if value is not None else value


class PromptNode(CanvasNodeBase):
    type: Literal["prompt"]
    config: PromptNodeConfig


class ImageAssetNode(CanvasNodeBase):
    type: Literal["image_asset"]
    config: ImageAssetNodeConfig


class VideoAssetNode(CanvasNodeBase):
    type: Literal["video_asset"]
    config: VideoAssetNodeConfig


class ImageGenerateNode(CanvasNodeBase):
    type: Literal["image_generate"]
    config: ImageGenerateNodeConfig = Field(default_factory=ImageGenerateNodeConfig)


class VideoGenerateNode(CanvasNodeBase):
    type: Literal["video_generate"]
    config: VideoGenerateNodeConfig = Field(default_factory=VideoGenerateNodeConfig)


class NoteNode(CanvasNodeBase):
    type: Literal["note"]
    config: NoteNodeConfig = Field(default_factory=NoteNodeConfig)


class FrameNode(CanvasNodeBase):
    type: Literal["frame"]
    config: FrameNodeConfig = Field(default_factory=FrameNodeConfig)


class DeliveryNode(CanvasNodeBase):
    type: Literal["delivery"]
    config: DeliveryNodeConfig = Field(default_factory=DeliveryNodeConfig)


CanvasNodeDefinition = Annotated[
    PromptNode
    | ImageAssetNode
    | VideoAssetNode
    | ImageGenerateNode
    | VideoGenerateNode
    | NoteNode
    | FrameNode
    | DeliveryNode,
    Field(discriminator="type"),
]


class CanvasEdge(_StrictModel):
    id: str
    source_node_id: str
    source_handle: str
    target_node_id: str
    target_handle: str
    data_type: CanvasDataType
    binding_mode: CanvasBindingMode = "follow_active"
    pinned_execution_id: str | None = Field(default=None, max_length=36)
    pinned_output_index: int | None = Field(default=None, ge=0)
    role: (
        Literal[
            "reference",
            "subject",
            "product",
            "style",
            "edit_target",
            "background",
            "other",
        ]
        | None
    ) = None
    order: int | None = Field(default=None, ge=0)

    @field_validator("id", "source_node_id", "target_node_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _validate_entity_id(value)

    @field_validator("source_handle", "target_handle")
    @classmethod
    def validate_handles(cls, value: str) -> str:
        return _validate_handle(value)

    @model_validator(mode="after")
    def validate_binding(self) -> "CanvasEdge":
        pinned = self.binding_mode == "pinned"
        if pinned and (
            self.pinned_execution_id is None or self.pinned_output_index is None
        ):
            raise ValueError("pinned edges require execution id and output index")
        if not pinned and (
            self.pinned_execution_id is not None or self.pinned_output_index is not None
        ):
            raise ValueError("follow_active edges cannot carry pinned output fields")
        return self


class CanvasFrame(_StrictModel):
    id: str
    title: str = Field(default="", max_length=255)
    position: CanvasPosition
    size: CanvasSize
    parent_frame_id: str | None = None
    collapsed: bool = False
    hidden_in_run: bool = False
    color_tag: str | None = Field(default=None, max_length=32)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_entity_id(value)

    @field_validator("parent_frame_id")
    @classmethod
    def validate_parent_id(cls, value: str | None) -> str | None:
        return _validate_entity_id(value) if value is not None else value


class CanvasDocumentSettings(_StrictModel):
    snap_to_grid: bool = False
    grid_size: int = Field(default=16, ge=1, le=256)


@dataclass(frozen=True, slots=True)
class CanvasPortSpec:
    data_type: CanvasDataType
    maximum: int | None
    required_for_execution: bool = False


NODE_INPUT_PORTS: dict[str, dict[str, CanvasPortSpec]] = {
    "prompt": {},
    "image_asset": {},
    "video_asset": {},
    "image_generate": {
        "prompt": CanvasPortSpec("text", 1, True),
        "references": CanvasPortSpec("image", None),
        "mask": CanvasPortSpec("mask", 1),
    },
    "video_generate": {
        "prompt": CanvasPortSpec("text", 1, True),
        "first_frame": CanvasPortSpec("image", 1),
        "reference_images": CanvasPortSpec("image", None),
        "reference_videos": CanvasPortSpec("video", None),
    },
    "note": {},
    "frame": {},
    "delivery": {
        "images": CanvasPortSpec("image", None),
        "videos": CanvasPortSpec("video", None),
    },
}

NODE_OUTPUT_PORTS: dict[str, dict[str, CanvasPortSpec]] = {
    "prompt": {"text": CanvasPortSpec("text", None)},
    "image_asset": {"image": CanvasPortSpec("image", None)},
    "video_asset": {"video": CanvasPortSpec("video", None)},
    "image_generate": {"image": CanvasPortSpec("image", None)},
    "video_generate": {"video": CanvasPortSpec("video", None)},
    "note": {},
    "frame": {},
    "delivery": {},
}


def _validate_group_depth(
    parents: dict[str, str | None],
    *,
    max_depth: int = MAX_CANVAS_GROUP_DEPTH,
) -> None:
    for entity_id in parents:
        seen: set[str] = set()
        current: str | None = entity_id
        depth = 0
        while current is not None:
            if current in seen:
                raise ValueError(f"group nesting contains a cycle at {current!r}")
            seen.add(current)
            current = parents.get(current)
            if current is not None:
                depth += 1
                if depth > max_depth:
                    raise ValueError(f"group nesting exceeds depth {max_depth}")


def _validate_acyclic(node_ids: set[str], edges: list[CanvasEdge]) -> None:
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    indegree = {node_id: 0 for node_id in node_ids}
    for edge in edges:
        if edge.target_node_id not in adjacency[edge.source_node_id]:
            adjacency[edge.source_node_id].add(edge.target_node_id)
            indegree[edge.target_node_id] += 1

    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        node_id = ready.pop()
        visited += 1
        for target_id in adjacency[node_id]:
            indegree[target_id] -= 1
            if indegree[target_id] == 0:
                ready.append(target_id)
    if visited != len(node_ids):
        raise ValueError("executable canvas edges must form an acyclic graph")


def _validate_graph_entities(
    nodes: list[CanvasNodeDefinition],
    frames: list[CanvasFrame],
) -> dict[str, CanvasNodeDefinition]:
    node_by_id = {node.id: node for node in nodes}
    if len(node_by_id) != len(nodes):
        raise ValueError("node ids must be unique")
    frame_by_id = {frame.id: frame for frame in frames}
    if len(frame_by_id) != len(frames):
        raise ValueError("frame ids must be unique")
    if set(node_by_id) & set(frame_by_id):
        raise ValueError("node and frame ids share one graph-wide namespace")

    group_ids = {node.id for node in nodes if node.type == "frame"} | set(frame_by_id)
    for node in nodes:
        if node.parent_group_id is not None and node.parent_group_id not in group_ids:
            raise ValueError(
                f"node {node.id!r} references unknown frame {node.parent_group_id!r}"
            )
    for frame in frames:
        if frame.parent_frame_id is not None and frame.parent_frame_id not in group_ids:
            raise ValueError(
                f"frame {frame.id!r} references unknown parent "
                f"{frame.parent_frame_id!r}"
            )
    group_parents = {
        node.id: node.parent_group_id for node in nodes if node.type == "frame"
    }
    group_parents.update({frame.id: frame.parent_frame_id for frame in frames})
    _validate_group_depth(group_parents)
    return node_by_id


def _validate_edge_contract(
    edge: CanvasEdge,
    node_by_id: dict[str, CanvasNodeDefinition],
) -> None:
    source = node_by_id.get(edge.source_node_id)
    target = node_by_id.get(edge.target_node_id)
    if source is None or target is None:
        raise ValueError(f"edge {edge.id!r} references a missing node")
    source_port = NODE_OUTPUT_PORTS[source.type].get(edge.source_handle)
    target_port = NODE_INPUT_PORTS[target.type].get(edge.target_handle)
    if source_port is None:
        raise ValueError(
            f"edge {edge.id!r} uses unknown source handle "
            f"{source.type}.{edge.source_handle}"
        )
    if target_port is None:
        raise ValueError(
            f"edge {edge.id!r} uses unknown target handle "
            f"{target.type}.{edge.target_handle}"
        )
    source_matches = source_port.data_type == edge.data_type
    if edge.data_type == "mask":
        source_matches = source_port.data_type == "image"
    if not source_matches or target_port.data_type != edge.data_type:
        raise ValueError(f"edge {edge.id!r} has incompatible port types")
    if edge.binding_mode == "pinned" and source.type not in {
        "image_generate",
        "video_generate",
    }:
        raise ValueError("only generated outputs can use pinned bindings")
    if edge.role is not None and edge.data_type not in {"image", "mask"}:
        raise ValueError("roles are only valid for image or mask inputs")


def _collect_incoming_edges(
    node_by_id: dict[str, CanvasNodeDefinition],
    edges: list[CanvasEdge],
) -> dict[tuple[str, str], list[CanvasEdge]]:
    edge_by_id = {edge.id: edge for edge in edges}
    if len(edge_by_id) != len(edges):
        raise ValueError("edge ids must be unique")
    incoming: dict[tuple[str, str], list[CanvasEdge]] = {}
    endpoint_keys: set[tuple[str, str, str, str]] = set()
    for edge in edges:
        _validate_edge_contract(edge, node_by_id)
        endpoint_key = (
            edge.source_node_id,
            edge.source_handle,
            edge.target_node_id,
            edge.target_handle,
        )
        if endpoint_key in endpoint_keys:
            raise ValueError("duplicate connections between the same ports")
        endpoint_keys.add(endpoint_key)
        incoming.setdefault((edge.target_node_id, edge.target_handle), []).append(edge)
    return incoming


def _validate_input_cardinality(
    node_by_id: dict[str, CanvasNodeDefinition],
    incoming: dict[tuple[str, str], list[CanvasEdge]],
) -> None:
    for (target_id, handle), handle_edges in incoming.items():
        target = node_by_id[target_id]
        port = NODE_INPUT_PORTS[target.type][handle]
        if port.maximum is not None and len(handle_edges) > port.maximum:
            raise ValueError(
                f"{target.type}.{handle} accepts at most {port.maximum} edge(s)"
            )
        if len(handle_edges) <= 1:
            continue
        orders = [edge.order for edge in handle_edges]
        if any(order is None for order in orders):
            raise ValueError(f"multi-input port {handle!r} requires order")
        if sorted(orders) != list(range(len(handle_edges))):
            raise ValueError(f"multi-input port {handle!r} order must be contiguous")


def _validate_video_modes(
    nodes: list[CanvasNodeDefinition],
    incoming: dict[tuple[str, str], list[CanvasEdge]],
) -> None:
    for node in nodes:
        if node.type != "video_generate":
            continue
        handles = {
            handle
            for (target_id, handle), values in incoming.items()
            if target_id == node.id and values
        }
        if node.config.mode == "t2v" and handles & {
            "first_frame",
            "reference_images",
            "reference_videos",
        }:
            raise ValueError("t2v nodes cannot have frame or reference inputs")
        if node.config.mode == "i2v" and handles & {
            "reference_images",
            "reference_videos",
        }:
            raise ValueError("i2v nodes only accept a first frame")
        if node.config.mode == "reference" and "first_frame" in handles:
            raise ValueError("reference video nodes cannot have a first frame")


def _validate_graph_size(graph: "CanvasGraph") -> None:
    for node in graph.nodes:
        config_bytes = len(
            json.dumps(
                node.config.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if config_bytes > MAX_CANVAS_NODE_CONFIG_BYTES:
            raise ValueError(
                f"node {node.id!r} config exceeds {MAX_CANVAS_NODE_CONFIG_BYTES} bytes"
            )
    graph_bytes = len(
        json.dumps(
            graph.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if graph_bytes > MAX_CANVAS_GRAPH_BYTES:
        raise ValueError(f"canvas graph exceeds {MAX_CANVAS_GRAPH_BYTES} bytes")


class CanvasGraph(_StrictModel):
    schema_version: Literal[1] = GRAPH_SCHEMA_VERSION
    nodes: list[CanvasNodeDefinition] = Field(
        default_factory=list, max_length=MAX_CANVAS_NODES
    )
    edges: list[CanvasEdge] = Field(default_factory=list, max_length=MAX_CANVAS_EDGES)
    frames: list[CanvasFrame] = Field(default_factory=list)
    settings: CanvasDocumentSettings = Field(default_factory=CanvasDocumentSettings)

    @model_validator(mode="after")
    def validate_graph(self) -> "CanvasGraph":
        node_by_id = _validate_graph_entities(self.nodes, self.frames)
        incoming = _collect_incoming_edges(node_by_id, self.edges)
        _validate_input_cardinality(node_by_id, incoming)
        _validate_video_modes(self.nodes, incoming)
        _validate_acyclic(set(node_by_id), self.edges)
        _validate_graph_size(self)
        return self


class CanvasConfigChange(_StrictModel):
    path: str
    value: Any
    before_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not _CONFIG_PATH_RE.fullmatch(value):
            raise ValueError("config path must be an explicit dotted field path")
        return value


class CanvasOperationBase(_StrictModel):
    operation_schema_version: Literal[1] = OPERATION_SCHEMA_VERSION
    target_id: str | None = None
    precondition_hash: str | None = Field(default=None, min_length=64, max_length=64)
    expected_entity_version: int | None = Field(default=None, ge=0)
    inverse_payload: dict[str, Any] | None = None
    conflict_keys: list[str] = Field(default_factory=list)


class AddNodeOperation(CanvasOperationBase):
    op: Literal["add_node"]
    node: CanvasNodeDefinition


class UpdateNodeConfigOperation(CanvasOperationBase):
    op: Literal["update_node_config"]
    node_id: str
    config: dict[str, Any] | None = None
    changes: list[CanvasConfigChange] = Field(default_factory=list)

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        return _validate_entity_id(value)

    @model_validator(mode="after")
    def require_one_update_form(self) -> "UpdateNodeConfigOperation":
        if (self.config is None) == (not self.changes):
            raise ValueError("provide either a complete config or explicit changes")
        return self


class UpdateNodeMetaOperation(CanvasOperationBase):
    op: Literal["update_node_meta"]
    node_id: str
    title: str | None = Field(default=None, max_length=255)
    parent_group_id: str | None = None
    ui: CanvasNodeUI | None = None

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        return _validate_entity_id(value)

    @field_validator("parent_group_id")
    @classmethod
    def validate_parent_id(cls, value: str | None) -> str | None:
        return _validate_entity_id(value) if value is not None else value


class CanvasMoveItem(_StrictModel):
    node_id: str
    x: float = Field(allow_inf_nan=False, ge=-10_000_000, le=10_000_000)
    y: float = Field(allow_inf_nan=False, ge=-10_000_000, le=10_000_000)

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        return _validate_entity_id(value)


class MoveNodesOperation(CanvasOperationBase):
    op: Literal["move_nodes"]
    items: list[CanvasMoveItem] = Field(min_length=1)


class ResizeNodeOperation(CanvasOperationBase):
    op: Literal["resize_node"]
    node_id: str
    size: CanvasSize

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        return _validate_entity_id(value)


class RemoveNodesOperation(CanvasOperationBase):
    op: Literal["remove_nodes"]
    node_ids: list[str] = Field(min_length=1)
    edge_ids: list[str]

    @field_validator("node_ids", "edge_ids")
    @classmethod
    def validate_ids(cls, values: list[str]) -> list[str]:
        return [_validate_entity_id(value) for value in values]


class AddEdgeOperation(CanvasOperationBase):
    op: Literal["add_edge"]
    edge: CanvasEdge


class UpdateEdgeOperation(CanvasOperationBase):
    op: Literal["update_edge"]
    edge_id: str
    binding_mode: CanvasBindingMode | None = None
    pinned_execution_id: str | None = Field(default=None, max_length=36)
    pinned_output_index: int | None = Field(default=None, ge=0)
    role: (
        Literal[
            "reference",
            "subject",
            "product",
            "style",
            "edit_target",
            "background",
            "other",
        ]
        | None
    ) = None
    order: int | None = Field(default=None, ge=0)

    @field_validator("edge_id")
    @classmethod
    def validate_edge_id(cls, value: str) -> str:
        return _validate_entity_id(value)


class RemoveEdgesOperation(CanvasOperationBase):
    op: Literal["remove_edges"]
    edge_ids: list[str] = Field(min_length=1)

    @field_validator("edge_ids")
    @classmethod
    def validate_ids(cls, values: list[str]) -> list[str]:
        return [_validate_entity_id(value) for value in values]


class AddFrameOperation(CanvasOperationBase):
    op: Literal["add_frame"]
    frame: CanvasFrame


class UpdateFrameOperation(CanvasOperationBase):
    op: Literal["update_frame"]
    frame_id: str
    title: str | None = Field(default=None, max_length=255)
    position: CanvasPosition | None = None
    size: CanvasSize | None = None
    parent_frame_id: str | None = None
    collapsed: bool | None = None
    hidden_in_run: bool | None = None
    color_tag: str | None = Field(default=None, max_length=32)

    @field_validator("frame_id")
    @classmethod
    def validate_frame_id(cls, value: str) -> str:
        return _validate_entity_id(value)

    @field_validator("parent_frame_id")
    @classmethod
    def validate_parent_id(cls, value: str | None) -> str | None:
        return _validate_entity_id(value) if value is not None else value


class RemoveFrameOperation(CanvasOperationBase):
    op: Literal["remove_frame"]
    frame_id: str

    @field_validator("frame_id")
    @classmethod
    def validate_frame_id(cls, value: str) -> str:
        return _validate_entity_id(value)


class UpdateDocumentSettingsOperation(CanvasOperationBase):
    op: Literal["update_document_settings"]
    settings: CanvasDocumentSettings


CanvasOperation = Annotated[
    AddNodeOperation
    | UpdateNodeConfigOperation
    | UpdateNodeMetaOperation
    | MoveNodesOperation
    | ResizeNodeOperation
    | RemoveNodesOperation
    | AddEdgeOperation
    | UpdateEdgeOperation
    | RemoveEdgesOperation
    | AddFrameOperation
    | UpdateFrameOperation
    | RemoveFrameOperation
    | UpdateDocumentSettingsOperation,
    Field(discriminator="op"),
]

CANVAS_OPERATION_ADAPTER = TypeAdapter(CanvasOperation)
CANVAS_OPERATIONS_ADAPTER = TypeAdapter(list[CanvasOperation])
CANVAS_NODE_ADAPTER = TypeAdapter(CanvasNodeDefinition)


def validate_required_inputs(graph: CanvasGraph, node_id: str) -> list[str]:
    """Return execution-required input handles that are currently unresolved."""

    node = next((item for item in graph.nodes if item.id == node_id), None)
    if node is None:
        raise KeyError(node_id)
    connected = {
        edge.target_handle for edge in graph.edges if edge.target_node_id == node_id
    }
    missing = [
        handle
        for handle, spec in NODE_INPUT_PORTS[node.type].items()
        if spec.required_for_execution and handle not in connected
    ]
    if node.type == "video_generate" and node.config.mode == "i2v":
        if "first_frame" not in connected:
            missing.append("first_frame")
    if node.type == "video_generate" and node.config.mode == "reference":
        if not connected & {"reference_images", "reference_videos"}:
            missing.append("reference_images|reference_videos")
    return missing


def ensure_finite_number(value: float) -> float:
    """Small public guard for callers constructing mutation payloads manually."""

    if not math.isfinite(value):
        raise ValueError("canvas coordinates and dimensions must be finite")
    return value


__all__ = [
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
    "GRAPH_SCHEMA_VERSION",
    "ImageAssetNode",
    "ImageGenerateNode",
    "MAX_CANVAS_EDGES",
    "MAX_CANVAS_GRAPH_BYTES",
    "MAX_CANVAS_NODES",
    "MoveNodesOperation",
    "NODE_INPUT_PORTS",
    "NODE_OUTPUT_PORTS",
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
    "ensure_finite_number",
    "validate_required_inputs",
]
