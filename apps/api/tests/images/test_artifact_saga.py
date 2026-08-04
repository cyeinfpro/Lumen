from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from PIL import Image as PILImage
from sqlalchemy import create_engine, inspect, text

from app.images.adapters import filesystem_store as filesystem_store_module
from app.images.adapters.filesystem_store import ArtifactStoreError
from app.images.adapters.filesystem_store import (
    ArtifactConflict,
    FileSystemArtifactStore,
)
from app.images.adapters.filesystem_store_parts import (
    atomic_publish as filesystem_atomic_publish_module,
)
from app.images.adapters.filesystem_store_parts import (
    objects as filesystem_objects_module,
)
from app.images.adapters.filesystem_store_parts import (
    publish as filesystem_publish_module,
)
from app.images.adapters.local_capacity import (
    CapacityExceeded,
    CapacityLimits,
    ScaledLocalCapacity,
)
from app.images.adapters.redis_capacity import RedisCapacity
from app.images.adapters.sqlalchemy_repository import SQLAlchemyImageRepository
from app.images.application.reconcile_policy import ImageArtifactReconciler
from app.images.application.upload import (
    UploadCommandError,
    UploadCommandService,
    UploadPolicy,
)
from lumen_core.capacity_leases import (
    CapacityLeaseGuard,
    race_with_capacity_lease,
)
from app.images.domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    ArtifactStatus,
    InvalidArtifactTransition,
    StagedSweepBudget,
    UploadTicket,
    ensure_artifact_transition,
)
from app.images.domain.resource_estimate import (
    ImageResourceEstimate,
    estimate_image_resources,
)
from app.images.processing.isolated import IsolatedImageProcessingExecutor
from app.images.processing.service import ImageProcessor
from lumen_core.models import Image


ROOT = Path(__file__).resolve().parents[4]
MIGRATION = ROOT / "apps/api/alembic/versions/0046_image_artifact_status.py"


class _Upload:
    filename = "upload.png"

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.sent = False

    async def read(self, _size: int) -> bytes:
        if self.sent:
            return b""
        self.sent = True
        return self.payload


class _Lease:
    def __init__(self) -> None:
        self.released = False

    async def renew(self) -> bool:
        return not self.released

    async def resize(self, _bytes_required: int) -> bool:
        return not self.released

    async def release(self) -> None:
        self.released = True


class _Capacity:
    async def reserve(self, _estimate: ImageResourceEstimate) -> _Lease:
        return _Lease()


class _Repository:
    def __init__(self) -> None:
        self.rows: dict[str, Image] = {}
        self.adopt_calls = 0

    def _touch(self, row: Image) -> Image:
        now = datetime.now(timezone.utc)
        if getattr(row, "created_at", None) is None:
            row.created_at = now
        row.updated_at = now
        return row

    async def create_staging(self, image: Image) -> Image:
        self.rows[image.id] = image
        return self._touch(image)

    async def get(self, image_id: str) -> Image | None:
        return self.rows.get(image_id)

    async def transition(
        self,
        image_id: str,
        *,
        expected: list[ArtifactStatus],
        target: ArtifactStatus,
        values: dict[str, Any] | None = None,
        reconcile_fence: int | None = None,
    ) -> Image:
        del reconcile_fence
        row = self.rows[image_id]
        assert ArtifactStatus(row.artifact_status) in expected
        for key, value in (values or {}).items():
            setattr(row, key, value)
        row.artifact_status = target.value
        return self._touch(row)

    async def update_publishing(
        self,
        image_id: str,
        *,
        values: dict[str, Any],
    ) -> Image:
        row = self.rows[image_id]
        assert row.artifact_status == ArtifactStatus.PUBLISHING.value
        for key, value in values.items():
            setattr(row, key, value)
        return self._touch(row)

    async def update_ready(
        self,
        image_id: str,
        *,
        values: dict[str, Any],
        reconcile_fence: int | None = None,
    ) -> Image:
        del reconcile_fence
        row = self.rows[image_id]
        assert row.artifact_status == ArtifactStatus.READY.value
        for key, value in values.items():
            setattr(row, key, value)
        return self._touch(row)

    async def create_storage_intent(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        session_id: str | None = None,
    ) -> Image:
        del session_id
        row = self.rows[image_id]
        assert row.user_id == user_id
        assert row.artifact_status == ArtifactStatus.PUBLISHING.value
        manifest = dict(row.artifact_manifest_jsonb or {})
        manifest["storage_intent"] = {
            "version": 1,
            "state": "pending",
            "user_id": user_id,
            "image_id": image_id,
            "token": token,
        }
        row.artifact_manifest_jsonb = manifest
        return self._touch(row)

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
        del session_id
        self.adopt_calls += 1
        row = self.rows[image_id]
        intent = (row.artifact_manifest_jsonb or {}).get("storage_intent", {})
        assert row.user_id == user_id
        assert intent.get("token") == token
        adopted_manifest = dict(manifest)
        adopted_manifest["storage_intent"] = {
            "version": 1,
            "state": "adopted",
            "user_id": user_id,
            "image_id": image_id,
            "token": token,
        }
        row.artifact_manifest_jsonb = adopted_manifest
        row.artifact_status = ArtifactStatus.READY.value
        row.ready_at = ready_at
        row.reconcile_after = None
        row.last_artifact_error = None
        return self._touch(row)

    async def record_storage_intent_failure(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        error_message: str,
        retry_at: datetime | None,
    ) -> bool:
        row = self.rows[image_id]
        intent = (row.artifact_manifest_jsonb or {}).get("storage_intent", {})
        if (
            row.user_id != user_id
            or row.artifact_status != ArtifactStatus.PUBLISHING.value
            or intent.get("token") != token
        ):
            return False
        row.last_artifact_error = error_message
        if retry_at is not None:
            row.reconcile_after = retry_at
        self._touch(row)
        return True

    async def abandon_storage_intent(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        error_message: str,
    ) -> bool:
        row = self.rows[image_id]
        intent = (row.artifact_manifest_jsonb or {}).get("storage_intent", {})
        if row.user_id != user_id or intent.get("token") != token:
            return False
        row.artifact_status = ArtifactStatus.FAILED.value
        row.deleted_at = datetime.now(timezone.utc)
        row.last_artifact_error = error_message
        row.reconcile_after = None
        self._touch(row)
        return True

    async def list_reconcile_candidates(self, **_kwargs: Any) -> list[Image]:
        return list(self.rows.values())

    async def active_upload_tickets(
        self,
        candidate_tickets: set[str] | None = None,
    ) -> set[str]:
        tickets: set[str] = set()
        for row in self.rows.values():
            if row.artifact_status not in {
                ArtifactStatus.STAGING.value,
                ArtifactStatus.PROCESSING.value,
                ArtifactStatus.PUBLISHING.value,
            }:
                continue
            manifest = row.artifact_manifest_jsonb or {}
            ticket = manifest.get("ticket")
            if isinstance(ticket, str):
                if candidate_tickets is None or ticket in candidate_tickets:
                    tickets.add(ticket)
        return tickets


class _FailSecondPublish:
    def __init__(self, delegate: FileSystemArtifactStore) -> None:
        self.delegate = delegate
        self.publish_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def publish_path(self, *args: Any, **kwargs: Any) -> Any:
        self.publish_calls += 1
        if self.publish_calls == 2:
            raise OSError("crash after original publish")
        return await self.delegate.publish_path(*args, **kwargs)


class _FakeRedis:
    def __init__(self) -> None:
        self.weights: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.now_ms = 1_000_000

    async def eval(self, script: str, _keys: int, *args: str) -> Any:
        if "ZRANGEBYSCORE" in script:
            now_ms = self.now_ms
            expired = [
                lease_id
                for lease_id, expiry in self.expiries.items()
                if expiry <= now_ms
            ]
            for lease_id in expired:
                self.expiries.pop(lease_id, None)
                self.weights.pop(lease_id, None)
            lease_id = args[2]
            weight = int(args[3])
            max_count = int(args[4])
            max_weight = int(args[5])
            ttl_ms = int(args[6])
            used = sum(self.weights.values())
            if len(self.weights) >= max_count or used + weight > max_weight:
                return [0, len(self.weights), used]
            self.expiries[lease_id] = now_ms + ttl_ms
            self.weights[lease_id] = weight
            return [1, len(self.weights), sum(self.weights.values())]
        if "HEXISTS" in script:
            lease_id = args[2]
            if lease_id not in self.weights:
                return 0
            self.expiries[lease_id] = self.now_ms + int(args[3])
            return 1
        lease_id = args[2]
        self.expiries.pop(lease_id, None)
        self.weights.pop(lease_id, None)
        return 1


def _png_bytes(size: tuple[int, int] = (32, 32)) -> bytes:
    output = io.BytesIO()
    PILImage.new("RGBA", size, (10, 20, 30, 128)).save(output, format="PNG")
    return output.getvalue()


def _policy() -> UploadPolicy:
    return UploadPolicy(
        allowed_mime={"image/png", "image/jpeg", "image/webp"},
        normalizable_mime={"image/mpo", "image/x-mpo"},
        extensions={
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
        },
        max_bytes=10 * 1024 * 1024,
        max_pixels=64_000_000,
        max_long_side=4096,
        mask_requested=False,
        reference_size=None,
    )


def _identity_for(payload: bytes) -> ArtifactIdentity:
    return ArtifactIdentity(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def _publish_temp_paths(destination: Path) -> list[Path]:
    return sorted(
        path
        for path in destination.parent.iterdir()
        if path.name.startswith(filesystem_publish_module.PUBLISH_TEMP_PREFIX)
        and path.name.endswith(filesystem_publish_module.PUBLISH_TEMP_SUFFIX)
    )


class _OSProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


class _FailingDirectoryOpenOS(_OSProxy):
    def __init__(self, directory: Path, error: int = errno.EIO) -> None:
        self.directory = directory
        self.error = error

    def open(
        self,
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if dir_fd is None and Path(path) == self.directory:
            raise OSError(
                self.error,
                "injected directory sync open failure",
                path,
            )
        return os.open(path, flags, mode, dir_fd=dir_fd)


class _SourceLinkUnsupportedOS(_OSProxy):
    def __init__(self, source: Path) -> None:
        self.source = source

    def link(
        self,
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if Path(source) == self.source:
            raise OSError(errno.EXDEV, "cross-device link")
        os.link(source, destination, *args, **kwargs)


def _fail_directory_sync_open(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    *,
    error: int = errno.EIO,
) -> None:
    monkeypatch.setattr(
        filesystem_objects_module,
        "os",
        _FailingDirectoryOpenOS(directory, error),
    )


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "image_artifact_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_artifact_store_persists_metadata_and_identity_deletes_staged_file(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(tmp_path)

    async def source():
        yield b"staged"

    staged = await store.stage(
        ticket=UploadTicket("ticket-1"),
        source=source(),
        max_bytes=100,
    )
    assert Path(staged.path).name.startswith("artifact-v1-")
    assert staged.created_at is not None
    assert staged.metadata_path is not None
    assert Path(staged.metadata_path).is_file()
    assert await store.delete_staged(staged) is True
    assert not Path(staged.path).exists()
    assert not Path(staged.metadata_path).exists()

    result = await store.sweep_staged(
        active_tickets=set(),
        stale_before=float("inf"),
        budget=StagedSweepBudget(
            max_files_per_pass=1,
            max_bytes_hashed_per_pass=100,
            max_seconds_per_pass=1,
        ),
    )
    assert result.deleted == 0


@pytest.mark.asyncio
async def test_artifact_stage_uses_one_blocking_writer_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_calls = 0
    original_run = filesystem_store_module._StageFileWriter._run_sync

    def counted_run(writer: Any) -> None:
        nonlocal run_calls
        run_calls += 1
        original_run(writer)

    monkeypatch.setattr(
        filesystem_store_module._StageFileWriter,
        "_run_sync",
        counted_run,
    )
    chunks = [bytes([index % 251]) * 64 * 1024 for index in range(32)]

    async def source():
        for chunk in chunks:
            yield chunk

    store = FileSystemArtifactStore(tmp_path)
    staged = await store.stage(
        ticket=UploadTicket("ticket-writer"),
        source=source(),
        max_bytes=sum(map(len, chunks)),
    )

    assert run_calls == 1
    assert Path(staged.path).read_bytes() == b"".join(chunks)


@pytest.mark.asyncio
async def test_artifact_store_rejects_symlink_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = tmp_path / "u"
    symlink.symlink_to(outside, target_is_directory=True)
    store = FileSystemArtifactStore(tmp_path)
    with pytest.raises(ArtifactStoreError):
        await store.identity(ArtifactKey("u/user-1/uploads/image.png"))


@pytest.mark.parametrize(
    "error",
    [
        errno.EACCES,
        errno.EIO,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    ],
)
def test_directory_fsync_open_failure_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: int,
) -> None:
    with monkeypatch.context() as failure:
        _fail_directory_sync_open(failure, tmp_path, error=error)
        with pytest.raises(OSError) as exc_info:
            filesystem_objects_module.fsync_directory(tmp_path)

    assert exc_info.value.errno == error


def test_directory_fsync_unsupported_uses_syncfs_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    syncfs_calls: list[int] = []

    def unsupported_directory_fsync(_fd: int) -> None:
        raise OSError(errno.EINVAL, "directory fsync unsupported")

    monkeypatch.setattr(
        filesystem_objects_module.os, "fsync", unsupported_directory_fsync
    )
    monkeypatch.setattr(
        filesystem_objects_module,
        "_sync_filesystem",
        syncfs_calls.append,
    )

    filesystem_objects_module.fsync_directory(tmp_path)

    assert len(syncfs_calls) == 1


def test_hardlink_publish_requires_directory_durability_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"hardlink durability"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/hardlink.bin")
    destination = tmp_path / key.value
    destination.parent.mkdir(parents=True)
    expected = _identity_for(payload)

    with monkeypatch.context() as failure:
        _fail_directory_sync_open(failure, destination.parent)
        with pytest.raises(OSError, match="directory sync open failure"):
            store._publish_path_sync(source, key, expected)  # noqa: SLF001
        assert destination.read_bytes() == payload
        assert source.read_bytes() == payload
        with pytest.raises(OSError, match="directory sync open failure"):
            store._publish_path_sync(source, key, expected)  # noqa: SLF001

    published = store._publish_path_sync(source, key, expected)  # noqa: SLF001
    assert published.created is False
    assert source.exists()


def test_rename_install_requires_directory_durability_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"rename durability"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/rename.bin")
    destination = tmp_path / key.value
    destination.parent.mkdir(parents=True)
    expected = _identity_for(payload)

    def rename_noreplace(source_path: Path, destination_path: Path) -> bool:
        source_path.rename(destination_path)
        return True

    with monkeypatch.context() as failure:
        failure.setattr(
            filesystem_publish_module,
            "os",
            _SourceLinkUnsupportedOS(source),
        )
        failure.setattr(
            filesystem_atomic_publish_module,
            "_rename_noreplace",
            rename_noreplace,
        )
        _fail_directory_sync_open(failure, destination.parent)
        with pytest.raises(OSError, match="directory sync open failure"):
            store._publish_path_sync(source, key, expected)  # noqa: SLF001
        assert destination.read_bytes() == payload
        assert source.read_bytes() == payload
        assert _publish_temp_paths(destination) == []
        with pytest.raises(OSError, match="directory sync open failure"):
            store._publish_path_sync(source, key, expected)  # noqa: SLF001

    published = store._publish_path_sync(source, key, expected)  # noqa: SLF001
    assert published.created is False
    assert source.exists()


def test_copy_install_requires_directory_durability_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"copy durability"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/copy.bin")
    destination = tmp_path / key.value
    destination.parent.mkdir(parents=True)
    expected = _identity_for(payload)

    with monkeypatch.context() as failure:
        failure.setattr(
            filesystem_publish_module,
            "os",
            _SourceLinkUnsupportedOS(source),
        )
        failure.setattr(
            filesystem_atomic_publish_module,
            "_rename_noreplace",
            lambda _source, _destination: False,
        )
        _fail_directory_sync_open(failure, destination.parent)
        with pytest.raises(OSError, match="directory sync open failure"):
            store._publish_path_sync(source, key, expected)  # noqa: SLF001
        assert destination.read_bytes() == payload
        assert source.read_bytes() == payload
        assert destination.stat().st_ino != source.stat().st_ino
        assert _publish_temp_paths(destination) == []
        with pytest.raises(OSError, match="directory sync open failure"):
            store._publish_path_sync(source, key, expected)  # noqa: SLF001

    published = store._publish_path_sync(source, key, expected)  # noqa: SLF001
    assert published.created is False
    assert source.exists()


@pytest.mark.asyncio
async def test_copy_fallback_concurrent_publish_resolves_identical_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"shared artifact"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/shared.bin")
    barrier = threading.Barrier(2)
    original_copy = FileSystemArtifactStore._copy_exclusive
    original_link = os.link
    winner_metrics: list[str] = []

    def unsupported_source_link(
        link_source: Path,
        destination: Path,
    ) -> None:
        if Path(link_source) == source:
            raise OSError(errno.EXDEV, "cross-device link")
        original_link(link_source, destination)

    def racing_copy(copy_source: Path, destination: Path) -> None:
        barrier.wait(timeout=2)
        original_copy(copy_source, destination)

    monkeypatch.setattr(os, "link", unsupported_source_link)
    monkeypatch.setattr(
        FileSystemArtifactStore,
        "_copy_exclusive",
        staticmethod(racing_copy),
    )
    monkeypatch.setattr(
        filesystem_atomic_publish_module,
        "RENAME_NOREPLACE_API",
        None,
    )
    monkeypatch.setattr(
        filesystem_store_module,
        "record_publish_idempotent_winner",
        winner_metrics.append,
    )

    first, second = await asyncio.gather(
        store.publish_path(source, key, expected=_identity_for(payload)),
        store.publish_path(source, key, expected=_identity_for(payload)),
    )

    assert sorted((first.created, second.created)) == [False, True]
    destination = tmp_path / key.value
    assert destination.read_bytes() == payload
    assert _publish_temp_paths(destination) == []
    assert winner_metrics == ["filesystem"]


@pytest.mark.asyncio
async def test_existing_winner_cleans_crash_residual_publish_temp(
    tmp_path: Path,
) -> None:
    payload = b"existing complete winner"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/existing-winner.bin")
    destination = tmp_path / key.value
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    stale_temp = destination.parent / (
        f"{filesystem_publish_module.PUBLISH_TEMP_PREFIX}crashed"
        f"{filesystem_publish_module.PUBLISH_TEMP_SUFFIX}"
    )
    stale_temp.write_bytes(payload)

    published = await store.publish_path(
        source,
        key,
        expected=_identity_for(payload),
    )

    assert published.created is False
    assert destination.read_bytes() == payload
    assert not stale_temp.exists()


@pytest.mark.asyncio
async def test_copy_fallback_concurrent_different_publish_preserves_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = (b"first concurrent artifact", b"second concurrent artifact")
    sources = (tmp_path / "first.bin", tmp_path / "second.bin")
    for source, payload in zip(sources, payloads, strict=True):
        source.write_bytes(payload)
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/concurrent-conflict.bin")
    barrier = threading.Barrier(2)
    original_copy = FileSystemArtifactStore._copy_exclusive
    original_link = os.link

    def unsupported_source_link(
        link_source: Path,
        destination: Path,
    ) -> None:
        if Path(link_source) in sources:
            raise OSError(errno.EXDEV, "cross-device link")
        original_link(link_source, destination)

    def racing_copy(copy_source: Path, destination: Path) -> None:
        barrier.wait(timeout=2)
        original_copy(copy_source, destination)

    monkeypatch.setattr(os, "link", unsupported_source_link)
    monkeypatch.setattr(
        FileSystemArtifactStore,
        "_copy_exclusive",
        staticmethod(racing_copy),
    )

    results = await asyncio.gather(
        *(
            store.publish_path(source, key, expected=_identity_for(payload))
            for source, payload in zip(sources, payloads, strict=True)
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, BaseException)]
    conflicts = [result for result in results if isinstance(result, ArtifactConflict)]
    destination = tmp_path / key.value
    assert len(successes) == 1
    assert successes[0].created is True
    assert len(conflicts) == 1
    assert destination.read_bytes() in payloads
    assert _publish_temp_paths(destination) == []


@pytest.mark.asyncio
async def test_copy_fallback_existing_different_content_is_explicit_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_payload = b"first artifact"
    second_payload = b"second artifact"
    first_source = tmp_path / "first.bin"
    second_source = tmp_path / "second.bin"
    first_source.write_bytes(first_payload)
    second_source.write_bytes(second_payload)
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/conflict.bin")
    conflict_metrics: list[str] = []
    original_link = os.link

    def unsupported_source_link(
        link_source: Path,
        destination: Path,
    ) -> None:
        if Path(link_source) in {first_source, second_source}:
            raise OSError(errno.EXDEV, "cross-device link")
        original_link(link_source, destination)

    monkeypatch.setattr(os, "link", unsupported_source_link)
    monkeypatch.setattr(
        filesystem_store_module,
        "record_publish_conflict",
        conflict_metrics.append,
    )

    await store.publish_path(
        first_source,
        key,
        expected=_identity_for(first_payload),
    )
    with pytest.raises(
        ArtifactConflict,
        match="artifact destination conflict",
    ) as exc:
        await store.publish_path(
            second_source,
            key,
            expected=_identity_for(second_payload),
        )

    assert isinstance(exc.value, FileExistsError)
    assert (tmp_path / key.value).read_bytes() == first_payload
    assert conflict_metrics == ["filesystem"]


def test_copy_fallback_fsyncs_temp_before_atomic_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"durable publish"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/durable.bin")
    destination = tmp_path / key.value
    original_fsync = os.fsync
    original_install = filesystem_publish_module.install_file_noreplace
    original_link = os.link
    events: list[str] = []

    def unsupported_source_link(
        link_source: Path,
        link_destination: Path,
    ) -> None:
        if Path(link_source) == source:
            raise OSError(errno.EXDEV, "cross-device link")
        original_link(link_source, link_destination)

    def tracking_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        events.append("directory_fsync" if stat.S_ISDIR(mode) else "file_fsync")
        original_fsync(fd)

    def tracking_install(temp_path: Path, final_path: Path) -> None:
        events.append("install")
        original_install(temp_path, final_path)

    monkeypatch.setattr(os, "link", unsupported_source_link)
    monkeypatch.setattr(os, "fsync", tracking_fsync)
    monkeypatch.setattr(
        filesystem_publish_module,
        "install_file_noreplace",
        tracking_install,
    )

    published = store._publish_path_sync(  # noqa: SLF001
        source,
        key,
        _identity_for(payload),
    )

    assert published.created is True
    assert events.index("file_fsync") < events.index("install")
    install_index = events.index("install")
    assert "directory_fsync" in events[install_index + 1 :]
    assert destination.read_bytes() == payload
    assert _publish_temp_paths(destination) == []


def test_publish_fsyncs_each_new_ancestor_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/nested/durable.bin")
    calls: list[Path] = []
    original = filesystem_objects_module.fsync_directory

    def tracking_fsync(path: Path) -> None:
        calls.append(path)
        original(path)

    monkeypatch.setattr(
        filesystem_objects_module,
        "fsync_directory",
        tracking_fsync,
    )

    store._path(key, create_parent=True)  # noqa: SLF001

    assert tmp_path in calls
    assert tmp_path / "u" in calls
    assert tmp_path / "u" / "user-1" in calls
    assert tmp_path / "u" / "user-1" / "uploads" in calls


def test_copy_fallback_interrupt_removes_temp_and_syncs_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source payload")
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/interrupted.bin")
    destination = tmp_path / key.value
    original_link = os.link
    original_fsync_directory = filesystem_publish_module.fsync_directory
    synced_directories: list[Path] = []

    def unsupported_source_link(
        link_source: Path,
        link_destination: Path,
    ) -> None:
        if Path(link_source) == source:
            raise OSError(errno.EXDEV, "cross-device link")
        original_link(link_source, link_destination)

    def interrupted_copy(src: Any, dst: Any, *, length: int) -> None:
        _ = length
        dst.write(src.read(4))
        dst.flush()
        raise KeyboardInterrupt

    def tracking_fsync_directory(path: Path) -> None:
        synced_directories.append(path)
        original_fsync_directory(path)

    monkeypatch.setattr(os, "link", unsupported_source_link)
    monkeypatch.setattr(shutil, "copyfileobj", interrupted_copy)
    monkeypatch.setattr(
        filesystem_publish_module,
        "fsync_directory",
        tracking_fsync_directory,
    )

    with pytest.raises(KeyboardInterrupt):
        store._publish_path_sync(  # noqa: SLF001
            source,
            key,
            _identity_for(b"source payload"),
        )

    assert not destination.exists()
    assert _publish_temp_paths(destination) == []
    assert destination.parent in synced_directories


def test_copy_fallback_os_exit_never_exposes_partial_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"complete staged source" * 1024
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    store = FileSystemArtifactStore(tmp_path)
    key = ArtifactKey("u/user-1/uploads/process-exit.bin")
    destination = tmp_path / key.value
    expected = _identity_for(payload)
    original_link = os.link

    def unsupported_source_link(
        link_source: Path,
        link_destination: Path,
    ) -> None:
        if Path(link_source) == source:
            raise OSError(errno.EXDEV, "cross-device link")
        original_link(link_source, link_destination)

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import errno
import os
import shutil
import sys
from pathlib import Path

from app.images.adapters.filesystem_store import FileSystemArtifactStore
from app.images.domain.artifact import ArtifactIdentity, ArtifactKey

source = Path(sys.argv[1])
root = Path(sys.argv[2])
key = ArtifactKey(sys.argv[3])
expected = ArtifactIdentity(sha256=sys.argv[4], size_bytes=int(sys.argv[5]))
original_link = os.link

def unsupported_source_link(link_source, link_destination):
    if Path(link_source) == source:
        raise OSError(errno.EXDEV, "cross-device link")
    original_link(link_source, link_destination)

def exit_during_copy(src, dst, *, length):
    del length
    dst.write(src.read(17))
    dst.flush()
    os._exit(73)

os.link = unsupported_source_link
shutil.copyfileobj = exit_during_copy
try:
    FileSystemArtifactStore(root)._publish_path_sync(source, key, expected)
except BaseException:
    os._exit(74)
os._exit(75)
""",
            str(source),
            str(tmp_path),
            key.value,
            expected.sha256,
            str(expected.size_bytes),
        ],
        cwd=ROOT / "apps/api",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert child.returncode == 73, child.stdout + child.stderr
    assert not destination.exists()
    stale_temps = _publish_temp_paths(destination)
    assert len(stale_temps) == 1
    assert stale_temps[0].read_bytes() == payload[:17]

    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(os, "link", unsupported_source_link)
        published = store._publish_path_sync(source, key, expected)  # noqa: SLF001

    assert published.created is True
    assert destination.read_bytes() == payload
    assert _publish_temp_paths(destination) == []


def test_copy_exclusive_closes_temp_fd_when_source_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    destination = tmp_path / "destination.bin"
    original_os_open = os.open
    original_path_open = Path.open
    temp_fds: list[int] = []

    def tracking_os_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        fd = original_os_open(path, flags, mode)
        opened = Path(path)
        if (
            opened.parent == destination.parent
            and opened.name.startswith(filesystem_publish_module.PUBLISH_TEMP_PREFIX)
            and opened.name.endswith(filesystem_publish_module.PUBLISH_TEMP_SUFFIX)
        ):
            temp_fds.append(fd)
        return fd

    def failing_source_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == source:
            raise OSError("source open failed")
        return original_path_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", tracking_os_open)
    monkeypatch.setattr(Path, "open", failing_source_open)

    with pytest.raises(OSError, match="source open failed"):
        FileSystemArtifactStore._copy_exclusive(source, destination)

    assert temp_fds
    assert not destination.exists()
    assert _publish_temp_paths(destination) == []
    for fd in temp_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_artifact_state_machine_rejects_invalid_jump() -> None:
    ensure_artifact_transition(ArtifactStatus.STAGING, ArtifactStatus.PROCESSING)
    ensure_artifact_transition(ArtifactStatus.PUBLISHING, ArtifactStatus.READY)
    with pytest.raises(InvalidArtifactTransition):
        ensure_artifact_transition(ArtifactStatus.STAGING, ArtifactStatus.READY)


def test_resource_estimate_accounts_for_transform_buffers() -> None:
    estimate = estimate_image_resources(
        width=8000,
        height=8000,
        mode="RGBA",
        upload_bytes=20 * 1024 * 1024,
    )
    assert estimate.pixels == 64_000_000
    assert estimate.decoded_bytes == 256_000_000
    assert estimate.transform_peak_bytes > estimate.decoded_bytes * 3
    assert estimate.peak_bytes > 1_000_000_000


@pytest.mark.asyncio
async def test_scaled_local_capacity_bounds_each_simulated_process() -> None:
    limits = CapacityLimits(max_concurrency=4, max_peak_bytes=400)
    first = ScaledLocalCapacity(limits, process_count=2)
    second = ScaledLocalCapacity(limits, process_count=2)
    estimate = ImageResourceEstimate(10, 10, 80, 10, 1, 1)
    first_leases = [await first.reserve(estimate), await first.reserve(estimate)]
    second_leases = [await second.reserve(estimate), await second.reserve(estimate)]
    with pytest.raises(CapacityExceeded):
        await first.reserve(estimate)
    with pytest.raises(CapacityExceeded):
        await second.reserve(estimate)
    for lease in first_leases + second_leases:
        await lease.release()


@pytest.mark.asyncio
async def test_scaled_local_capacity_fails_closed_when_workers_exceed_slots() -> None:
    capacity = ScaledLocalCapacity(
        CapacityLimits(max_concurrency=2, max_peak_bytes=400),
        process_count=4,
    )
    estimate = ImageResourceEstimate(10, 10, 80, 10, 1, 1)
    with pytest.raises(CapacityExceeded):
        await capacity.reserve(estimate)


@pytest.mark.asyncio
async def test_redis_capacity_is_shared_and_expired_lease_is_reclaimed() -> None:
    redis = _FakeRedis()
    limits = CapacityLimits(max_concurrency=1, max_peak_bytes=200)
    first = RedisCapacity(redis, limits)
    second = RedisCapacity(redis, limits)
    estimate = ImageResourceEstimate(10, 10, 80, 10, 1, 1)
    lease = await first.reserve(estimate)
    with pytest.raises(CapacityExceeded):
        await second.reserve(estimate)
    redis.expiries[lease.lease_id] = 0
    recovered = await second.reserve(estimate)
    await recovered.release()
    await lease.release()


def test_redis_capacity_uses_redis_server_time_in_lease_scripts() -> None:
    from app.images.adapters import redis_capacity

    assert "redis.call('TIME')" in redis_capacity._RESERVE_LUA
    assert "redis.call('TIME')" in redis_capacity._RENEW_LUA


@pytest.mark.asyncio
async def test_partial_publish_converges_to_ready_via_reconciler(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(tmp_path)
    failing_store = _FailSecondPublish(store)
    repository = _Repository()
    service = UploadCommandService(
        artifacts=failing_store,  # type: ignore[arg-type]
        capacity=_Capacity(),
        storage_capacity=_Capacity(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        processing_executor=IsolatedImageProcessingExecutor(),
    )
    with pytest.raises(OSError, match="crash after original publish"):
        await service.execute(
            user_id="user-1",
            upload_file=_Upload(_png_bytes()),
            filename="upload.png",
            policy=_policy(),
        )
    row = next(iter(repository.rows.values()))
    assert row.artifact_status == ArtifactStatus.PUBLISHING.value
    original = Path(tmp_path, row.storage_key)
    normalized = original.with_name(f"{row.id}.ref.webp")
    assert original.is_file()
    assert not normalized.exists()

    stats = await ImageArtifactReconciler(
        repository=repository,  # type: ignore[arg-type]
        artifacts=store,
        storage_capacity=_Capacity(),  # type: ignore[arg-type]
    ).run_once(stale_after=timedelta(seconds=0))

    assert stats.rebuilt_reference == 1
    assert stats.marked_ready == 1
    assert row.artifact_status == ArtifactStatus.READY.value
    assert normalized.is_file()


@pytest.mark.asyncio
async def test_successful_upload_publishes_verified_ready_artifacts(
    tmp_path: Path,
) -> None:
    repository = _Repository()
    service = UploadCommandService(
        artifacts=FileSystemArtifactStore(tmp_path),
        capacity=_Capacity(),
        storage_capacity=_Capacity(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        processing_executor=IsolatedImageProcessingExecutor(),
    )
    row = await service.execute(
        user_id="user-1",
        upload_file=_Upload(_png_bytes()),
        filename="upload.png",
        policy=_policy(),
    )
    assert row.artifact_status == ArtifactStatus.READY.value
    assert row.ready_at is not None
    original = Path(tmp_path, row.storage_key)
    normalized_key = row.metadata_jsonb["normalized_ref"]["storage_key"]
    assert original.is_file()
    assert Path(tmp_path, normalized_key).is_file()
    assert list((tmp_path / ".upload-tmp").rglob("*")) == []


@pytest.mark.asyncio
async def test_upload_durability_failure_keeps_intent_and_unique_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _png_bytes()
    repository = _Repository()
    service = UploadCommandService(
        artifacts=FileSystemArtifactStore(tmp_path),
        capacity=_Capacity(),
        storage_capacity=_Capacity(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        processing_executor=IsolatedImageProcessingExecutor(),
    )
    uploads_directory = tmp_path / "u" / "user-1" / "uploads"

    with monkeypatch.context() as failure:
        _fail_directory_sync_open(failure, uploads_directory)
        with pytest.raises(OSError, match="directory sync open failure"):
            await service.execute(
                user_id="user-1",
                upload_file=_Upload(payload),
                filename="upload.png",
                policy=_policy(),
            )

    row = next(iter(repository.rows.values()))
    manifest = row.artifact_manifest_jsonb
    ticket = manifest["ticket"]
    ticket_directory = tmp_path / ".upload-tmp" / ticket
    staged_sources = list(ticket_directory.glob("artifact-v1-*.source"))
    processed_sources = list(ticket_directory.glob("processed-*"))

    assert repository.adopt_calls == 0
    assert row.artifact_status == ArtifactStatus.PUBLISHING.value
    assert row.reconcile_after is not None
    assert manifest["storage_intent"]["state"] == "pending"
    assert len(staged_sources) == 1
    assert staged_sources[0].read_bytes() == payload
    assert len(processed_sources) == 1
    assert Path(tmp_path, row.storage_key).read_bytes() == payload
    normalized_key = row.metadata_jsonb["normalized_ref"]["storage_key"]
    assert not Path(tmp_path, normalized_key).exists()


@pytest.mark.asyncio
async def test_ready_commit_is_not_overridden_by_post_commit_lease_failure(
    tmp_path: Path,
) -> None:
    repository = _Repository()

    class _FailAfterReadyLease(_Lease):
        async def renew(self) -> bool:
            return not any(
                row.artifact_status == ArtifactStatus.READY.value
                for row in repository.rows.values()
            )

    class _ReadyAwareCapacity:
        def __init__(self) -> None:
            self.calls = 0

        async def reserve(self, _estimate: ImageResourceEstimate) -> _Lease:
            self.calls += 1
            return _Lease() if self.calls == 1 else _FailAfterReadyLease()

    service = UploadCommandService(
        artifacts=FileSystemArtifactStore(tmp_path),
        capacity=_ReadyAwareCapacity(),  # type: ignore[arg-type]
        storage_capacity=_Capacity(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        processing_executor=IsolatedImageProcessingExecutor(),
    )

    row = await service.execute(
        user_id="user-1",
        upload_file=_Upload(_png_bytes()),
        filename="upload.png",
        policy=_policy(),
    )

    assert row.artifact_status == ArtifactStatus.READY.value
    assert row.ready_at is not None


@pytest.mark.asyncio
async def test_capacity_race_cancels_children_when_caller_is_cancelled() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    guard = CapacityLeaseGuard.create(_Lease(), ttl_seconds=30)
    task = asyncio.create_task(race_with_capacity_lease(work(), guard))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert not any(
        pending.get_name() == "image-capacity-lease-lost" and not pending.done()
        for pending in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_processing_lease_loss_cancels_work_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SequencedLease:
        def __init__(self, renewals: list[bool] | None = None) -> None:
            self.renewals = list(renewals or [])
            self.released = False

        async def renew(self) -> bool:
            if self.renewals:
                return self.renewals.pop(0)
            return True

        async def resize(self, _bytes_required: int) -> bool:
            return not self.released

        async def release(self) -> None:
            self.released = True

    class SequencedCapacity:
        def __init__(self) -> None:
            self.leases = [
                SequencedLease(),
                SequencedLease([True, True, False]),
            ]

        async def reserve(
            self,
            _estimate: ImageResourceEstimate,
        ) -> SequencedLease:
            return self.leases.pop(0)

    class BlockingExecutor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def inspect(self, source_path: Path, **kwargs: Any):
            return ImageProcessor().inspect(source_path, **kwargs)

        async def process(self, _request: Any):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        CapacityLimits,
        "from_env",
        classmethod(
            lambda _cls: CapacityLimits(
                max_concurrency=1,
                max_peak_bytes=1024 * 1024,
                lease_ttl_seconds=0.2,  # type: ignore[arg-type]
            )
        ),
    )
    repository = _Repository()
    executor = BlockingExecutor()
    service = UploadCommandService(
        artifacts=FileSystemArtifactStore(tmp_path),
        capacity=SequencedCapacity(),  # type: ignore[arg-type]
        storage_capacity=_Capacity(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        processing_executor=executor,  # type: ignore[arg-type]
    )

    with pytest.raises(UploadCommandError) as exc_info:
        await asyncio.wait_for(
            service.execute(
                user_id="user-1",
                upload_file=_Upload(_png_bytes()),
                filename="upload.png",
                policy=_policy(),
            ),
            timeout=2,
        )

    assert getattr(exc_info.value, "code", None) == "upload_capacity_exceeded"
    assert executor.started.is_set()
    assert executor.cancelled.is_set()
    row = next(iter(repository.rows.values()))
    assert row.artifact_status == ArtifactStatus.FAILED.value
    assert list((tmp_path / ".upload-tmp").rglob("*")) == []
    assert list((tmp_path / "u").rglob("*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        ArtifactStatus.STAGING,
        ArtifactStatus.PUBLISHING,
        ArtifactStatus.READY,
    ],
)
async def test_repository_commit_ack_loss_recovers_observed_state(
    target: ArtifactStatus,
) -> None:
    row = Image(
        id="image-1",
        user_id="user-1",
        source="uploaded",
        storage_key="u/user-1/uploads/image-1.png",
        mime="image/png",
        width=1,
        height=1,
        size_bytes=1,
        sha256="a" * 64,
        artifact_status=target.value,
    )

    class _PrimarySession:
        async def commit(self) -> None:
            raise TimeoutError("ack lost")

        async def rollback(self) -> None:
            return None

    class _RecoverySession:
        async def __aenter__(self) -> "_RecoverySession":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, _model: Any, _image_id: str) -> Image:
            return row

    class _Factory:
        def __call__(self) -> _RecoverySession:
            return _RecoverySession()

    repository = SQLAlchemyImageRepository(_Factory())  # type: ignore[arg-type]
    recovered = await repository._resolve_commit(  # noqa: SLF001
        _PrimarySession(),  # type: ignore[arg-type]
        image_id=row.id,
        target_status=target,
    )
    assert recovered is row


def test_image_artifact_migration_upgrade_and_downgrade_sqlite() -> None:
    migration = _load_migration()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE images (
                id VARCHAR(36) PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            text(
                """
                INSERT INTO images (id, created_at, updated_at)
                VALUES ('image-1', '2026-07-25 00:00:00', '2026-07-25 00:00:00')
                """
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        columns = {item["name"] for item in inspect(connection).get_columns("images")}
        row = connection.execute(
            text(
                """
                SELECT artifact_status, publish_attempt, ready_at
                FROM images WHERE id = 'image-1'
                """
            )
        ).one()
        assert {
            "artifact_status",
            "artifact_manifest_jsonb",
            "publish_attempt",
            "reconcile_after",
            "last_artifact_error",
            "ready_at",
        }.issubset(columns)
        assert row.artifact_status == "ready"
        assert row.publish_attempt == 0
        assert row.ready_at is not None

        migration.downgrade()
        downgraded = {
            item["name"] for item in inspect(connection).get_columns("images")
        }
        assert "artifact_status" not in downgraded
