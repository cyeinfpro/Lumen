"""User-first row locks for account-deletion-sensitive generation work."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from lumen_core.model_entities import User


async def lock_active_generation_user(
    session: Any,
    *,
    user_id: str,
) -> bool:
    """Lock the account before task rows, matching account-deletion order."""
    user = (
        await session.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    return user is not None and getattr(user, "deleted_at", None) is None


__all__ = ["lock_active_generation_user"]
