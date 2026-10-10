"""Validate planned input shape before any independent branch can spend."""

from lumen_core.canvas import CanvasPromptTooLongError, resolve_canvas_text_node
from lumen_core.canvas_schemas import NODE_INPUT_PORTS
from lumen_core.constants import MAX_PROMPT_CHARS
from .errors import canvas_http
from .execution_image_inputs import image_task_inputs
from .graph_resolution import ResolvedNode


def validate_plan_inputs(graph, plan):
    nodes = {node["id"]: node for node in graph["nodes"]}
    incoming = {}
    for edge in graph["edges"]:
        incoming.setdefault(edge["target_node_id"], []).append(edge)
    resolved_text = {}
    for step in plan.steps:
        if step.reuse:
            continue
        node = nodes[step.node_id]
        edges = incoming.get(step.node_id, [])
        counts = {}
        for edge in edges:
            counts[edge["target_handle"]] = counts.get(edge["target_handle"], 0) + 1
        validate_required_ports(node, counts)
        text_edges = [edge for edge in edges if edge["target_handle"] == "prompt"]
        for edge in text_edges:
            source = edge["source_node_id"]
            if source not in resolved_text:
                try:
                    resolved_text[source] = resolve_canvas_text_node(
                        nodes, graph["edges"], source
                    )
                except CanvasPromptTooLongError as exc:
                    raise canvas_http(
                        "canvas_prompt_too_long", "planned prompt is too long", 422
                    ) from exc
            prompt = resolved_text[source]
            if not prompt or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
                raise canvas_http(
                    "canvas_prompt_unresolved", "planned prompt is unavailable", 422
                )
        validate_media_shape(node, counts)


def validate_required_ports(node, counts):
    for handle, port in NODE_INPUT_PORTS[node["type"]].items():
        count = counts.get(handle, 0)
        if (port.required_for_execution and not count) or (
            port.maximum is not None and count > port.maximum
        ):
            raise canvas_http(
                "canvas_input_cardinality_invalid",
                "planned node inputs are incomplete",
                422,
                node_id=node["id"],
                target_handle=handle,
            )


def validate_media_shape(node, counts):
    kind, config = node["type"], node["config"]
    if not kind.startswith("video"):
        # Count-only placeholders never cross an authorization/submission boundary.
        images = {
            handle: [{"image_id": f"shape-{index}"} for index in range(count)]
            for handle, count in counts.items()
            if handle != "prompt"
        }
        image_task_inputs(
            node_type=kind, resolved=ResolvedNode(node, "", images, {}, {})
        )
        return
    mode = config.get("mode") or config.get("action") or "t2v"
    first = counts.get("first_frame", 0)
    references = counts.get("reference_images", 0) + counts.get("reference_videos", 0)
    valid = (
        mode == "t2v"
        and not first
        and not references
        or mode == "i2v"
        and first == 1
        and not references
        or mode == "reference"
        and not first
        and references > 0
    )
    if not valid:
        raise canvas_http(
            "canvas_input_cardinality_invalid",
            "planned video inputs do not match the selected mode",
            422,
            node_id=node["id"],
        )
