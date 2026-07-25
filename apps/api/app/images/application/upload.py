from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image as PILImage

from lumen_core.constants import ImageSource, ImageVisibility
from lumen_core.model_base import new_uuid7
from lumen_core.models import Image

from ..adapters.filesystem_store import ArtifactStoreError
from ..adapters.local_capacity import (
    CapacityExceeded,
    CapacityLimits,
    CapacityUnavailable,
)
from ..adapters.sqlalchemy_repository import SQLAlchemyImageRepository
from ..domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    ArtifactManifestItem,
    ArtifactStatus,
    StagedArtifact,
    UploadTicket,
)
from ..domain.resource_estimate import ImageResourceEstimate
from ..ports.artifact_store import ArtifactStorePort
from ..ports.capacity import CapacityPort
from ..processing.service import ImageProcessor, PreparedUpload, ProcessingError
from .transactions import CapacityLeaseLost, maintained_capacity_lease


logger = logging.getLogger(__name__)


class UploadCommandError(ValueError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class UploadPolicy:
    allowed_mime: set[str]
    normalizable_mime: set[str]
    extensions: dict[str, str]
    max_bytes: int
    max_pixels: int
    max_long_side: int
    mask_requested: bool
    reference_size: tuple[int, int] | None


class _ProcessingStage:
    def __init__(self, staged: StagedArtifact) -> None:
        self.path = Path(staged.path)
        self.size_bytes = staged.identity.size_bytes
        self.sha256 = staged.identity.sha256
        self.lease = None
        self._owned: list[Path] = []

    def new_temp_path(self, *, suffix: str) -> Path:
        fd, name = tempfile.mkstemp(
            prefix="processed-",
            suffix=suffix,
            dir=str(self.path.parent),
        )
        os.close(fd)
        path = Path(name)
        path.chmod(0o600)
        self._owned.append(path)
        return path

    async def cleanup(self) -> None:
        for path in self._owned:
            await asyncio.to_thread(path.unlink, missing_ok=True)


@dataclass
class _UploadExecutionState:
    ticket: UploadTicket
    staged: StagedArtifact | None = None
    processing_stage: _ProcessingStage | None = None
    image_id: str | None = None
    publishing_started: bool = False


def _planned_manifest(
    ticket: UploadTicket,
    original_key: ArtifactKey,
    normalized_key: ArtifactKey,
) -> dict[str, Any]:
    return {
        "version": 1,
        "ticket": ticket.value,
        "artifacts": {
            "original": {
                "storage_key": original_key.value,
                "required": True,
            },
            "normalized_ref": {
                "storage_key": normalized_key.value,
                "required": True,
            },
        },
    }


def _published_manifest(
    ticket: UploadTicket,
    original: ArtifactManifestItem,
    normalized_ref: ArtifactManifestItem,
) -> dict[str, Any]:
    return {
        "version": 1,
        "ticket": ticket.value,
        "artifacts": {
            "original": original.to_json(),
            "normalized_ref": normalized_ref.to_json(),
        },
    }


class UploadCommandService:
    def __init__(
        self,
        *,
        artifacts: ArtifactStorePort,
        capacity: CapacityPort,
        repository: SQLAlchemyImageRepository,
        processor: ImageProcessor | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.capacity = capacity
        self.repository = repository
        self.processor = processor or ImageProcessor()

    async def _source_chunks(self, upload_file: Any) -> AsyncIterator[bytes]:
        while chunk := await upload_file.read(256 * 1024):
            yield chunk

    async def _mark_failed(self, image_id: str, error: BaseException) -> None:
        try:
            row = await self.repository.get(image_id)
            if row is None:
                return
            try:
                status = ArtifactStatus(row.artifact_status)
            except ValueError:
                return
            if status not in {
                ArtifactStatus.STAGING,
                ArtifactStatus.PROCESSING,
                ArtifactStatus.PUBLISHING,
            }:
                return
            await self.repository.transition(
                image_id,
                expected=[status],
                target=ArtifactStatus.FAILED,
                values={
                    "last_artifact_error": str(error)[:2000],
                    "reconcile_after": None,
                },
            )
        except Exception:
            logger.exception(
                "failed to mark image artifact failed image_id=%s",
                image_id,
            )

    async def _stage_and_inspect(
        self,
        state: _UploadExecutionState,
        *,
        upload_file: Any,
        policy: UploadPolicy,
        storage_guard: Callable[[int], None] | None,
    ) -> Any:
        ttl_seconds = CapacityLimits.from_env().lease_ttl_seconds
        staging_lease = await self.capacity.reserve(
            ImageResourceEstimate(
                upload_bytes=policy.max_bytes,
                decoded_bytes=0,
                transform_peak_bytes=0,
                output_reserve_bytes=0,
                pixels=0,
                cpu_weight=1,
            )
        )
        async with maintained_capacity_lease(
            staging_lease,
            ttl_seconds=ttl_seconds,
        ):
            state.staged = await self.artifacts.stage(
                state.ticket,
                self._source_chunks(upload_file),
                max_bytes=policy.max_bytes,
            )
            if storage_guard is not None:
                storage_guard(state.staged.identity.size_bytes)
            return await asyncio.to_thread(
                self.processor.inspect,
                Path(state.staged.path),
                upload_bytes=state.staged.identity.size_bytes,
                allowed_mime=policy.allowed_mime,
                normalizable_mime=policy.normalizable_mime,
                max_pixels=policy.max_pixels,
                max_long_side=policy.max_long_side,
            )

    async def _process_and_persist(
        self,
        state: _UploadExecutionState,
        *,
        user_id: str,
        filename: str | None,
        inspection: Any,
        policy: UploadPolicy,
        metadata_reader: Callable[[PILImage.Image, str | None], dict[str, Any]] | None,
        metadata_finalizer: Callable[[str, str, dict[str, Any]], None] | None,
        storage_guard: Callable[[int], None] | None,
    ) -> tuple[ArtifactKey, ArtifactKey, PreparedUpload]:
        assert state.staged is not None
        ttl_seconds = CapacityLimits.from_env().lease_ttl_seconds
        lease = await self.capacity.reserve(inspection.estimate)
        async with maintained_capacity_lease(
            lease,
            ttl_seconds=ttl_seconds,
        ):
            state.image_id = new_uuid7()
            extension = policy.extensions[inspection.output_mime]
            original_key = ArtifactKey(
                f"u/{user_id}/uploads/{state.image_id}.{extension}"
            )
            normalized_key = ArtifactKey(
                f"u/{user_id}/uploads/{state.image_id}.ref.webp"
            )
            image = Image(
                id=state.image_id,
                user_id=user_id,
                source=ImageSource.UPLOADED.value,
                storage_key=original_key.value,
                mime=inspection.output_mime,
                width=inspection.width,
                height=inspection.height,
                size_bytes=state.staged.identity.size_bytes,
                sha256=state.staged.identity.sha256,
                blurhash=None,
                visibility=ImageVisibility.PRIVATE.value,
                metadata_jsonb={},
                artifact_status=ArtifactStatus.STAGING.value,
                artifact_manifest_jsonb=_planned_manifest(
                    state.ticket,
                    original_key,
                    normalized_key,
                ),
                publish_attempt=0,
            )
            await self.repository.create_staging(image)
            await self.repository.transition(
                state.image_id,
                expected=[ArtifactStatus.STAGING],
                target=ArtifactStatus.PROCESSING,
            )
            state.processing_stage = _ProcessingStage(state.staged)
            prepared = await asyncio.to_thread(
                self.processor.process,
                state.processing_stage,
                filename,
                allowed_mime=policy.allowed_mime,
                normalizable_mime=policy.normalizable_mime,
                max_bytes=policy.max_bytes,
                max_pixels=policy.max_pixels,
                max_long_side=policy.max_long_side,
                mask_requested=policy.mask_requested,
                reference_size=policy.reference_size,
                metadata_reader=metadata_reader,
            )
            metadata = dict(prepared.metadata)
            if storage_guard is not None:
                storage_guard(
                    prepared.size_bytes + int(prepared.normalized_ref_meta["bytes"])
                )
            metadata["normalized_ref"] = {
                **prepared.normalized_ref_meta,
                "storage_key": normalized_key.value,
            }
            if metadata_finalizer is not None:
                metadata_finalizer(state.image_id, extension, metadata)
            manifest = _published_manifest(
                state.ticket,
                ArtifactManifestItem(
                    key=original_key,
                    identity=prepared.original_identity,
                    mime=prepared.mime,
                ),
                ArtifactManifestItem(
                    key=normalized_key,
                    identity=prepared.normalized_ref_identity,
                    mime=str(prepared.normalized_ref_meta["mime"]),
                ),
            )
            await self.repository.transition(
                state.image_id,
                expected=[ArtifactStatus.PROCESSING],
                target=ArtifactStatus.PUBLISHING,
                values={
                    "storage_key": original_key.value,
                    "mime": prepared.mime,
                    "width": prepared.width,
                    "height": prepared.height,
                    "size_bytes": prepared.size_bytes,
                    "sha256": prepared.sha256,
                    "metadata_jsonb": metadata,
                    "artifact_manifest_jsonb": manifest,
                    "publish_attempt": 1,
                    "reconcile_after": datetime.now(timezone.utc)
                    + timedelta(minutes=2),
                    "last_artifact_error": None,
                },
            )
            state.publishing_started = True
            return original_key, normalized_key, prepared

    async def _publish_and_mark_ready(
        self,
        state: _UploadExecutionState,
        *,
        original_key: ArtifactKey,
        normalized_key: ArtifactKey,
        prepared: PreparedUpload,
    ) -> Image:
        assert state.image_id is not None
        original = await self.artifacts.publish_path(
            prepared.original_path,
            original_key,
            expected=prepared.original_identity,
        )
        normalized_ref = await self.artifacts.publish_path(
            prepared.normalized_ref_path,
            normalized_key,
            expected=prepared.normalized_ref_identity,
        )
        await self._verify_published(original.key, original.identity)
        await self._verify_published(normalized_ref.key, normalized_ref.identity)
        manifest = _published_manifest(
            state.ticket,
            ArtifactManifestItem(
                key=original.key,
                identity=original.identity,
                mime=prepared.mime,
            ),
            ArtifactManifestItem(
                key=normalized_ref.key,
                identity=normalized_ref.identity,
                mime=str(prepared.normalized_ref_meta["mime"]),
            ),
        )
        return await self.repository.transition(
            state.image_id,
            expected=[ArtifactStatus.PUBLISHING],
            target=ArtifactStatus.READY,
            values={
                "artifact_manifest_jsonb": manifest,
                "reconcile_after": None,
                "last_artifact_error": None,
                "ready_at": datetime.now(timezone.utc),
            },
        )

    async def _handle_failure(
        self,
        state: _UploadExecutionState,
        error: Exception,
    ) -> None:
        if state.image_id is None:
            return
        if not state.publishing_started:
            await self._mark_failed(state.image_id, error)
            return
        try:
            await self.repository.update_publishing(
                state.image_id,
                values={
                    "last_artifact_error": str(error)[:2000],
                    "reconcile_after": datetime.now(timezone.utc),
                },
            )
        except Exception:
            logger.exception(
                "failed to record image artifact reconciliation requirement image_id=%s",
                state.image_id,
            )

    async def _cleanup_state(self, state: _UploadExecutionState) -> None:
        if state.processing_stage is not None:
            await state.processing_stage.cleanup()
        if state.staged is not None:
            try:
                await self.artifacts.delete_staged(state.staged)
            except Exception:
                logger.exception(
                    "failed to remove staged image artifact ticket=%s",
                    state.ticket.value,
                )

    async def execute(
        self,
        *,
        user_id: str,
        upload_file: Any,
        filename: str | None,
        policy: UploadPolicy,
        metadata_reader: Callable[[PILImage.Image, str | None], dict[str, Any]]
        | None = None,
        metadata_finalizer: Callable[[str, str, dict[str, Any]], None] | None = None,
        storage_guard: Callable[[int], None] | None = None,
    ) -> Image:
        state = _UploadExecutionState(ticket=UploadTicket(new_uuid7()))
        try:
            inspection = await self._stage_and_inspect(
                state,
                upload_file=upload_file,
                policy=policy,
                storage_guard=storage_guard,
            )
            original_key, normalized_key, prepared = await self._process_and_persist(
                state,
                user_id=user_id,
                filename=filename,
                inspection=inspection,
                policy=policy,
                metadata_reader=metadata_reader,
                metadata_finalizer=metadata_finalizer,
                storage_guard=storage_guard,
            )
            return await self._publish_and_mark_ready(
                state,
                original_key=original_key,
                normalized_key=normalized_key,
                prepared=prepared,
            )
        except (CapacityExceeded, CapacityUnavailable, CapacityLeaseLost) as exc:
            await self._handle_failure(state, exc)
            raise UploadCommandError(
                "upload_capacity_exceeded",
                "image upload capacity is temporarily exhausted",
                503,
            ) from exc
        except ProcessingError as exc:
            await self._handle_failure(state, exc)
            raise UploadCommandError(exc.code, exc.message, exc.status_code) from exc
        except ArtifactStoreError as exc:
            await self._handle_failure(state, exc)
            if "maximum bytes" in str(exc):
                code, status = "too_large", 413
            elif "empty upload" in str(exc):
                code, status = "empty_file", 400
            else:
                code, status = "upload_storage_error", 503
            raise UploadCommandError(code, str(exc), status) from exc
        except FileExistsError as exc:
            await self._handle_failure(state, exc)
            raise UploadCommandError(
                "storage_conflict",
                "image storage key already exists",
                409,
            ) from exc
        except Exception as exc:
            await self._handle_failure(state, exc)
            raise
        finally:
            await self._cleanup_state(state)

    async def _verify_published(
        self,
        key: ArtifactKey,
        expected: ArtifactIdentity,
    ) -> None:
        actual = await self.artifacts.identity(key)
        if actual is None or not expected.matches(actual):
            raise ArtifactStoreError(
                f"published image artifact verification failed key={key.value}"
            )
