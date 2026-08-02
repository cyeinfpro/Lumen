from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from lumen_core.capacity_leases import (
    CapacityLeaseGuard,
    CapacityLeaseLost,
    assert_capacity_leases_owned,
    maintained_capacity_lease,
    race_with_capacity_leases,
)
from lumen_core.model_base import new_uuid7
from lumen_core.models import Image
from lumen_core.storage_capacity import (
    StorageCapacityExceeded,
    StorageCapacityLeasePort,
    StorageCapacityPort,
    StorageCapacityUnavailable,
)

from ...services.active_user import (
    ActiveSessionExpired,
    ActiveSessionRevoked,
    ActiveUserDeleted,
    ActiveUserFenceError,
)
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
    UploadTicket,
)
from ..domain.resource_estimate import ImageResourceEstimate
from ..ports.artifact_store import ArtifactStorePort
from ..ports.capacity import CapacityPort
from ..ports.image_processing import ImageProcessingExecutorPort
from ..processing.service import PreparedUpload, ProcessingError
from .upload_processing import (
    UploadExecutionState,
    UploadProcessingContext,
    UploadProcessingOperation,
    published_manifest,
)


logger = logging.getLogger(__name__)

_INITIAL_STORAGE_SAFETY_MIN_BYTES = 1024 * 1024
_INITIAL_STORAGE_SAFETY_MAX_BYTES = 8 * 1024 * 1024


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


@dataclass(frozen=True)
class _ReservedUploadContext:
    user_id: str
    session_id: str | None
    upload_file: Any
    filename: str | None
    policy: UploadPolicy
    metadata_profile: str | None
    metadata_finalizer: Callable[[str, str, dict[str, Any]], None] | None
    storage_guard: Callable[[int], None] | None
    storage_lease: StorageCapacityLeasePort | None
    storage_lease_guard: CapacityLeaseGuard | None
    storage_reservation_bytes: int | None


def _active_user_upload_error(error: ActiveUserFenceError) -> UploadCommandError:
    if isinstance(error, ActiveSessionExpired):
        code, message = "session_expired", "session expired"
    elif isinstance(error, ActiveSessionRevoked):
        code, message = "session_revoked", "session was revoked"
    elif isinstance(error, ActiveUserDeleted):
        code, message = "user_deleted", "user account was deleted"
    else:  # pragma: no cover - every active-user fence error is mapped above.
        code, message = "session_revoked", "session was revoked"
    return UploadCommandError(code, message, 401)


def _artifact_store_upload_error(error: ArtifactStoreError) -> UploadCommandError:
    if "maximum bytes" in str(error):
        code, status = "too_large", 413
    elif "empty upload" in str(error):
        code, status = "empty_file", 400
    else:
        code, status = "upload_storage_error", 503
    return UploadCommandError(code, str(error), status)


def _map_upload_error(error: Exception) -> UploadCommandError | None:
    if isinstance(error, (CapacityExceeded, CapacityUnavailable, CapacityLeaseLost)):
        return UploadCommandError(
            "upload_capacity_exceeded",
            "image upload capacity is temporarily exhausted",
            503,
        )
    if isinstance(error, StorageCapacityExceeded):
        return UploadCommandError(
            "storage_insufficient_space",
            "image processing exceeded available storage",
            507,
        )
    if isinstance(error, StorageCapacityUnavailable):
        return UploadCommandError(
            "storage_capacity_unavailable",
            "image storage capacity is temporarily unavailable",
            503,
        )
    if isinstance(error, ProcessingError):
        return UploadCommandError(error.code, error.message, error.status_code)
    if isinstance(error, ActiveUserFenceError):
        return _active_user_upload_error(error)
    if isinstance(error, ArtifactStoreError):
        return _artifact_store_upload_error(error)
    if isinstance(error, FileExistsError):
        return UploadCommandError(
            "storage_conflict",
            "image storage key already exists",
            409,
        )
    return None


def _initial_storage_reservation_bytes(max_bytes: int) -> int:
    source_limit = max(0, int(max_bytes))
    safety_margin = max(
        _INITIAL_STORAGE_SAFETY_MIN_BYTES,
        min(_INITIAL_STORAGE_SAFETY_MAX_BYTES, source_limit // 4),
    )
    return source_limit + safety_margin


def _processing_storage_reservation_bytes(
    staged_bytes: int,
    inspection: Any,
) -> int:
    return max(
        0,
        int(staged_bytes) + int(inspection.estimate.output_reserve_bytes),
    )


class UploadCommandService:
    def __init__(
        self,
        *,
        artifacts: ArtifactStorePort,
        capacity: CapacityPort,
        storage_capacity: StorageCapacityPort,
        repository: SQLAlchemyImageRepository,
        processing_executor: ImageProcessingExecutorPort,
        storage_lease_ttl_seconds: float | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.capacity = capacity
        self.storage_capacity = storage_capacity
        self.repository = repository
        self.processing_executor = processing_executor
        self._processing_operation = UploadProcessingOperation(
            repository=repository,
            processing_executor=processing_executor,
        )
        self.storage_lease_ttl_seconds = (
            CapacityLimits.from_env().lease_ttl_seconds
            if storage_lease_ttl_seconds is None
            else storage_lease_ttl_seconds
        )

    async def aclose(self) -> None:
        await self.processing_executor.aclose()

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
        state: UploadExecutionState,
        *,
        upload_file: Any,
        policy: UploadPolicy,
        storage_guard: Callable[[int], None] | None,
        storage_lease_guard: CapacityLeaseGuard | None,
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
        ) as lease_guard:
            guards = tuple(
                guard
                for guard in (lease_guard, storage_lease_guard)
                if guard is not None
            )
            state.staged = await race_with_capacity_leases(
                self.artifacts.stage(
                    state.ticket,
                    self._source_chunks(upload_file),
                    max_bytes=policy.max_bytes,
                ),
                guards,
            )
            if storage_guard is not None:
                storage_guard(state.staged.identity.size_bytes)
            return await race_with_capacity_leases(
                self.processing_executor.inspect(
                    Path(state.staged.path),
                    upload_bytes=state.staged.identity.size_bytes,
                    allowed_mime=policy.allowed_mime,
                    normalizable_mime=policy.normalizable_mime,
                    max_pixels=policy.max_pixels,
                    max_long_side=policy.max_long_side,
                ),
                guards,
            )

    async def _process_and_persist(
        self,
        state: UploadExecutionState,
        *,
        context: UploadProcessingContext,
    ) -> tuple[ArtifactKey, ArtifactKey, PreparedUpload]:
        return await self._processing_operation.run(state, context)

    async def _run_reserved(
        self,
        state: UploadExecutionState,
        context: _ReservedUploadContext,
    ) -> Image:
        inspection = await self._stage_and_inspect(
            state,
            upload_file=context.upload_file,
            policy=context.policy,
            storage_guard=context.storage_guard,
            storage_lease_guard=context.storage_lease_guard,
        )
        storage_reservation_bytes = await self._resize_storage_lease(
            state,
            inspection=inspection,
            storage_lease=context.storage_lease,
            storage_lease_guard=context.storage_lease_guard,
            storage_reservation_bytes=context.storage_reservation_bytes,
        )
        processing_lease = await self.capacity.reserve(inspection.estimate)
        async with maintained_capacity_lease(
            processing_lease,
            ttl_seconds=CapacityLimits.from_env().lease_ttl_seconds,
        ) as lease_guard:
            processing_context = UploadProcessingContext(
                lease_guard=lease_guard,
                user_id=context.user_id,
                session_id=context.session_id,
                filename=context.filename,
                inspection=inspection,
                policy=context.policy,
                metadata_profile=context.metadata_profile,
                metadata_finalizer=context.metadata_finalizer,
                storage_guard=context.storage_guard,
                storage_lease_guard=context.storage_lease_guard,
                storage_reservation_bytes=storage_reservation_bytes,
            )
            original_key, normalized_key, prepared = await self._process_and_persist(
                state,
                context=processing_context,
            )
            return await self._publish_and_mark_ready(
                state,
                lease_guard=lease_guard,
                user_id=context.user_id,
                session_id=context.session_id,
                original_key=original_key,
                normalized_key=normalized_key,
                prepared=prepared,
                storage_lease_guard=context.storage_lease_guard,
            )

    @staticmethod
    async def _resize_storage_lease(
        state: UploadExecutionState,
        *,
        inspection: Any,
        storage_lease: StorageCapacityLeasePort | None,
        storage_lease_guard: CapacityLeaseGuard | None,
        storage_reservation_bytes: int | None,
    ) -> int | None:
        if storage_lease is None or storage_lease_guard is None or state.staged is None:
            return storage_reservation_bytes
        resized_bytes = _processing_storage_reservation_bytes(
            state.staged.identity.size_bytes,
            inspection,
        )
        if not await storage_lease.resize(resized_bytes):
            storage_lease_guard.mark_lost()
            raise CapacityLeaseLost("storage capacity lease ownership changed")
        await storage_lease_guard.assert_owned()
        return resized_bytes

    async def _publish_and_mark_ready(
        self,
        state: UploadExecutionState,
        *,
        lease_guard: CapacityLeaseGuard,
        user_id: str,
        session_id: str | None,
        original_key: ArtifactKey,
        normalized_key: ArtifactKey,
        prepared: PreparedUpload,
        storage_lease_guard: CapacityLeaseGuard | None,
    ) -> Image:
        assert state.image_id is not None
        guards = tuple(
            guard for guard in (lease_guard, storage_lease_guard) if guard is not None
        )
        await assert_capacity_leases_owned(guards)
        async with self._active_user_fence(user_id, session_id=session_id):
            original = await race_with_capacity_leases(
                self.artifacts.publish_path(
                    prepared.original_path,
                    original_key,
                    expected=prepared.original_identity,
                ),
                guards,
            )
            await assert_capacity_leases_owned(guards)
            normalized_ref = await race_with_capacity_leases(
                self.artifacts.publish_path(
                    prepared.normalized_ref_path,
                    normalized_key,
                    expected=prepared.normalized_ref_identity,
                ),
                guards,
            )
            await self._verify_published(original.key, original.identity)
            await self._verify_published(normalized_ref.key, normalized_ref.identity)
        manifest = published_manifest(
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
        await assert_capacity_leases_owned(guards)
        transition_kwargs = {
            "expected": [ArtifactStatus.PUBLISHING],
            "target": ArtifactStatus.READY,
            "values": {
                "artifact_manifest_jsonb": manifest,
                "reconcile_after": None,
                "last_artifact_error": None,
                "ready_at": datetime.now(timezone.utc),
            },
        }
        if session_id:
            transition_kwargs.update(
                active_user_id=user_id,
                session_id=session_id,
            )
        return await self.repository.transition(
            state.image_id,
            **transition_kwargs,
        )

    async def _handle_failure(
        self,
        state: UploadExecutionState,
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

    async def _cleanup_state(self, state: UploadExecutionState) -> None:
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

    @asynccontextmanager
    async def _active_user_fence(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
    ) -> AsyncIterator[None]:
        fence = getattr(self.repository, "active_user_fence", None)
        if not callable(fence):
            yield
            return
        if session_id:
            async with fence(user_id, session_id=session_id):
                yield
            return
        async with fence(user_id):
            yield

    async def _execute_reserved(
        self,
        *,
        user_id: str,
        session_id: str | None,
        upload_file: Any,
        filename: str | None,
        policy: UploadPolicy,
        metadata_profile: str | None = None,
        metadata_finalizer: Callable[[str, str, dict[str, Any]], None] | None = None,
        storage_guard: Callable[[int], None] | None = None,
        storage_lease: StorageCapacityLeasePort | None = None,
        storage_lease_guard: CapacityLeaseGuard | None = None,
        storage_reservation_bytes: int | None = None,
    ) -> Image:
        state = UploadExecutionState(ticket=UploadTicket(new_uuid7()))
        try:
            return await self._run_reserved(
                state,
                _ReservedUploadContext(
                    user_id=user_id,
                    session_id=session_id,
                    upload_file=upload_file,
                    filename=filename,
                    policy=policy,
                    metadata_profile=metadata_profile,
                    metadata_finalizer=metadata_finalizer,
                    storage_guard=storage_guard,
                    storage_lease=storage_lease,
                    storage_lease_guard=storage_lease_guard,
                    storage_reservation_bytes=storage_reservation_bytes,
                ),
            )
        except Exception as exc:
            await self._handle_failure(state, exc)
            mapped = _map_upload_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        finally:
            await self._cleanup_state(state)

    async def execute(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        upload_file: Any,
        filename: str | None,
        policy: UploadPolicy,
        metadata_profile: str | None = None,
        metadata_finalizer: Callable[[str, str, dict[str, Any]], None] | None = None,
        storage_guard: Callable[[int], None] | None = None,
    ) -> Image:
        reserved_bytes = _initial_storage_reservation_bytes(policy.max_bytes)
        try:
            storage_lease = await self.storage_capacity.reserve(reserved_bytes)
        except StorageCapacityExceeded as exc:
            raise UploadCommandError(
                "storage_insufficient_space",
                "not enough free storage to accept this upload",
                507,
            ) from exc
        except StorageCapacityUnavailable as exc:
            raise UploadCommandError(
                "storage_capacity_unavailable",
                "image storage capacity is temporarily unavailable",
                503,
            ) from exc

        try:
            async with maintained_capacity_lease(
                storage_lease,
                ttl_seconds=self.storage_lease_ttl_seconds,
            ) as storage_lease_guard:
                return await self._execute_reserved(
                    user_id=user_id,
                    session_id=session_id,
                    upload_file=upload_file,
                    filename=filename,
                    policy=policy,
                    metadata_profile=metadata_profile,
                    metadata_finalizer=metadata_finalizer,
                    storage_guard=storage_guard,
                    storage_lease=storage_lease,
                    storage_lease_guard=storage_lease_guard,
                    storage_reservation_bytes=reserved_bytes,
                )
        except CapacityLeaseLost as exc:
            raise UploadCommandError(
                "storage_capacity_unavailable",
                "image storage capacity lease was lost",
                503,
            ) from exc

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
