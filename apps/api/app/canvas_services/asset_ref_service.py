"""Maintain Canvas head asset references."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import (
    CanvasAssetRef,
    CanvasExecutionTask,
    CanvasNodeExecution,
    CanvasVersion,
)
from lumen_core.models import Image, Video

from .errors import canvas_http


def _asset_nodes(
    graph: dict[str, Any],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    images: list[tuple[str, str]] = []
    videos: list[tuple[str, str]] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue
        image_id = config.get("image_id")
        video_id = config.get("video_id")
        if (
            node.get("type") == "image_asset"
            and isinstance(image_id, str)
            and image_id.strip()
        ):
            images.append((node_id, image_id))
        if (
            node.get("type") == "video_asset"
            and isinstance(video_id, str)
            and video_id.strip()
        ):
            videos.append((node_id, video_id))
    return images, videos


async def sync_head_asset_refs(
    db: AsyncSession,
    *,
    canvas_id: str,
    user_id: str,
    graph: dict[str, Any],
) -> None:
    image_nodes, video_nodes = _asset_nodes(graph)
    image_ids = {asset_id for _, asset_id in image_nodes}
    video_ids = {asset_id for _, asset_id in video_nodes}
    owned_images = set()
    owned_videos = set()
    if image_ids:
        owned_images = set(
            (
                await db.execute(
                    select(Image.id).where(
                        Image.id.in_(image_ids),
                        Image.user_id == user_id,
                        Image.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    if video_ids:
        owned_videos = set(
            (
                await db.execute(
                    select(Video.id).where(
                        Video.id.in_(video_ids),
                        Video.user_id == user_id,
                        Video.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    if owned_images != image_ids or owned_videos != video_ids:
        raise canvas_http(
            "canvas_asset_not_found",
            "one or more Canvas assets are unavailable",
            422,
        )

    await db.execute(
        delete(CanvasAssetRef).where(
            CanvasAssetRef.canvas_id == canvas_id,
            CanvasAssetRef.scope == "head",
        )
    )
    for node_id, image_id in image_nodes:
        db.add(
            CanvasAssetRef(
                canvas_id=canvas_id,
                node_id=node_id,
                scope="head",
                retention_class="current",
                image_id=image_id,
            )
        )
    for node_id, video_id in video_nodes:
        db.add(
            CanvasAssetRef(
                canvas_id=canvas_id,
                node_id=node_id,
                scope="head",
                retention_class="current",
                video_id=video_id,
            )
        )


async def delete_head_asset_refs(
    db: AsyncSession,
    *,
    canvas_id: str,
) -> None:
    await db.execute(
        delete(CanvasAssetRef).where(
            CanvasAssetRef.canvas_id == canvas_id,
            CanvasAssetRef.scope == "head",
        )
    )


async def ensure_asset_not_canvas_referenced(
    db: AsyncSession,
    *,
    image_id: str | None = None,
    video_id: str | None = None,
) -> None:
    if image_id is not None:
        condition = CanvasAssetRef.image_id == image_id
        owner_query = select(Image.owner_generation_id).where(Image.id == image_id)
        task_owner_column = CanvasExecutionTask.generation_id
    else:
        condition = CanvasAssetRef.video_id == video_id
        owner_query = select(Video.owner_generation_id).where(Video.id == video_id)
        task_owner_column = CanvasExecutionTask.video_generation_id
    reference = (
        await db.execute(select(CanvasAssetRef.id).where(condition).limit(1))
    ).scalar_one_or_none()
    if reference is not None:
        raise canvas_http(
            "canvas_asset_referenced",
            "asset is retained by a Canvas document or execution",
            409,
        )
    owner_generation_id = (await db.execute(owner_query)).scalar_one_or_none()
    if owner_generation_id is None:
        return
    task_reference = (
        await db.execute(
            select(CanvasExecutionTask.id)
            .where(task_owner_column == owner_generation_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if task_reference is not None:
        raise canvas_http(
            "canvas_asset_referenced",
            "asset is retained by a Canvas document or execution",
            409,
        )


async def materialize_version_asset_refs(
    db: AsyncSession,
    *,
    version: CanvasVersion,
    user_id: str,
    graph: dict[str, Any],
    selection_snapshot: dict[str, Any],
) -> None:
    image_nodes, video_nodes = _asset_nodes(graph)
    image_ids = {asset_id for _, asset_id in image_nodes}
    video_ids = {asset_id for _, asset_id in video_nodes}
    items = selection_snapshot.get("selections")
    execution_ids = (
        {
            item["execution_id"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("execution_id"), str)
        }
        if isinstance(items, list)
        else set()
    )
    if execution_ids:
        executions = (
            (
                await db.execute(
                    select(CanvasNodeExecution).where(
                        CanvasNodeExecution.id.in_(execution_ids),
                        CanvasNodeExecution.canvas_id == version.canvas_id,
                        CanvasNodeExecution.user_id == user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for execution in executions:
            for output in execution.outputs_jsonb or []:
                if not isinstance(output, dict):
                    continue
                if isinstance(output.get("image_id"), str):
                    image_ids.add(output["image_id"])
                if isinstance(output.get("video_id"), str):
                    video_ids.add(output["video_id"])
    for image_id in sorted(image_ids):
        db.add(
            CanvasAssetRef(
                canvas_id=version.canvas_id,
                version_id=version.id,
                scope="version",
                retention_class="checkpoint",
                image_id=image_id,
            )
        )
    for video_id in sorted(video_ids):
        db.add(
            CanvasAssetRef(
                canvas_id=version.canvas_id,
                version_id=version.id,
                scope="version",
                retention_class="checkpoint",
                video_id=video_id,
            )
        )
