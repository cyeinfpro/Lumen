from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from lumen_core.capacity_leases import (
    CapacityLeaseGuard,
    assert_capacity_leases_owned,
    maintained_capacity_lease,
    race_with_capacity_leases,
)
from lumen_core.storage_capacity import (
    StorageCapacityExceeded,
    StorageCapacityPort,
)

from ..adapters.local_capacity import CapacityLimits
from ..adapters.sqlalchemy_repository import (
    ArtifactTransitionConflict,
    SQLAlchemyImageRepository,
)
from ..domain.artifact import (
    ArtifactIdentity,
    ArtifactManifestItem,
    ArtifactStatus,
    PublishedArtifact,
    StagedSweepBudget,
)
from ..metrics import record_staged_sweep_failure
from ..ports.artifact_store import ArtifactStorePort
from ..processing.service import ImageProcessor


logger = logging.getLogger(__name__)


@dataclass
class ReconcileStats:
    scanned: int = 0
    hashed_bytes: int = 0
    marked_ready: int = 0
    marked_failed: int = 0
    quarantined_rows: int = 0
    rebuilt_reference: int = 0
    deleted_staged: int = 0
    quarantined_staged: int = 0
    deferred: int = 0
    budget_exhausted: bool = False
    next_cursor: str | None = None
    sweep_error_code: str | None = None


class ReconcileLeaseLost(RuntimeError):
    pass


class ReconcilePersistenceError(RuntimeError):
    pass


_RECONCILE_MAX_ATTEMPTS = 8
_RECONCILE_BASE_BACKOFF = timedelta(seconds=30)
_RECONCILE_MAX_BACKOFF = timedelta(hours=1)


class ReconcileLeaseGuardPort(Protocol):
    fence: int

    async def assert_owned(self) -> None: ...

    async def wait_lost(self) -> None: ...


async def _assert_lease_owned(
    lease_guard: ReconcileLeaseGuardPort | None,
) -> None:
    if lease_guard is not None:
        await lease_guard.assert_owned()


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


def _reconcile_error_code(error: BaseException) -> str:
    if isinstance(error, StorageCapacityExceeded):
        return "storage_capacity_exhausted"
    name = error.__class__.__name__
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return value[:64] or "reconcile_failed"


def _reconcile_backoff(attempts: int) -> timedelta:
    exponent = max(0, min(attempts - 1, 16))
    seconds = _RECONCILE_BASE_BACKOFF.total_seconds() * (2**exponent)
    return min(timedelta(seconds=seconds), _RECONCILE_MAX_BACKOFF)


def _sweep_error_context(
    artifacts: ArtifactStorePort,
    error: Exception,
) -> tuple[str | None, int | None, str]:
    cursor = getattr(error, "_lumen_sweep_cursor", None)
    slot = getattr(error, "_lumen_sweep_slot", None)
    root = getattr(error, "_lumen_sweep_root", None)
    if not isinstance(cursor, str):
        cursor = None
    if not isinstance(slot, int):
        slot = None
    if not isinstance(root, str):
        configured_root = getattr(artifacts, "root", None)
        root = "unknown" if configured_root is None else str(configured_root)
    return cursor, slot, root


class ImageArtifactReconciler:
    def __init__(
        self,
        *,
        repository: SQLAlchemyImageRepository,
        artifacts: ArtifactStorePort,
        storage_capacity: StorageCapacityPort,
        processor: ImageProcessor | None = None,
        storage_lease_ttl_seconds: float | None = None,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.storage_capacity = storage_capacity
        self.processor = processor or ImageProcessor()
        self.storage_lease_ttl_seconds = (
            CapacityLimits.from_env().lease_ttl_seconds
            if storage_lease_ttl_seconds is None
            else storage_lease_ttl_seconds
        )

    async def run_once(
        self,
        *,
        now: datetime | None = None,
        stale_after: timedelta = timedelta(minutes=5),
        limit: int = 100,
        max_files_per_pass: int = 100,
        max_bytes_hashed_per_pass: int = 512 * 1024 * 1024,
        max_seconds_per_pass: float = 5.0,
        lease_guard: ReconcileLeaseGuardPort | None = None,
    ) -> ReconcileStats:
        await _assert_lease_owned(lease_guard)
        current = now or datetime.now(timezone.utc)
        rows = await self.repository.list_reconcile_candidates(
            due_before=current,
            stale_before=current - stale_after,
            limit=limit,
        )
        stats = ReconcileStats(scanned=len(rows))
        for row in rows:
            reconcile_fence: int | None = None
            claimed = lease_guard is None
            try:
                if lease_guard is not None:
                    await _assert_lease_owned(lease_guard)
                    reconcile_fence = lease_guard.fence
                    claimed = await self.repository.claim_reconcile(
                        row.id,
                        expected_status=ArtifactStatus(row.artifact_status),
                        expected_updated_at=row.updated_at,
                        fence=reconcile_fence,
                    )
                    if not claimed:
                        stats.deferred += 1
                        continue
                await self._reconcile_row(
                    row,
                    stats,
                    current,
                    lease_guard,
                    reconcile_fence,
                )
            except ReconcileLeaseLost:
                raise
            except ReconcilePersistenceError:
                raise
            except ArtifactTransitionConflict:
                stats.deferred += 1
                continue
            except Exception as exc:
                if not claimed:
                    logger.exception(
                        "failed to claim image reconcile row image_id=%s",
                        row.id,
                    )
                    stats.deferred += 1
                    continue
                try:
                    await self._record_reconcile_failure(
                        row,
                        error=exc,
                        now=current,
                        stats=stats,
                        lease_guard=lease_guard,
                        reconcile_fence=reconcile_fence,
                    )
                except ArtifactTransitionConflict:
                    stats.deferred += 1
        sweep_budget = StagedSweepBudget(
            max_files_per_pass=max_files_per_pass,
            max_bytes_hashed_per_pass=max_bytes_hashed_per_pass,
            max_seconds_per_pass=max_seconds_per_pass,
        )
        stale_timestamp = (current - stale_after).timestamp()
        before_delete = (
            None if lease_guard is None else lambda: _assert_lease_owned(lease_guard)
        )
        try:
            sweep = await self.artifacts.sweep_staged(
                active_tickets=None,
                stale_before=stale_timestamp,
                budget=sweep_budget,
                load_active_tickets=self.repository.active_upload_tickets,
                before_delete=before_delete,
            )
        except ReconcileLeaseLost:
            raise
        except Exception as exc:
            error_code = _reconcile_error_code(exc)
            cursor, slot, root = _sweep_error_context(self.artifacts, exc)
            stats.deferred += 1
            stats.sweep_error_code = error_code
            record_staged_sweep_failure(error_code)
            logger.exception(
                "image staged sweep failed",
                extra={
                    "sweep_error_class": exc.__class__.__name__,
                    "sweep_error_code": error_code,
                    "sweep_cursor": cursor,
                    "sweep_slot": slot,
                    "sweep_max_files": sweep_budget.max_files_per_pass,
                    "sweep_max_bytes": sweep_budget.max_bytes_hashed_per_pass,
                    "sweep_max_seconds": sweep_budget.max_seconds_per_pass,
                    "sweep_root": root,
                    "reconcile_instance": f"pid:{os.getpid()}",
                    "reconcile_fence": (
                        None if lease_guard is None else lease_guard.fence
                    ),
                },
            )
        else:
            stats.scanned += sweep.scanned
            stats.hashed_bytes = sweep.hashed_bytes
            stats.deleted_staged += sweep.deleted
            stats.deferred += sweep.deferred
            stats.quarantined_staged += sweep.quarantined
            stats.budget_exhausted = sweep.budget_exhausted
            stats.next_cursor = sweep.next_cursor
        return stats

    async def _reconcile_row(
        self,
        row: Any,
        stats: ReconcileStats,
        now: datetime,
        lease_guard: ReconcileLeaseGuardPort | None,
        reconcile_fence: int | None,
    ) -> None:
        status = ArtifactStatus(row.artifact_status)
        if status in {ArtifactStatus.STAGING, ArtifactStatus.PROCESSING}:
            await _assert_lease_owned(lease_guard)
            await self.repository.transition(
                row.id,
                expected=[status],
                target=ArtifactStatus.FAILED,
                values={
                    "last_artifact_error": "stale upload phase cannot be resumed",
                    "reconcile_after": None,
                    **self._cleared_reconcile_state(),
                },
                reconcile_fence=reconcile_fence,
            )
            stats.marked_failed += 1
            return
        original, normalized_ref = _manifest_items(row.artifact_manifest_jsonb)
        if original is None or normalized_ref is None:
            await _assert_lease_owned(lease_guard)
            await self._quarantine_row(
                row,
                status=status,
                error_code="invalid_artifact_manifest",
                error_message="invalid artifact manifest",
                now=now,
                stats=stats,
                lease_guard=lease_guard,
                reconcile_fence=reconcile_fence,
            )
            return
        original_actual = await self.artifacts.identity(original.key)
        if original_actual is None or not original.identity.matches(original_actual):
            await _assert_lease_owned(lease_guard)
            await self.repository.transition(
                row.id,
                expected=[status],
                target=ArtifactStatus.FAILED,
                values={
                    "last_artifact_error": "original artifact missing or changed",
                    "reconcile_after": None,
                    **self._cleared_reconcile_state(),
                },
                reconcile_fence=reconcile_fence,
            )
            stats.marked_failed += 1
            return
        ref_actual = await self.artifacts.identity(normalized_ref.key)
        manifest = dict(row.artifact_manifest_jsonb or {})
        if ref_actual is None:
            await self._repair_missing_reference(
                original=original,
                normalized_ref=normalized_ref,
                row=row,
                status=status,
                manifest=manifest,
                stats=stats,
                now=now,
                reconcile_lease_guard=lease_guard,
                reconcile_fence=reconcile_fence,
            )
            return
        elif not normalized_ref.identity.matches(ref_actual):
            await _assert_lease_owned(lease_guard)
            await self.repository.transition(
                row.id,
                expected=[status],
                target=ArtifactStatus.FAILED,
                values={
                    "last_artifact_error": "reference artifact changed",
                    "reconcile_after": None,
                    **self._cleared_reconcile_state(),
                },
                reconcile_fence=reconcile_fence,
            )
            stats.marked_failed += 1
            return
        if status == ArtifactStatus.PUBLISHING:
            await _assert_lease_owned(lease_guard)
            await self.repository.transition(
                row.id,
                expected=[ArtifactStatus.PUBLISHING],
                target=ArtifactStatus.READY,
                values={
                    "artifact_manifest_jsonb": manifest,
                    "last_artifact_error": None,
                    "reconcile_after": None,
                    "ready_at": now,
                    **self._cleared_reconcile_state(),
                },
                reconcile_fence=reconcile_fence,
            )
            stats.marked_ready += 1
        elif status == ArtifactStatus.READY and manifest != row.artifact_manifest_jsonb:
            await _assert_lease_owned(lease_guard)
            await self.repository.update_ready(
                row.id,
                values={
                    "artifact_manifest_jsonb": manifest,
                    "last_artifact_error": None,
                    "reconcile_after": None,
                    **self._cleared_reconcile_state(),
                },
                reconcile_fence=reconcile_fence,
            )

    @staticmethod
    def _cleared_reconcile_state() -> dict[str, Any]:
        return {
            "reconcile_attempts": 0,
            "last_reconcile_error_code": None,
            "last_reconcile_error_at": None,
            "quarantined_at": None,
            "reconcile_fence": 0,
        }

    async def _record_reconcile_failure(
        self,
        row: Any,
        *,
        error: BaseException,
        now: datetime,
        stats: ReconcileStats,
        lease_guard: ReconcileLeaseGuardPort | None,
        reconcile_fence: int | None,
    ) -> None:
        status = ArtifactStatus(row.artifact_status)
        attempts = max(0, int(getattr(row, "reconcile_attempts", 0) or 0)) + 1
        quarantined_at = now if attempts >= _RECONCILE_MAX_ATTEMPTS else None
        retry_at = (
            None if quarantined_at is not None else now + _reconcile_backoff(attempts)
        )
        try:
            await _assert_lease_owned(lease_guard)
            await self.repository.record_reconcile_failure(
                row.id,
                expected_status=status,
                attempts=attempts,
                error_code=_reconcile_error_code(error),
                error_message=str(error) or error.__class__.__name__,
                error_at=now,
                reconcile_after=retry_at,
                quarantined_at=quarantined_at,
                reconcile_fence=reconcile_fence,
            )
        except ReconcileLeaseLost:
            raise
        except ArtifactTransitionConflict:
            raise
        except Exception as exc:
            raise ReconcilePersistenceError(
                f"failed to persist image reconcile backoff image_id={row.id}"
            ) from exc
        stats.deferred += 1
        if quarantined_at is not None:
            stats.quarantined_rows += 1

    async def _quarantine_row(
        self,
        row: Any,
        *,
        status: ArtifactStatus,
        error_code: str,
        error_message: str,
        now: datetime,
        stats: ReconcileStats,
        lease_guard: ReconcileLeaseGuardPort | None,
        reconcile_fence: int | None,
    ) -> None:
        attempts = max(0, int(getattr(row, "reconcile_attempts", 0) or 0)) + 1
        try:
            await _assert_lease_owned(lease_guard)
            await self.repository.record_reconcile_failure(
                row.id,
                expected_status=status,
                attempts=attempts,
                error_code=error_code,
                error_message=error_message,
                error_at=now,
                reconcile_after=None,
                quarantined_at=now,
                reconcile_fence=reconcile_fence,
            )
        except ReconcileLeaseLost:
            raise
        except ArtifactTransitionConflict:
            raise
        except Exception as exc:
            raise ReconcilePersistenceError(
                f"failed to persist image reconcile quarantine image_id={row.id}"
            ) from exc
        stats.deferred += 1
        stats.quarantined_rows += 1

    async def _repair_missing_reference(
        self,
        original: ArtifactManifestItem,
        normalized_ref: ArtifactManifestItem,
        *,
        row: Any,
        status: ArtifactStatus,
        manifest: dict[str, Any],
        stats: ReconcileStats,
        now: datetime,
        reconcile_lease_guard: ReconcileLeaseGuardPort | None,
        reconcile_fence: int | None,
    ) -> None:
        reserved_bytes = max(
            64 * 1024 * 1024,
            original.identity.size_bytes * 2,
        )
        storage_lease = await self.storage_capacity.reserve(reserved_bytes)
        async with maintained_capacity_lease(
            storage_lease,
            ttl_seconds=self.storage_lease_ttl_seconds,
        ) as storage_guard:
            rebuilt = await self._rebuild_reference(
                original,
                normalized_ref,
                reconcile_lease_guard,
                storage_guard,
                reserved_bytes=reserved_bytes,
            )
            updated_manifest = _replace_manifest_item(
                manifest,
                "normalized_ref",
                ArtifactManifestItem(
                    key=normalized_ref.key,
                    identity=rebuilt,
                    mime=normalized_ref.mime,
                ),
            )
            await _assert_lease_owned(reconcile_lease_guard)
            await storage_guard.assert_owned()
            values = {
                "artifact_manifest_jsonb": updated_manifest,
                "last_artifact_error": None,
                "reconcile_after": None,
                **self._cleared_reconcile_state(),
            }
            if status == ArtifactStatus.PUBLISHING:
                values["ready_at"] = now
                await self.repository.transition(
                    row.id,
                    expected=[ArtifactStatus.PUBLISHING],
                    target=ArtifactStatus.READY,
                    values=values,
                    reconcile_fence=reconcile_fence,
                )
                stats.marked_ready += 1
            elif status == ArtifactStatus.READY:
                await self.repository.update_ready(
                    row.id,
                    values=values,
                    reconcile_fence=reconcile_fence,
                )
            stats.rebuilt_reference += 1

    async def _rebuild_reference(
        self,
        original: ArtifactManifestItem,
        normalized_ref: ArtifactManifestItem,
        lease_guard: ReconcileLeaseGuardPort | None,
        storage_guard: CapacityLeaseGuard,
        *,
        reserved_bytes: int,
    ) -> ArtifactIdentity:
        await _assert_lease_owned(lease_guard)
        await storage_guard.assert_owned()
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
            expected = await race_with_capacity_leases(
                asyncio.to_thread(
                    self.processor.rebuild_reference,
                    source_path,
                    output_path,
                ),
                (storage_guard,),
            )
            await _assert_lease_owned(lease_guard)
            await assert_capacity_leases_owned((storage_guard,))
            if expected.size_bytes * 2 > reserved_bytes:
                raise StorageCapacityExceeded(
                    "reconciled reference exceeded its storage reservation"
                )
            published = await self._publish_with_reconcile_lease(
                race_with_capacity_leases(
                    self.artifacts.publish_path(
                        output_path,
                        normalized_ref.key,
                        expected=expected,
                    ),
                    (storage_guard,),
                ),
                lease_guard=lease_guard,
            )
            return published.identity
        finally:
            await asyncio.to_thread(output_path.unlink, missing_ok=True)

    async def _publish_with_reconcile_lease(
        self,
        work: Awaitable[PublishedArtifact],
        *,
        lease_guard: ReconcileLeaseGuardPort | None,
    ) -> PublishedArtifact:
        if lease_guard is None:
            return await work
        work_task = asyncio.ensure_future(work)
        lost_task = asyncio.create_task(
            lease_guard.wait_lost(),
            name="image-reconcile-lease-lost-during-publish",
        )
        try:
            done, _pending = await asyncio.wait(
                (work_task, lost_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_task in done:
                try:
                    published = await work_task
                except Exception as exc:
                    raise ReconcileLeaseLost(
                        "image artifact reconcile lease was lost during publish"
                    ) from exc
                await self._discard_stale_publish(published)
                raise ReconcileLeaseLost(
                    "image artifact reconcile lease was lost during publish"
                )
            lost_task.cancel()
            await asyncio.gather(lost_task, return_exceptions=True)
            published = await work_task
            try:
                await lease_guard.assert_owned()
            except ReconcileLeaseLost:
                await self._discard_stale_publish(published)
                raise
            return published
        finally:
            if not work_task.done():
                work_task.cancel()
            if not lost_task.done():
                lost_task.cancel()
            await asyncio.gather(work_task, lost_task, return_exceptions=True)

    async def _discard_stale_publish(
        self,
        published: PublishedArtifact,
    ) -> None:
        if not published.created:
            return
        try:
            await self.artifacts.delete(
                published.key,
                expected=published.identity,
            )
        except Exception as exc:
            raise ReconcileLeaseLost(
                "stale reconcile owner could not remove published artifact"
            ) from exc
