from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.images.application.reconcile_policy import ImageArtifactReconciler
from app.images.domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    ArtifactManifestItem,
    ArtifactStatus,
    StagedSweepResult,
)


_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
_ORIGINAL = ArtifactManifestItem(
    key=ArtifactKey("u/user-1/uploads/image-1.png"),
    identity=ArtifactIdentity(sha256="a" * 64, size_bytes=8),
    mime="image/png",
)
_REFERENCE = ArtifactManifestItem(
    key=ArtifactKey("u/user-1/uploads/image-1.ref.webp"),
    identity=ArtifactIdentity(sha256="b" * 64, size_bytes=4),
    mime="image/webp",
)


class _Repository:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.failures: list[dict[str, Any]] = []

    async def list_reconcile_candidates(self, **_kwargs: Any) -> list[Any]:
        return [self.row]

    async def active_upload_tickets(
        self,
        _candidate_tickets: set[str] | None = None,
    ) -> set[str]:
        return set()

    async def record_reconcile_failure(
        self,
        _image_id: str,
        **values: Any,
    ) -> None:
        self.failures.append(values)


class _Artifacts:
    def __init__(self, *, identity_error: Exception | None = None) -> None:
        self.identity_error = identity_error

    async def identity(self, _key: ArtifactKey) -> ArtifactIdentity | None:
        if self.identity_error is not None:
            raise self.identity_error
        raise AssertionError("identity should not be queried")

    async def sweep_staged(self, **_kwargs: Any) -> StagedSweepResult:
        return StagedSweepResult()


class _StorageLease:
    async def renew(self) -> bool:
        return True

    async def release(self) -> None:
        return None


class _StorageCapacity:
    async def reserve(self, _bytes_required: int) -> _StorageLease:
        return _StorageLease()


def _row(*, attempts: int, manifest: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        id="image-1",
        artifact_status=ArtifactStatus.READY.value,
        artifact_manifest_jsonb=(
            {
                "artifacts": {
                    "original": _ORIGINAL.to_json(),
                    "normalized_ref": _REFERENCE.to_json(),
                }
            }
            if manifest is None
            else manifest
        ),
        reconcile_attempts=attempts,
    )


@pytest.mark.asyncio
async def test_transient_reconcile_failure_persists_exponential_backoff() -> None:
    row = _row(attempts=2)
    repository = _Repository(row)
    stats = await ImageArtifactReconciler(
        repository=repository,  # type: ignore[arg-type]
        artifacts=_Artifacts(identity_error=OSError("storage unavailable")),  # type: ignore[arg-type]
        storage_capacity=_StorageCapacity(),
    ).run_once(now=_NOW)

    assert stats.deferred == 1
    assert stats.quarantined_rows == 0
    failure = repository.failures[0]
    assert failure["attempts"] == 3
    assert failure["error_code"] == "o_s_error"
    assert failure["reconcile_after"] == _NOW + timedelta(minutes=2)
    assert failure["quarantined_at"] is None


@pytest.mark.asyncio
async def test_repeated_transient_failure_is_quarantined_at_attempt_limit() -> None:
    row = _row(attempts=7)
    repository = _Repository(row)
    stats = await ImageArtifactReconciler(
        repository=repository,  # type: ignore[arg-type]
        artifacts=_Artifacts(identity_error=OSError("storage unavailable")),  # type: ignore[arg-type]
        storage_capacity=_StorageCapacity(),
    ).run_once(now=_NOW)

    assert stats.deferred == 1
    assert stats.quarantined_rows == 1
    failure = repository.failures[0]
    assert failure["attempts"] == 8
    assert failure["reconcile_after"] is None
    assert failure["quarantined_at"] == _NOW


@pytest.mark.asyncio
async def test_structurally_invalid_manifest_is_quarantined_immediately() -> None:
    row = _row(attempts=0, manifest={"artifacts": {"original": {}}})
    repository = _Repository(row)
    stats = await ImageArtifactReconciler(
        repository=repository,  # type: ignore[arg-type]
        artifacts=_Artifacts(),  # type: ignore[arg-type]
        storage_capacity=_StorageCapacity(),
    ).run_once(now=_NOW)

    assert stats.deferred == 1
    assert stats.quarantined_rows == 1
    failure = repository.failures[0]
    assert failure["attempts"] == 1
    assert failure["error_code"] == "invalid_artifact_manifest"
    assert failure["reconcile_after"] is None
    assert failure["quarantined_at"] == _NOW
