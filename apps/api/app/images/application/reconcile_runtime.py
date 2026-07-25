"""Background lifecycle for image artifact recovery."""

from __future__ import annotations

import asyncio
import logging

from ...config import settings
from ...db import SessionLocal
from ..adapters.filesystem_store import FileSystemArtifactStore
from ..adapters.sqlalchemy_repository import SQLAlchemyImageRepository
from .reconcile_policy import ImageArtifactReconciler


logger = logging.getLogger(__name__)


def build_image_artifact_reconciler() -> ImageArtifactReconciler:
    return ImageArtifactReconciler(
        repository=SQLAlchemyImageRepository(SessionLocal),
        artifacts=FileSystemArtifactStore(settings.storage_root),
    )


async def run_image_artifact_reconciler_once() -> int:
    stats = await build_image_artifact_reconciler().run_once()
    repaired = stats.marked_ready + stats.marked_failed + stats.rebuilt_reference
    if repaired or stats.deleted_staged or stats.deferred:
        logger.info(
            "image artifact reconciliation scanned=%d repaired=%d "
            "deleted_staged=%d deferred=%d",
            stats.scanned,
            repaired,
            stats.deleted_staged,
            stats.deferred,
        )
    return repaired


async def image_artifact_reconciler_loop(
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = 60.0,
) -> None:
    """Keep artifact rows/files convergent after process or commit failures."""
    while not stop_event.is_set():
        try:
            await run_image_artifact_reconciler_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("image artifact reconciliation iteration failed")
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=interval_seconds,
            )
        except TimeoutError:
            pass
