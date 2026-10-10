"""Request-local graph indexes for semantic input comparisons."""

from dataclasses import dataclass, field
from typing import Any
from .canvas_schemas import CanvasGraph


@dataclass
class CanvasSnapshotIndex:
    graph: CanvasGraph
    nodes: dict[str, Any]
    incoming: dict[str, list[Any]]
    text: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def build(cls, graph):
        parsed = (
            graph
            if isinstance(graph, CanvasGraph)
            else CanvasGraph.model_validate(graph)
        )
        incoming = {}
        for edge in parsed.edges:
            incoming.setdefault(edge.target_node_id, []).append(edge)
        for edges in incoming.values():
            edges.sort(
                key=lambda edge: (edge.target_handle, int(edge.order or 0), edge.id)
            )
        return cls(parsed, {node.id: node for node in parsed.nodes}, incoming)
