from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lumen_core import storage_capacity as storage_capacity_module
from lumen_core.storage_capacity import (
    FileStorageCapacity,
    RedisStorageCapacity,
    ResilientStorageCapacity,
    StorageCapacityExceeded,
    StorageCapacityLimits,
)
from app.images.application.upload import (
    UploadCommandError,
    UploadCommandService,
    UploadPolicy,
)


class _Redis:
    def __init__(self) -> None:
        self.now_ms = 1_000_000
        self.weights: dict[str, int] = {}
        self.expiries: dict[str, int] = {}

    async def eval(self, script: str, _keys: int, *args: str) -> Any:
        if "HGET" in script:
            lease_id = args[2]
            if lease_id not in self.weights:
                return [-1, 0, int(args[4])]
            requested = int(args[3])
            available = int(args[4])
            reserved_without_current = sum(
                value
                for current_id, value in self.weights.items()
                if current_id != lease_id
            )
            if (
                requested > available
                or reserved_without_current + requested > available
            ):
                return [0, sum(self.weights.values()), available]
            self.weights[lease_id] = requested
            self.expiries[lease_id] = self.now_ms + int(args[5])
            return [1, reserved_without_current + requested, available]
        if "ZRANGEBYSCORE" in script:
            expired = [
                lease_id
                for lease_id, expiry in self.expiries.items()
                if expiry <= self.now_ms
            ]
            for lease_id in expired:
                self.expiries.pop(lease_id, None)
                self.weights.pop(lease_id, None)
            lease_id = args[2]
            requested = int(args[3])
            available = int(args[4])
            ttl_ms = int(args[5])
            reserved = sum(self.weights.values())
            if requested > available or reserved + requested > available:
                return [0, reserved, available]
            self.weights[lease_id] = requested
            self.expiries[lease_id] = self.now_ms + ttl_ms
            return [1, reserved + requested, available]
        if "HEXISTS" in script:
            lease_id = args[2]
            if lease_id not in self.weights:
                return 0
            self.expiries[lease_id] = self.now_ms + int(args[3])
            return 1
        lease_id = args[2]
        self.weights.pop(lease_id, None)
        self.expiries.pop(lease_id, None)
        return 1


@pytest.mark.asyncio
async def test_redis_storage_capacity_reserves_shared_bytes_and_reclaims_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        storage_capacity_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1_000),
    )
    redis = _Redis()
    limits = StorageCapacityLimits(minimum_free_bytes=100, lease_ttl_seconds=10)
    first = RedisStorageCapacity(redis, tmp_path, limits)
    second = RedisStorageCapacity(redis, tmp_path, limits)

    lease = await first.reserve(600)
    with pytest.raises(StorageCapacityExceeded):
        await second.reserve(301)

    redis.now_ms += 10_001
    replacement = await second.reserve(900)
    assert len(redis.weights) == 1
    await replacement.release()
    await lease.release()
    assert redis.weights == {}


@pytest.mark.asyncio
async def test_redis_storage_capacity_resizes_one_owned_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        storage_capacity_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1_000),
    )
    redis = _Redis()
    limits = StorageCapacityLimits(minimum_free_bytes=100, lease_ttl_seconds=10)
    capacity = RedisStorageCapacity(redis, tmp_path, limits)

    lease = await capacity.reserve(600)
    assert await lease.resize(800) is True
    with pytest.raises(StorageCapacityExceeded):
        await capacity.reserve(101)
    assert await lease.resize(400) is True
    other = await capacity.reserve(500)

    await other.release()
    await lease.release()


@pytest.mark.asyncio
async def test_file_storage_capacity_is_persistent_across_process_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        storage_capacity_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1_000),
    )
    clock = [100.0]
    limits = StorageCapacityLimits(minimum_free_bytes=100, lease_ttl_seconds=10)
    first = FileStorageCapacity(tmp_path, limits, clock=lambda: clock[0])
    second = FileStorageCapacity(tmp_path, limits, clock=lambda: clock[0])

    lease = await first.reserve(600)
    with pytest.raises(StorageCapacityExceeded):
        await second.reserve(301)

    clock[0] += 11
    replacement = await second.reserve(900)
    assert await replacement.renew() is True
    await replacement.release()
    await lease.release()
    state = (tmp_path / ".lumen-capacity" / "storage-leases.json").read_text()
    assert '"leases":{}' in state


@pytest.mark.asyncio
async def test_file_storage_capacity_resizes_one_owned_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        storage_capacity_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1_000),
    )
    limits = StorageCapacityLimits(minimum_free_bytes=100, lease_ttl_seconds=10)
    capacity = FileStorageCapacity(tmp_path, limits)

    lease = await capacity.reserve(600)
    assert await lease.resize(800) is True
    with pytest.raises(StorageCapacityExceeded):
        await capacity.reserve(101)
    assert await lease.resize(400) is True
    other = await capacity.reserve(500)

    await other.release()
    await lease.release()


@pytest.mark.asyncio
async def test_scaled_local_policy_uses_one_file_ledger_without_backend_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        storage_capacity_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1_000),
    )
    limits = StorageCapacityLimits(minimum_free_bytes=100, lease_ttl_seconds=10)

    class _Primary:
        calls = 0

        async def reserve(self, _bytes_required: int) -> Any:
            self.calls += 1
            raise AssertionError("scaled_local must not switch between ledgers")

    primary = _Primary()
    first = ResilientStorageCapacity(
        primary,  # type: ignore[arg-type]
        FileStorageCapacity(tmp_path, limits),
        degraded_policy="scaled_local",
    )
    second = ResilientStorageCapacity(
        primary,  # type: ignore[arg-type]
        FileStorageCapacity(tmp_path, limits),
        degraded_policy="scaled_local",
    )

    lease = await first.reserve(600)
    with pytest.raises(StorageCapacityExceeded):
        await second.reserve(301)
    assert primary.calls == 0
    await lease.release()


class _Lease:
    def __init__(self) -> None:
        self.release_calls = 0
        self.resize_calls: list[int] = []

    async def renew(self) -> bool:
        return True

    async def resize(self, bytes_required: int) -> bool:
        self.resize_calls.append(bytes_required)
        return True

    async def release(self) -> None:
        self.release_calls += 1


class _StorageCapacity:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.reservations: list[int] = []
        self.lease = _Lease()

    async def reserve(self, bytes_required: int) -> _Lease:
        self.reservations.append(bytes_required)
        if self.error is not None:
            raise self.error
        return self.lease


class _UploadService(UploadCommandService):
    def __init__(
        self, storage_capacity: _StorageCapacity, *, fail: bool = False
    ) -> None:
        super().__init__(
            artifacts=object(),  # type: ignore[arg-type]
            capacity=object(),  # type: ignore[arg-type]
            storage_capacity=storage_capacity,
            repository=object(),  # type: ignore[arg-type]
            processing_executor=object(),  # type: ignore[arg-type]
            storage_lease_ttl_seconds=30,
        )
        self.fail = fail
        self.executed = False

    async def _execute_reserved(self, **kwargs: Any) -> Any:
        self.executed = True
        guard = kwargs["storage_lease_guard"]
        assert guard is not None
        await guard.assert_owned()
        if self.fail:
            raise RuntimeError("boom")
        return "ok"


def _policy() -> UploadPolicy:
    return UploadPolicy(
        allowed_mime={"image/png"},
        normalizable_mime=set(),
        extensions={"image/png": "png"},
        max_bytes=100,
        max_pixels=100,
        max_long_side=10,
        mask_requested=False,
        reference_size=None,
    )


@pytest.mark.asyncio
async def test_upload_reserves_storage_before_execution_and_releases_after_commit() -> (
    None
):
    capacity = _StorageCapacity()
    service = _UploadService(capacity)

    result = await service.execute(
        user_id="user-1",
        upload_file=object(),
        filename="image.png",
        policy=_policy(),
    )

    assert result == "ok"
    assert service.executed is True
    assert capacity.reservations == [1_048_676]
    assert capacity.lease.release_calls == 1


@pytest.mark.asyncio
async def test_upload_releases_storage_reservation_on_failure() -> None:
    capacity = _StorageCapacity()
    service = _UploadService(capacity, fail=True)

    with pytest.raises(RuntimeError, match="boom"):
        await service.execute(
            user_id="user-1",
            upload_file=object(),
            filename="image.png",
            policy=_policy(),
        )

    assert capacity.lease.release_calls == 1


@pytest.mark.asyncio
async def test_upload_fails_before_read_when_storage_cannot_be_reserved() -> None:
    capacity = _StorageCapacity(
        error=StorageCapacityExceeded("full"),
    )
    service = _UploadService(capacity)

    with pytest.raises(UploadCommandError) as exc_info:
        await service.execute(
            user_id="user-1",
            upload_file=object(),
            filename="image.png",
            policy=_policy(),
        )

    assert exc_info.value.code == "storage_insufficient_space"
    assert exc_info.value.status_code == 507
    assert service.executed is False


class _ProcessingCapacity:
    def __init__(self) -> None:
        self.lease = _Lease()

    async def reserve(self, _estimate: Any) -> _Lease:
        return self.lease


class _ResizeUploadService(UploadCommandService):
    def __init__(self, storage_capacity: _StorageCapacity) -> None:
        self.processing_capacity = _ProcessingCapacity()
        super().__init__(
            artifacts=object(),  # type: ignore[arg-type]
            capacity=self.processing_capacity,  # type: ignore[arg-type]
            storage_capacity=storage_capacity,
            repository=object(),  # type: ignore[arg-type]
            processing_executor=object(),  # type: ignore[arg-type]
            storage_lease_ttl_seconds=30,
        )
        self.processing_reservation: int | None = None

    async def _stage_and_inspect(self, state: Any, **_kwargs: Any) -> Any:
        state.staged = SimpleNamespace(
            path="/tmp/staged",
            identity=SimpleNamespace(size_bytes=40),
        )
        return SimpleNamespace(
            estimate=SimpleNamespace(output_reserve_bytes=120),
        )

    async def _process_and_persist(self, _state: Any, **kwargs: Any) -> Any:
        self.processing_reservation = kwargs["storage_reservation_bytes"]
        return object(), object(), object()

    async def _publish_and_mark_ready(self, _state: Any, **_kwargs: Any) -> str:
        return "ok"

    async def _cleanup_state(self, _state: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_upload_resizes_storage_lease_after_inspection() -> None:
    storage = _StorageCapacity()
    service = _ResizeUploadService(storage)

    result = await service.execute(
        user_id="user-1",
        upload_file=object(),
        filename="image.png",
        policy=_policy(),
    )

    assert result == "ok"
    assert storage.reservations == [1_048_676]
    assert storage.lease.resize_calls == [160]
    assert service.processing_reservation == 160
    assert storage.lease.release_calls == 1
