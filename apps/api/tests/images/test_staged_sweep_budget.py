from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.images.adapters import filesystem_store as filesystem_store_module
from app.images.adapters.filesystem_store import FileSystemArtifactStore
from app.images.application import reconcile_policy as reconcile_policy_module
from app.images.application.reconcile_policy import ImageArtifactReconciler
from app.images.application.reconcile_policy import ReconcileLeaseLost
from app.images.domain.artifact import (
    ArtifactIdentity,
    ArtifactStatus,
    StagedSweepBudget,
    StagedSweepResult,
    UploadTicket,
)


_GIB = 1024**3


class _StorageLease:
    async def renew(self) -> bool:
        return True

    async def release(self) -> None:
        return None


class _StorageCapacity:
    async def reserve(self, _bytes_required: int) -> _StorageLease:
        return _StorageLease()


def _budget(
    *,
    files: int = 10,
    hashed_bytes: int = 256 * 1024 * 1024,
    seconds: float = 5,
) -> StagedSweepBudget:
    return StagedSweepBudget(
        max_files_per_pass=files,
        max_bytes_hashed_per_pass=hashed_bytes,
        max_seconds_per_pass=seconds,
    )


def _legacy_sparse_file(
    root: Path,
    *,
    ticket: str,
    name: str,
    size_bytes: int,
) -> Path:
    ticket_dir = root / ".upload-tmp" / ticket
    ticket_dir.mkdir(mode=0o700, parents=True)
    path = ticket_dir / name
    with path.open("wb") as handle:
        handle.truncate(size_bytes)
    os.utime(path, (1, 1))
    return path


def _force_sweep_slot(store: FileSystemArtifactStore, slot: int) -> None:
    store._persist_sweep_cursor(  # noqa: SLF001 - targeted adapter contract test
        filesystem_store_module._SweepCursor(slot=slot)  # noqa: SLF001
    )


def _move_metadata_to_shard(
    store: FileSystemArtifactStore,
    metadata_path: Path,
    *,
    shard: int,
) -> Path:
    current_shard = int(metadata_path.parent.name, 16)
    while current_shard != shard:
        store._rotate_metadata(  # noqa: SLF001 - targeted adapter contract test
            metadata_path,
            current_shard=current_shard,
        )
        current_shard = (
            current_shard + 1
        ) % filesystem_store_module._STAGED_SHARD_COUNT
        metadata_path = (
            store._metadata_shard_path(current_shard) / metadata_path.name  # noqa: SLF001
        )
    return metadata_path


@pytest.mark.asyncio
async def test_sparse_hundred_gigabyte_sweep_respects_file_and_hash_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    size_bytes = 40 * _GIB
    paths = [
        _legacy_sparse_file(
            tmp_path,
            ticket=f"ticket-{index}",
            name=f"legacy-{index}.source",
            size_bytes=size_bytes,
        )
        for index in range(3)
    ]
    hash_calls: list[tuple[Path, int]] = []

    def fake_hash(
        path: Path,
        *,
        max_bytes: int,
        deadline: float,
        monotonic: Any,
    ) -> Any:
        del deadline, monotonic
        info = path.lstat()
        assert info.st_size <= max_bytes
        hash_calls.append((path, max_bytes))
        return filesystem_store_module._HashAttempt(  # noqa: SLF001
            identity=ArtifactIdentity(
                sha256=hashlib.sha256(path.name.encode()).hexdigest(),
                size_bytes=info.st_size,
                device=info.st_dev,
                inode=info.st_ino,
            ),
            fingerprint=filesystem_store_module._fingerprint(info),  # noqa: SLF001
            bytes_hashed=info.st_size,
        )

    def reject_materialized_walks(_path: Path) -> Any:
        raise AssertionError("staged sweep must use streaming directory iteration")

    monkeypatch.setattr(filesystem_store_module, "_hash_staged_file", fake_hash)
    monkeypatch.setattr(Path, "iterdir", reject_materialized_walks)
    monkeypatch.setattr(Path, "rglob", reject_materialized_walks)

    first_store = FileSystemArtifactStore(tmp_path)
    _force_sweep_slot(first_store, filesystem_store_module._STAGED_LEGACY_SLOT)
    first = await first_store.sweep_staged(
        active_tickets=set(),
        stale_before=10,
        budget=_budget(files=2, hashed_bytes=60 * _GIB),
    )

    assert first.scanned == 2
    assert first.hashed_bytes == size_bytes
    assert first.hashed_bytes <= 60 * _GIB
    assert first.deleted == 1
    assert first.budget_exhausted is True
    assert first.next_cursor is not None
    assert len(hash_calls) == 1

    second_store = FileSystemArtifactStore(tmp_path)
    second = await second_store.sweep_staged(
        active_tickets=set(),
        stale_before=10,
        budget=_budget(files=2, hashed_bytes=60 * _GIB),
    )
    third_store = FileSystemArtifactStore(tmp_path)
    third = await third_store.sweep_staged(
        active_tickets=set(),
        stale_before=10,
        budget=_budget(files=2, hashed_bytes=60 * _GIB),
    )

    assert second.scanned <= 2
    assert third.scanned <= 2
    assert second.hashed_bytes <= 60 * _GIB
    assert third.hashed_bytes <= 60 * _GIB
    assert second.deleted + third.deleted == 2
    assert len(hash_calls) == 3
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_sweep_stops_when_monotonic_time_budget_is_consumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _legacy_sparse_file(
        tmp_path,
        ticket="ticket-time-1",
        name="first.source",
        size_bytes=1024,
    )
    _legacy_sparse_file(
        tmp_path,
        ticket="ticket-time-2",
        name="second.source",
        size_bytes=1024,
    )
    clock = [0.0]
    hash_calls = 0

    def monotonic() -> float:
        return clock[0]

    def consume_time_budget(
        path: Path,
        *,
        max_bytes: int,
        deadline: float,
        monotonic: Any,
    ) -> Any:
        nonlocal hash_calls
        del max_bytes, monotonic
        hash_calls += 1
        info = path.lstat()
        clock[0] = deadline
        return filesystem_store_module._HashAttempt(  # noqa: SLF001
            identity=ArtifactIdentity(
                sha256="a" * 64,
                size_bytes=info.st_size,
                device=info.st_dev,
                inode=info.st_ino,
            ),
            fingerprint=filesystem_store_module._fingerprint(info),  # noqa: SLF001
            bytes_hashed=info.st_size,
        )

    monkeypatch.setattr(
        filesystem_store_module,
        "_hash_staged_file",
        consume_time_budget,
    )
    store = FileSystemArtifactStore(tmp_path, monotonic=monotonic)
    _force_sweep_slot(store, filesystem_store_module._STAGED_LEGACY_SLOT)

    result = await store.sweep_staged(
        active_tickets=set(),
        stale_before=10,
        budget=_budget(files=10, hashed_bytes=10_000, seconds=1),
    )

    assert result.scanned <= 10
    assert result.hashed_bytes == first.stat().st_size
    assert result.budget_exhausted is True
    assert result.deleted == 0
    assert hash_calls == 1
    assert clock[0] == 1


@pytest.mark.asyncio
async def test_hash_timeout_rotates_metadata_so_same_shard_record_can_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSystemArtifactStore(tmp_path)

    async def stage(ticket: str, payload: bytes):
        async def source():
            yield payload

        return await store.stage(
            UploadTicket(ticket),
            source(),
            max_bytes=len(payload),
        )

    blocking = await stage("ticket-blocking", b"x" * 1024)
    following = await stage("ticket-following", b"next")
    blocking_path = Path(blocking.path)
    following_path = Path(following.path)
    shard = 0
    blocking_metadata = _move_metadata_to_shard(
        store,
        Path(blocking.metadata_path or ""),
        shard=shard,
    )
    following_metadata = _move_metadata_to_shard(
        store,
        Path(following.metadata_path or ""),
        shard=shard,
    )
    _force_sweep_slot(store, shard)

    original_scan = FileSystemArtifactStore._scan_metadata_shard

    def blocking_first_scan(
        sweep_store: FileSystemArtifactStore,
        current_shard: int,
        *,
        max_files: int,
        deadline: float,
    ) -> Any:
        page = original_scan(
            sweep_store,
            current_shard,
            max_files=max_files,
            deadline=deadline,
        )
        return filesystem_store_module._ScanPage(  # noqa: SLF001
            paths=tuple(
                sorted(
                    page.paths,
                    key=lambda path: (path.name != blocking_metadata.name, path.name),
                )
            ),
            complete=page.complete,
            time_exhausted=page.time_exhausted,
        )

    clock = [0.0]
    hash_calls: list[Path] = []

    def monotonic() -> float:
        return clock[0]

    def timeout_blocking_file(
        path: Path,
        *,
        max_bytes: int,
        deadline: float,
        monotonic: Any,
    ) -> Any:
        del monotonic
        info = path.lstat()
        hash_calls.append(path)
        if path == blocking_path:
            hashed_bytes = min(info.st_size, max_bytes)
            clock[0] = deadline
            return filesystem_store_module._HashAttempt(  # noqa: SLF001
                identity=None,
                fingerprint=filesystem_store_module._fingerprint(info),  # noqa: SLF001
                bytes_hashed=hashed_bytes,
                budget_exhausted=True,
                deadline_exhausted=True,
            )
        payload = path.read_bytes()
        return filesystem_store_module._HashAttempt(  # noqa: SLF001
            identity=ArtifactIdentity(
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=info.st_size,
                device=info.st_dev,
                inode=info.st_ino,
            ),
            fingerprint=filesystem_store_module._fingerprint(info),  # noqa: SLF001
            bytes_hashed=info.st_size,
        )

    monkeypatch.setattr(
        FileSystemArtifactStore,
        "_scan_metadata_shard",
        blocking_first_scan,
    )
    monkeypatch.setattr(
        filesystem_store_module,
        "_hash_staged_file",
        timeout_blocking_file,
    )
    sweep_store = FileSystemArtifactStore(tmp_path, monotonic=monotonic)

    first = await sweep_store.sweep_staged(
        active_tickets=set(),
        stale_before=float("inf"),
        budget=_budget(files=2, hashed_bytes=2048, seconds=1),
    )

    assert first.budget_exhausted is True
    assert first.hashed_bytes == blocking_path.stat().st_size
    assert first.hashed_bytes <= 2048
    assert first.deleted == 0
    assert blocking_path.is_file()
    assert following_path.is_file()
    assert not blocking_metadata.exists()
    rotated_blocking_metadata = (
        store._metadata_shard_path(1) / blocking_metadata.name  # noqa: SLF001
    )
    assert rotated_blocking_metadata.exists()
    assert following_metadata.exists()
    assert first.next_cursor == f"v1:{shard}:"

    resumed_store = FileSystemArtifactStore(tmp_path, monotonic=monotonic)
    second = await resumed_store.sweep_staged(
        active_tickets=set(),
        stale_before=float("inf"),
        budget=_budget(files=2, hashed_bytes=2048, seconds=1),
    )

    assert second.hashed_bytes == len(b"next")
    assert second.hashed_bytes <= 2048
    assert second.deleted == 1
    assert blocking_path.is_file()
    assert not following_path.exists()
    assert hash_calls == [blocking_path, following_path]


@pytest.mark.asyncio
async def test_metadata_tombstone_advances_cursor_without_deferral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSystemArtifactStore(tmp_path)

    async def source():
        yield b"staged"

    staged = await store.stage(
        UploadTicket("ticket-tombstone"),
        source(),
        max_bytes=100,
    )
    metadata_path = Path(staged.metadata_path or "")
    shard = int(metadata_path.parent.name, 16)
    _force_sweep_slot(store, shard)
    original_scan = FileSystemArtifactStore._scan_metadata_shard
    deleted_paths: list[Path] = []
    tombstone_metrics: list[None] = []

    def scan_then_delete(
        sweep_store: FileSystemArtifactStore,
        current_shard: int,
        *,
        max_files: int,
        deadline: float,
    ) -> Any:
        page = original_scan(
            sweep_store,
            current_shard,
            max_files=max_files,
            deadline=deadline,
        )
        if page.paths and not deleted_paths:
            page.paths[0].unlink()
            deleted_paths.append(page.paths[0])
        return page

    monkeypatch.setattr(
        FileSystemArtifactStore,
        "_scan_metadata_shard",
        scan_then_delete,
    )
    monkeypatch.setattr(
        filesystem_store_module,
        "record_staged_sweep_tombstone",
        lambda: tombstone_metrics.append(None),
    )

    first = await store.sweep_staged(
        active_tickets=set(),
        stale_before=float("inf"),
        budget=_budget(files=1, hashed_bytes=100),
    )

    assert first.scanned == 1
    assert first.deferred == 0
    assert first.next_cursor == (
        f"v1:{(shard + 1) % filesystem_store_module._STAGED_SLOT_COUNT}:"
    )
    assert tombstone_metrics == [None]

    second = await store.sweep_staged(
        active_tickets=set(),
        stale_before=float("inf"),
        budget=_budget(files=1, hashed_bytes=100),
    )

    assert second.deferred == 0
    assert second.next_cursor != first.next_cursor
    assert tombstone_metrics == [None]


@pytest.mark.asyncio
async def test_file_larger_than_total_hash_budget_is_quarantined_without_stall(
    tmp_path: Path,
) -> None:
    huge = _legacy_sparse_file(
        tmp_path,
        ticket="ticket-huge",
        name="huge.source",
        size_bytes=10,
    )
    small = _legacy_sparse_file(
        tmp_path,
        ticket="ticket-small",
        name="small.source",
        size_bytes=4,
    )
    store = FileSystemArtifactStore(tmp_path)
    _force_sweep_slot(store, filesystem_store_module._STAGED_LEGACY_SLOT)

    result = await store.sweep_staged(
        active_tickets=set(),
        stale_before=10,
        budget=_budget(files=10, hashed_bytes=5),
    )

    assert result.quarantined == 1
    assert result.deleted == 1
    assert result.budget_exhausted is False
    assert huge.exists()
    assert not small.exists()
    quarantine = tmp_path / filesystem_store_module._STAGED_QUARANTINE_DIRECTORY
    assert len(list(quarantine.iterdir())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
async def test_active_or_fresh_staged_file_is_filtered_before_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active: bool,
) -> None:
    store = FileSystemArtifactStore(tmp_path)

    async def source():
        yield b"staged"

    staged = await store.stage(
        UploadTicket("ticket-active"),
        source(),
        max_bytes=100,
    )
    Path(staged.metadata_path or "").unlink()
    sweep_store = FileSystemArtifactStore(tmp_path)
    _force_sweep_slot(
        sweep_store,
        filesystem_store_module._STAGED_LEGACY_SLOT,
    )

    def unexpected_hash(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("active/fresh filtering must happen before hashing")

    monkeypatch.setattr(
        filesystem_store_module,
        "_hash_staged_file",
        unexpected_hash,
    )
    result = await sweep_store.sweep_staged(
        active_tickets={"ticket-active"} if active else set(),
        stale_before=(
            float("inf")
            if active
            else (staged.created_at or staged.modified_at or 0) - 1
        ),
        budget=_budget(files=1, hashed_bytes=100),
    )

    assert result.scanned == 1
    assert result.hashed_bytes == 0
    assert result.deleted == 0
    assert Path(staged.path).is_file()
    assert sweep_store._find_metadata_path(  # noqa: SLF001
        sweep_store._validated_staged_relative_path(  # noqa: SLF001
            Path(staged.path)
        )
    )


@pytest.mark.asyncio
async def test_identity_change_after_hash_is_refused_before_delete(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(tmp_path)

    async def source():
        yield b"original"

    staged = await store.stage(
        UploadTicket("ticket-race"),
        source(),
        max_bytes=100,
    )
    path = Path(staged.path)
    metadata_path = Path(staged.metadata_path or "")
    _force_sweep_slot(store, int(metadata_path.parent.name, 16))

    async def mutate_after_hash() -> None:
        path.write_bytes(b"mutated!")

    result = await store.sweep_staged(
        active_tickets=set(),
        stale_before=float("inf"),
        budget=_budget(files=1, hashed_bytes=100),
        before_delete=mutate_after_hash,
    )

    assert result.hashed_bytes == len(b"original")
    assert result.deleted == 0
    assert result.deferred == 1
    assert path.read_bytes() == b"mutated!"


@pytest.mark.asyncio
async def test_symlink_staged_entry_is_rejected_without_hash_or_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    ticket_dir = tmp_path / ".upload-tmp" / "ticket-symlink"
    ticket_dir.mkdir(mode=0o700, parents=True)
    staged_link = ticket_dir / "legacy.source"
    staged_link.symlink_to(outside)
    os.utime(staged_link, (1, 1), follow_symlinks=False)

    def unexpected_hash(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("symlink staged entries must never be hashed")

    monkeypatch.setattr(
        filesystem_store_module,
        "_hash_staged_file",
        unexpected_hash,
    )
    store = FileSystemArtifactStore(tmp_path)
    _force_sweep_slot(store, filesystem_store_module._STAGED_LEGACY_SLOT)

    result = await store.sweep_staged(
        active_tickets=set(),
        stale_before=10,
        budget=_budget(files=1, hashed_bytes=100),
    )

    assert result.scanned == 1
    assert result.deferred == 1
    assert result.deleted == 0
    assert staged_link.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_ensure_directory_closes_lstat_to_mkdir_race_and_is_concurrent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSystemArtifactStore(tmp_path)
    target = tmp_path / "shared" / "nested"
    original_mkdir = Path.mkdir
    injected = False

    def racing_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        nonlocal injected
        if path == tmp_path / "shared" and not injected:
            injected = True
            original_mkdir(path, mode=mode, parents=parents, exist_ok=True)
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    store._ensure_directory(target)  # noqa: SLF001

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: store._ensure_directory(target), range(32)))

    assert target.is_dir()


@pytest.mark.asyncio
async def test_reconciler_uses_bounded_sweep_and_exposes_sweep_stats() -> None:
    class Repository:
        async def list_reconcile_candidates(self, **_kwargs: Any) -> list[Any]:
            return []

        async def active_upload_tickets(
            self,
            candidate_tickets: set[str] | None = None,
        ) -> set[str]:
            active = {"active-ticket"}
            return active if candidate_tickets is None else active & candidate_tickets

    class Store:
        received: tuple[set[str], float, StagedSweepBudget] | None = None

        async def sweep_staged(
            self,
            *,
            active_tickets: set[str],
            stale_before: float,
            budget: StagedSweepBudget,
            load_active_tickets: Any = None,
            before_delete: Any = None,
        ) -> StagedSweepResult:
            del before_delete
            if load_active_tickets is not None:
                active_tickets = await load_active_tickets({"active-ticket"})
            self.received = (active_tickets, stale_before, budget)
            return StagedSweepResult(
                scanned=3,
                hashed_bytes=4096,
                deleted=1,
                deferred=2,
                quarantined=1,
                budget_exhausted=True,
                next_cursor="v1:2:",
            )

    store = Store()
    stats = await ImageArtifactReconciler(
        repository=Repository(),  # type: ignore[arg-type]
        artifacts=store,  # type: ignore[arg-type]
        storage_capacity=_StorageCapacity(),
    ).run_once(
        max_files_per_pass=7,
        max_bytes_hashed_per_pass=8192,
        max_seconds_per_pass=0.5,
    )

    assert store.received is not None
    assert store.received[0] == {"active-ticket"}
    assert store.received[2] == _budget(files=7, hashed_bytes=8192, seconds=0.5)
    assert stats.scanned == 3
    assert stats.hashed_bytes == 4096
    assert stats.deleted_staged == 1
    assert stats.deferred == 2
    assert stats.quarantined_staged == 1
    assert stats.budget_exhausted is True
    assert stats.next_cursor == "v1:2:"


@pytest.mark.asyncio
async def test_sweep_failure_preserves_row_reconcile_and_records_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id="image-1",
        artifact_status=ArtifactStatus.STAGING.value,
        updated_at=now - timedelta(minutes=10),
    )

    class Repository:
        def __init__(self) -> None:
            self.transition_calls = 0

        async def list_reconcile_candidates(self, **_kwargs: Any) -> list[Any]:
            return [row]

        async def transition(self, *_args: Any, **_kwargs: Any) -> None:
            self.transition_calls += 1

        async def active_upload_tickets(
            self,
            _candidate_tickets: set[str] | None = None,
        ) -> set[str]:
            return set()

    class Store:
        root = tmp_path

        async def sweep_staged(self, **_kwargs: Any) -> StagedSweepResult:
            error = PermissionError("staged index unavailable")
            error._lumen_sweep_cursor = "v1:2:"  # type: ignore[attr-defined]
            error._lumen_sweep_slot = 2  # type: ignore[attr-defined]
            raise error

    repository = Repository()
    metric_reasons: list[str] = []
    monkeypatch.setattr(
        reconcile_policy_module,
        "record_staged_sweep_failure",
        metric_reasons.append,
    )

    with caplog.at_level(
        logging.ERROR,
        logger="app.images.application.reconcile_policy",
    ):
        stats = await ImageArtifactReconciler(
            repository=repository,  # type: ignore[arg-type]
            artifacts=Store(),  # type: ignore[arg-type]
            storage_capacity=_StorageCapacity(),
        ).run_once(
            now=now,
            stale_after=timedelta(seconds=0),
            max_files_per_pass=7,
            max_bytes_hashed_per_pass=8192,
            max_seconds_per_pass=0.5,
        )

    assert repository.transition_calls == 1
    assert stats.marked_failed == 1
    assert stats.deferred == 1
    assert stats.sweep_error_code == "permission_error"
    assert metric_reasons == ["permission_error"]
    record = next(
        item for item in caplog.records if item.message == "image staged sweep failed"
    )
    assert record.sweep_error_class == "PermissionError"
    assert record.sweep_error_code == "permission_error"
    assert record.sweep_cursor == "v1:2:"
    assert record.sweep_slot == 2
    assert record.sweep_max_files == 7
    assert record.sweep_max_bytes == 8192
    assert record.sweep_max_seconds == 0.5
    assert record.sweep_root == str(tmp_path)
    assert record.reconcile_instance == f"pid:{os.getpid()}"
    assert record.reconcile_fence is None


@pytest.mark.asyncio
async def test_sweep_lease_loss_is_not_classified_or_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Repository:
        async def list_reconcile_candidates(self, **_kwargs: Any) -> list[Any]:
            return []

        async def active_upload_tickets(
            self,
            _candidate_tickets: set[str] | None = None,
        ) -> set[str]:
            return set()

    class Store:
        async def sweep_staged(self, **_kwargs: Any) -> StagedSweepResult:
            raise ReconcileLeaseLost("lease lost during sweep")

    metric_reasons: list[str] = []
    monkeypatch.setattr(
        reconcile_policy_module,
        "record_staged_sweep_failure",
        metric_reasons.append,
    )

    with pytest.raises(ReconcileLeaseLost, match="lease lost during sweep"):
        await ImageArtifactReconciler(
            repository=Repository(),  # type: ignore[arg-type]
            artifacts=Store(),  # type: ignore[arg-type]
            storage_capacity=_StorageCapacity(),
        ).run_once()

    assert metric_reasons == []
