from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lumen_core.capacity_leases import CapacityLeaseGuard
from lumen_core.models import Image

from app.images.application.upload import UploadCommandService, UploadPolicy
from app.images.application.upload_processing import (
    UploadExecutionState,
    UploadProcessingContext,
)
from app.images.domain.artifact import (
    ArtifactIdentity,
    ArtifactStatus,
    PublishedArtifact,
    StagedArtifact,
    UploadTicket,
)
from app.images.processing.service import PreparedUpload


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
        self.fence_calls: list[tuple[str, str | None]] = []
        self.fence_depth = 0

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
        return self.identities.get(key.value)

    async def delete_staged(self, staged: StagedArtifact) -> bool:
        Path(staged.path).unlink(missing_ok=True)
        return True


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


@pytest.mark.asyncio
async def test_upload_session_fences_cover_staging_publish_and_ready(
    tmp_path: Path,
) -> None:
    source = tmp_path / "staged.png"
    source_bytes = b"source"
    source.write_bytes(source_bytes)
    ticket = UploadTicket("ticket-1")
    staged = StagedArtifact(
        ticket=ticket,
        path=str(source),
        identity=ArtifactIdentity(
            sha256=hashlib.sha256(source_bytes).hexdigest(),
            size_bytes=len(source_bytes),
        ),
    )
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
    state = UploadExecutionState(ticket=ticket, staged=staged)
    lease_guard = CapacityLeaseGuard.create(_Lease(), ttl_seconds=30)
    inspection = SimpleNamespace(
        output_mime="image/png",
        mime="image/png",
        width=1,
        height=1,
    )

    original_key, normalized_key, prepared = await service._process_and_persist(
        state,
        context=UploadProcessingContext(
            lease_guard=lease_guard,
            user_id="user-1",
            session_id="session-1",
            filename="upload.png",
            inspection=inspection,
            policy=_policy(),
            metadata_profile=None,
            metadata_finalizer=None,
            storage_guard=None,
            storage_lease_guard=None,
            storage_reservation_bytes=None,
        ),
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
        (ArtifactStatus.READY, "user-1", "session-1"),
    ]
    assert repository.fence_calls == [("user-1", "session-1")]
    assert artifacts.publish_fence_depths == [1, 1]
    assert ready.artifact_status == ArtifactStatus.READY.value
    assert not source.exists()
    assert list(tmp_path.glob("processed-*")) == []
