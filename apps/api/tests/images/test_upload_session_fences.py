from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lumen_core.capacity_leases import CapacityLeaseGuard
from lumen_core.model_entities import User
from lumen_core.models import Base, Image

from app.images.adapters.sqlalchemy_repository import (
    ArtifactTransitionConflict,
    SQLAlchemyImageRepository,
)
from app.images.application.upload import (
    UploadCommandService,
    UploadPolicy,
    UploadPublishTimeout,
    _UploadCommandState,
)
from app.images.application.upload_processing import (
    UploadProcessingContext,
)
from app.images.domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    ArtifactManifestItem,
    ArtifactStatus,
    PublishedArtifact,
    StagedArtifact,
    UploadTicket,
)
from app.images.processing.service import PreparedUpload
from app.services.active_user import ActiveSessionRevoked


class _Lease:
    async def renew(self) -> bool:
        return True

    async def release(self) -> None:
        return None


class _SessionFenceRepository:
    def __init__(self) -> None:
        self.rows: dict[str, Image] = {}
        self.staging_sessions: list[str | None] = []
        self.transitions: list[tuple[ArtifactStatus, str | None, str | None]] = []
        self.fence_calls: list[tuple[str, str, str | None, str]] = []
        self.adopt_calls: list[str] = []
        self.failure_calls: list[tuple[str, datetime | None]] = []
        self.abandon_calls: list[str] = []
        self.fence_depth = 0
        self.reject_adoption: Exception | None = None

    async def create_staging(
        self,
        image: Image,
        *,
        session_id: str | None = None,
    ) -> Image:
        self.staging_sessions.append(session_id)
        self.rows[image.id] = image
        return image

    async def transition(
        self,
        image_id: str,
        *,
        expected: list[ArtifactStatus],
        target: ArtifactStatus,
        values: dict[str, Any] | None = None,
        active_user_id: str | None = None,
        session_id: str | None = None,
    ) -> Image:
        image = self.rows[image_id]
        assert ArtifactStatus(image.artifact_status) in expected
        for name, value in (values or {}).items():
            setattr(image, name, value)
        image.artifact_status = target.value
        self.transitions.append((target, active_user_id, session_id))
        return image

    async def create_storage_intent(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        session_id: str | None = None,
    ) -> Image:
        self.fence_depth += 1
        try:
            self.fence_calls.append(("intent", user_id, session_id, token))
            image = self.rows[image_id]
            manifest = dict(image.artifact_manifest_jsonb)
            manifest["storage_intent"] = {
                "version": 1,
                "state": "pending",
                "user_id": user_id,
                "image_id": image_id,
                "token": token,
            }
            image.artifact_manifest_jsonb = manifest
            return image
        finally:
            self.fence_depth -= 1

    async def adopt_storage_intent(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        manifest: dict[str, Any],
        ready_at: datetime,
        session_id: str | None = None,
    ) -> Image:
        self.fence_depth += 1
        try:
            self.fence_calls.append(("adopt", user_id, session_id, token))
            self.adopt_calls.append(token)
            if self.reject_adoption is not None:
                raise self.reject_adoption
            image = self.rows[image_id]
            image.artifact_status = ArtifactStatus.READY.value
            adopted_manifest = dict(manifest)
            adopted_manifest["storage_intent"] = {
                "version": 1,
                "state": "adopted",
                "user_id": user_id,
                "image_id": image_id,
                "token": token,
            }
            image.artifact_manifest_jsonb = adopted_manifest
            image.ready_at = ready_at
            image.reconcile_after = None
            image.last_artifact_error = None
            return image
        finally:
            self.fence_depth -= 1

    async def record_storage_intent_failure(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        error_message: str,
        retry_at: datetime | None,
    ) -> bool:
        image = self.rows[image_id]
        intent = image.artifact_manifest_jsonb.get("storage_intent", {})
        if (
            image.user_id != user_id
            or intent.get("token") != token
            or image.artifact_status != ArtifactStatus.PUBLISHING.value
        ):
            return False
        self.failure_calls.append((token, retry_at))
        image.last_artifact_error = error_message
        if retry_at is not None:
            image.reconcile_after = retry_at
        return True

    async def abandon_storage_intent(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        error_message: str,
    ) -> bool:
        image = self.rows[image_id]
        intent = image.artifact_manifest_jsonb.get("storage_intent", {})
        if image.user_id != user_id or intent.get("token") != token:
            return False
        self.abandon_calls.append(token)
        image.artifact_status = ArtifactStatus.FAILED.value
        image.deleted_at = datetime.now(timezone.utc)
        image.last_artifact_error = error_message
        image.reconcile_after = None
        return True

    @asynccontextmanager
    async def active_user_fence(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
    ):
        self.fence_calls.append((user_id, session_id))
        self.fence_depth += 1
        try:
            yield
        finally:
            self.fence_depth -= 1


class _Artifacts:
    def __init__(self, repository: _SessionFenceRepository) -> None:
        self.repository = repository
        self.identities: dict[str, ArtifactIdentity] = {}
        self.publish_fence_depths: list[int] = []
        self.identity_fence_depths: list[int] = []

    async def publish_path(
        self,
        _source: Path,
        key: Any,
        *,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        self.publish_fence_depths.append(self.repository.fence_depth)
        self.identities[key.value] = expected
        return PublishedArtifact(key=key, identity=expected, created=True)

    async def identity(self, key: Any) -> ArtifactIdentity | None:
        self.identity_fence_depths.append(self.repository.fence_depth)
        return self.identities.get(key.value)

    async def delete_staged(self, staged: StagedArtifact) -> bool:
        Path(staged.path).unlink(missing_ok=True)
        return True


class _BlockingArtifacts(_Artifacts):
    def __init__(self, repository: _SessionFenceRepository) -> None:
        super().__init__(repository)
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def publish_path(
        self,
        _source: Path,
        _key: Any,
        *,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        del expected
        self.publish_fence_depths.append(self.repository.fence_depth)
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


class _ProcessingExecutor:
    async def process(self, request: Any) -> PreparedUpload:
        source = request.source_path
        source_bytes = source.read_bytes()
        reference_bytes = b"reference"
        reference_path = request.output_paths[-1]
        reference_path.write_bytes(reference_bytes)
        return PreparedUpload(
            original_path=source,
            mime="image/png",
            width=1,
            height=1,
            size_bytes=len(source_bytes),
            sha256=hashlib.sha256(source_bytes).hexdigest(),
            metadata={},
            normalized_ref_path=reference_path,
            normalized_ref_meta={
                "bytes": len(reference_bytes),
                "sha256": hashlib.sha256(reference_bytes).hexdigest(),
                "mime": "image/webp",
            },
        )

    async def aclose(self) -> None:
        return None


def _policy() -> UploadPolicy:
    return UploadPolicy(
        allowed_mime={"image/png"},
        normalizable_mime=set(),
        extensions={"image/png": "png"},
        max_bytes=1024,
        max_pixels=1,
        max_long_side=1,
        mask_requested=False,
        reference_size=None,
    )


def _staged(tmp_path: Path, *, ticket: UploadTicket) -> StagedArtifact:
    source = tmp_path / f"{ticket.value}.png"
    source_bytes = b"source"
    source.write_bytes(source_bytes)
    return StagedArtifact(
        ticket=ticket,
        path=str(source),
        identity=ArtifactIdentity(
            sha256=hashlib.sha256(source_bytes).hexdigest(),
            size_bytes=len(source_bytes),
        ),
    )


def _inspection() -> SimpleNamespace:
    return SimpleNamespace(
        output_mime="image/png",
        mime="image/png",
        width=1,
        height=1,
    )


def _processing_context(lease_guard: CapacityLeaseGuard) -> UploadProcessingContext:
    return UploadProcessingContext(
        lease_guard=lease_guard,
        user_id="user-1",
        session_id="session-1",
        filename="upload.png",
        inspection=_inspection(),
        policy=_policy(),
        metadata_profile=None,
        metadata_finalizer=None,
        storage_guard=None,
        storage_lease_guard=None,
        storage_reservation_bytes=None,
    )


@pytest.mark.asyncio
async def test_upload_session_fences_are_short_and_exclude_file_publication(
    tmp_path: Path,
) -> None:
    ticket = UploadTicket("ticket-1")
    staged = _staged(tmp_path, ticket=ticket)
    repository = _SessionFenceRepository()
    artifacts = _Artifacts(repository)
    service = UploadCommandService(
        artifacts=artifacts,  # type: ignore[arg-type]
        capacity=object(),  # type: ignore[arg-type]
        storage_capacity=object(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        processing_executor=_ProcessingExecutor(),  # type: ignore[arg-type]
        storage_lease_ttl_seconds=30,
    )
    state = _UploadCommandState(ticket=ticket, user_id="user-1", staged=staged)
    lease_guard = CapacityLeaseGuard.create(_Lease(), ttl_seconds=30)

    original_key, normalized_key, prepared = await service._process_and_persist(
        state,
        context=_processing_context(lease_guard),
    )
    ready = await service._publish_and_mark_ready(
        state,
        lease_guard=lease_guard,
        user_id="user-1",
        session_id="session-1",
        original_key=original_key,
        normalized_key=normalized_key,
        prepared=prepared,
        storage_lease_guard=None,
    )
    await service._cleanup_state(state)

    assert repository.staging_sessions == ["session-1"]
    assert repository.transitions == [
        (ArtifactStatus.PROCESSING, "user-1", "session-1"),
        (ArtifactStatus.PUBLISHING, "user-1", "session-1"),
    ]
    assert [call[:3] for call in repository.fence_calls] == [
        ("intent", "user-1", "session-1"),
        ("adopt", "user-1", "session-1"),
    ]
    assert repository.fence_calls[0][3] == repository.fence_calls[1][3]
    assert repository.adopt_calls == [repository.fence_calls[0][3]]
    assert artifacts.publish_fence_depths == [0, 0]
    assert artifacts.identity_fence_depths == [0, 0]
    assert ready.artifact_status == ArtifactStatus.READY.value
    assert ready.artifact_manifest_jsonb["storage_intent"] == {
        "version": 1,
        "state": "adopted",
        "user_id": "user-1",
        "image_id": ready.id,
        "token": repository.adopt_calls[0],
    }
    assert not Path(staged.path).exists()
    assert list(tmp_path.glob("processed-*")) == []


@pytest.mark.asyncio
async def test_upload_publish_timeout_keeps_intent_reconcilable(
    tmp_path: Path,
) -> None:
    ticket = UploadTicket("ticket-timeout")
    repository = _SessionFenceRepository()
    artifacts = _BlockingArtifacts(repository)
    service = UploadCommandService(
        artifacts=artifacts,  # type: ignore[arg-type]
        capacity=object(),  # type: ignore[arg-type]
        storage_capacity=object(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        processing_executor=_ProcessingExecutor(),  # type: ignore[arg-type]
        storage_lease_ttl_seconds=30,
        publish_timeout_seconds=0.02,
    )
    state = _UploadCommandState(
        ticket=ticket,
        user_id="user-1",
        staged=_staged(tmp_path, ticket=ticket),
    )
    lease_guard = CapacityLeaseGuard.create(_Lease(), ttl_seconds=30)
    original_key, normalized_key, prepared = await service._process_and_persist(
        state,
        context=_processing_context(lease_guard),
    )
    scheduled = repository.rows[state.image_id].reconcile_after

    with pytest.raises(UploadPublishTimeout) as exc_info:
        await service._publish_and_mark_ready(
            state,
            lease_guard=lease_guard,
            user_id="user-1",
            session_id="session-1",
            original_key=original_key,
            normalized_key=normalized_key,
            prepared=prepared,
            storage_lease_guard=None,
        )
    await service._handle_failure(state, exc_info.value)
    await service._cleanup_state(state)

    row = repository.rows[state.image_id]
    assert artifacts.started.is_set()
    assert artifacts.cancelled.is_set()
    assert artifacts.publish_fence_depths == [0]
    assert repository.adopt_calls == []
    assert repository.failure_calls == [(state.storage_intent_token, None)]
    assert row.artifact_status == ArtifactStatus.PUBLISHING.value
    assert row.reconcile_after == scheduled
    assert row.artifact_manifest_jsonb["storage_intent"]["token"] == (
        state.storage_intent_token
    )
    assert row.artifact_manifest_jsonb["storage_intent"]["state"] == "pending"


@pytest.mark.asyncio
async def test_rejected_adoption_abandons_intent_for_orphan_recovery(
    tmp_path: Path,
) -> None:
    ticket = UploadTicket("ticket-rejected")
    repository = _SessionFenceRepository()
    repository.reject_adoption = ActiveSessionRevoked()
    artifacts = _Artifacts(repository)
    service = UploadCommandService(
        artifacts=artifacts,  # type: ignore[arg-type]
        capacity=object(),  # type: ignore[arg-type]
        storage_capacity=object(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        processing_executor=_ProcessingExecutor(),  # type: ignore[arg-type]
        storage_lease_ttl_seconds=30,
    )
    state = _UploadCommandState(
        ticket=ticket,
        user_id="user-1",
        staged=_staged(tmp_path, ticket=ticket),
    )
    lease_guard = CapacityLeaseGuard.create(_Lease(), ttl_seconds=30)
    original_key, normalized_key, prepared = await service._process_and_persist(
        state,
        context=_processing_context(lease_guard),
    )

    with pytest.raises(ActiveSessionRevoked):
        await service._publish_and_mark_ready(
            state,
            lease_guard=lease_guard,
            user_id="user-1",
            session_id="session-1",
            original_key=original_key,
            normalized_key=normalized_key,
            prepared=prepared,
            storage_lease_guard=None,
        )
    await service._cleanup_state(state)

    row = repository.rows[state.image_id]
    assert artifacts.publish_fence_depths == [0, 0]
    assert artifacts.identity_fence_depths == [0, 0]
    assert repository.abandon_calls == [state.storage_intent_token]
    assert row.artifact_status == ArtifactStatus.FAILED.value
    assert row.deleted_at is not None
    assert row.reconcile_after is None


def _manifest(*, image_id: str, other_user: bool = False) -> dict[str, Any]:
    user_id = "user-2" if other_user else "user-1"
    original = ArtifactManifestItem(
        key=ArtifactKey(f"u/{user_id}/uploads/{image_id}.png"),
        identity=ArtifactIdentity(sha256="a" * 64, size_bytes=10),
        mime="image/png",
    )
    normalized = ArtifactManifestItem(
        key=ArtifactKey(f"u/{user_id}/uploads/{image_id}.ref.webp"),
        identity=ArtifactIdentity(sha256="b" * 64, size_bytes=5),
        mime="image/webp",
    )
    return {
        "version": 1,
        "ticket": f"ticket-{image_id}",
        "artifacts": {
            "original": original.to_json(),
            "normalized_ref": normalized.to_json(),
        },
    }


@pytest.mark.asyncio
async def test_repository_storage_intent_fences_token_owner_and_paths() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[User.__table__, Image.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = SQLAlchemyImageRepository(factory)
    image_id = "image-intent"
    abandoned_id = "image-abandoned"
    token = "intent-token"
    try:
        async with factory() as session:
            session.add(
                User(
                    id="user-1",
                    email="user-1@example.com",
                    display_name="User 1",
                )
            )
            session.add(
                Image(
                    id=image_id,
                    user_id="user-1",
                    source="uploaded",
                    storage_key=f"u/user-1/uploads/{image_id}.png",
                    mime="image/png",
                    width=1,
                    height=1,
                    size_bytes=10,
                    sha256="a" * 64,
                    artifact_status=ArtifactStatus.PUBLISHING.value,
                    artifact_manifest_jsonb=_manifest(image_id=image_id),
                    reconcile_after=datetime.now(timezone.utc) + timedelta(minutes=2),
                )
            )
            session.add(
                Image(
                    id=abandoned_id,
                    user_id="user-1",
                    source="uploaded",
                    storage_key=f"u/user-1/uploads/{abandoned_id}.png",
                    mime="image/png",
                    width=1,
                    height=1,
                    size_bytes=10,
                    sha256="a" * 64,
                    artifact_status=ArtifactStatus.PUBLISHING.value,
                    artifact_manifest_jsonb=_manifest(image_id=abandoned_id),
                    reconcile_after=datetime.now(timezone.utc) + timedelta(minutes=2),
                )
            )
            await session.commit()

        intent = await repository.create_storage_intent(
            image_id,
            user_id="user-1",
            token=token,
        )
        same_intent = await repository.create_storage_intent(
            image_id,
            user_id="user-1",
            token=token,
        )
        assert intent.artifact_manifest_jsonb["storage_intent"]["token"] == token
        assert same_intent.artifact_manifest_jsonb == intent.artifact_manifest_jsonb

        with pytest.raises(ArtifactTransitionConflict):
            await repository.create_storage_intent(
                image_id,
                user_id="user-1",
                token="newer-token",
            )
        with pytest.raises(ArtifactTransitionConflict):
            await repository.adopt_storage_intent(
                image_id,
                user_id="user-1",
                token="stale-token",
                manifest=_manifest(image_id=image_id),
                ready_at=datetime.now(timezone.utc),
            )
        with pytest.raises(ValueError, match="invalid adopted"):
            await repository.adopt_storage_intent(
                image_id,
                user_id="user-1",
                token=token,
                manifest=_manifest(image_id=image_id, other_user=True),
                ready_at=datetime.now(timezone.utc),
            )
        assert not await repository.record_storage_intent_failure(
            image_id,
            user_id="user-1",
            token="stale-token",
            error_message="stale request",
            retry_at=datetime.now(timezone.utc),
        )

        ready = await repository.adopt_storage_intent(
            image_id,
            user_id="user-1",
            token=token,
            manifest=_manifest(image_id=image_id),
            ready_at=datetime.now(timezone.utc),
        )
        repeated = await repository.adopt_storage_intent(
            image_id,
            user_id="user-1",
            token=token,
            manifest=_manifest(image_id=image_id),
            ready_at=datetime.now(timezone.utc),
        )
        assert ready.artifact_status == ArtifactStatus.READY.value
        assert ready.artifact_manifest_jsonb["storage_intent"]["state"] == "adopted"
        assert ready.artifact_manifest_jsonb["storage_intent"]["token"] == token
        assert repeated.id == ready.id
        with pytest.raises(ArtifactTransitionConflict):
            await repository.adopt_storage_intent(
                image_id,
                user_id="user-1",
                token="stale-token",
                manifest=_manifest(image_id=image_id),
                ready_at=datetime.now(timezone.utc),
            )

        await repository.create_storage_intent(
            abandoned_id,
            user_id="user-1",
            token="abandon-token",
        )
        assert await repository.abandon_storage_intent(
            abandoned_id,
            user_id="user-1",
            token="abandon-token",
            error_message="session revoked",
        )
        abandoned = await repository.get(abandoned_id)
        assert abandoned is not None
        assert abandoned.artifact_status == ArtifactStatus.FAILED.value
        assert abandoned.deleted_at is not None
        assert abandoned.reconcile_after is None
        assert not await repository.abandon_storage_intent(
            abandoned_id,
            user_id="user-1",
            token="abandon-token",
            error_message="repeat",
        )
    finally:
        await engine.dispose()
