from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.images.application.reconcile_policy import (
    ImageArtifactReconciler,
    ReconcileLeaseLost,
)
from app.images.application.reconcile_runtime import ReconcileLeaseGuard
from app.images.application.reconcile_runtime import _acquire_reconcile_lease
from app.images.domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    ArtifactManifestItem,
    ArtifactStatus,
    PublishedArtifact,
    StagedArtifact,
    StagedSweepResult,
    UploadTicket,
)


_NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)
_ORIGINAL_IDENTITY = ArtifactIdentity(sha256="a" * 64, size_bytes=8)
_REFERENCE_IDENTITY = ArtifactIdentity(sha256="b" * 64, size_bytes=9)
_REBUILT_IDENTITY = ArtifactIdentity(sha256="c" * 64, size_bytes=7)
_ORIGINAL_ITEM = ArtifactManifestItem(
    key=ArtifactKey("u/user-1/uploads/image-1.png"),
    identity=_ORIGINAL_IDENTITY,
    mime="image/png",
)
_REFERENCE_ITEM = ArtifactManifestItem(
    key=ArtifactKey("u/user-1/uploads/image-1.ref.webp"),
    identity=_REFERENCE_IDENTITY,
    mime="image/webp",
)


class _RenewalSequence:
    def __init__(self, *results: bool) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        if not self.results:
            raise AssertionError("unexpected reconcile lease confirmation")
        return self.results.pop(0)


class _FenceRedis:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.calls: list[tuple[Any, ...]] = []

    async def set(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append((*args, kwargs))
        return self.acquired


class _StorageLease:
    async def renew(self) -> bool:
        return True

    async def release(self) -> None:
        return None


class _StorageCapacity:
    async def reserve(self, _bytes_required: int) -> _StorageLease:
        return _StorageLease()


class _Repository:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.claim_calls = 0
        self.transition_calls = 0
        self.update_ready_calls = 0
        self.reconcile_failures: list[dict[str, Any]] = []

    async def list_reconcile_candidates(self, **_kwargs: Any) -> list[Any]:
        return self.rows

    async def active_upload_tickets(
        self,
        _candidate_tickets: set[str] | None = None,
    ) -> set[str]:
        return set()

    async def claim_reconcile(self, *_args: Any, **_kwargs: Any) -> bool:
        self.claim_calls += 1
        return True

    async def transition(self, *_args: Any, **_kwargs: Any) -> None:
        self.transition_calls += 1

    async def update_ready(self, *_args: Any, **_kwargs: Any) -> None:
        self.update_ready_calls += 1

    async def record_reconcile_failure(
        self,
        _image_id: str,
        **kwargs: Any,
    ) -> None:
        self.reconcile_failures.append(kwargs)

    @asynccontextmanager
    async def guard_reconcile_publish_cleanup(
        self,
        _image_id: str,
        *,
        stale_fence: int,
    ) -> AsyncIterator[bool]:
        del stale_fence
        yield True


class _ArtifactStore:
    def __init__(
        self,
        tmp_path: Path,
        *,
        staged: list[StagedArtifact] | None = None,
    ) -> None:
        self.source_path = tmp_path / "image-1.png"
        self.source_path.write_bytes(b"original")
        self.staged = staged or []
        self.publish_calls = 0
        self.delete_calls = 0
        self.delete_staged_calls = 0
        self.published_keys: set[ArtifactKey] = set()

    async def identity(self, key: ArtifactKey) -> ArtifactIdentity | None:
        if key == _ORIGINAL_ITEM.key:
            return _ORIGINAL_IDENTITY
        if key == _REFERENCE_ITEM.key:
            return None
        raise AssertionError(f"unexpected artifact key: {key}")

    def processing_path(self, _key: ArtifactKey) -> Path:
        return self.source_path

    async def publish_path(
        self,
        _source: Path,
        key: ArtifactKey,
        *,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        self.publish_calls += 1
        self.published_keys.add(key)
        return PublishedArtifact(key=key, identity=expected, created=True)

    async def delete(
        self,
        key: ArtifactKey,
        expected: ArtifactIdentity | None = None,
    ) -> bool:
        del expected
        self.delete_calls += 1
        if key not in self.published_keys:
            return False
        self.published_keys.remove(key)
        return True

    async def delete_staged(self, _staged: StagedArtifact) -> bool:
        self.delete_staged_calls += 1
        return True

    async def sweep_staged(
        self,
        *,
        active_tickets: set[str],
        stale_before: float,
        budget: Any,
        load_active_tickets: Any = None,
        before_delete: Any = None,
    ) -> StagedSweepResult:
        if load_active_tickets is not None:
            active_tickets = await load_active_tickets(
                {staged.ticket.value for staged in self.staged}
            )
        scanned = 0
        deleted = 0
        for staged in self.staged[: budget.max_files_per_pass]:
            scanned += 1
            if staged.ticket.value in active_tickets:
                continue
            if staged.modified_at is None or staged.modified_at > stale_before:
                continue
            if before_delete is not None:
                await before_delete()
            if await self.delete_staged(staged):
                deleted += 1
        return StagedSweepResult(scanned=scanned, deleted=deleted)


class _BlockingPublishArtifactStore(_ArtifactStore):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.publish_started = asyncio.Event()
        self.finish_publish = asyncio.Event()

    async def publish_path(
        self,
        _source: Path,
        key: ArtifactKey,
        *,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        self.publish_calls += 1
        self.publish_started.set()
        await self.finish_publish.wait()
        self.published_keys.add(key)
        return PublishedArtifact(key=key, identity=expected, created=True)


class _PublishedBeforeReturnArtifactStore(_ArtifactStore):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.publish_started = asyncio.Event()
        self.file_published = asyncio.Event()
        self.return_publish = asyncio.Event()

    async def publish_path(
        self,
        _source: Path,
        key: ArtifactKey,
        *,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        self.publish_calls += 1
        self.publish_started.set()
        self.published_keys.add(key)
        self.file_published.set()
        await self.return_publish.wait()
        return PublishedArtifact(key=key, identity=expected, created=True)


class _TakeoverRepository(_Repository):
    def __init__(self, rows: list[Any]) -> None:
        super().__init__(rows)
        self.current_fence = 0
        self.current_status = ArtifactStatus.PUBLISHING

    async def claim_reconcile(
        self,
        _image_id: str,
        *,
        expected_status: ArtifactStatus,
        fence: int,
        **_kwargs: Any,
    ) -> bool:
        self.claim_calls += 1
        if expected_status != self.current_status or fence <= self.current_fence:
            return False
        self.current_fence = fence
        return True

    async def transition(
        self,
        _image_id: str,
        *,
        expected: list[ArtifactStatus],
        target: ArtifactStatus,
        reconcile_fence: int | None = None,
        **_kwargs: Any,
    ) -> None:
        if self.current_status not in expected or reconcile_fence != self.current_fence:
            raise AssertionError("stale reconcile transition")
        self.transition_calls += 1
        self.current_status = target
        self.current_fence = 0

    @asynccontextmanager
    async def guard_reconcile_publish_cleanup(
        self,
        _image_id: str,
        *,
        stale_fence: int,
    ) -> AsyncIterator[bool]:
        yield self.current_fence == stale_fence


class _Processor:
    def rebuild_reference(
        self,
        _source_path: Path,
        output_path: Path,
    ) -> ArtifactIdentity:
        output_path.write_bytes(b"rebuilt")
        return _REBUILT_IDENTITY


class _OversizedProcessor:
    def rebuild_reference(
        self,
        _source_path: Path,
        output_path: Path,
    ) -> ArtifactIdentity:
        output_path.write_bytes(b"rebuilt")
        return ArtifactIdentity(
            sha256="e" * 64,
            size_bytes=33 * 1024 * 1024,
        )


def _row(status: ArtifactStatus) -> SimpleNamespace:
    return SimpleNamespace(
        id="image-1",
        artifact_status=status.value,
        updated_at=_NOW - timedelta(minutes=10),
        reconcile_attempts=0,
        artifact_manifest_jsonb={
            "artifacts": {
                "original": _ORIGINAL_ITEM.to_json(),
                "normalized_ref": _REFERENCE_ITEM.to_json(),
            }
        },
    )


def _guard(*renewals: bool) -> tuple[ReconcileLeaseGuard, _RenewalSequence]:
    renewal = _RenewalSequence(*renewals)
    return (
        ReconcileLeaseGuard.create(
            token="lease-token",
            fence=1,
            ttl_seconds=300,
            renew=renewal,
            monotonic=lambda: 100.0,
        ),
        renewal,
    )


@pytest.mark.asyncio
async def test_reconcile_lease_acquisition_uses_expiring_owner_token() -> None:
    redis = _FenceRedis(True)

    token = await _acquire_reconcile_lease(redis)

    assert token is not None
    assert len(token) == 32
    assert redis.calls[0][0] == "lock:image-artifact-reconciler"
    assert redis.calls[0][1] == token
    assert redis.calls[0][-1] == {"ex": 300, "nx": True}


@pytest.mark.asyncio
async def test_reconcile_lease_guard_fails_closed_at_monotonic_deadline() -> None:
    now = [100.0]
    renewal = _RenewalSequence(True)
    guard = ReconcileLeaseGuard.create(
        token="lease-token",
        fence=1,
        ttl_seconds=8,
        safety_seconds=2,
        renew=renewal,
        monotonic=lambda: now[0],
    )

    await guard.assert_owned()
    now[0] = 106.0

    with pytest.raises(ReconcileLeaseLost, match="expired"):
        await guard.assert_owned()

    assert renewal.calls == 1
    assert guard.lost.is_set()


@pytest.mark.asyncio
async def test_reconcile_guard_cannot_recover_after_concurrent_loss() -> None:
    guard: ReconcileLeaseGuard

    async def renew_after_loss() -> bool:
        guard.mark_lost()
        return True

    guard = ReconcileLeaseGuard.create(
        token="lease-token",
        fence=1,
        ttl_seconds=300,
        renew=renew_after_loss,
        monotonic=lambda: 100.0,
    )

    with pytest.raises(ReconcileLeaseLost, match="was lost"):
        await guard.assert_owned()


@pytest.mark.asyncio
async def test_lease_loss_blocks_stale_row_state_transition(tmp_path: Path) -> None:
    repository = _Repository([_row(ArtifactStatus.STAGING)])
    artifacts = _ArtifactStore(tmp_path)
    guard, renewal = _guard(True, True, False)

    with pytest.raises(ReconcileLeaseLost, match="ownership changed"):
        await ImageArtifactReconciler(
            repository=repository,  # type: ignore[arg-type]
            artifacts=artifacts,  # type: ignore[arg-type]
            storage_capacity=_StorageCapacity(),
        ).run_once(
            now=_NOW,
            stale_after=timedelta(seconds=0),
            lease_guard=guard,
        )

    assert renewal.calls == 3
    assert repository.claim_calls == 1
    assert repository.transition_calls == 0


@pytest.mark.asyncio
async def test_lease_loss_during_rebuild_blocks_final_publish(tmp_path: Path) -> None:
    repository = _Repository([_row(ArtifactStatus.PUBLISHING)])
    artifacts = _ArtifactStore(tmp_path)
    guard, renewal = _guard(True, True, True, False)

    with pytest.raises(ReconcileLeaseLost, match="ownership changed"):
        await ImageArtifactReconciler(
            repository=repository,  # type: ignore[arg-type]
            artifacts=artifacts,  # type: ignore[arg-type]
            storage_capacity=_StorageCapacity(),
            processor=_Processor(),  # type: ignore[arg-type]
        ).run_once(
            now=_NOW,
            stale_after=timedelta(seconds=0),
            lease_guard=guard,
        )

    assert renewal.calls == 4
    assert repository.claim_calls == 1
    assert artifacts.publish_calls == 0
    assert repository.transition_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [ArtifactStatus.PUBLISHING, ArtifactStatus.READY],
)
async def test_lease_loss_after_publish_blocks_database_write(
    tmp_path: Path,
    status: ArtifactStatus,
) -> None:
    repository = _Repository([_row(status)])
    artifacts = _ArtifactStore(tmp_path)
    guard, renewal = _guard(True, True, True, True, False)

    with pytest.raises(ReconcileLeaseLost, match="ownership changed"):
        await ImageArtifactReconciler(
            repository=repository,  # type: ignore[arg-type]
            artifacts=artifacts,  # type: ignore[arg-type]
            storage_capacity=_StorageCapacity(),
            processor=_Processor(),  # type: ignore[arg-type]
        ).run_once(
            now=_NOW,
            stale_after=timedelta(seconds=0),
            lease_guard=guard,
        )

    assert renewal.calls == 5
    assert repository.claim_calls == 1
    assert artifacts.publish_calls == 1
    assert artifacts.delete_calls == 1
    assert artifacts.published_keys == set()
    assert repository.transition_calls == 0
    assert repository.update_ready_calls == 0


@pytest.mark.asyncio
async def test_lease_loss_during_publish_cannot_leave_stale_final_key(
    tmp_path: Path,
) -> None:
    repository = _Repository([_row(ArtifactStatus.PUBLISHING)])
    artifacts = _BlockingPublishArtifactStore(tmp_path)
    guard, _renewal = _guard(*([True] * 10))
    reconciler = ImageArtifactReconciler(
        repository=repository,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        storage_capacity=_StorageCapacity(),
        processor=_Processor(),  # type: ignore[arg-type]
    )

    task = asyncio.create_task(
        reconciler.run_once(
            now=_NOW,
            stale_after=timedelta(seconds=0),
            lease_guard=guard,
        )
    )
    await asyncio.wait_for(artifacts.publish_started.wait(), timeout=1)
    guard.mark_lost()
    await asyncio.sleep(0)
    artifacts.finish_publish.set()

    with pytest.raises(ReconcileLeaseLost, match="lost during publish"):
        await asyncio.wait_for(task, timeout=1)

    assert artifacts.publish_calls == 1
    assert artifacts.delete_calls == 1
    assert artifacts.published_keys == set()
    assert repository.transition_calls == 0


@pytest.mark.asyncio
async def test_stale_owner_does_not_delete_file_adopted_by_newer_ready_owner(
    tmp_path: Path,
) -> None:
    row = _row(ArtifactStatus.PUBLISHING)
    repository = _TakeoverRepository([row])
    artifacts = _PublishedBeforeReturnArtifactStore(tmp_path)
    guard, _renewal = _guard(*([True] * 10))
    reconciler = ImageArtifactReconciler(
        repository=repository,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        storage_capacity=_StorageCapacity(),
        processor=_Processor(),  # type: ignore[arg-type]
    )

    stale_owner = asyncio.create_task(
        reconciler.run_once(
            now=_NOW,
            stale_after=timedelta(seconds=0),
            lease_guard=guard,
        )
    )
    await asyncio.wait_for(artifacts.publish_started.wait(), timeout=1)
    guard.mark_lost()
    await asyncio.wait_for(artifacts.file_published.wait(), timeout=1)

    assert await repository.claim_reconcile(
        row.id,
        expected_status=ArtifactStatus.PUBLISHING,
        expected_updated_at=row.updated_at,
        fence=2,
    )
    await repository.transition(
        row.id,
        expected=[ArtifactStatus.PUBLISHING],
        target=ArtifactStatus.READY,
        reconcile_fence=2,
    )
    artifacts.return_publish.set()

    with pytest.raises(ReconcileLeaseLost, match="lost during publish"):
        await asyncio.wait_for(stale_owner, timeout=1)

    assert repository.current_status == ArtifactStatus.READY
    assert repository.current_fence == 0
    assert artifacts.delete_calls == 0
    assert artifacts.published_keys == {_REFERENCE_ITEM.key}


@pytest.mark.asyncio
async def test_lease_loss_blocks_staged_artifact_delete(tmp_path: Path) -> None:
    staged_path = tmp_path / "staged.bin"
    staged_path.write_bytes(b"staged")
    staged = StagedArtifact(
        ticket=UploadTicket("ticket-1"),
        path=str(staged_path),
        identity=ArtifactIdentity(sha256="d" * 64, size_bytes=6),
        modified_at=0,
    )
    repository = _Repository([])
    artifacts = _ArtifactStore(tmp_path, staged=[staged])
    guard, renewal = _guard(True, False)

    with pytest.raises(ReconcileLeaseLost, match="ownership changed"):
        await ImageArtifactReconciler(
            repository=repository,  # type: ignore[arg-type]
            artifacts=artifacts,  # type: ignore[arg-type]
            storage_capacity=_StorageCapacity(),
        ).run_once(
            now=_NOW,
            stale_after=timedelta(seconds=0),
            lease_guard=guard,
        )

    assert renewal.calls == 2
    assert artifacts.delete_staged_calls == 0


@pytest.mark.asyncio
async def test_reconcile_rejects_reservation_overrun_before_publish(
    tmp_path: Path,
) -> None:
    repository = _Repository([_row(ArtifactStatus.PUBLISHING)])
    artifacts = _ArtifactStore(tmp_path)

    stats = await ImageArtifactReconciler(
        repository=repository,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        storage_capacity=_StorageCapacity(),
        processor=_OversizedProcessor(),  # type: ignore[arg-type]
    ).run_once(
        now=_NOW,
        stale_after=timedelta(seconds=0),
    )

    assert stats.deferred == 1
    assert artifacts.publish_calls == 0
    assert repository.transition_calls == 0
    assert repository.reconcile_failures[0]["error_code"] == (
        "storage_capacity_exhausted"
    )
