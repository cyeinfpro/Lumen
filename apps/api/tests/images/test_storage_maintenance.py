from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any

import pytest

from app.images.application import storage_maintenance
from app.images.application.reconcile_policy import ReconcileLeaseLost
from app.images.application.storage_maintenance import (
    OrphanSweepBudget,
    _discover_candidates,
    sweep_orphan_image_files,
)


class _Rows:
    def __init__(self, *, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _Rows:
        return self


class _EmptyDb:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, _statement: Any) -> _Rows:
        self.calls += 1
        return _Rows()


class _OrderedScandir:
    def __init__(self, iterator: Any, entries: list[Any]) -> None:
        self._iterator = iterator
        self._entries = entries

    def __enter__(self) -> _OrderedScandir:
        return self

    def __exit__(self, *_args: Any) -> None:
        self._iterator.close()

    def __iter__(self) -> Any:
        return iter(self._entries)


def _force_upload_order(
    monkeypatch: pytest.MonkeyPatch,
    names: list[str],
) -> None:
    real_scandir = os.scandir
    rank = {name: index for index, name in enumerate(names)}

    def ordered_scandir(path: Any) -> _OrderedScandir:
        iterator = real_scandir(path)
        entries = list(iterator)
        if Path(path).name == "uploads":
            entries.sort(
                key=lambda entry: (
                    rank.get(entry.name, len(rank)),
                    entry.name,
                )
            )
        return _OrderedScandir(iterator, entries)

    monkeypatch.setattr(storage_maintenance.os, "scandir", ordered_scandir)


def _orphan(root: Path, name: str, size: int = 8) -> Path:
    path = root / "u" / "user-1" / "uploads" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _storage_file(root: Path, key: str, size: int = 8) -> Path:
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.mark.asyncio
async def test_orphan_sweep_uses_file_byte_and_cursor_budgets(
    tmp_path: Path,
) -> None:
    for index in range(5):
        _orphan(tmp_path, f"{index}.webp", size=10)
    db = _EmptyDb()

    first = await sweep_orphan_image_files(
        db,  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        max_files=2,
        max_entries=100,
        max_bytes=25,
        max_seconds=10,
        minimum_age_seconds=0,
    )
    assert first["scanned"] == 2
    assert first["bytes_scanned"] == 20
    assert first["budget_exhausted"] is True
    assert first["next_cursor"]

    second = await sweep_orphan_image_files(
        db,  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        cursor=first["next_cursor"],
        max_files=2,
        max_entries=100,
        max_bytes=25,
        max_seconds=10,
        minimum_age_seconds=0,
    )
    assert second["scanned"] <= 2
    assert set(first["orphans"]).isdisjoint(second["orphans"])
    assert db.calls >= 2


@pytest.mark.asyncio
async def test_byte_budget_stop_retries_unconsumed_file_without_starvation(
    tmp_path: Path,
) -> None:
    _orphan(tmp_path, "a.webp", size=7)
    _orphan(tmp_path, "b.webp", size=7)

    first = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        max_files=10,
        max_entries=100,
        max_bytes=10,
        max_seconds=10,
        minimum_age_seconds=0,
    )
    assert first["orphans"] == ["u/user-1/uploads/a.webp"]
    assert first["budget_exhausted"] is True
    assert first["next_cursor"] is not None

    second = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        cursor=first["next_cursor"],
        max_files=10,
        max_entries=100,
        max_bytes=10,
        max_seconds=10,
        minimum_age_seconds=0,
    )
    assert second["orphans"] == ["u/user-1/uploads/b.webp"]
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_database_budget_stop_keeps_discovery_cursor_moving_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orphan(tmp_path, "a.webp")
    _orphan(tmp_path, "b.webp")

    async def database_timeout(*_args: Any, **_kwargs: Any) -> tuple[set[str], bool]:
        return set(), True

    monkeypatch.setattr(
        storage_maintenance,
        "_load_known_storage_keys",
        database_timeout,
    )
    result = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        max_files=1,
        max_entries=100,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=0,
    )

    assert result["database_timed_out"] is True
    assert result["budget_exhausted"] is True
    assert result["next_cursor"] is not None


@pytest.mark.asyncio
async def test_orphan_cursor_uses_stable_order_independent_of_scandir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orphan(tmp_path, "z.webp")
    _orphan(tmp_path, "a.webp")
    _force_upload_order(monkeypatch, ["z.webp", "a.webp"])

    cursor: str | None = None
    seen: list[str] = []
    for _ in range(4):
        result = await sweep_orphan_image_files(
            _EmptyDb(),  # type: ignore[arg-type]
            storage_root=str(tmp_path),
            cursor=cursor,
            max_files=1,
            max_entries=10,
            max_bytes=1024,
            max_seconds=10,
            minimum_age_seconds=0,
        )
        assert result["entries_scanned"] <= 10
        assert result["scanned"] <= 1
        seen.extend(result["orphans"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("circular orphan cursor did not complete its round")

    assert seen == [
        "u/user-1/uploads/a.webp",
        "u/user-1/uploads/z.webp",
    ]


@pytest.mark.asyncio
async def test_orphan_sweep_does_not_read_sparse_hundred_gigabyte_file(
    tmp_path: Path,
) -> None:
    path = _orphan(tmp_path, "huge.webp", size=1)
    with path.open("r+b") as handle:
        handle.truncate(100 * 1024 * 1024 * 1024)

    result = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        max_files=10,
        max_entries=100,
        max_bytes=1024 * 1024,
        max_seconds=10,
        minimum_age_seconds=0,
    )

    assert result["scanned"] == 0
    assert result["bytes_scanned"] == 0
    assert result["oversized"] == ["u/user-1/uploads/huge.webp"]
    assert path.exists()


@pytest.mark.asyncio
async def test_orphan_sweep_skips_leaf_lock_and_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _storage_file(tmp_path, "u/user-1/uploads/a.webp")
    publish_lock = _storage_file(
        tmp_path,
        "u/user-1/uploads/.artifact-publish.lock",
    )
    lifecycle_lock = _storage_file(
        tmp_path,
        "u/user-1/uploads/.artifact-lifecycle.lock",
    )
    atomic_tmp = _storage_file(tmp_path, "u/user-1/uploads/.a.webp.abcd1234.tmp")
    variant_tmp = _storage_file(
        tmp_path,
        "u/user-1/g/generation-task/.lumen-variant-0123.webp",
    )

    async def no_known_keys(_db: Any, _candidates: set[str]) -> set[str]:
        return set()

    monkeypatch.setattr(
        storage_maintenance,
        "_known_storage_keys",
        no_known_keys,
    )
    result = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
        max_files=10,
        max_entries=100,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=0,
    )

    assert result["orphans"] == ["u/user-1/uploads/a.webp"]
    assert result["deleted"] == 1
    assert publish_lock.exists()
    assert lifecycle_lock.exists()
    assert atomic_tmp.exists()
    assert variant_tmp.exists()


def test_orphan_discovery_stops_at_wall_clock_budget(tmp_path: Path) -> None:
    for index in range(10):
        _orphan(tmp_path, f"{index}.webp")
    ticks = iter([0.0, 0.1, 1.1, 1.2])

    discovery = _discover_candidates(
        tmp_path,
        cursor=None,
        budget=OrphanSweepBudget(
            max_files=10,
            max_entries=100,
            max_bytes=1024,
            max_seconds=1.0,
        ),
        monotonic=lambda: next(ticks, 2.0),
        minimum_modified_at=None,
    )

    assert discovery.budget_exhausted is True
    assert discovery.entries_scanned <= 2
    assert len(discovery.candidates) <= 2
    assert discovery.next_cursor is not None


@pytest.mark.asyncio
async def test_orphan_delete_rechecks_identity_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _orphan(tmp_path, "changed.webp")

    async def mutate_before_delete(_db: Any, _candidates: set[str]) -> set[str]:
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(b"replacement")
        replacement.replace(path)
        return set()

    monkeypatch.setattr(
        storage_maintenance,
        "_known_storage_keys",
        mutate_before_delete,
    )
    result = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
        max_files=10,
        max_entries=100,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=0,
    )

    assert result["deleted"] == 0
    assert result["changed"] == ["u/user-1/uploads/changed.webp"]
    assert path.read_bytes() == b"replacement"


@pytest.mark.asyncio
async def test_orphan_delete_detects_same_inode_same_size_in_place_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _orphan(tmp_path, "changed-in-place.webp")
    path.write_bytes(b"original")
    before = path.stat()
    calls = 0

    async def rewrite_before_delete(
        _db: Any,
        _candidates: set[str],
    ) -> set[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            time.sleep(0.002)
            path.write_bytes(b"changed!")
            os.utime(
                path,
                ns=(before.st_atime_ns, before.st_mtime_ns),
            )
            after = path.stat()
            assert after.st_dev == before.st_dev
            assert after.st_ino == before.st_ino
            assert after.st_size == before.st_size
            assert after.st_mtime_ns == before.st_mtime_ns
            assert after.st_ctime_ns != before.st_ctime_ns
        return set()

    monkeypatch.setattr(
        storage_maintenance,
        "_known_storage_keys",
        rewrite_before_delete,
    )
    result = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
        max_files=10,
        max_entries=100,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=0,
    )

    assert calls == 2
    assert result["deleted"] == 0
    assert result["changed"] == ["u/user-1/uploads/changed-in-place.webp"]
    assert path.read_bytes() == b"changed!"


@pytest.mark.asyncio
async def test_young_file_advances_cursor_then_is_revisited_next_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    young = _orphan(tmp_path, "a-young.webp")
    old = _orphan(tmp_path, "z-old.webp")
    now = 10_000.0
    os.utime(young, (now, now))
    os.utime(old, (now - 7200, now - 7200))
    _force_upload_order(monkeypatch, ["z-old.webp", "a-young.webp"])

    first = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
        max_files=1,
        max_entries=1,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=3600,
        wall_time=lambda: now,
    )
    assert first["deleted"] == 0
    assert first["too_young"] == ["u/user-1/uploads/a-young.webp"]
    assert first["budget_exhausted"] is True
    assert first["next_cursor"] is not None

    second = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
        cursor=first["next_cursor"],
        max_files=1,
        max_entries=1,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=3600,
        wall_time=lambda: now,
    )
    assert second["deleted"] == 1
    assert not old.exists()
    assert young.exists()

    completed = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
        cursor=second["next_cursor"],
        max_files=1,
        max_entries=1,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=3600,
        wall_time=lambda: now,
    )
    assert completed["next_cursor"] is None
    assert young.exists()

    revisited = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
        max_files=10,
        max_entries=10,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=3600,
        wall_time=lambda: now + 7200,
    )
    assert revisited["deleted"] == 1
    assert not young.exists()


@pytest.mark.asyncio
async def test_orphan_delete_rechecks_database_reference_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _orphan(tmp_path, "referenced-during-sweep.webp")
    calls = 0

    async def become_known(_db: Any, candidates: set[str]) -> set[str]:
        nonlocal calls
        calls += 1
        return set() if calls == 1 else set(candidates)

    monkeypatch.setattr(
        storage_maintenance,
        "_known_storage_keys",
        become_known,
    )
    result = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
        max_files=10,
        max_entries=100,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=0,
    )

    assert calls == 2
    assert result["deleted"] == 0
    assert result["orphans"] == []
    assert path.exists()


@pytest.mark.asyncio
async def test_orphan_delete_stops_when_lease_guard_fails_before_unlink(
    tmp_path: Path,
) -> None:
    path = _orphan(tmp_path, "lease-lost.webp")
    guard_calls = 0

    async def assert_owned() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 3:
            raise ReconcileLeaseLost("lease lost before unlink")

    with pytest.raises(ReconcileLeaseLost, match="before unlink"):
        await sweep_orphan_image_files(
            _EmptyDb(),  # type: ignore[arg-type]
            storage_root=str(tmp_path),
            dry_run=False,
            max_files=10,
            max_entries=100,
            max_bytes=1024,
            max_seconds=10,
            minimum_age_seconds=0,
            assert_owned=assert_owned,
        )

    assert guard_calls == 3
    assert path.exists()


@pytest.mark.asyncio
async def test_nested_attempt_cursor_resumes_across_all_supported_layouts(
    tmp_path: Path,
) -> None:
    keys = [
        "u/user-1/uploads/legacy-upload.webp",
        "u/user-1/g/legacy-generation/orig.png",
        "u/user-1/g/generation-task/attempts/2/orig.png",
        "u/user-1/g/generation-task/attempts/2/display2048.webp",
        "u/user-1/g/generation-task/attempts/3/orig.png",
        "u/user-1/g/generation-task/executions/7/attempts/4/orig.png",
        (
            "u/user-1/completion-tools/completion-task/"
            "attempts/3/completion-image/orig.png"
        ),
        (
            "u/user-1/completion-tools/completion-task/"
            "attempts/execution-9-attempt-3/legacy-image/orig.png"
        ),
        (
            "u/user-1/completion-tools/completion-task/"
            "attempts/3/completion-image/display2048.webp"
        ),
        ("u/user-1/completion-tools/completion-task/attempts/3/second-image/orig.png"),
        (
            "u/user-1/completion-tools/completion-task/"
            "executions/9/attempts/3/execution-image/orig.png"
        ),
        "u/user-1/v/legacy-video/output.mp4",
        "u/user-1/v/video-task/final/stable-finalization/output.mp4",
        "u/user-1/vref/reference-video/original.mp4",
        "u/user-1/storyboards/run-1/assembly/version-1/output.mp4",
    ]
    for key in keys:
        _storage_file(tmp_path, key)

    cursor: str | None = None
    seen: list[str] = []
    for _ in range(len(keys) + 2):
        result = await sweep_orphan_image_files(
            _EmptyDb(),  # type: ignore[arg-type]
            storage_root=str(tmp_path),
            cursor=cursor,
            max_files=1,
            max_entries=100,
            max_bytes=1024,
            max_seconds=10,
            minimum_age_seconds=0,
        )
        assert result["entries_scanned"] <= 100
        assert result["scanned"] <= 1
        seen.extend(result["orphans"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("nested orphan cursor did not complete its round")

    assert len(seen) == len(set(seen))
    assert set(seen) == set(keys)


@pytest.mark.asyncio
async def test_nested_anchor_seek_resumes_without_replaying_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = [f"u/user-1/g/g{index:03d}/attempts/1/orig.png" for index in range(100)]
    for key in keys:
        _storage_file(tmp_path, key)

    real_scandir = os.scandir
    scandir_calls: list[Path] = []

    def counted_scandir(path: Any) -> Any:
        scandir_calls.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(storage_maintenance.os, "scandir", counted_scandir)
    result = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        cursor=keys[-1],
        max_files=1,
        max_entries=3,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=0,
    )

    assert result["entries_scanned"] == 1
    assert result["scanned"] == 0
    assert result["budget_exhausted"] is False
    assert result["next_cursor"] is None
    assert len(scandir_calls) <= 15


def test_nested_anchor_seek_finishes_with_tight_wall_clock_budget(
    tmp_path: Path,
) -> None:
    keys = [f"u/user-1/g/g{index:03d}/attempts/1/orig.png" for index in range(100)]
    for key in keys:
        _storage_file(tmp_path, key)
    ticks = iter([0.0, 0.1, 0.2, 1.1, 1.2])

    discovery = _discover_candidates(
        tmp_path,
        cursor=keys[-1],
        budget=OrphanSweepBudget(
            max_files=10,
            max_entries=1_000,
            max_bytes=1024,
            max_seconds=1.0,
        ),
        monotonic=lambda: next(ticks, 2.0),
        minimum_modified_at=None,
    )

    assert discovery.entries_scanned == 1
    assert discovery.candidates == ()
    assert discovery.budget_exhausted is False
    assert discovery.next_cursor is None


@pytest.mark.asyncio
async def test_empty_leaf_directories_advance_cursor_and_finish_round(
    tmp_path: Path,
) -> None:
    for index in range(12):
        (tmp_path / "u" / "user-1" / "g" / f"g{index:03d}").mkdir(
            parents=True,
        )

    cursor: str | None = None
    exhausted_cursors: list[str] = []
    for _ in range(20):
        result = await sweep_orphan_image_files(
            _EmptyDb(),  # type: ignore[arg-type]
            storage_root=str(tmp_path),
            cursor=cursor,
            max_files=1,
            max_entries=1,
            max_bytes=1024,
            max_seconds=10,
            minimum_age_seconds=0,
        )
        assert result["scanned"] == 0
        if result["budget_exhausted"]:
            assert result["next_cursor"] is not None
            assert result["next_cursor"] != cursor
            exhausted_cursors.append(result["next_cursor"])
            cursor = result["next_cursor"]
            continue
        assert result["next_cursor"] is None
        break
    else:
        pytest.fail("empty-directory orphan sweep did not finish its round")

    assert len(exhausted_cursors) == len(set(exhausted_cursors))
    assert len(exhausted_cursors) >= 10


@pytest.mark.asyncio
async def test_orphan_sweep_rejects_symlink_and_cursor_path_escape(
    tmp_path: Path,
) -> None:
    outside_root = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_attempt = outside_root / "generation-attempt"
    outside_attempt.mkdir(parents=True)
    outside_file = outside_attempt / "orig.png"
    outside_file.write_bytes(b"outside")

    linked_attempt = (
        tmp_path / "u" / "user-1" / "g" / "generation-task" / "attempts" / "2"
    )
    linked_attempt.parent.mkdir(parents=True)
    linked_attempt.symlink_to(outside_attempt, target_is_directory=True)

    real_attempt = linked_attempt.parent / "3"
    real_attempt.mkdir()
    linked_file = real_attempt / "orig.png"
    linked_file.symlink_to(outside_file)

    completion_image = (
        tmp_path
        / "u"
        / "user-1"
        / "completion-tools"
        / "completion-task"
        / "attempts"
        / "4"
        / "completion-image"
    )
    completion_image.parent.mkdir(parents=True)
    completion_image.symlink_to(outside_attempt, target_is_directory=True)

    result = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
        cursor="u/user-1/g/generation-task/attempts/2/orig.png",
        max_files=10,
        max_entries=100,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=0,
    )
    escaped = await sweep_orphan_image_files(
        _EmptyDb(),  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
        cursor="../../generation-attempt/orig.png",
        max_files=10,
        max_entries=100,
        max_bytes=1024,
        max_seconds=10,
        minimum_age_seconds=0,
    )

    assert result["scanned"] == 0
    assert result["deleted"] == 0
    assert escaped["scanned"] == 0
    assert escaped["deleted"] == 0
    assert outside_file.read_bytes() == b"outside"
    assert linked_attempt.is_symlink()
    assert linked_file.is_symlink()
    assert completion_image.is_symlink()
