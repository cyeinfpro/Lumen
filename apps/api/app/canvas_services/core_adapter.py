"""Adapters around the core Canvas graph contract."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from lumen_core.canvas import (
    CanvasMutationError,
    CanvasPreconditionFailedError,
    apply_canvas_mutation,
    canonical_hash,
)
from lumen_core.canvas_schemas import CANVAS_OPERATIONS_ADAPTER, CanvasGraph

from .errors import canvas_http


def json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def validated_graph(value: Any) -> dict[str, Any]:
    try:
        result = CanvasGraph.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise canvas_http("invalid_canvas_graph", str(exc), 422) from exc
    normalized = json_value(result)
    if not isinstance(normalized, dict):
        raise canvas_http("invalid_canvas_graph", "graph must be an object", 422)
    return normalized


def apply_graph_operations(
    graph: dict[str, Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        parsed_operations = CANVAS_OPERATIONS_ADAPTER.validate_python(operations)
        result = apply_canvas_mutation(graph, parsed_operations).graph
    except CanvasPreconditionFailedError as exc:
        raise canvas_http(
            "canvas_precondition_failed",
            str(exc),
            409,
        ) from exc
    except CanvasMutationError as exc:
        raise canvas_http("invalid_canvas_operation", str(exc), 422) from exc
    except (TypeError, ValueError) as exc:
        raise canvas_http("invalid_canvas_operation", str(exc), 422) from exc
    return validated_graph(result)


def stable_hash(value: Any) -> str:
    result = canonical_hash(value)
    return str(result)
