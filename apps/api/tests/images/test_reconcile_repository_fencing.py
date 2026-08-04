from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.images.adapters.sqlalchemy_repository import (
    ArtifactCommitError,
    ArtifactTransitionConflict,
    SQLAlchemyImageRepository,
)
from app.images.domain.artifact import ArtifactStatus
from lumen_core.model_entities.media_workflows import ImageReconcileEpoch
from lumen_core.models import Base, Image


_NOW = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)


@asynccontextmanager
async def _repository() -> AsyncIterator[
    tuple[SQLAlchemyImageRepository, async_sessionmaker[AsyncSession]]
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    Image.__table__,
                    ImageReconcileEpoch.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield SQLAlchemyImageRepository(factory), factory
    finally:
        await engine.dispose()


async def _insert_image(
    factory: async_sessionmaker[AsyncSession],
    *,
    image_id: str,
    status: ArtifactStatus,
) -> Image:
    image = Image(
        id=image_id,
        user_id="user-1",
        source="uploaded",
        storage_key=f"u/user-1/uploads/{image_id}.png",
        mime="image/png",
        width=16,
        height=16,
        size_bytes=128,
        sha256="a" * 64,
        artifact_status=status.value,
        artifact_manifest_jsonb={},
        reconcile_after=_NOW - timedelta(seconds=1),
        updated_at=_NOW - timedelta(minutes=10),
    )
    async with factory() as session:
        session.add(image)
        await session.commit()
        await session.refresh(image)
    return image


@pytest.mark.asyncio
async def test_newer_reconcile_fence_blocks_stale_transition() -> None:
    async with _repository() as (repository, factory):
        image = await _insert_image(
            factory,
            image_id="image-transition",
            status=ArtifactStatus.PUBLISHING,
        )

        assert await repository.claim_reconcile(
            image.id,
            expected_status=ArtifactStatus.PUBLISHING,
            expected_updated_at=image.updated_at,
            fence=11,
        )
        assert await repository.claim_reconcile(
            image.id,
            expected_status=ArtifactStatus.PUBLISHING,
            expected_updated_at=image.updated_at,
            fence=12,
        )
        assert not await repository.claim_reconcile(
            image.id,
            expected_status=ArtifactStatus.PUBLISHING,
            expected_updated_at=image.updated_at,
            fence=11,
        )

        updated = await repository.transition(
            image.id,
            expected=[ArtifactStatus.PUBLISHING],
            target=ArtifactStatus.READY,
            values={"ready_at": _NOW},
            reconcile_fence=12,
        )
        assert updated.artifact_status == ArtifactStatus.READY.value
        assert updated.reconcile_fence == 0

        with pytest.raises(ArtifactTransitionConflict):
            await repository.transition(
                image.id,
                expected=[ArtifactStatus.PUBLISHING],
                target=ArtifactStatus.READY,
                values={"ready_at": _NOW},
                reconcile_fence=11,
            )


@pytest.mark.asyncio
async def test_stale_publish_cleanup_guard_rechecks_current_database_owner() -> None:
    async with _repository() as (repository, factory):
        image = await _insert_image(
            factory,
            image_id="image-publish-cleanup",
            status=ArtifactStatus.PUBLISHING,
        )

        assert await repository.claim_reconcile(
            image.id,
            expected_status=ArtifactStatus.PUBLISHING,
            expected_updated_at=image.updated_at,
            fence=51,
        )
        async with repository.guard_reconcile_publish_cleanup(
            image.id,
            stale_fence=51,
        ) as can_delete:
            assert can_delete

        assert await repository.claim_reconcile(
            image.id,
            expected_status=ArtifactStatus.PUBLISHING,
            expected_updated_at=image.updated_at,
            fence=52,
        )
        await repository.transition(
            image.id,
            expected=[ArtifactStatus.PUBLISHING],
            target=ArtifactStatus.READY,
            values={"ready_at": _NOW},
            reconcile_fence=52,
        )

        async with repository.guard_reconcile_publish_cleanup(
            image.id,
            stale_fence=51,
        ) as can_delete:
            assert not can_delete


@pytest.mark.asyncio
async def test_unfenced_writes_cannot_cross_active_reconcile_claim() -> None:
    async with _repository() as (repository, factory):
        image = await _insert_image(
            factory,
            image_id="image-unfenced-writer",
            status=ArtifactStatus.PUBLISHING,
        )
        assert await repository.claim_reconcile(
            image.id,
            expected_status=ArtifactStatus.PUBLISHING,
            expected_updated_at=image.updated_at,
            fence=31,
        )

        with pytest.raises(ArtifactTransitionConflict):
            await repository.transition(
                image.id,
                expected=[ArtifactStatus.PUBLISHING],
                target=ArtifactStatus.READY,
                values={"ready_at": _NOW},
            )
        with pytest.raises(ArtifactTransitionConflict):
            await repository.update_publishing(
                image.id,
                values={"last_artifact_error": "stale uploader"},
            )

        persisted = await repository.get(image.id)
        assert persisted is not None
        assert persisted.artifact_status == ArtifactStatus.PUBLISHING.value
        assert persisted.reconcile_fence == 31
        assert persisted.ready_at is None
        assert persisted.last_artifact_error is None


@pytest.mark.asyncio
async def test_reconcile_fence_sequence_is_persistent_and_monotonic() -> None:
    async with _repository() as (repository, _factory):
        assert await repository.next_reconcile_fence() == 1
        assert await repository.next_reconcile_fence() == 2


@pytest.mark.asyncio
async def test_newer_reconcile_fence_blocks_stale_failure_record() -> None:
    async with _repository() as (repository, factory):
        image = await _insert_image(
            factory,
            image_id="image-failure",
            status=ArtifactStatus.READY,
        )

        assert await repository.claim_reconcile(
            image.id,
            expected_status=ArtifactStatus.READY,
            expected_updated_at=image.updated_at,
            fence=20,
        )
        assert await repository.claim_reconcile(
            image.id,
            expected_status=ArtifactStatus.READY,
            expected_updated_at=image.updated_at,
            fence=21,
        )

        with pytest.raises(ArtifactTransitionConflict):
            await repository.record_reconcile_failure(
                image.id,
                expected_status=ArtifactStatus.READY,
                attempts=1,
                error_code="stale_worker",
                error_message="stale worker must not write",
                error_at=_NOW,
                reconcile_after=_NOW + timedelta(minutes=1),
                quarantined_at=None,
                reconcile_fence=20,
            )

        await repository.record_reconcile_failure(
            image.id,
            expected_status=ArtifactStatus.READY,
            attempts=1,
            error_code="winner",
            error_message="new owner",
            error_at=_NOW,
            reconcile_after=_NOW + timedelta(minutes=1),
            quarantined_at=None,
            reconcile_fence=21,
        )
        persisted = await repository.get(image.id)
        assert persisted is not None
        assert persisted.last_reconcile_error_code == "winner"
        assert persisted.reconcile_fence == 0


@pytest.mark.asyncio
async def test_soft_deleted_candidate_cannot_be_claimed_or_finalized() -> None:
    async with _repository() as (repository, factory):
        deleted_before_claim = await _insert_image(
            factory,
            image_id="image-deleted-before-claim",
            status=ArtifactStatus.PUBLISHING,
        )
        claimed_then_deleted = await _insert_image(
            factory,
            image_id="image-claimed-then-deleted",
            status=ArtifactStatus.PUBLISHING,
        )

        async with factory() as session:
            await session.execute(
                update(Image)
                .where(Image.id == deleted_before_claim.id)
                .values(deleted_at=_NOW)
            )
            await session.commit()

        candidates = await repository.list_reconcile_candidates(
            due_before=_NOW,
            stale_before=_NOW,
            limit=10,
        )
        assert [row.id for row in candidates] == [claimed_then_deleted.id]
        assert not await repository.claim_reconcile(
            deleted_before_claim.id,
            expected_status=ArtifactStatus.PUBLISHING,
            expected_updated_at=deleted_before_claim.updated_at,
            fence=40,
        )

        assert await repository.claim_reconcile(
            claimed_then_deleted.id,
            expected_status=ArtifactStatus.PUBLISHING,
            expected_updated_at=claimed_then_deleted.updated_at,
            fence=41,
        )
        async with factory() as session:
            await session.execute(
                update(Image)
                .where(Image.id == claimed_then_deleted.id)
                .values(deleted_at=_NOW)
            )
            await session.commit()

        with pytest.raises(ArtifactTransitionConflict):
            await repository.transition(
                claimed_then_deleted.id,
                expected=[ArtifactStatus.PUBLISHING],
                target=ArtifactStatus.READY,
                values={"ready_at": _NOW},
                reconcile_fence=41,
            )

        persisted = await repository.get(claimed_then_deleted.id)
        assert persisted is not None
        assert persisted.deleted_at is not None
        assert persisted.artifact_status == ArtifactStatus.PUBLISHING.value
        assert persisted.reconcile_fence == 41


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_fence", "persisted_manifest"),
    [
        (52, {"winner": "ours"}),
        (0, {"winner": "newer"}),
    ],
)
async def test_commit_ack_loss_requires_expected_fence_and_payload(
    persisted_fence: int,
    persisted_manifest: dict[str, str],
) -> None:
    row = SimpleNamespace(
        id="image-commit-unknown",
        artifact_status=ArtifactStatus.READY.value,
        reconcile_fence=persisted_fence,
        artifact_manifest_jsonb=persisted_manifest,
        storage_key="u/user-1/uploads/image-commit-unknown.png",
        deleted_at=None,
    )

    class _PrimarySession:
        async def commit(self) -> None:
            raise TimeoutError("commit outcome unknown")

        async def rollback(self) -> None:
            return None

    class _RecoverySession:
        async def __aenter__(self) -> "_RecoverySession":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, _model: Any, _image_id: str) -> Any:
            return row

    class _Factory:
        def __call__(self) -> _RecoverySession:
            return _RecoverySession()

    repository = SQLAlchemyImageRepository(_Factory())  # type: ignore[arg-type]
    with pytest.raises(ArtifactCommitError, match="commit not observed"):
        await repository._resolve_commit(  # noqa: SLF001
            _PrimarySession(),  # type: ignore[arg-type]
            image_id=row.id,
            target_status=ArtifactStatus.READY,
            expected_values={
                "artifact_manifest_jsonb": {"winner": "ours"},
                "storage_key": row.storage_key,
                "deleted_at": None,
            },
            expected_reconcile_fence=0,
        )
