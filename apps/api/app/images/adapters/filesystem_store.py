from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..metrics import (
    record_publish_conflict,
    record_publish_idempotent_winner,
    record_staged_sweep_tombstone,
    record_upload_writer,
)
from .filesystem_staging import (
    ArtifactIdentityMismatch,
    ArtifactStoreError,
    FileFingerprint as _FileFingerprint,
    HashAttempt as _HashAttempt,
    LegacyPage as _LegacyPage,
    RecordOutcome as _RecordOutcome,
    ScanPage as _ScanPage,
    StagedRecord as _StagedRecord,
    SweepCursor as _SweepCursor,
    SweepProgress as _SweepProgress,
    file_fingerprint as _fingerprint,
    hash_staged_file as _hash_staged_file,
)
from .filesystem_store_parts.objects import (
    CHUNK_SIZE as _CHUNK_SIZE,
    FileSystemObjectsMixin,
    artifact_identity as _identity,
    fsync_directory as _fsync_directory,
    hash_file as _hash_file,
)
from .filesystem_store_parts.publish import (
    LINK_UNSUPPORTED_ERRNOS as _LINK_UNSUPPORTED_ERRNOS,
    PUBLISH_LOCK_FILE as _PUBLISH_LOCK_FILE,
    ArtifactConflict,
    FileSystemPublishMixin,
    publish_directory_lock as _publish_directory_lock,
    publish_file,
)
from .filesystem_store_parts.reconcile import FileSystemReconcileMixin
from .filesystem_store_parts.retention import FileSystemRetentionMixin
from .filesystem_store_parts.staging import (
    MAX_METADATA_BYTES as _MAX_METADATA_BYTES,
    STAGE_FILE_PATTERN as _STAGE_FILE_PATTERN,
    STAGED_CURSOR_FILE as _STAGED_CURSOR_FILE,
    STAGED_INDEX_DIRECTORY as _STAGED_INDEX_DIRECTORY,
    STAGED_LEGACY_SLOT as _STAGED_LEGACY_SLOT,
    STAGED_QUARANTINE_DIRECTORY as _STAGED_QUARANTINE_DIRECTORY,
    STAGED_SHARD_COUNT as _STAGED_SHARD_COUNT,
    STAGED_SLOT_COUNT as _STAGED_SLOT_COUNT,
    FileSystemStagingMixin,
)
from .filesystem_store_parts.variants import FileSystemVariantsMixin
from .filesystem_writer import (
    StageFileWriter as _StageFileWriter,
    write_all as _write_all,
)

__all__ = (
    "ArtifactConflict",
    "ArtifactIdentityMismatch",
    "ArtifactStoreError",
    "FileSystemArtifactStore",
    "_CHUNK_SIZE",
    "_FileFingerprint",
    "_HashAttempt",
    "_LegacyPage",
    "_LINK_UNSUPPORTED_ERRNOS",
    "_MAX_METADATA_BYTES",
    "_PUBLISH_LOCK_FILE",
    "_RecordOutcome",
    "_STAGED_CURSOR_FILE",
    "_STAGED_INDEX_DIRECTORY",
    "_STAGED_LEGACY_SLOT",
    "_STAGED_QUARANTINE_DIRECTORY",
    "_STAGED_SHARD_COUNT",
    "_STAGED_SLOT_COUNT",
    "_STAGE_FILE_PATTERN",
    "_ScanPage",
    "_StageFileWriter",
    "_StagedRecord",
    "_SweepCursor",
    "_SweepProgress",
    "_fingerprint",
    "_fsync_directory",
    "_hash_file",
    "_hash_staged_file",
    "_identity",
    "_publish_directory_lock",
    "_write_all",
    "publish_file_sync",
)


class FileSystemArtifactStore(
    FileSystemRetentionMixin,
    FileSystemReconcileMixin,
    FileSystemVariantsMixin,
    FileSystemPublishMixin,
    FileSystemStagingMixin,
    FileSystemObjectsMixin,
):
    """Compatibility facade for filesystem artifact responsibilities."""

    @staticmethod
    def _hash_staged_file(
        path: Path,
        *,
        max_bytes: int,
        deadline: float,
        monotonic: Callable[[], float],
    ) -> _HashAttempt:
        return _hash_staged_file(
            path,
            max_bytes=max_bytes,
            deadline=deadline,
            monotonic=monotonic,
        )

    @staticmethod
    def _record_publish_conflict(backend: str) -> None:
        record_publish_conflict(backend)

    @staticmethod
    def _record_publish_idempotent_winner(backend: str) -> None:
        record_publish_idempotent_winner(backend)

    @staticmethod
    def _record_staged_sweep_tombstone() -> None:
        record_staged_sweep_tombstone()

    @staticmethod
    def _record_upload_writer(
        *,
        upload_bytes: int,
        queue_wait_seconds: float,
        duration_seconds: float,
    ) -> None:
        record_upload_writer(
            upload_bytes=upload_bytes,
            queue_wait_seconds=queue_wait_seconds,
            duration_seconds=duration_seconds,
        )


def publish_file_sync(
    source: Path,
    destination: Path,
) -> None:
    publish_file(
        source,
        destination,
        store_factory=FileSystemArtifactStore,
    )
