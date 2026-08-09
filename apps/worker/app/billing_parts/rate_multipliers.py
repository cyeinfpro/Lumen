"""Worker billing rate-multiplier resolution shared by billing runtimes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.model_entities.accounts import User
from lumen_core.model_entities.tasks import Completion

from .helpers import snapshot_rate_multiplier_x10000


async def dynamic_rate_multiplier_x10000(
    session: AsyncSession,
    user_id: str,
    *,
    strict: bool = False,
) -> int:
    if not isinstance(session, AsyncSession):
        return 10_000
    raw = (
        await session.execute(
            select(User.billing_rate_multiplier).where(User.id == user_id)
        )
    ).scalar_one_or_none()
    return billing_core.parse_rate_multiplier_x10000(raw, strict=strict)


async def completion_rate_multiplier_x10000(
    session: AsyncSession,
    completion: Completion,
) -> int:
    snapshot = snapshot_rate_multiplier_x10000(completion, strict=True)
    if snapshot is not None:
        return snapshot
    return await dynamic_rate_multiplier_x10000(
        session,
        completion.user_id,
        strict=True,
    )


__all__ = (
    "completion_rate_multiplier_x10000",
    "dynamic_rate_multiplier_x10000",
)
