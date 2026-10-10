"""Read-only local media shape and known durations for Canvas video preflight."""

from dataclasses import dataclass
from sqlalchemy import select
from lumen_core.canvas_models import CanvasNodeExecution
from lumen_core.model_entities import Video


@dataclass(frozen=True)
class PreviewMedia:
    kind: str
    image_id: str | None = None
    video_id: str | None = None


async def video_preview_inputs(db, *, user, graph, node):
    nodes = {item["id"]: item for item in graph["nodes"]}
    snapshots, markers, first_frame = [], [], None
    for edge in graph["edges"]:
        if edge["target_node_id"] != node["id"] or edge["target_handle"] not in {
            "first_frame",
            "reference_images",
            "reference_videos",
        }:
            continue
        source = nodes[edge["source_node_id"]]
        kind = "video" if edge["target_handle"] == "reference_videos" else "image"
        if edge["target_handle"] == "first_frame":
            first_frame = "planned-local-frame"
            continue
        item = {"kind": kind}
        marker = PreviewMedia(
            kind,
            image_id="planned-local-image" if kind == "image" else None,
            video_id="planned-local-video" if kind == "video" else None,
        )
        if kind == "video":
            duration = await known_video_duration(
                db, user=user, graph=graph, source=source, edge=edge
            )
            if duration is not None:
                item["upstream_reference_duration_ms"] = duration
        snapshots.append(item)
        markers.append(marker)
    # Markers express local-media requirements only; they never reach a provider,
    # asset lookup, durable identity, outbox, or billing writer.
    return snapshots, tuple(markers), first_frame


async def known_video_duration(db, *, user, graph, source, edge):
    video_id = (
        source["config"].get("video_id") if source["type"] == "video_asset" else None
    )
    bound = graph.get("_canvas_bound_outputs", {}).get(edge["id"])
    reused = graph.get("_canvas_reused_outputs", {}).get(source["id"])
    reference = bound or reused
    if reference is not None:
        video_id = reference.asset_id
    elif edge.get("binding_mode") == "pinned":
        row = (
            await db.execute(
                select(CanvasNodeExecution).where(
                    CanvasNodeExecution.id == edge["pinned_execution_id"],
                    CanvasNodeExecution.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        index = edge.get("pinned_output_index", 0)
        outputs = row.outputs_jsonb if row is not None else []
        if 0 <= index < len(outputs):
            video_id = outputs[index].get("video_id")
    if video_id:
        duration = (
            await db.execute(
                select(Video.duration_ms).where(
                    Video.id == video_id,
                    Video.user_id == user.id,
                    Video.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return duration if isinstance(duration, int) and duration > 0 else None
    duration = source["config"].get("duration_s")
    return duration * 1000 if isinstance(duration, int) and duration > 0 else None
