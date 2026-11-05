"""Shared owner visibility policy, independent of HTTP route aggregation."""

from __future__ import annotations
from typing import Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.byok_retention import (
    applies_to_user as byok_retention_applies_to_user,
    cutoffs as byok_retention_cutoffs,
    is_user_visible as byok_retention_is_user_visible,
)
from lumen_core.model_entities import Image
from ...byok_service import read_byok_settings_cached, retention_policy_from_settings
from ..domain.artifact import ArtifactStatus
from .http_route_parts.upload import http_error
from .visibility_batch import ImageVisibilityCandidate, visible_image_ids


async def image_referenced_by_visible_user_history(
    db: AsyncSession,
    img: Image,
    user: Any,
    policy: Any,
) -> bool:
    visible_after = byok_retention_cutoffs(policy=policy).visible_after
    candidate = ImageVisibilityCandidate(
        image_id=img.id,
        owner_generation_id=getattr(img, "owner_generation_id", None),
        created_at=img.created_at,
    )
    return img.id in await visible_image_ids(
        db,
        [candidate],
        user_id=user.id,
        visible_after=visible_after,
    )


async def ensure_image_visible_to_user(
    db: AsyncSession,
    img: Image,
    user: Any,
) -> None:
    if not byok_retention_applies_to_user(user):
        return
    created_at = getattr(img, "created_at", None)
    if created_at is None:
        return
    policy = retention_policy_from_settings(await read_byok_settings_cached(db))
    if not byok_retention_is_user_visible(
        account_mode=getattr(user, "account_mode", None),
        created_at=created_at,
        policy=policy,
    ):
        if await image_referenced_by_visible_user_history(db, img, user, policy):
            return
        raise http_error("not_found", "image not found", 404)


async def get_owned_image(db: AsyncSession, user: Any, image_id: str) -> Image:
    image = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
                Image.artifact_status == ArtifactStatus.READY.value,
            )
        )
    ).scalar_one_or_none()
    if image is None:
        raise http_error("not_found", "image not found", 404)
    await ensure_image_visible_to_user(db, image, user)
    return image
