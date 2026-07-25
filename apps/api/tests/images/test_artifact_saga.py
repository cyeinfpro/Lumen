from __future__ import annotations

import importlib.util
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from PIL import Image as PILImage
from sqlalchemy import create_engine, inspect, text

from app.images.adapters.filesystem_store import FileSystemArtifactStore
from app.images.adapters.filesystem_store import ArtifactStoreError
from app.images.adapters.local_capacity import (
    CapacityExceeded,
    CapacityLimits,
    ScaledLocalCapacity,
)
from app.images.adapters.redis_capacity import RedisCapacity
from app.images.adapters.sqlalchemy_repository import SQLAlchemyImageRepository
from app.images.application.reconcile_policy import ImageArtifactReconciler
from app.images.application.upload import UploadCommandService, UploadPolicy
from app.images.domain.artifact import (
    ArtifactKey,
    ArtifactStatus,
    InvalidArtifactTransition,
    UploadTicket,
    ensure_artifact_transition,
)
from app.images.domain.resource_estimate import (
    ImageResourceEstimate,
    estimate_image_resources,
)
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

    async def release(self) -> None:
        self.released = True


class _Capacity:
    async def reserve(self, _estimate: ImageResourceEstimate) -> _Lease:
        return _Lease()


class _Repository:
    def __init__(self) -> None:
        self.rows: dict[str, Image] = {}

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
    ) -> Image:
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
    ) -> Image:
        row = self.rows[image_id]
        assert row.artifact_status == ArtifactStatus.READY.value
        for key, value in values.items():
            setattr(row, key, value)
        return self._touch(row)

    async def list_reconcile_candidates(self, **_kwargs: Any) -> list[Image]:
        return list(self.rows.values())

    async def active_upload_tickets(self) -> set[str]:
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

    async def eval(self, script: str, _keys: int, *args: str) -> Any:
        if "ZRANGEBYSCORE" in script:
            now_ms = int(args[2])
            expired = [
                lease_id
                for lease_id, expiry in self.expiries.items()
                if expiry <= now_ms
            ]
            for lease_id in expired:
                self.expiries.pop(lease_id, None)
                self.weights.pop(lease_id, None)
            lease_id = args[4]
            weight = int(args[5])
            max_count = int(args[6])
            max_weight = int(args[7])
            used = sum(self.weights.values())
            if len(self.weights) >= max_count or used + weight > max_weight:
                return [0, len(self.weights), used]
            self.expiries[lease_id] = int(args[3])
            self.weights[lease_id] = weight
            return [1, len(self.weights), sum(self.weights.values())]
        if "HEXISTS" in script:
            lease_id = args[2]
            if lease_id not in self.weights:
                return 0
            self.expiries[lease_id] = int(args[3])
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
async def test_artifact_store_lists_and_identity_deletes_crash_leftovers(
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
    leftovers = await store.list_staged()
    assert leftovers == [staged]
    assert await store.delete_staged(leftovers[0]) is True
    assert await store.list_staged() == []


@pytest.mark.asyncio
async def test_artifact_store_rejects_symlink_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = tmp_path / "u"
    symlink.symlink_to(outside, target_is_directory=True)
    store = FileSystemArtifactStore(tmp_path)
    with pytest.raises(ArtifactStoreError):
        await store.identity(ArtifactKey("u/user-1/uploads/image.png"))


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
        repository=repository,  # type: ignore[arg-type]
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
        repository=repository,  # type: ignore[arg-type]
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
