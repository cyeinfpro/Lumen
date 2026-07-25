from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..adapters.sqlalchemy_repository import SQLAlchemyImageRepository
from ..domain.artifact import (
    ArtifactIdentity,
    ArtifactManifestItem,
    ArtifactStatus,
)
from ..ports.artifact_store import ArtifactStorePort
from ..processing.service import ImageProcessor


@dataclass
class ReconcileStats:
    scanned: int = 0
    marked_ready: int = 0
    marked_failed: int = 0
    rebuilt_reference: int = 0
    deleted_staged: int = 0
    deferred: int = 0


def _manifest_items(
    manifest: Any,
) -> tuple[ArtifactManifestItem | None, ArtifactManifestItem | None]:
    if not isinstance(manifest, dict):
        return None, None
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return None, None
    return (
        ArtifactManifestItem.from_json(artifacts.get("original")),
        ArtifactManifestItem.from_json(artifacts.get("normalized_ref")),
    )


def _replace_manifest_item(
    manifest: dict[str, Any],
    name: str,
    item: ArtifactManifestItem,
) -> dict[str, Any]:
    updated = dict(manifest)
    artifacts = dict(updated.get("artifacts") or {})
    artifacts[name] = item.to_json()
    updated["artifacts"] = artifacts
    return updated


class ImageArtifactReconciler:
    def __init__(
        self,
        *,
        repository: SQLAlchemyImageRepository,
        artifacts: ArtifactStorePort,
        processor: ImageProcessor | None = None,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.processor = processor or ImageProcessor()

    async def run_once(
        self,
        *,
        now: datetime | None = None,
        stale_after: timedelta = timedelta(minutes=5),
        limit: int = 100,
    ) -> ReconcileStats:
        current = now or datetime.now(timezone.utc)
        rows = await self.repository.list_reconcile_candidates(
            due_before=current,
            stale_before=current - stale_after,
            limit=limit,
        )
        stats = ReconcileStats(scanned=len(rows))
        for row in rows:
            try:
                await self._reconcile_row(row, stats, current)
            except Exception:
                stats.deferred += 1
        active_tickets = await self.repository.active_upload_tickets()
        stale_timestamp = (current - stale_after).timestamp()
        for staged in await self.artifacts.list_staged():
            if staged.ticket.value in active_tickets:
                continue
            if staged.modified_at is None or staged.modified_at > stale_timestamp:
                continue
            try:
                if await self.artifacts.delete_staged(staged):
                    stats.deleted_staged += 1
            except Exception:
                stats.deferred += 1
        return stats

    async def _reconcile_row(
        self,
        row: Any,
        stats: ReconcileStats,
        now: datetime,
    ) -> None:
        status = ArtifactStatus(row.artifact_status)
        if status in {ArtifactStatus.STAGING, ArtifactStatus.PROCESSING}:
            await self.repository.transition(
                row.id,
                expected=[status],
                target=ArtifactStatus.FAILED,
                values={
                    "last_artifact_error": "stale upload phase cannot be resumed",
                    "reconcile_after": None,
                },
            )
            stats.marked_failed += 1
            return
        original, normalized_ref = _manifest_items(row.artifact_manifest_jsonb)
        if original is None or normalized_ref is None:
            if status == ArtifactStatus.PUBLISHING:
                await self.repository.transition(
                    row.id,
                    expected=[ArtifactStatus.PUBLISHING],
                    target=ArtifactStatus.FAILED,
                    values={
                        "last_artifact_error": "invalid artifact manifest",
                        "reconcile_after": None,
                    },
                )
                stats.marked_failed += 1
            else:
                stats.deferred += 1
            return
        original_actual = await self.artifacts.identity(original.key)
        if original_actual is None or not original.identity.matches(original_actual):
            await self.repository.transition(
                row.id,
                expected=[status],
                target=ArtifactStatus.FAILED,
                values={
                    "last_artifact_error": "original artifact missing or changed",
                    "reconcile_after": None,
                },
            )
            stats.marked_failed += 1
            return
        ref_actual = await self.artifacts.identity(normalized_ref.key)
        manifest = dict(row.artifact_manifest_jsonb or {})
        if ref_actual is None:
            rebuilt = await self._rebuild_reference(
                original,
                normalized_ref,
            )
            manifest = _replace_manifest_item(
                manifest,
                "normalized_ref",
                ArtifactManifestItem(
                    key=normalized_ref.key,
                    identity=rebuilt,
                    mime=normalized_ref.mime,
                ),
            )
            stats.rebuilt_reference += 1
        elif not normalized_ref.identity.matches(ref_actual):
            await self.repository.transition(
                row.id,
                expected=[status],
                target=ArtifactStatus.FAILED,
                values={
                    "last_artifact_error": "reference artifact changed",
                    "reconcile_after": None,
                },
            )
            stats.marked_failed += 1
            return
        if status == ArtifactStatus.PUBLISHING:
            await self.repository.transition(
                row.id,
                expected=[ArtifactStatus.PUBLISHING],
                target=ArtifactStatus.READY,
                values={
                    "artifact_manifest_jsonb": manifest,
                    "last_artifact_error": None,
                    "reconcile_after": None,
                    "ready_at": now,
                },
            )
            stats.marked_ready += 1
        elif status == ArtifactStatus.READY and manifest != row.artifact_manifest_jsonb:
            await self.repository.update_ready(
                row.id,
                values={
                    "artifact_manifest_jsonb": manifest,
                    "last_artifact_error": None,
                    "reconcile_after": None,
                },
            )

    async def _rebuild_reference(
        self,
        original: ArtifactManifestItem,
        normalized_ref: ArtifactManifestItem,
    ) -> ArtifactIdentity:
        source_path = self.artifacts.processing_path(original.key)
        temp_dir = source_path.parent
        fd, name = await asyncio.to_thread(
            tempfile.mkstemp,
            ".ref.webp",
            f".{source_path.name}.reconcile-",
            str(temp_dir),
        )
        os.close(fd)
        output_path = Path(name)
        try:
            expected = await asyncio.to_thread(
                self.processor.rebuild_reference,
                source_path,
                output_path,
            )
            published = await self.artifacts.publish_path(
                output_path,
                normalized_ref.key,
                expected=expected,
            )
            return published.identity
        finally:
            await asyncio.to_thread(output_path.unlink, missing_ok=True)
