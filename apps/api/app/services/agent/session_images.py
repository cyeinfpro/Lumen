"""Durable Agent session image-slot accounting."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import GenerationStatus
from lumen_core.model_entities import AgentRun, Generation, Image


async def session_image_slot_count(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: str,
    snapshotted_image_ids: set[str],
    image_visibility_filter: Any | None = None,
) -> int:
    visible_snapshot_ids: set[str] = set()
    if snapshotted_image_ids:
        snapshot_statement = select(Image.id).where(
            Image.id.in_(snapshotted_image_ids),
            Image.user_id == user_id,
            Image.deleted_at.is_(None),
            Image.artifact_status == "ready",
        )
        if image_visibility_filter is not None:
            snapshot_statement = snapshot_statement.where(image_visibility_filter)
        visible_snapshot_ids = set(
            (await db.execute(snapshot_statement)).scalars()
        )
    session_assistant_ids = select(AgentRun.assistant_message_id).where(
        AgentRun.agent_session_id == session_id,
        AgentRun.user_id == user_id,
    )
    active_generation_ids = set(
        (
            await db.execute(
                select(Generation.id).where(
                    Generation.user_id == user_id,
                    Generation.message_id.in_(session_assistant_ids),
                    Generation.status.in_(
                        (
                            GenerationStatus.QUEUED.value,
                            GenerationStatus.RUNNING.value,
                        )
                    ),
                )
            )
        ).scalars()
    )
    generated_statement = (
        select(Image.id, Image.owner_generation_id)
        .join(Generation, Generation.id == Image.owner_generation_id)
        .where(
            Generation.user_id == user_id,
            Generation.message_id.in_(session_assistant_ids),
            Generation.status.in_(
                (
                    GenerationStatus.QUEUED.value,
                    GenerationStatus.RUNNING.value,
                    GenerationStatus.SUCCEEDED.value,
                )
            ),
            Image.user_id == user_id,
            Image.deleted_at.is_(None),
            Image.artifact_status == "ready",
        )
    )
    if image_visibility_filter is not None:
        generated_statement = generated_statement.where(image_visibility_filter)
    generated_images = list((await db.execute(generated_statement)).all())
    ready_generation_ids = {
        owner_generation_id
        for _image_id, owner_generation_id in generated_images
        if owner_generation_id is not None
    }
    ready_not_snapshotted = {
        image_id
        for image_id, _owner_generation_id in generated_images
        if image_id not in visible_snapshot_ids
    }
    pending_generation_ids = active_generation_ids - ready_generation_ids
    return (
        len(visible_snapshot_ids)
        + len(ready_not_snapshotted)
        + len(pending_generation_ids)
    )


__all__ = ["session_image_slot_count"]
