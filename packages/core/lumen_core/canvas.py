"""Infinite-canvas graph hashing, mutation application, and stale propagation."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
import unicodedata
from typing import Any

from pydantic import BaseModel

from .canvas_schemas import (
    CANVAS_NODE_ADAPTER,
    CANVAS_OPERATION_ADAPTER,
    AddEdgeOperation,
    AddFrameOperation,
    AddNodeOperation,
    CanvasEdge,
    CanvasFrame,
    CanvasGraph,
    CanvasOperation,
    MoveNodesOperation,
    RemoveEdgesOperation,
    RemoveFrameOperation,
    RemoveNodesOperation,
    ResizeNodeOperation,
    UpdateDocumentSettingsOperation,
    UpdateEdgeOperation,
    UpdateFrameOperation,
    UpdateNodeConfigOperation,
    UpdateNodeMetaOperation,
)


class CanvasMutationError(ValueError):
    """Base error for deterministic domain-mutation rejection."""


class CanvasEntityNotFoundError(CanvasMutationError):
    """A mutation referenced a node, edge, or frame that does not exist."""


class CanvasPreconditionFailedError(CanvasMutationError):
    """A mutation precondition no longer matches the materialized graph."""


@dataclass(frozen=True, slots=True)
class CanvasMutationResult:
    graph: CanvasGraph
    stale_node_ids: tuple[str, ...]
    changed_node_ids: tuple[str, ...]
    changed_edge_ids: tuple[str, ...]


def _plain_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _canonical_number(value: int | float | Decimal) -> str:
    if isinstance(value, bool):
        raise TypeError("booleans are not canonical JSON numbers")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not support NaN or infinity")
        decimal_value = Decimal(str(value))
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical JSON does not support NaN or infinity")
        decimal_value = value
    else:
        return str(value)
    if decimal_value == 0:
        return "0"
    rendered = format(decimal_value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _canonical_json(value: Any) -> str:
    value = _plain_value(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(value)
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, Mapping):
        normalized_items: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("canonical JSON object keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized_items:
                raise ValueError("object keys collide after NFC normalization")
            normalized_items[key] = item
        return (
            "{"
            + ",".join(
                f"{_canonical_json(key)}:{_canonical_json(normalized_items[key])}"
                for key in sorted(normalized_items)
            )
            + "}"
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_dumps(value: Any) -> str:
    """Serialize with stable keys, NFC strings, and normalized finite numbers."""

    return _canonical_json(value)


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json_dumps(value).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canvas_graph_hash(graph: CanvasGraph | Mapping[str, Any]) -> str:
    parsed = (
        graph if isinstance(graph, CanvasGraph) else CanvasGraph.model_validate(graph)
    )
    return canonical_hash(parsed.model_dump(mode="python"))


def canvas_selection_hash(selection_snapshot: Mapping[str, Any]) -> str:
    return canonical_hash(selection_snapshot)


def canvas_node_definition_hash(node: BaseModel | Mapping[str, Any]) -> str:
    parsed = (
        node
        if isinstance(node, BaseModel)
        else CANVAS_NODE_ADAPTER.validate_python(node)
    )
    return canonical_hash(
        {
            "node_type": getattr(parsed, "type"),
            "config": getattr(parsed, "config").model_dump(mode="python"),
        }
    )


def canvas_input_hash(input_snapshot: Mapping[str, Any]) -> str:
    return canonical_hash(input_snapshot)


def _binding_identity_matches(edge: CanvasEdge, raw_binding: Mapping[str, Any]) -> bool:
    expected = {
        "edge_id": edge.id,
        "source_node_id": edge.source_node_id,
        "target_handle": edge.target_handle,
        "role": edge.role,
        "order": int(edge.order or 0),
        "binding_mode": edge.binding_mode,
    }
    return all(raw_binding.get(key) == value for key, value in expected.items())


def _binding_source_matches(
    edge: CanvasEdge,
    raw_binding: Mapping[str, Any],
    source: Any,
    selections: Mapping[str, tuple[str | None, int]],
) -> tuple[bool, str | None]:
    if source.type == "prompt":
        text = source.config.text.strip()
        return raw_binding.get("text") == text, text or None

    asset = raw_binding.get("asset")
    if source.type == "image_asset":
        matches = (
            isinstance(asset, Mapping)
            and asset.get("image_id") == source.config.image_id
        )
        return matches, None
    if source.type == "video_asset":
        matches = (
            isinstance(asset, Mapping)
            and asset.get("video_id") == source.config.video_id
        )
        return matches, None

    selected = (
        (edge.pinned_execution_id, int(edge.pinned_output_index or 0))
        if edge.binding_mode == "pinned"
        else selections.get(source.id)
    )
    if selected is None:
        return False, None
    matches = (
        raw_binding.get("source_execution_id") == selected[0]
        and int(raw_binding.get("output_index") or 0) == selected[1]
    )
    return matches, None


def canvas_input_snapshot_matches_graph(
    graph: CanvasGraph | Mapping[str, Any],
    *,
    node_id: str,
    input_snapshot: Mapping[str, Any],
    selections: Mapping[str, tuple[str | None, int]],
) -> bool:
    """Check whether current graph bindings still resolve to a stored input snapshot."""

    parsed = _graph_model(graph)
    node_by_id = {node.id: node for node in parsed.nodes}
    if node_id not in node_by_id:
        return False
    raw_bindings = input_snapshot.get("bindings")
    if not isinstance(raw_bindings, list):
        return False
    incoming = sorted(
        (edge for edge in parsed.edges if edge.target_node_id == node_id),
        key=lambda edge: (
            edge.target_handle,
            int(edge.order or 0),
            edge.id,
        ),
    )
    if len(incoming) != len(raw_bindings):
        return False

    prompt_values: list[str] = []
    for edge, raw_binding in zip(incoming, raw_bindings, strict=True):
        if not isinstance(raw_binding, Mapping):
            return False
        if not _binding_identity_matches(edge, raw_binding):
            return False

        source = node_by_id.get(edge.source_node_id)
        if source is None:
            return False
        matches, prompt = _binding_source_matches(
            edge,
            raw_binding,
            source,
            selections,
        )
        if not matches:
            return False
        if prompt is not None:
            prompt_values.append(prompt)

    return prompt_values == [str(input_snapshot.get("prompt") or "")]


def canvas_execution_fingerprint(
    *,
    definition_hash: str,
    input_hash: str,
    node_schema_version: int,
    effective_model: str | None,
    effective_provider_capability: Mapping[str, Any] | str | None,
    processor_version: str,
) -> str:
    return canonical_hash(
        {
            "definition_hash": definition_hash,
            "input_hash": input_hash,
            "node_schema_version": node_schema_version,
            "effective_model": effective_model,
            "effective_provider_capability": effective_provider_capability,
            "processor_version": processor_version,
        }
    )


def _graph_model(graph: CanvasGraph | Mapping[str, Any]) -> CanvasGraph:
    return (
        graph if isinstance(graph, CanvasGraph) else CanvasGraph.model_validate(graph)
    )


def topological_node_ids(graph: CanvasGraph | Mapping[str, Any]) -> tuple[str, ...]:
    parsed = _graph_model(graph)
    node_order = {node.id: index for index, node in enumerate(parsed.nodes)}
    adjacency: dict[str, set[str]] = {node.id: set() for node in parsed.nodes}
    indegree = {node.id: 0 for node in parsed.nodes}
    for edge in parsed.edges:
        targets = adjacency[edge.source_node_id]
        if edge.target_node_id not in targets:
            targets.add(edge.target_node_id)
            indegree[edge.target_node_id] += 1
    ready = deque(
        sorted(
            (node_id for node_id, degree in indegree.items() if degree == 0),
            key=node_order.__getitem__,
        )
    )
    ordered: list[str] = []
    while ready:
        node_id = ready.popleft()
        ordered.append(node_id)
        unlocked: list[str] = []
        for target_id in adjacency[node_id]:
            indegree[target_id] -= 1
            if indegree[target_id] == 0:
                unlocked.append(target_id)
        ready.extend(sorted(unlocked, key=node_order.__getitem__))
    if len(ordered) != len(parsed.nodes):
        raise ValueError("canvas graph contains a cycle")
    return tuple(ordered)


def propagate_stale(
    graph: CanvasGraph | Mapping[str, Any],
    root_node_ids: Iterable[str],
    *,
    include_roots: bool = True,
    follow_active_only: bool = True,
) -> tuple[str, ...]:
    """Return roots and descendants whose resolved inputs are no longer fresh."""

    parsed = _graph_model(graph)
    node_ids = {node.id for node in parsed.nodes}
    roots = {node_id for node_id in root_node_ids if node_id in node_ids}
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for edge in parsed.edges:
        if follow_active_only and edge.binding_mode != "follow_active":
            continue
        adjacency[edge.source_node_id].add(edge.target_node_id)

    seen = set(roots)
    queue = deque(roots)
    while queue:
        node_id = queue.popleft()
        for target_id in adjacency[node_id]:
            if target_id not in seen:
                seen.add(target_id)
                queue.append(target_id)
    if not include_roots:
        seen -= roots
    node_order = {node.id: index for index, node in enumerate(parsed.nodes)}
    return tuple(sorted(seen, key=node_order.__getitem__))


def stale_nodes_for_selection_change(
    graph: CanvasGraph | Mapping[str, Any], source_node_id: str
) -> tuple[str, ...]:
    return propagate_stale(
        graph,
        [source_node_id],
        include_roots=False,
        follow_active_only=True,
    )


def _operation_model(operation: CanvasOperation | Mapping[str, Any]) -> CanvasOperation:
    if isinstance(operation, BaseModel):
        return operation
    return CANVAS_OPERATION_ADAPTER.validate_python(operation)


def _entity_index(items: list[dict[str, Any]], entity_id: str, kind: str) -> int:
    for index, item in enumerate(items):
        if item["id"] == entity_id:
            return index
    raise CanvasEntityNotFoundError(f"{kind} {entity_id!r} does not exist")


def _operation_target(
    operation: CanvasOperation,
    graph_data: dict[str, Any],
) -> dict[str, Any] | None:
    if isinstance(operation, (AddNodeOperation, AddEdgeOperation, AddFrameOperation)):
        return None
    if isinstance(
        operation,
        (
            UpdateNodeConfigOperation,
            UpdateNodeMetaOperation,
            ResizeNodeOperation,
        ),
    ):
        return graph_data["nodes"][
            _entity_index(graph_data["nodes"], operation.node_id, "node")
        ]
    if isinstance(operation, MoveNodesOperation):
        return None
    if isinstance(operation, RemoveNodesOperation):
        return None
    if isinstance(operation, UpdateEdgeOperation):
        return graph_data["edges"][
            _entity_index(graph_data["edges"], operation.edge_id, "edge")
        ]
    if isinstance(operation, RemoveEdgesOperation):
        return None
    if isinstance(operation, UpdateFrameOperation):
        return graph_data["frames"][
            _entity_index(graph_data["frames"], operation.frame_id, "frame")
        ]
    if isinstance(operation, RemoveFrameOperation):
        return graph_data["frames"][
            _entity_index(graph_data["frames"], operation.frame_id, "frame")
        ]
    if isinstance(operation, UpdateDocumentSettingsOperation):
        return graph_data["settings"]
    return None


def _check_precondition(
    operation: CanvasOperation,
    graph_data: dict[str, Any],
) -> None:
    target = _operation_target(operation, graph_data)
    if operation.precondition_hash is not None:
        if target is None:
            raise CanvasPreconditionFailedError(
                f"{operation.op} does not have one hashable target"
            )
        if canonical_hash(target) != operation.precondition_hash:
            raise CanvasPreconditionFailedError(
                f"{operation.op} precondition hash no longer matches"
            )
    if operation.expected_entity_version is not None:
        if target is None or "schema_version" not in target:
            raise CanvasPreconditionFailedError(
                f"{operation.op} target has no entity version"
            )
        if target["schema_version"] != operation.expected_entity_version:
            raise CanvasPreconditionFailedError(
                f"{operation.op} entity version no longer matches"
            )


def _config_value(config: dict[str, Any], path: str) -> Any:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise CanvasMutationError(f"config path {path!r} does not exist")
        current = current[part]
    return current


def _set_config_value(config: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current: Any = config
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            raise CanvasMutationError(f"config path {path!r} does not exist")
        current = current[part]
    if not isinstance(current, dict) or parts[-1] not in current:
        raise CanvasMutationError(f"config path {path!r} does not exist")
    current[parts[-1]] = deepcopy(value)


@dataclass(slots=True)
class _MutationState:
    graph_data: dict[str, Any]
    stale_roots: set[str]
    changed_nodes: set[str]
    changed_edges: set[str]


def _apply_add_node(state: _MutationState, operation: AddNodeOperation) -> None:
    if any(item["id"] == operation.node.id for item in state.graph_data["nodes"]):
        raise CanvasMutationError(f"node {operation.node.id!r} already exists")
    if any(item["id"] == operation.node.id for item in state.graph_data["frames"]):
        raise CanvasMutationError(
            f"id {operation.node.id!r} is already used by a frame"
        )
    state.graph_data["nodes"].append(operation.node.model_dump(mode="python"))
    state.changed_nodes.add(operation.node.id)


def _apply_update_node_config(
    state: _MutationState,
    operation: UpdateNodeConfigOperation,
) -> None:
    index = _entity_index(state.graph_data["nodes"], operation.node_id, "node")
    node_data = state.graph_data["nodes"][index]
    if operation.config is not None:
        node_data["config"] = deepcopy(operation.config)
    else:
        config = deepcopy(node_data["config"])
        for change in operation.changes:
            current_value = _config_value(config, change.path)
            if (
                change.before_hash is not None
                and canonical_hash(current_value) != change.before_hash
            ):
                raise CanvasPreconditionFailedError(
                    f"config path {change.path!r} no longer matches"
                )
            _set_config_value(config, change.path, change.value)
        node_data["config"] = config
    state.graph_data["nodes"][index] = CANVAS_NODE_ADAPTER.validate_python(
        node_data
    ).model_dump(mode="python")
    state.changed_nodes.add(operation.node_id)
    state.stale_roots.add(operation.node_id)


def _apply_update_node_meta(
    state: _MutationState,
    operation: UpdateNodeMetaOperation,
) -> None:
    index = _entity_index(state.graph_data["nodes"], operation.node_id, "node")
    node_data = state.graph_data["nodes"][index]
    fields = operation.model_fields_set
    if "title" in fields and operation.title is not None:
        node_data["title"] = operation.title
    if "parent_group_id" in fields:
        node_data["parent_group_id"] = operation.parent_group_id
    if "ui" in fields and operation.ui is not None:
        node_data["ui"] = operation.ui.model_dump(mode="python")
    state.graph_data["nodes"][index] = CANVAS_NODE_ADAPTER.validate_python(
        node_data
    ).model_dump(mode="python")
    state.changed_nodes.add(operation.node_id)


def _apply_move_nodes(state: _MutationState, operation: MoveNodesOperation) -> None:
    if len({item.node_id for item in operation.items}) != len(operation.items):
        raise CanvasMutationError("move_nodes contains duplicate node ids")
    for item in operation.items:
        index = _entity_index(state.graph_data["nodes"], item.node_id, "node")
        state.graph_data["nodes"][index]["position"] = {"x": item.x, "y": item.y}
        state.changed_nodes.add(item.node_id)


def _apply_resize_node(state: _MutationState, operation: ResizeNodeOperation) -> None:
    index = _entity_index(state.graph_data["nodes"], operation.node_id, "node")
    state.graph_data["nodes"][index]["size"] = operation.size.model_dump(mode="python")
    state.changed_nodes.add(operation.node_id)


def _apply_remove_nodes(state: _MutationState, operation: RemoveNodesOperation) -> None:
    node_ids = set(operation.node_ids)
    if len(node_ids) != len(operation.node_ids):
        raise CanvasMutationError("remove_nodes contains duplicate node ids")
    for node_id in node_ids:
        _entity_index(state.graph_data["nodes"], node_id, "node")
    associated = {
        edge["id"]
        for edge in state.graph_data["edges"]
        if edge["source_node_id"] in node_ids or edge["target_node_id"] in node_ids
    }
    if associated != set(operation.edge_ids):
        raise CanvasPreconditionFailedError(
            "remove_nodes edge_ids must exactly match associated edges"
        )
    state.stale_roots.update(
        edge["target_node_id"]
        for edge in state.graph_data["edges"]
        if edge["source_node_id"] in node_ids and edge["target_node_id"] not in node_ids
    )
    state.graph_data["nodes"] = [
        item for item in state.graph_data["nodes"] if item["id"] not in node_ids
    ]
    state.graph_data["edges"] = [
        item for item in state.graph_data["edges"] if item["id"] not in associated
    ]
    _normalize_edge_orders(state.graph_data["edges"])
    state.changed_nodes.update(node_ids)
    state.changed_edges.update(associated)


def _apply_add_edge(state: _MutationState, operation: AddEdgeOperation) -> None:
    if any(item["id"] == operation.edge.id for item in state.graph_data["edges"]):
        raise CanvasMutationError(f"edge {operation.edge.id!r} already exists")
    state.graph_data["edges"].append(operation.edge.model_dump(mode="python"))
    state.changed_edges.add(operation.edge.id)
    state.stale_roots.add(operation.edge.target_node_id)


def _apply_update_edge(state: _MutationState, operation: UpdateEdgeOperation) -> None:
    index = _entity_index(state.graph_data["edges"], operation.edge_id, "edge")
    edge_data = state.graph_data["edges"][index]
    fields = operation.model_fields_set
    if "binding_mode" in fields and operation.binding_mode is not None:
        edge_data["binding_mode"] = operation.binding_mode
        if operation.binding_mode == "follow_active":
            edge_data["pinned_execution_id"] = None
            edge_data["pinned_output_index"] = None
    for field_name in (
        "pinned_execution_id",
        "pinned_output_index",
        "role",
        "order",
    ):
        if field_name in fields:
            edge_data[field_name] = getattr(operation, field_name)
    state.graph_data["edges"][index] = CanvasEdge.model_validate(edge_data).model_dump(
        mode="python"
    )
    state.changed_edges.add(operation.edge_id)
    state.stale_roots.add(edge_data["target_node_id"])


def _apply_remove_edges(state: _MutationState, operation: RemoveEdgesOperation) -> None:
    edge_ids = set(operation.edge_ids)
    if len(edge_ids) != len(operation.edge_ids):
        raise CanvasMutationError("remove_edges contains duplicate edge ids")
    targets: set[str] = set()
    for edge_id in edge_ids:
        index = _entity_index(state.graph_data["edges"], edge_id, "edge")
        targets.add(state.graph_data["edges"][index]["target_node_id"])
    state.graph_data["edges"] = [
        item for item in state.graph_data["edges"] if item["id"] not in edge_ids
    ]
    _normalize_edge_orders(state.graph_data["edges"])
    state.changed_edges.update(edge_ids)
    state.stale_roots.update(targets)


def _normalize_edge_orders(edges: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for edge in edges:
        key = (edge["target_node_id"], edge["target_handle"])
        groups.setdefault(key, []).append(edge)
    for values in groups.values():
        values.sort(key=lambda item: (int(item.get("order") or 0), item["id"]))
        if len(values) > 1:
            for order, edge in enumerate(values):
                edge["order"] = order


def _apply_add_frame(state: _MutationState, operation: AddFrameOperation) -> None:
    if any(item["id"] == operation.frame.id for item in state.graph_data["frames"]):
        raise CanvasMutationError(f"frame {operation.frame.id!r} already exists")
    if any(item["id"] == operation.frame.id for item in state.graph_data["nodes"]):
        raise CanvasMutationError(
            f"id {operation.frame.id!r} is already used by a node"
        )
    state.graph_data["frames"].append(operation.frame.model_dump(mode="python"))


def _apply_update_frame(state: _MutationState, operation: UpdateFrameOperation) -> None:
    index = _entity_index(state.graph_data["frames"], operation.frame_id, "frame")
    frame_data = state.graph_data["frames"][index]
    fields = operation.model_fields_set
    for field_name in (
        "title",
        "position",
        "size",
        "parent_frame_id",
        "collapsed",
        "hidden_in_run",
        "color_tag",
    ):
        if field_name not in fields:
            continue
        value = getattr(operation, field_name)
        if field_name in {"position", "size"} and value is not None:
            value = value.model_dump(mode="python")
        if field_name == "title" and value is None:
            continue
        frame_data[field_name] = value
    state.graph_data["frames"][index] = CanvasFrame.model_validate(
        frame_data
    ).model_dump(mode="python")


def _apply_remove_frame(state: _MutationState, operation: RemoveFrameOperation) -> None:
    index = _entity_index(state.graph_data["frames"], operation.frame_id, "frame")
    removed = state.graph_data["frames"][index]
    parent_id = removed.get("parent_frame_id")
    del state.graph_data["frames"][index]
    for node_data in state.graph_data["nodes"]:
        if node_data.get("parent_group_id") == operation.frame_id:
            node_data["parent_group_id"] = parent_id
    for frame_data in state.graph_data["frames"]:
        if frame_data.get("parent_frame_id") == operation.frame_id:
            frame_data["parent_frame_id"] = parent_id


def _apply_document_settings(
    state: _MutationState,
    operation: UpdateDocumentSettingsOperation,
) -> None:
    state.graph_data["settings"] = operation.settings.model_dump(mode="python")


_OPERATION_HANDLERS: dict[type[Any], Any] = {
    AddNodeOperation: _apply_add_node,
    UpdateNodeConfigOperation: _apply_update_node_config,
    UpdateNodeMetaOperation: _apply_update_node_meta,
    MoveNodesOperation: _apply_move_nodes,
    ResizeNodeOperation: _apply_resize_node,
    RemoveNodesOperation: _apply_remove_nodes,
    AddEdgeOperation: _apply_add_edge,
    UpdateEdgeOperation: _apply_update_edge,
    RemoveEdgesOperation: _apply_remove_edges,
    AddFrameOperation: _apply_add_frame,
    UpdateFrameOperation: _apply_update_frame,
    RemoveFrameOperation: _apply_remove_frame,
    UpdateDocumentSettingsOperation: _apply_document_settings,
}


def apply_canvas_mutation(
    graph: CanvasGraph | Mapping[str, Any],
    operations: Iterable[CanvasOperation | Mapping[str, Any]],
) -> CanvasMutationResult:
    """Apply a domain-operation batch atomically and validate the final graph."""

    original = _graph_model(graph)
    state = _MutationState(
        graph_data=deepcopy(original.model_dump(mode="python")),
        stale_roots=set(),
        changed_nodes=set(),
        changed_edges=set(),
    )

    for raw_operation in operations:
        operation = _operation_model(raw_operation)
        _check_precondition(operation, state.graph_data)
        handler = _OPERATION_HANDLERS.get(type(operation))
        if handler is None:
            raise CanvasMutationError(f"unsupported operation {operation.op!r}")
        handler(state, operation)

    try:
        result_graph = CanvasGraph.model_validate(state.graph_data)
    except ValueError as exc:
        raise CanvasMutationError(str(exc)) from exc

    stale_ids = set(propagate_stale(result_graph, state.stale_roots))
    node_order = {node.id: index for index, node in enumerate(result_graph.nodes)}
    ordered_stale = tuple(sorted(stale_ids, key=node_order.__getitem__))
    return CanvasMutationResult(
        graph=result_graph,
        stale_node_ids=ordered_stale,
        changed_node_ids=tuple(sorted(state.changed_nodes)),
        changed_edge_ids=tuple(sorted(state.changed_edges)),
    )


__all__ = [
    "CanvasEntityNotFoundError",
    "CanvasMutationError",
    "CanvasMutationResult",
    "CanvasPreconditionFailedError",
    "apply_canvas_mutation",
    "canonical_hash",
    "canonical_json_bytes",
    "canonical_json_dumps",
    "canvas_execution_fingerprint",
    "canvas_graph_hash",
    "canvas_input_hash",
    "canvas_input_snapshot_matches_graph",
    "canvas_node_definition_hash",
    "canvas_selection_hash",
    "propagate_stale",
    "stale_nodes_for_selection_change",
    "topological_node_ids",
]
