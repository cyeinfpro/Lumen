"""Batch BYOK image visibility checks backed by bounded SQL predicates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import String, cast, column, func, literal, or_, select, values
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Conversation, Generation, Message


_MESSAGE_IMAGE_REFERENCE_PATH = (
    '$.** ? (@.image_id == $target || @.source_image_id == $target || '
    "@.mask_image_id == $target)"
)


@dataclass(frozen=True)
class ImageVisibilityCandidate:
    image_id: str
    owner_generation_id: str | None
    created_at: datetime


def visible_reference_image_ids_statement(
    candidates: Iterable[ImageVisibilityCandidate],
    *,
    user_id: str,
    visible_after: datetime,
):
    rows = [
        (candidate.image_id, candidate.owner_generation_id)
        for candidate in candidates
    ]
    candidate_values = (
        values(
            column("image_id", String(36)),
            column("owner_generation_id", String(36)),
            name="candidate_images",
        )
        .data(rows)
        .cte("candidate_images")
    )

    visible_generation_reference = (
        select(Generation.id)
        .join(Message, Message.id == Generation.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Generation.user_id == user_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
            Message.deleted_at.is_(None),
            Message.created_at >= visible_after,
            or_(
                Generation.id == candidate_values.c.owner_generation_id,
                Generation.primary_input_image_id == candidate_values.c.image_id,
                Generation.mask_image_id == candidate_values.c.image_id,
                candidate_values.c.image_id
                == Generation.input_image_ids.any_(),
            ),
        )
        .exists()
    )
    visible_message_reference = (
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
            Message.deleted_at.is_(None),
            Message.created_at >= visible_after,
            func.jsonb_path_exists(
                Message.content,
                cast(
                    literal(_MESSAGE_IMAGE_REFERENCE_PATH),
                    postgresql.JSONPATH(),
                ),
                func.jsonb_build_object(
                    "target",
                    candidate_values.c.image_id,
                ),
            ),
        )
        .exists()
    )
    return select(candidate_values.c.image_id).where(
        or_(visible_generation_reference, visible_message_reference)
    )


async def visible_image_ids(
    db: AsyncSession,
    candidates: Iterable[ImageVisibilityCandidate],
    *,
    user_id: str,
    visible_after: datetime,
) -> set[str]:
    candidate_rows = list(candidates)
    recent_ids = {
        candidate.image_id
        for candidate in candidate_rows
        if candidate.created_at >= visible_after
    }
    old_candidates = [
        candidate
        for candidate in candidate_rows
        if candidate.image_id not in recent_ids
    ]
    if not old_candidates:
        return recent_ids
    referenced_ids = (
        (
            await db.execute(
                visible_reference_image_ids_statement(
                    old_candidates,
                    user_id=user_id,
                    visible_after=visible_after,
                )
            )
        )
        .scalars()
        .all()
    )
    return recent_ids | set(referenced_ids)


__all__ = [
    "ImageVisibilityCandidate",
    "visible_image_ids",
    "visible_reference_image_ids_statement",
]
