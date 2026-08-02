"""Tracked background cleanup for Volcano media file operations."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from lumen_core.volcano_asset_media import VolcanoAssetInstallReceipt

from ..storage_writes import StorageWriteCoordinator


logger = logging.getLogger(__name__)
CleanupInstall = Callable[[VolcanoAssetInstallReceipt | None], Awaitable[None]]


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


def track_background_task(
    task: asyncio.Task[Any],
    storage_writes: StorageWriteCoordinator | None = None,
) -> None:
    tracker = getattr(storage_writes, "track_background_task", None)
    if callable(tracker):
        tracker(task)
    else:
        task.add_done_callback(_consume_background_task)


async def _discard_path_after_task(
    task: asyncio.Task[Any],
    path: Path,
) -> None:
    try:
        await task
    except BaseException:
        pass
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except OSError:
        logger.warning("Volcano stage cleanup failed path=%s", path, exc_info=True)


def discard_path_in_background(
    task: asyncio.Task[Any],
    path: Path,
    storage_writes: StorageWriteCoordinator | None = None,
) -> None:
    track_background_task(
        asyncio.create_task(
            _discard_path_after_task(task, path),
            name="volcano-discard-stage",
        ),
        storage_writes,
    )


async def _cleanup_abandoned_install(
    task: asyncio.Task[Any],
    receipt: VolcanoAssetInstallReceipt,
    cleanup_install: CleanupInstall,
) -> None:
    try:
        created = bool(await task)
    except BaseException:
        return
    if created:
        await cleanup_install(receipt)


def cleanup_install_in_background(
    task: asyncio.Task[Any],
    receipt: VolcanoAssetInstallReceipt,
    cleanup_install: CleanupInstall,
    storage_writes: StorageWriteCoordinator | None = None,
) -> None:
    track_background_task(
        asyncio.create_task(
            _cleanup_abandoned_install(task, receipt, cleanup_install),
            name="volcano-discard-install",
        ),
        storage_writes,
    )


def cleanup_receipt_in_background(
    receipt: VolcanoAssetInstallReceipt | None,
    cleanup_install: CleanupInstall,
    *,
    task_name: str,
    storage_writes: StorageWriteCoordinator | None = None,
) -> None:
    if receipt is None:
        return
    track_background_task(
        asyncio.create_task(
            cleanup_install(receipt),
            name=task_name,
        ),
        storage_writes,
    )


__all__ = [
    "cleanup_install_in_background",
    "cleanup_receipt_in_background",
    "discard_path_in_background",
    "track_background_task",
]
