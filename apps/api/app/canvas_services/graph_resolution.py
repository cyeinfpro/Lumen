"""Resolve executable Canvas node inputs from the authoritative graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas import CanvasPromptTooLongError, resolve_canvas_text_node
from lumen_core.canvas_models import CanvasNodeExecution, CanvasNodeSelection
from lumen_core.canvas_schemas import (
    EXECUTABLE_NODE_TYPES,
    NODE_INPUT_PORTS,
    NODE_OUTPUT_PORTS,
)
from lumen_core.constants import MAX_PROMPT_CHARS
from lumen_core.models import Image, User, Video

from .errors import canvas_http


@dataclass(frozen=True)
class ResolvedNode:
    node: dict[str, Any]
    prompt: str
    images_by_handle: dict[str, list[dict[str, Any]]]
    videos_by_handle: dict[str, list[dict[str, Any]]]
    snapshot: dict[str, Any]


def find_node(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in graph.get("nodes") or []:
        if isinstance(node, dict) and node.get("id") == node_id:
            return node
    raise canvas_http("not_found", "canvas node not found", 404)


async def _owned_images(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: set[str],
) -> dict[str, Image]:
    if not image_ids:
        return {}
    rows = (
        await db.execute(
            select(Image).where(
                Image.id.in_(image_ids),
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalars()
    images = {row.id: row for row in rows}
    missing = image_ids - images.keys()
    if missing:
        raise canvas_http(
            "canvas_input_not_found",
            "input image is unavailable",
            422,
            image_id=sorted(missing)[0],
        )
    return images


async def _owned_image(
    db: AsyncSession,
    *,
    user_id: str,
    image_id: str,
) -> Image:
    return (await _owned_images(db, user_id=user_id, image_ids={image_id}))[image_id]


async def _owned_videos(
    db: AsyncSession,
    *,
    user_id: str,
    video_ids: set[str],
) -> dict[str, Video]:
    if not video_ids:
        return {}
    rows = (
        await db.execute(
            select(Video).where(
                Video.id.in_(video_ids),
                Video.user_id == user_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalars()
    videos = {row.id: row for row in rows}
    missing = video_ids - videos.keys()
    if missing:
        raise canvas_http(
            "canvas_input_not_found",
            "input video is unavailable",
            422,
            video_id=sorted(missing)[0],
        )
    return videos


async def _owned_video(
    db: AsyncSession,
    *,
    user_id: str,
    video_id: str,
) -> Video:
    return (await _owned_videos(db, user_id=user_id, video_ids={video_id}))[video_id]


async def _execution_output(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    source_node_id: str,
    edge: dict[str, Any],
) -> tuple[CanvasNodeExecution, int, dict[str, Any]]:
    execution_id = edge.get("pinned_execution_id")
    output_index = edge.get("pinned_output_index")
    if edge.get("binding_mode") != "pinned":
        selection = (
            await db.execute(
                select(CanvasNodeSelection).where(
                    CanvasNodeSelection.canvas_id == canvas_id,
                    CanvasNodeSelection.node_id == source_node_id,
                )
            )
        ).scalar_one_or_none()
        if selection is None or selection.execution_id is None:
            raise canvas_http(
                "canvas_input_unresolved",
                "upstream node has no active output",
                422,
                node_id=source_node_id,
            )
        execution_id = selection.execution_id
        output_index = selection.output_index
    if not isinstance(execution_id, str):
        raise canvas_http(
            "canvas_input_unresolved",
            "pinned upstream output is missing",
            422,
            node_id=source_node_id,
        )
    execution = (
        await db.execute(
            select(CanvasNodeExecution).where(
                CanvasNodeExecution.id == execution_id,
                CanvasNodeExecution.canvas_id == canvas_id,
                CanvasNodeExecution.user_id == user_id,
                CanvasNodeExecution.node_id == source_node_id,
                CanvasNodeExecution.status.in_(
                    ("succeeded", "partial_failed", "reused")
                ),
            )
        )
    ).scalar_one_or_none()
    if execution is None:
        raise canvas_http(
            "canvas_input_unresolved",
            "upstream execution is unavailable",
            422,
            node_id=source_node_id,
        )
    index = int(output_index or 0)
    outputs = execution.outputs_jsonb or []
    if index < 0 or index >= len(outputs) or not isinstance(outputs[index], dict):
        raise canvas_http(
            "canvas_input_unresolved",
            "upstream output index is unavailable",
            422,
            node_id=source_node_id,
            output_index=index,
        )
    return execution, index, outputs[index]


def _validate_execution_output_contract(
    *,
    edge: dict[str, Any],
    output: dict[str, Any],
    target_node_type: str,
) -> None:
    target_handle = str(edge.get("target_handle") or "")
    edge_data_type = str(edge.get("data_type") or "")
    target_port = NODE_INPUT_PORTS.get(target_node_type, {}).get(target_handle)
    expected_output_type = (
        "image"
        if edge_data_type in {"image", "mask"}
        else "video"
        if edge_data_type == "video"
        else edge_data_type
    )
    if (
        target_port is None
        or target_port.data_type != edge_data_type
        or output.get("type") != expected_output_type
    ):
        raise canvas_http(
            "canvas_input_type_mismatch",
            "upstream output type does not match the target input",
            422,
            edge_id=edge.get("id"),
            output_type=output.get("type"),
            edge_data_type=edge_data_type,
            target_handle=target_handle,
            target_data_type=(
                target_port.data_type if target_port is not None else None
            ),
        )


def _validate_edge_contract(
    *,
    edge: dict[str, Any],
    source_type: str,
    target_node_type: str,
) -> None:
    source_handle = str(edge.get("source_handle") or "")
    target_handle = str(edge.get("target_handle") or "")
    edge_data_type = str(edge.get("data_type") or "")
    source_port = NODE_OUTPUT_PORTS.get(source_type, {}).get(source_handle)
    target_port = NODE_INPUT_PORTS.get(target_node_type, {}).get(target_handle)
    source_matches = source_port is not None and source_port.data_type == edge_data_type
    if edge_data_type == "mask":
        source_matches = source_port is not None and source_port.data_type in {
            "image",
            "mask",
        }
    if (
        not source_matches
        or target_port is None
        or target_port.data_type != edge_data_type
    ):
        raise canvas_http(
            "canvas_input_type_mismatch",
            "upstream output type does not match the target input",
            422,
            edge_id=edge.get("id"),
            source_type=source_type,
            source_handle=source_handle,
            edge_data_type=edge_data_type,
            target_handle=target_handle,
            target_data_type=(
                target_port.data_type if target_port is not None else None
            ),
        )


def _validate_resolved_inputs(
    *,
    node_type: str,
    config: dict[str, Any],
    prompt_values: list[str],
    images_by_handle: dict[str, list[dict[str, Any]]],
    videos_by_handle: dict[str, list[dict[str, Any]]],
) -> None:
    if node_type not in EXECUTABLE_NODE_TYPES:
        return
    if len(prompt_values) != 1 or not prompt_values[0].strip():
        raise canvas_http(
            "canvas_prompt_unresolved",
            "executable node requires exactly one non-empty prompt",
            422,
        )
    if len(prompt_values[0]) > MAX_PROMPT_CHARS:
        raise canvas_http(
            "canvas_prompt_too_long",
            "resolved Canvas prompt exceeds the supported length",
            422,
            maximum=MAX_PROMPT_CHARS,
            actual=len(prompt_values[0]),
        )
    _validate_resolved_port_cardinality(
        node_type=node_type,
        prompt_values=prompt_values,
        images_by_handle=images_by_handle,
        videos_by_handle=videos_by_handle,
    )
    if node_type == "video_generate":
        _validate_legacy_video_inputs(
            config=config,
            images_by_handle=images_by_handle,
            videos_by_handle=videos_by_handle,
        )
    elif node_type == "video_reference_generate":
        _require_reference_video_media(images_by_handle, videos_by_handle)


def _validate_resolved_port_cardinality(
    *,
    node_type: str,
    prompt_values: list[str],
    images_by_handle: dict[str, list[dict[str, Any]]],
    videos_by_handle: dict[str, list[dict[str, Any]]],
) -> None:
    for handle, spec in NODE_INPUT_PORTS[node_type].items():
        count = _resolved_input_count(
            handle=handle,
            data_type=spec.data_type,
            prompt_values=prompt_values,
            images_by_handle=images_by_handle,
            videos_by_handle=videos_by_handle,
        )
        if spec.required_for_execution and count == 0:
            raise canvas_http(
                "canvas_input_unresolved",
                "required Canvas input is unavailable",
                422,
                node_type=node_type,
                target_handle=handle,
            )
        if spec.maximum is not None and count > spec.maximum:
            raise canvas_http(
                "canvas_input_cardinality_invalid",
                "Canvas input exceeds the target port cardinality",
                422,
                node_type=node_type,
                target_handle=handle,
                maximum=spec.maximum,
                actual=count,
            )


def _resolved_input_count(
    *,
    handle: str,
    data_type: str,
    prompt_values: list[str],
    images_by_handle: dict[str, list[dict[str, Any]]],
    videos_by_handle: dict[str, list[dict[str, Any]]],
) -> int:
    if data_type == "text":
        return len(prompt_values) if handle == "prompt" else 0
    if data_type in {"image", "mask"}:
        return len(images_by_handle.get(handle, []))
    return len(videos_by_handle.get(handle, []))


def _validate_legacy_video_inputs(
    *,
    config: dict[str, Any],
    images_by_handle: dict[str, list[dict[str, Any]]],
    videos_by_handle: dict[str, list[dict[str, Any]]],
) -> None:
    mode = str(config.get("mode") or "t2v")
    first_frames = images_by_handle.get("first_frame", [])
    reference_images = images_by_handle.get("reference_images", [])
    reference_videos = videos_by_handle.get("reference_videos", [])
    reference_count = len(reference_images) + len(reference_videos)
    if len(reference_images) > 9 or len(reference_videos) > 3:
        raise canvas_http(
            "canvas_input_cardinality_invalid",
            "reference video exceeds the supported media limits",
            422,
            maximum_images=9,
            maximum_videos=3,
            actual_images=len(reference_images),
            actual_videos=len(reference_videos),
        )
    if mode == "t2v" and (first_frames or reference_count):
        raise canvas_http(
            "canvas_input_cardinality_invalid",
            "t2v does not accept frame or reference media",
            422,
        )
    if mode == "i2v":
        _validate_i2v_inputs(first_frames, reference_count)
    elif mode == "reference":
        _validate_reference_mode_inputs(first_frames, reference_count)


def _validate_i2v_inputs(
    first_frames: list[dict[str, Any]],
    reference_count: int,
) -> None:
    if len(first_frames) != 1:
        raise canvas_http(
            "canvas_input_unresolved",
            "i2v requires exactly one first frame",
            422,
            target_handle="first_frame",
        )
    if reference_count:
        raise canvas_http(
            "canvas_input_cardinality_invalid",
            "i2v does not accept reference media",
            422,
        )


def _validate_reference_mode_inputs(
    first_frames: list[dict[str, Any]],
    reference_count: int,
) -> None:
    if first_frames:
        raise canvas_http(
            "canvas_input_cardinality_invalid",
            "reference video does not accept a first frame",
            422,
        )
    if reference_count == 0:
        raise canvas_http(
            "canvas_input_unresolved",
            "reference video requires at least one reference media",
            422,
            target_handle="reference_images|reference_videos",
        )


def _require_reference_video_media(
    images_by_handle: dict[str, list[dict[str, Any]]],
    videos_by_handle: dict[str, list[dict[str, Any]]],
) -> None:
    reference_count = len(images_by_handle.get("reference_images", [])) + len(
        videos_by_handle.get("reference_videos", [])
    )
    if reference_count == 0:
        raise canvas_http(
            "canvas_input_unresolved",
            "reference video requires at least one reference media",
            422,
            target_handle="reference_images|reference_videos",
        )


async def _prepare_resolution_inputs(
    db: AsyncSession,
    *,
    user_id: str,
    graph: dict[str, Any],
    node_id: str,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Image],
    dict[str, Video],
]:
    node = find_node(graph, node_id)
    nodes = {
        item["id"]: item
        for item in graph.get("nodes") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    incoming = [
        edge
        for edge in graph.get("edges") or []
        if isinstance(edge, dict) and edge.get("target_node_id") == node_id
    ]
    incoming.sort(
        key=lambda edge: (
            str(edge.get("target_handle") or ""),
            edge.get("order") if isinstance(edge.get("order"), int) else 0,
            str(edge.get("id") or ""),
        )
    )
    graph_edges = [
        edge for edge in graph.get("edges") or [] if isinstance(edge, dict)
    ]
    static_image_ids: set[str] = set()
    static_video_ids: set[str] = set()
    node_type = str(node.get("type") or "")
    for edge in incoming:
        source_id = edge.get("source_node_id")
        source = nodes.get(source_id)
        if source is None:
            raise canvas_http(
                "canvas_input_unresolved",
                "upstream node is missing",
                422,
                node_id=source_id,
            )
        _validate_edge_contract(
            edge=edge,
            source_type=str(source.get("type") or ""),
            target_node_type=node_type,
        )
        config = source.get("config") if isinstance(source.get("config"), dict) else {}
        if source.get("type") in {"image_asset", "mask_asset"}:
            static_image_ids.add(str(config.get("image_id") or ""))
        elif source.get("type") == "video_asset":
            static_video_ids.add(str(config.get("video_id") or ""))
    image_cache = await _owned_images(
        db,
        user_id=user_id,
        image_ids=static_image_ids,
    )
    video_cache = await _owned_videos(
        db,
        user_id=user_id,
        video_ids=static_video_ids,
    )
    return node, nodes, incoming, graph_edges, image_cache, video_cache


def _resolve_text_value(
    *,
    nodes: dict[str, dict[str, Any]],
    graph_edges: list[dict[str, Any]],
    source_id: str,
    source_type: str,
) -> str:
    try:
        resolved_text = resolve_canvas_text_node(nodes, graph_edges, source_id)
    except CanvasPromptTooLongError as exc:
        raise canvas_http(
            "canvas_prompt_too_long",
            "resolved Canvas prompt exceeds the supported length",
            422,
            maximum=MAX_PROMPT_CHARS,
        ) from exc
    if resolved_text is None:
        raise canvas_http(
            "canvas_input_unresolved",
            "upstream text node is unavailable",
            422,
            node_id=source_id,
        )
    return resolved_text.strip() if source_type == "prompt" else resolved_text


async def resolve_node(
    db: AsyncSession,
    *,
    user: User,
    canvas_id: str,
    graph: dict[str, Any],
    node_id: str,
) -> ResolvedNode:
    (
        node,
        nodes,
        incoming,
        graph_edges,
        image_cache,
        video_cache,
    ) = await _prepare_resolution_inputs(
        db,
        user_id=user.id,
        graph=graph,
        node_id=node_id,
    )
    prompt_values: list[str] = []
    images_by_handle: dict[str, list[dict[str, Any]]] = {}
    videos_by_handle: dict[str, list[dict[str, Any]]] = {}
    bindings: list[dict[str, Any]] = []

    for edge in incoming:
        source_id = str(edge.get("source_node_id") or "")
        source = nodes[source_id]
        source_type = str(source.get("type") or "")
        target_handle = str(edge.get("target_handle") or "")
        order = edge.get("order")
        binding: dict[str, Any] = {
            "edge_id": edge.get("id"),
            "source_node_id": source_id,
            "target_handle": target_handle,
            "role": edge.get("role"),
            "order": order if isinstance(order, int) else 0,
            "binding_mode": edge.get("binding_mode") or "follow_active",
        }
        if source_type in {"prompt", "prompt_merge"}:
            text = _resolve_text_value(
                nodes=nodes,
                graph_edges=graph_edges,
                source_id=source_id,
                source_type=source_type,
            )
            if text.strip():
                prompt_values.append(text)
                binding["text"] = text
            bindings.append(binding)
            continue

        output: dict[str, Any]
        if source_type in {"image_asset", "mask_asset"}:
            image_id = str((source.get("config") or {}).get("image_id") or "")
            image = image_cache[image_id]
            output = {
                "type": "image",
                "image_id": image.id,
                "sha256": image.sha256,
                "width": image.width,
                "height": image.height,
            }
        elif source_type == "video_asset":
            video_id = str((source.get("config") or {}).get("video_id") or "")
            video = video_cache[video_id]
            output = {
                "type": "video",
                "video_id": video.id,
                "sha256": video.sha256,
                "width": video.width,
                "height": video.height,
            }
        else:
            execution, output_index, output = await _execution_output(
                db,
                user_id=user.id,
                canvas_id=canvas_id,
                source_node_id=str(source_id),
                edge=edge,
            )
            _validate_execution_output_contract(
                edge=edge,
                output=output,
                target_node_type=str(node.get("type") or ""),
            )
            binding.update(
                {
                    "source_execution_id": execution.id,
                    "output_index": output_index,
                }
            )

        if output.get("type") == "image" and output.get("image_id"):
            image_id = str(output["image_id"])
            image = image_cache.get(image_id)
            if image is None:
                image = await _owned_image(
                    db,
                    user_id=user.id,
                    image_id=image_id,
                )
                image_cache[image_id] = image
            item = {
                "image_id": image.id,
                "sha256": image.sha256,
                "width": image.width,
                "height": image.height,
                "role": edge.get("role") or "reference",
                **{
                    key: binding[key]
                    for key in ("source_execution_id", "output_index")
                    if key in binding
                },
            }
            images_by_handle.setdefault(target_handle, []).append(item)
            binding["asset"] = item
        elif output.get("type") == "video" and output.get("video_id"):
            video_id = str(output["video_id"])
            video = video_cache.get(video_id)
            if video is None:
                video = await _owned_video(
                    db,
                    user_id=user.id,
                    video_id=video_id,
                )
                video_cache[video_id] = video
            item = {
                "video_id": video.id,
                "sha256": video.sha256,
                "width": video.width,
                "height": video.height,
                "role": edge.get("role") or "reference",
                **{
                    key: binding[key]
                    for key in ("source_execution_id", "output_index")
                    if key in binding
                },
            }
            videos_by_handle.setdefault(target_handle, []).append(item)
            binding["asset"] = item
        else:
            raise canvas_http(
                "canvas_input_type_mismatch",
                "upstream output type does not match the target input",
                422,
                edge_id=edge.get("id"),
            )
        bindings.append(binding)

    _validate_resolved_inputs(
        node_type=str(node.get("type") or ""),
        config=dict(node.get("config") or {}),
        prompt_values=prompt_values,
        images_by_handle=images_by_handle,
        videos_by_handle=videos_by_handle,
    )
    return ResolvedNode(
        node=node,
        prompt=prompt_values[0],
        images_by_handle=images_by_handle,
        videos_by_handle=videos_by_handle,
        snapshot={
            "prompt": prompt_values[0],
            "bindings": bindings,
            "images_by_handle": images_by_handle,
            "videos_by_handle": videos_by_handle,
        },
    )
