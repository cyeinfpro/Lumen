"""Durable Agent session image-slot accounting."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import GenerationStatus
from lumen_core.agent_events import AGENT_RUN_ACTIVE_STATUSES
from lumen_core.model_entities import (
    AgentRun,
    AgentRunReference,
    AgentSessionImage,
    Generation,
    Image,
    User,
)
from lumen_core.schema_models import (
    AGENT_MAX_SESSION_IMAGES,
    AgentSessionImageListOut,
    AgentSessionImageOut,
    stable_reference_label,
)

from .common import http_error
from .repository import get_owned_agent_session, retention_filter


async def _sync_agent_session_catalog(
    db: AsyncSession,
    *,
    session_id: str,
    user: User,
    rows: list[AgentSessionImage],
) -> list[AgentSessionImage]:
    by_image = {row.image_id: row for row in rows}
    used_labels = {row.reference_label for row in rows}
    visible = await retention_filter(db, user, Image.created_at)
    reference_statement = (
        select(AgentRunReference)
        .join(AgentRun, AgentRun.id == AgentRunReference.agent_run_id)
        .join(Image, Image.id == AgentRunReference.image_id)
        .where(
            AgentRun.agent_session_id == session_id,
            AgentRun.user_id == user.id,
            AgentRunReference.user_id == user.id,
            Image.user_id == user.id,
            Image.deleted_at.is_(None),
            Image.artifact_status == "ready",
        )
        .order_by(
            AgentRun.created_at,
            AgentRun.id,
            AgentRunReference.ordinal,
        )
    )
    if visible is not None:
        reference_statement = reference_statement.where(visible)
    references = list((await db.execute(reference_statement)).scalars().all())
    assistant_ids = select(AgentRun.assistant_message_id).where(
        AgentRun.agent_session_id == session_id,
        AgentRun.user_id == user.id,
    )
    generated_statement = (
        select(Image)
        .join(Generation, Generation.id == Image.owner_generation_id)
        .where(
            Generation.user_id == user.id,
            Generation.message_id.in_(assistant_ids),
            Generation.status == GenerationStatus.SUCCEEDED.value,
            Image.user_id == user.id,
            Image.deleted_at.is_(None),
            Image.artifact_status == "ready",
        )
        .order_by(Generation.created_at, Generation.id, Image.id)
    )
    if visible is not None:
        generated_statement = generated_statement.where(visible)
    generated = list((await db.execute(generated_statement)).scalars().all())

    def available_label(preferred: str | None = None) -> str | None:
        if preferred and preferred not in used_labels:
            return preferred
        for index in range(AGENT_MAX_SESSION_IMAGES):
            candidate = stable_reference_label(index)
            if candidate not in used_labels:
                return candidate
        return None

    candidates = [
        (
            reference.image_id,
            reference.reference_label,
            reference.role,
            reference.display_label,
            "history",
        )
        for reference in references
    ] + [
        (image.id, None, "reference", "Agent result", "generated")
        for image in generated
    ]
    for image_id, preferred, role, display_label, source in candidates:
        if image_id in by_image:
            continue
        label = available_label(preferred)
        if label is None:
            break
        used_labels.add(label)
        row = AgentSessionImage(
            agent_session_id=session_id,
            user_id=user.id,
            image_id=image_id,
            reference_label=label,
            role=role,
            display_label=display_label,
            source=source,
            active=source != "generated",
        )
        db.add(row)
        rows.append(row)
        by_image[image_id] = row
    await db.flush()
    return rows


async def session_image_slot_count(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: str,
    snapshotted_image_ids: set[str],
    image_visibility_filter: Any | None = None,
) -> int:
    catalog_rows = list(
        (
            await db.execute(
                select(AgentSessionImage.image_id, AgentSessionImage.active).where(
                    AgentSessionImage.agent_session_id == session_id,
                    AgentSessionImage.user_id == user_id,
                )
            )
        ).all()
    )
    if catalog_rows:
        snapshotted_image_ids = {
            image_id for image_id, active in catalog_rows if active
        }
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
        visible_snapshot_ids = set((await db.execute(snapshot_statement)).scalars())
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
    if catalog_rows:
        return len(visible_snapshot_ids) + len(active_generation_ids)
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


async def list_agent_session_images(
    db: AsyncSession,
    *,
    session_id: str,
    user: User,
) -> AgentSessionImageListOut:
    await get_owned_agent_session(
        db,
        session_id=session_id,
        user_id=user.id,
        for_update=True,
    )
    rows = list(
        (
            await db.execute(
                select(AgentSessionImage)
                .where(
                    AgentSessionImage.agent_session_id == session_id,
                    AgentSessionImage.user_id == user.id,
                )
                .order_by(AgentSessionImage.reference_label.asc())
            )
        )
        .scalars()
        .all()
    )
    row_count_before_sync = len(rows)
    rows = await _sync_agent_session_catalog(
        db,
        session_id=session_id,
        user=user,
        rows=rows,
    )
    if len(rows) != row_count_before_sync:
        await db.flush()
    await db.commit()
    rows.sort(key=lambda row: int(row.reference_label.removeprefix("ref_")))
    items = [
        AgentSessionImageOut(
            image_id=row.image_id,
            reference_label=row.reference_label,
            role=row.role,
            display_label=row.display_label,
            source=row.source,
            active=row.active,
        )
        for row in rows
    ]
    visible = await retention_filter(db, user, Image.created_at)
    used = await session_image_slot_count(
        db,
        session_id=session_id,
        user_id=user.id,
        snapshotted_image_ids={row.image_id for row in rows if row.active},
        image_visibility_filter=visible,
    )
    return AgentSessionImageListOut(
        items=items,
        used=used,
        maximum=AGENT_MAX_SESSION_IMAGES,
    )


async def eject_agent_session_image(
    db: AsyncSession,
    *,
    session_id: str,
    image_id: str,
    user: User,
) -> AgentSessionImageListOut:
    await get_owned_agent_session(
        db,
        session_id=session_id,
        user_id=user.id,
        for_update=True,
    )
    active_run = (
        await db.execute(
            select(AgentRun.id).where(
                AgentRun.agent_session_id == session_id,
                AgentRun.user_id == user.id,
                AgentRun.status.in_(AGENT_RUN_ACTIVE_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if active_run is not None:
        raise http_error(
            "agent_run_active",
            "session images cannot change during an active Agent run",
            409,
        )
    row = (
        await db.execute(
            select(AgentSessionImage)
            .where(
                AgentSessionImage.agent_session_id == session_id,
                AgentSessionImage.user_id == user.id,
                AgentSessionImage.image_id == image_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise http_error("not_found", "Agent session image not found", 404)
    row.active = False
    await db.commit()
    return await list_agent_session_images(
        db,
        session_id=session_id,
        user=user,
    )


__all__ = [
    "eject_agent_session_image",
    "list_agent_session_images",
    "session_image_slot_count",
]
