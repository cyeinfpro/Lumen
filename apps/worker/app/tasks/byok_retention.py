"""Scheduled BYOK retention cleanup."""

from __future__ import annotations

import logging

from lumen_core.byok_retention import (
    BYOK_DEFAULT_DELETE_ENABLED,
    ByokRetentionPolicy,
    prune_expired_byok_user_data,
)

from .. import runtime_settings
from ..db import SessionLocal

logger = logging.getLogger(__name__)


async def _policy_from_runtime_settings() -> ByokRetentionPolicy:
    return ByokRetentionPolicy(
        hide_enabled=bool(
            await runtime_settings.resolve_int("byok.retention_hide_enabled", 1)
        ),
        delete_enabled=bool(
            await runtime_settings.resolve_int(
                "byok.retention_delete_enabled",
                int(BYOK_DEFAULT_DELETE_ENABLED),
            )
        ),
        hide_days=await runtime_settings.resolve_int("byok.retention_hide_days", 3),
        delete_days=await runtime_settings.resolve_int(
            "byok.retention_delete_days",
            7,
        ),
    ).normalized()


async def cleanup_byok_retention(ctx: dict | None = None) -> dict[str, int | bool]:  # type: ignore[type-arg]
    policy = await _policy_from_runtime_settings()
    if not policy.delete_enabled:
        logger.info("byok retention cleanup skipped: delete disabled")
        return {
            "skipped": True,
            "messages_deleted": 0,
            "images_deleted": 0,
            "conversations_deleted": 0,
        }

    async with SessionLocal() as session:
        counts = await prune_expired_byok_user_data(session, policy=policy)
        await session.commit()
    logger.info("byok retention cleanup completed counts=%s", counts)
    return {"skipped": False, **counts}


__all__ = ["cleanup_byok_retention"]
