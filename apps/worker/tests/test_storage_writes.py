from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from app import artifact_commit
from app.artifact_commit import ArtifactAdoption
from app.storage import LocalStorage, StoragePutResult
from app.storage_writes import (
    CapacityLeaseLost,
    StorageWriteCoordinator,
    StorageWriteOperation,
)
from lumen_core.storage_capacity import StorageCapacityExceeded


class _Lease:
    def __init__(self, *, renew_result: bool = True) -> None:
        self.renew_result = renew_result
        self.release_calls = 0

    async def renew(self) -> bool:
        return self.renew_result

    async def release(self) -> None:
        self.release_calls += 1


@pytest.mark.asyncio
async def test_artifact_commit_and_rollback_confirmation_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never = asyncio.Event()

    class Session:
        async def commit(self) -> None:
            await never.wait()

        async def rollback(self) -> None:
            await never.wait()

    async def probe() -> ArtifactAdoption:
        return ArtifactAdoption.UNKNOWN

    monkeypatch.setattr(artifact_commit, "ARTIFACT_COMMIT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        artifact_commit,
        "ARTIFACT_CONFIRMATION_TIMEOUT_SECONDS",
        0.05,
    )

    result = await asyncio.wait_for(
        artifact_commit.commit_with_adoption_probe(
            Session(),
            probe=probe,
            logger=logging.getLogger(__name__),
            label="bounded artifact",
        ),
        timeout=0.5,
    )

    assert result.outcome is ArtifactAdoption.UNKNOWN
    assert isinstance(result.commit_error, TimeoutError)


class _Capacity:
    def __init__(
        self,
        *,
        error: BaseException | None = None,
        lease: _Lease | None = None,
    ) -> None:
        self.error = error
        self.requests: list[int] = []
        self.lease = lease or _Lease()

    async def reserve(self, bytes_required: int) -> _Lease:
        self.requests.append(bytes_required)
        if self.error is not None:
            raise self.error
        return self.lease


def _coordinator(
    root: Path,
    capacity: _Capacity,
    *,
    storage: LocalStorage | None = None,
    lease_ttl_seconds: float = 30,
    operation_timeout_seconds: float = 30,
) -> StorageWriteCoordinator:
    return StorageWriteCoordinator(
        storage=storage or LocalStorage(root),
        capacity=capacity,  # type: ignore[arg-type]
        lease_ttl_seconds=lease_ttl_seconds,
        operation_timeout_seconds=operation_timeout_seconds,
    )


@pytest.mark.asyncio
async def test_batch_write_reserves_double_peak_and_releases(tmp_path: Path) -> None:
    capacity = _Capacity()
    coordinator = _coordinator(tmp_path, capacity)

    created = await coordinator.write_files(
        [
            ("u/user/g/gen/orig.png", b"original"),
            ("u/user/g/gen/thumb.webp", b"thumb"),
        ]
    )

    assert capacity.requests == [2 * (len(b"original") + len(b"thumb"))]
    assert capacity.lease.release_calls == 1
    assert created == [
        "u/user/g/gen/orig.png",
        "u/user/g/gen/thumb.webp",
    ]
    assert (tmp_path / created[0]).read_bytes() == b"original"
    assert (tmp_path / created[1]).read_bytes() == b"thumb"


@pytest.mark.asyncio
async def test_capacity_rejection_happens_before_any_write(tmp_path: Path) -> None:
    capacity = _Capacity(error=StorageCapacityExceeded("full"))
    coordinator = _coordinator(tmp_path, capacity)

    with pytest.raises(OSError):
        await coordinator.write_files([("u/user/g/gen/orig.png", b"data")])

    assert not (tmp_path / "u").exists()
    assert capacity.lease.release_calls == 0


@pytest.mark.asyncio
async def test_partial_failure_cleans_only_attempt_created_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path)
    existing_key = "u/user/g/gen/existing.png"
    storage.put_bytes(existing_key, b"existing")
    original_put = storage.put_bytes_result

    def fail_one(
        key: str,
        data: bytes,
        *,
        max_bytes: int | None = None,
    ) -> StoragePutResult:
        if key.endswith("bad.png"):
            raise RuntimeError("write failed")
        return original_put(key, data, max_bytes=max_bytes)

    monkeypatch.setattr(storage, "put_bytes_result", fail_one)
    capacity = _Capacity()
    coordinator = _coordinator(tmp_path, capacity, storage=storage)

    with pytest.raises(RuntimeError, match="write failed"):
        await coordinator.write_files(
            [
                (existing_key, b"existing"),
                ("u/user/g/gen/new.png", b"new"),
                ("u/user/g/gen/bad.png", b"bad"),
            ]
        )

    assert storage.get_bytes(existing_key) == b"existing"
    assert not storage.path_for("u/user/g/gen/new.png").exists()
    assert capacity.lease.release_calls == 1


def test_local_storage_enforces_write_byte_ceiling(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(OSError):
        storage.put_bytes_result(
            "u/user/g/gen/orig.png",
            b"oversized",
            max_bytes=3,
        )

    assert not storage.path_for("u/user/g/gen/orig.png").exists()


@pytest.mark.asyncio
async def test_cancellation_waits_for_started_write_before_cleanup_and_release(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path)
    capacity = _Capacity()
    coordinator = _coordinator(tmp_path, capacity, storage=storage)
    started = threading.Event()
    finish = threading.Event()
    key = "u/user/g/gen/orig.png"

    def write() -> bool:
        result = storage.put_bytes_result(key, b"data")
        started.set()
        assert finish.wait(timeout=2)
        return bool(result.created)

    task = asyncio.create_task(
        coordinator.write_operations(
            [StorageWriteOperation(key=key, size_bytes=4, write=write)]
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.05)

    assert capacity.lease.release_calls == 0
    assert storage.path_for(key).exists()

    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not storage.path_for(key).exists()
    assert capacity.lease.release_calls == 1


@pytest.mark.asyncio
async def test_cancellation_of_stuck_write_exits_with_bounded_background_cleanup(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path)
    capacity = _Capacity()
    coordinator = _coordinator(
        tmp_path,
        capacity,
        storage=storage,
        operation_timeout_seconds=0.05,
    )
    started = threading.Event()
    finish = threading.Event()
    key = "u/user/g/gen/stuck.png"

    def write() -> bool:
        result = storage.put_bytes_result(key, b"data")
        started.set()
        finish.wait()
        return bool(result.created)

    task = asyncio.create_task(
        coordinator.write_operations(
            [StorageWriteOperation(key=key, size_bytes=4, write=write)]
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    assert capacity.lease.release_calls == 1

    finish.set()
    for _attempt in range(50):
        if not storage.path_for(key).exists():
            break
        await asyncio.sleep(0.02)
    assert not storage.path_for(key).exists()


@pytest.mark.asyncio
async def test_lease_loss_waits_for_started_write_before_cleanup_and_release(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path)
    capacity = _Capacity(lease=_Lease(renew_result=False))
    coordinator = _coordinator(
        tmp_path,
        capacity,
        storage=storage,
        lease_ttl_seconds=0.15,
    )
    started = threading.Event()
    finish = threading.Event()
    key = "u/user/g/gen/orig.png"

    def write() -> bool:
        result = storage.put_bytes_result(key, b"data")
        started.set()
        assert finish.wait(timeout=2)
        return bool(result.created)

    task = asyncio.create_task(
        coordinator.write_operations(
            [StorageWriteOperation(key=key, size_bytes=4, write=write)]
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(0.12)

    assert capacity.lease.release_calls == 0
    assert storage.path_for(key).exists()

    finish.set()
    with pytest.raises(CapacityLeaseLost):
        await task

    assert not storage.path_for(key).exists()
    assert capacity.lease.release_calls == 1
