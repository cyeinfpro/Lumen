from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from lumen_core.capacity_leases import (
    CapacityLeaseGuard,
    assert_capacity_leases_owned,
    race_with_capacity_leases,
)
from lumen_core.constants import ImageSource, ImageVisibility
from lumen_core.model_base import new_uuid7
from lumen_core.model_entities import Image
from lumen_core.storage_capacity import StorageCapacityExceeded

from ..adapters.sqlalchemy_repository import SQLAlchemyImageRepository
from ..domain.artifact import (
    ArtifactKey,
    ArtifactManifestItem,
    ArtifactStatus,
    StagedArtifact,
    UploadTicket,
)
from ..metrics import record_capacity_reservation_ratio
from ..ports.image_processing import (
    ImageProcessingExecutorPort,
    ImageProcessingRequest,
)
from ..processing.service import PreparedUpload


class _ProcessingStage:
    def __init__(self, staged: StagedArtifact) -> None:
        self.path = Path(staged.path)
        self.size_bytes = staged.identity.size_bytes
        self.sha256 = staged.identity.sha256
        self.lease = None
        self._owned = []

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
class UploadExecutionState:
    ticket: UploadTicket
    staged: StagedArtifact | None = None
    processing_stage: _ProcessingStage | None = None
    image_id: str | None = None
    publishing_started: bool = False


@dataclass(frozen=True)
class UploadProcessingContext:
    lease_guard: CapacityLeaseGuard
    user_id: str
    session_id: str | None
    filename: str | None
    inspection: Any
    policy: Any
    metadata_profile: str | None
    metadata_finalizer: Callable[[str, str, dict[str, Any]], None] | None
    storage_guard: Callable[[int], None] | None
    storage_lease_guard: CapacityLeaseGuard | None
    storage_reservation_bytes: int | None

    @property
    def lease_guards(self) -> tuple[CapacityLeaseGuard, ...]:
        return tuple(
            guard
            for guard in (self.lease_guard, self.storage_lease_guard)
            if guard is not None
        )


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


def published_manifest(
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


class UploadProcessingOperation:
    def __init__(
        self,
        *,
        repository: SQLAlchemyImageRepository,
        processing_executor: ImageProcessingExecutorPort,
    ) -> None:
        self.repository = repository
        self.processing_executor = processing_executor

    async def run(
        self,
        state: UploadExecutionState,
        context: UploadProcessingContext,
    ) -> tuple[ArtifactKey, ArtifactKey, PreparedUpload]:
        assert state.staged is not None
        extension = context.policy.extensions[context.inspection.output_mime]
        state.image_id = new_uuid7()
        original_key, normalized_key = self._artifact_keys(
            context.user_id,
            state.image_id,
            extension,
        )
        await self._create_staging_row(
            state,
            context,
            original_key=original_key,
            normalized_key=normalized_key,
        )
        await self._transition(
            state,
            context,
            expected=ArtifactStatus.STAGING,
            target=ArtifactStatus.PROCESSING,
        )
        stage = _ProcessingStage(state.staged)
        state.processing_stage = stage
        prepared = await self._process(state, context, stage)
        self._validate_storage_reservation(state, context, prepared)
        metadata = self._metadata(context, state, prepared, normalized_key, extension)
        manifest = self._prepared_manifest(
            state.ticket,
            original_key,
            normalized_key,
            prepared,
        )
        await self._transition(
            state,
            context,
            expected=ArtifactStatus.PROCESSING,
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
                "reconcile_after": datetime.now(timezone.utc) + timedelta(minutes=2),
                "last_artifact_error": None,
            },
        )
        state.publishing_started = True
        return original_key, normalized_key, prepared

    @staticmethod
    def _artifact_keys(
        user_id: str,
        image_id: str,
        extension: str,
    ) -> tuple[ArtifactKey, ArtifactKey]:
        return (
            ArtifactKey(f"u/{user_id}/uploads/{image_id}.{extension}"),
            ArtifactKey(f"u/{user_id}/uploads/{image_id}.ref.webp"),
        )

    async def _create_staging_row(
        self,
        state: UploadExecutionState,
        context: UploadProcessingContext,
        *,
        original_key: ArtifactKey,
        normalized_key: ArtifactKey,
    ) -> None:
        assert state.staged is not None
        assert state.image_id is not None
        image = Image(
            id=state.image_id,
            user_id=context.user_id,
            source=ImageSource.UPLOADED.value,
            storage_key=original_key.value,
            mime=context.inspection.output_mime,
            width=context.inspection.width,
            height=context.inspection.height,
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
        await assert_capacity_leases_owned(context.lease_guards)
        if context.session_id:
            await self.repository.create_staging(image, session_id=context.session_id)
        else:
            await self.repository.create_staging(image)
        await assert_capacity_leases_owned(context.lease_guards)

    async def _transition(
        self,
        state: UploadExecutionState,
        context: UploadProcessingContext,
        *,
        expected: ArtifactStatus,
        target: ArtifactStatus,
        values: dict[str, Any] | None = None,
    ) -> None:
        assert state.image_id is not None
        transition_kwargs: dict[str, Any] = {
            "expected": [expected],
            "target": target,
        }
        if values is not None:
            transition_kwargs["values"] = values
        if context.session_id:
            transition_kwargs.update(
                active_user_id=context.user_id,
                session_id=context.session_id,
            )
        await self.repository.transition(state.image_id, **transition_kwargs)

    async def _process(
        self,
        state: UploadExecutionState,
        context: UploadProcessingContext,
        stage: _ProcessingStage,
    ) -> PreparedUpload:
        assert state.staged is not None
        output_paths = self._output_paths(stage, context)
        return await race_with_capacity_leases(
            self.processing_executor.process(
                ImageProcessingRequest(
                    source_path=Path(state.staged.path),
                    source_size_bytes=state.staged.identity.size_bytes,
                    source_sha256=state.staged.identity.sha256,
                    filename=context.filename,
                    allowed_mime=frozenset(context.policy.allowed_mime),
                    normalizable_mime=frozenset(context.policy.normalizable_mime),
                    max_bytes=context.policy.max_bytes,
                    max_pixels=context.policy.max_pixels,
                    max_long_side=context.policy.max_long_side,
                    mask_requested=context.policy.mask_requested,
                    reference_size=context.policy.reference_size,
                    metadata_profile=context.metadata_profile,
                    output_paths=tuple(output_paths),
                )
            ),
            context.lease_guards,
        )

    @staticmethod
    def _output_paths(
        stage: _ProcessingStage,
        context: UploadProcessingContext,
    ) -> list[Path]:
        output_paths: list[Path] = []
        if context.inspection.mime in context.policy.normalizable_mime:
            output_paths.append(stage.new_temp_path(suffix=".normalized.jpg"))
        output_paths.append(stage.new_temp_path(suffix=".ref.webp"))
        return output_paths

    @staticmethod
    def _validate_storage_reservation(
        state: UploadExecutionState,
        context: UploadProcessingContext,
        prepared: PreparedUpload,
    ) -> None:
        assert state.staged is not None
        transient_bytes = state.staged.identity.size_bytes + 2 * (
            prepared.size_bytes + int(prepared.normalized_ref_meta["bytes"])
        )
        if context.storage_reservation_bytes is not None:
            record_capacity_reservation_ratio(
                reserved_bytes=context.storage_reservation_bytes,
                actual_bytes=transient_bytes,
            )
        if (
            context.storage_reservation_bytes is not None
            and transient_bytes > context.storage_reservation_bytes
        ):
            raise StorageCapacityExceeded(
                "image processing exceeded its storage reservation"
            )

    @staticmethod
    def _metadata(
        context: UploadProcessingContext,
        state: UploadExecutionState,
        prepared: PreparedUpload,
        normalized_key: ArtifactKey,
        extension: str,
    ) -> dict[str, Any]:
        assert state.image_id is not None
        metadata = dict(prepared.metadata)
        if context.storage_guard is not None:
            context.storage_guard(
                prepared.size_bytes + int(prepared.normalized_ref_meta["bytes"])
            )
        metadata["normalized_ref"] = {
            **prepared.normalized_ref_meta,
            "storage_key": normalized_key.value,
        }
        if context.metadata_finalizer is not None:
            context.metadata_finalizer(state.image_id, extension, metadata)
        return metadata

    @staticmethod
    def _prepared_manifest(
        ticket: UploadTicket,
        original_key: ArtifactKey,
        normalized_key: ArtifactKey,
        prepared: PreparedUpload,
    ) -> dict[str, Any]:
        return published_manifest(
            ticket,
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
