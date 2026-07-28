from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.images.application.reconcile_policy import (
    ImageArtifactReconciler,
    ReconcilePersistenceError,
    ReconcileStats,
)
from app.images.domain.artifact import ArtifactStatus


_NOW = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)


class _FailingRepository:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.failure_calls = 0

    async def list_reconcile_candidates(self, **_kwargs: Any) -> list[Any]:
        return [self.row]

    async def record_reconcile_failure(
        self,
        _image_id: str,
        **_values: Any,
    ) -> None:
        self.failure_calls += 1
        raise OSError("database write unavailable")


class _Artifacts:
    async def sweep_staged(self, **_kwargs: Any) -> Any:
        raise AssertionError("staged sweep must not run after persistence failure")


class _StorageCapacity:
    async def reserve(self, _bytes_required: int) -> Any:
        raise AssertionError("storage capacity must not be used")


def _row(*, attempts: int = 0) -> Any:
    return SimpleNamespace(
        id="image-1",
        artifact_status=ArtifactStatus.READY.value,
        artifact_manifest_jsonb={"artifacts": {"original": {}}},
        reconcile_attempts=attempts,
    )


@pytest.mark.asyncio
async def test_backoff_persistence_failure_propagates_without_false_stats() -> None:
    row = _row(attempts=7)
    repository = _FailingRepository(row)
    reconciler = ImageArtifactReconciler(
        repository=repository,  # type: ignore[arg-type]
        artifacts=_Artifacts(),  # type: ignore[arg-type]
        storage_capacity=_StorageCapacity(),
    )
    stats = ReconcileStats()

    with pytest.raises(ReconcilePersistenceError) as exc_info:
        await reconciler._record_reconcile_failure(  # noqa: SLF001
            row,
            error=OSError("artifact unavailable"),
            now=_NOW,
            stats=stats,
            lease_guard=None,
            reconcile_fence=None,
        )

    assert isinstance(exc_info.value.__cause__, OSError)
    assert repository.failure_calls == 1
    assert stats.deferred == 0
    assert stats.quarantined_rows == 0


@pytest.mark.asyncio
async def test_quarantine_persistence_failure_propagates_without_false_stats() -> None:
    row = _row()
    repository = _FailingRepository(row)
    reconciler = ImageArtifactReconciler(
        repository=repository,  # type: ignore[arg-type]
        artifacts=_Artifacts(),  # type: ignore[arg-type]
        storage_capacity=_StorageCapacity(),
    )
    stats = ReconcileStats()

    with pytest.raises(ReconcilePersistenceError) as exc_info:
        await reconciler._quarantine_row(  # noqa: SLF001
            row,
            status=ArtifactStatus.READY,
            error_code="invalid_artifact_manifest",
            error_message="invalid artifact manifest",
            now=_NOW,
            stats=stats,
            lease_guard=None,
            reconcile_fence=None,
        )

    assert isinstance(exc_info.value.__cause__, OSError)
    assert repository.failure_calls == 1
    assert stats.deferred == 0
    assert stats.quarantined_rows == 0


@pytest.mark.asyncio
async def test_run_once_does_not_rewrite_quarantine_persistence_failure() -> None:
    row = _row()
    repository = _FailingRepository(row)
    reconciler = ImageArtifactReconciler(
        repository=repository,  # type: ignore[arg-type]
        artifacts=_Artifacts(),  # type: ignore[arg-type]
        storage_capacity=_StorageCapacity(),
    )

    with pytest.raises(ReconcilePersistenceError):
        await reconciler.run_once(now=_NOW)

    assert repository.failure_calls == 1
