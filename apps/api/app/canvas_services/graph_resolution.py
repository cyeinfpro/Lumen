"""Resolve executable Canvas node inputs from the authoritative graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import CanvasNodeExecution, CanvasNodeSelection
from lumen_core.canvas_schemas import NODE_INPUT_PORTS
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


async def _owned_image(
    db: AsyncSession,
    *,
    user_id: str,
    image_id: str,
) -> Image:
    row = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise canvas_http(
            "canvas_input_not_found",
            "input image is unavailable",
            422,
            image_id=image_id,
        )
    return row


async def _owned_video(
    db: AsyncSession,
    *,
    user_id: str,
    video_id: str,
) -> Video:
    row = (
        await db.execute(
            select(Video).where(
                Video.id == video_id,
                Video.user_id == user_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise canvas_http(
            "canvas_input_not_found",
            "input video is unavailable",
            422,
            video_id=video_id,
        )
    return row


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


async def resolve_node(
    db: AsyncSession,
    *,
    user: User,
    canvas_id: str,
    graph: dict[str, Any],
    node_id: str,
) -> ResolvedNode:
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
            int(edge.get("order") or 0),
            str(edge.get("id") or ""),
        )
    )
    prompt_values: list[str] = []
    images_by_handle: dict[str, list[dict[str, Any]]] = {}
    videos_by_handle: dict[str, list[dict[str, Any]]] = {}
    bindings: list[dict[str, Any]] = []

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
        source_type = source.get("type")
        target_handle = str(edge.get("target_handle") or "")
        binding: dict[str, Any] = {
            "edge_id": edge.get("id"),
            "source_node_id": source_id,
            "target_handle": target_handle,
            "role": edge.get("role"),
            "order": int(edge.get("order") or 0),
            "binding_mode": edge.get("binding_mode") or "follow_active",
        }
        if source_type == "prompt":
            text = str((source.get("config") or {}).get("text") or "").strip()
            if text:
                prompt_values.append(text)
                binding["text"] = text
            bindings.append(binding)
            continue

        output: dict[str, Any]
        if source_type == "image_asset":
            image_id = str((source.get("config") or {}).get("image_id") or "")
            image = await _owned_image(db, user_id=user.id, image_id=image_id)
            output = {
                "type": "image",
                "image_id": image.id,
                "sha256": image.sha256,
                "width": image.width,
                "height": image.height,
            }
        elif source_type == "video_asset":
            video_id = str((source.get("config") or {}).get("video_id") or "")
            video = await _owned_video(db, user_id=user.id, video_id=video_id)
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
            image = await _owned_image(
                db,
                user_id=user.id,
                image_id=str(output["image_id"]),
            )
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
            video = await _owned_video(
                db,
                user_id=user.id,
                video_id=str(output["video_id"]),
            )
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

    if len(prompt_values) != 1:
        raise canvas_http(
            "canvas_prompt_unresolved",
            "executable node requires exactly one non-empty prompt",
            422,
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
