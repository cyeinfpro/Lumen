from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from image_job import (
    durable_files,
    retention_dir_reader,
    retention_row_cursor,
    retention_scan_store,
    retention_walk,
)
from image_job.persistence import RetentionFacade
from image_job.persistence_parts import retention as retention_module
from image_job.persistence_parts import (
    retention_references as reference_retention,
)


class _ScandirSequence:
    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = iter(entries)

    def __iter__(self) -> _ScandirSequence:
        return self

    def __next__(self) -> os.DirEntry[str]:
        return next(self._entries)

    def close(self) -> None:
        return None


def _sorted_scandir(
    real_scandir: Any,
    directory_fd: int,
) -> _ScandirSequence:
    with real_scandir(directory_fd) as entries:
        ordered = sorted(entries, key=lambda entry: entry.name)
        for entry in ordered:
            entry.stat(follow_symlinks=False)
    return _ScandirSequence(ordered)


def _ordered_page_reader(
    real_scandir: Any,
    *,
    page_entries: int = 64,
) -> Any:
    def read_page(
        directory_fd: int,
        *,
        device: int,
        inode: int,
        offset: int,
        buffer_bytes: int,
    ) -> retention_walk.DirectoryPage:
        info = os.fstat(directory_fd)
        assert (info.st_dev, info.st_ino) == (device, inode)
        with real_scandir(directory_fd) as entries:
            names = sorted(os.fsencode(entry.name) for entry in entries)
        start = min(offset, len(names))
        stop = min(start + page_entries, len(names))
        reached_end = stop == len(names)
        return retention_walk.DirectoryPage(
            names=tuple(names[start:stop]),
            next_offset=stop,
            reached_end=reached_end,
            bytes_read=(
                0
                if start == stop
                else buffer_bytes - 1
                if reached_end
                else buffer_bytes
            ),
        )

    return read_page


def _facade(tmp_path: Path, **overrides: Any) -> RetentionFacade:
    async def db_exec(_sql: str, _params: tuple[Any, ...]) -> int:
        return 0

    async def db_all(
        _sql: str,
        _params: tuple[Any, ...],
    ) -> list[Any]:
        return []

    values: dict[str, Any] = {
        "data_dir": lambda: tmp_path / "data",
        "refs_dir": lambda: tmp_path / "refs",
        "db_exec_sync": lambda _sql, _params: 0,
        "db_exec": db_exec,
        "db_all": db_all,
        "utc_now": lambda: datetime(2026, 7, 27, tzinfo=timezone.utc),
        "max_retention_days": lambda: 30,
        "job_ttl_days": lambda: 30,
        "log": logging.getLogger(__name__),
    }
    values.update(overrides)
    return RetentionFacade(**values)


def _old_job_dir(
    tmp_path: Path,
    job_id: str,
    *,
    day: str = "01",
    file_count: int = 1,
) -> Path:
    job_dir = (
        tmp_path
        / "data"
        / "images"
        / "temp"
        / "2026"
        / "07"
        / day
        / job_id
    )
    job_dir.mkdir(parents=True)
    for index in range(file_count):
        (job_dir / f"image-{index}.png").write_bytes(b"x")
    os.utime(job_dir, (1, 1))
    return job_dir


def _reference_db(
    tmp_path: Path,
) -> tuple[Path, Any]:
    db_path = tmp_path / "refs.sqlite3"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            """
            CREATE TABLE refs (
                auth_hash TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                token TEXT NOT NULL,
                ext TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(auth_hash, sha256)
            )
            """
        )
    finally:
        conn.close()

    def open_db() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    return db_path, open_db


@pytest.mark.asyncio
async def test_finished_rows_query_is_ordered_and_limited(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    async def db_all(
        sql: str,
        params: tuple[Any, ...],
    ) -> list[Any]:
        calls.append((sql, params))
        return []

    facade = _facade(
        tmp_path,
        db_all=db_all,
        finished_row_batch_size=7,
        sweep_filesystem_fn=lambda _cutoff: (0, 0),
    )

    await facade.run_pass()

    assert len(calls) == 2
    backfill_sql, backfill_params = calls[0]
    normalized_backfill = " ".join(backfill_sql.split()).upper()
    assert "WHERE FINISHED_AT IS NOT NULL" in normalized_backfill
    assert "RETENTION_EXPIRES_AT IS NULL" in normalized_backfill
    assert "FINISHED_AT ASC, JOB_ID ASC" in normalized_backfill
    assert "CASE WHEN FINISHED_AT > ?" in normalized_backfill
    assert "LIMIT ?" in normalized_backfill
    assert backfill_params == ("", "", "", 7)

    expiry_sql, expiry_params = calls[1]
    normalized_expiry = " ".join(expiry_sql.split()).upper()
    assert "RETENTION_EXPIRES_AT <= ?" in normalized_expiry
    assert (
        "RETENTION_EXPIRES_AT ASC, JOB_ID ASC"
        in normalized_expiry
    )
    assert "CASE WHEN RETENTION_EXPIRES_AT > ?" in normalized_expiry
    assert "LIMIT ?" in normalized_expiry
    assert expiry_params == (
        "2026-07-27T00:00:00+00:00",
        "",
        "",
        "",
        7,
    )


@pytest.mark.asyncio
async def test_long_lived_old_row_cannot_starve_indexed_expired_row(
    tmp_path: Path,
) -> None:
    calls = 0
    expired_row = {
        "job_id": "expired-later",
        "status": "succeeded",
        "created_at": "2026-07-01T00:00:00+00:00",
        "finished_at": "2026-07-20T00:00:00+00:00",
        "retention_days": 1,
        "images_json": "[]",
        "retention_expires_at": "2026-07-21T00:00:00+00:00",
    }

    async def db_all(
        sql: str,
        _params: tuple[Any, ...],
    ) -> list[Any]:
        nonlocal calls
        calls += 1
        if "retention_expires_at IS NULL" in sql:
            return []
        return [expired_row]

    deleted: list[str] = []

    async def db_exec(sql: str, params: tuple[Any, ...]) -> int:
        if sql.lstrip().startswith("DELETE FROM jobs"):
            deleted.append(str(params[0]))
        return 1

    facade = _facade(
        tmp_path,
        db_all=db_all,
        db_exec=db_exec,
        finished_row_batch_size=1,
        sweep_filesystem_fn=lambda _cutoff: (0, 0),
    )

    await facade.run_pass()

    assert calls == 2
    assert deleted == ["expired-later"]


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", [".", ".."])
async def test_noncanonical_job_id_cannot_delete_live_partition_artifacts(
    tmp_path: Path,
    job_id: str,
) -> None:
    live_dir = _old_job_dir(tmp_path, "live-job")
    live_artifact = live_dir / "image-0.png"
    poison_row = {
        "job_id": job_id,
        "status": "succeeded",
        "created_at": "2026-07-01T00:00:00+00:00",
        "finished_at": "2026-07-01T00:00:00+00:00",
        "retention_days": 1,
        "images_json": "[]",
        "retention_expires_at": "2026-07-02T00:00:00+00:00",
    }
    calls = 0

    async def db_all(
        sql: str,
        _params: tuple[Any, ...],
    ) -> list[Any]:
        nonlocal calls
        calls += 1
        if "retention_expires_at IS NULL" in sql:
            return []
        if "WHERE job_id IN" in sql:
            return [{"job_id": "live-job"}]
        return [poison_row]

    statements: list[tuple[str, tuple[Any, ...]]] = []

    async def db_exec(sql: str, params: tuple[Any, ...]) -> int:
        statements.append((sql, params))
        return 1

    facade = _facade(
        tmp_path,
        db_all=db_all,
        db_exec=db_exec,
        sweep_filesystem_fn=lambda _cutoff: (0, 0),
    )

    await facade.run_pass()

    assert calls >= 2
    assert live_artifact.exists()
    assert any(sql.lstrip().startswith("UPDATE jobs") for sql, _ in statements)
    assert not any(sql.lstrip().startswith("DELETE FROM jobs") for sql, _ in statements)


def test_ref_scan_and_delete_stop_at_configured_budgets(
    tmp_path: Path,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    paths = [refs_dir / f"ref-{index}.png" for index in range(3)]
    for path in paths:
        path.write_bytes(b"x")
        os.utime(path, (1, 1))
    statements: list[tuple[str, tuple[Any, ...]]] = []

    def db_exec_sync(sql: str, params: tuple[Any, ...]) -> int:
        statements.append((sql, params))
        return 0

    facade = _facade(
        tmp_path,
        db_exec_sync=db_exec_sync,
        ref_scan_max_entries=2,
        ref_delete_batch_size=5,
        scan_time_budget_s=60,
    )

    removed_files, removed_bytes = facade.sweep_filesystem(2)

    assert (removed_files, removed_bytes) == (1, 1)
    assert sum(path.exists() for path in paths) == 2
    assert statements == []


def test_ref_scan_stops_at_monotonic_deadline(tmp_path: Path) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    paths = [refs_dir / f"ref-{index}.png" for index in range(3)]
    for path in paths:
        path.write_bytes(b"x")
        os.utime(path, (1, 1))
    ticks = iter((0.0, 0.0, 2.0))

    facade = _facade(
        tmp_path,
        ref_scan_max_entries=100,
        scan_time_budget_s=1,
        monotonic=lambda: next(ticks, 2.0),
    )

    removed_files, removed_bytes = facade.sweep_dir(refs_dir, 2)

    assert (removed_files, removed_bytes) == (0, 0)
    assert sum(path.exists() for path in paths) == 3


def test_deadline_cursor_reaches_orphan_after_live_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    for name in ("a.png", "b.png", "c.png"):
        path = refs_dir / name
        path.write_bytes(b"live")
        os.utime(path, (3, 3))
    orphan = refs_dir / "d.png"
    orphan.write_bytes(b"x")
    os.utime(orphan, (1, 1))
    real_scandir = retention_walk._scandir_directory
    calls = 0

    def deadline_clock() -> float:
        nonlocal calls
        value = 2.0 if calls % 5 == 4 else 0.0
        calls += 1
        return value

    monkeypatch.setattr(
        retention_walk,
        "_read_directory_page",
        _ordered_page_reader(real_scandir),
    )
    facade = _facade(
        tmp_path,
        ref_scan_max_entries=100,
        scan_time_budget_s=1,
        monotonic=deadline_clock,
    )

    passes = []
    for _pass in range(5):
        passes.append(facade.sweep_dir(refs_dir, 2))
        if not orphan.exists():
            break

    assert (1, 1) in passes
    assert not orphan.exists()
    assert {
        path.name
        for path in refs_dir.iterdir()
    } == {"a.png", "b.png", "c.png"}


def test_reference_scan_cursor_advances_past_poison_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    paths = [refs_dir / name for name in ("a.png", "b.png", "c.png")]
    for path in paths:
        path.write_bytes(path.name.encode())
        os.utime(path, (1, 1))
    real_scandir = retention_walk._scandir_directory
    real_unlink = retention_walk.unlink_verified_entry
    attempts: list[str] = []

    def fail_first(
        directory: retention_walk.DirectoryHandle,
        entry: retention_walk.EntrySnapshot,
    ) -> None:
        attempts.append(entry.name)
        if entry.name == "a.png":
            raise PermissionError("injected poison reference")
        real_unlink(directory, entry)

    monkeypatch.setattr(
        retention_walk,
        "_read_directory_page",
        _ordered_page_reader(real_scandir),
    )
    monkeypatch.setattr(
        retention_walk,
        "unlink_verified_entry",
        fail_first,
    )
    facade = _facade(
        tmp_path,
        ref_scan_max_entries=1,
        scan_time_budget_s=60,
    )

    for _pass in range(8):
        facade.sweep_dir(refs_dir, 2)

    assert attempts == ["a.png", "b.png", "c.png", "a.png"]
    assert (refs_dir / "a.png").exists()
    assert not (refs_dir / "b.png").exists()
    assert not (refs_dir / "c.png").exists()


def test_persisted_ref_retention_fsyncs_parent_before_metadata_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    target = refs_dir / "old-token.png"
    target.write_bytes(b"x")
    os.utime(target, (1, 1))
    db_path, open_db = _reference_db(tmp_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            """
            INSERT INTO refs(auth_hash, sha256, token, ext, size, created_at)
            VALUES ('owner', 'sha', 'old-token', 'png', 1, '1960-01-01T00:00:00+00:00')
            """
        )
    finally:
        conn.close()
    synced: list[tuple[int, int]] = []
    real_sync = retention_walk.fsync_directory_fd

    def tracking_sync(directory_fd: int) -> None:
        info = os.fstat(directory_fd)
        synced.append((info.st_dev, info.st_ino))
        real_sync(directory_fd)

    monkeypatch.setattr(
        retention_walk,
        "fsync_directory_fd",
        tracking_sync,
    )
    facade = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
    )

    assert facade.sweep_filesystem(2) == (1, 1)
    root_info = refs_dir.stat()
    assert synced[-1] == (root_info.st_dev, root_info.st_ino)
    assert len(synced) >= 2
    assert not target.exists()
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 0
    finally:
        check.close()


def test_ref_metadata_survives_parent_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    target = refs_dir / "old-token.png"
    target.write_bytes(b"x")
    os.utime(target, (1, 1))
    db_path, open_db = _reference_db(tmp_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            """
            INSERT INTO refs(auth_hash, sha256, token, ext, size, created_at)
            VALUES ('owner', 'sha', 'old-token', 'png', 1, '1960-01-01T00:00:00+00:00')
            """
        )
    finally:
        conn.close()

    refs_info = refs_dir.stat()

    def fail_sync(directory_fd: int) -> None:
        info = os.fstat(directory_fd)
        if (info.st_dev, info.st_ino) == (
            refs_info.st_dev,
            refs_info.st_ino,
        ):
            raise OSError("injected parent fsync failure")
        os.fsync(directory_fd)

    monkeypatch.setattr(
        retention_walk,
        "fsync_directory_fd",
        fail_sync,
    )
    facade = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
    )

    assert facade.sweep_filesystem(2) == (0, 0)
    assert not target.exists()
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 1
    finally:
        check.close()


@pytest.mark.skipif(
    not retention_walk.descriptor_relative_traversal_available(),
    reason="descriptor-relative traversal is unavailable",
)
def test_verified_unlink_restores_leaf_replaced_before_atomic_detach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    target = refs_dir / "old-token.png"
    target.write_bytes(b"original")
    displaced = refs_dir / "old-token.displaced"
    real_rename = retention_walk._rename_entry
    swapped = False

    def replace_before_rename(
        source: str,
        quarantine_name: str,
        source_fd: int,
        quarantine_fd: int,
    ) -> None:
        nonlocal swapped
        if not swapped and source == target.name:
            target.rename(displaced)
            target.write_bytes(b"replacement")
            swapped = True
        real_rename(
            source,
            quarantine_name,
            source_fd,
            quarantine_fd,
        )

    monkeypatch.setattr(
        retention_walk,
        "_rename_entry",
        replace_before_rename,
    )
    with retention_walk.open_directory(refs_dir) as directory:
        expected = retention_walk.directory_entry_snapshot(
            directory,
            target.name,
        )
        with pytest.raises(OSError):
            retention_walk.unlink_verified_entry(directory, expected)

    assert swapped
    assert target.read_bytes() == b"replacement"
    assert displaced.read_bytes() == b"original"
    assert not any(
        path.name.startswith(".retention-quarantine-")
        for path in refs_dir.iterdir()
    )


@pytest.mark.skipif(
    not retention_walk.descriptor_relative_traversal_available(),
    reason="descriptor-relative traversal is unavailable",
)
def test_verified_unlink_restores_directory_replacement_before_atomic_detach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    target = refs_dir / "old-token.png"
    target.write_bytes(b"original")
    displaced = refs_dir / "old-token.displaced"
    real_rename = retention_walk._rename_entry
    swapped = False

    def replace_before_rename(
        source: str,
        quarantine_name: str,
        source_fd: int,
        quarantine_fd: int,
    ) -> None:
        nonlocal swapped
        if not swapped and source == target.name:
            target.rename(displaced)
            target.mkdir()
            target.joinpath("live.txt").write_bytes(b"live-directory")
            swapped = True
        real_rename(
            source,
            quarantine_name,
            source_fd,
            quarantine_fd,
        )

    monkeypatch.setattr(
        retention_walk,
        "_rename_entry",
        replace_before_rename,
    )
    with retention_walk.open_directory(refs_dir) as directory:
        expected = retention_walk.directory_entry_snapshot(
            directory,
            target.name,
        )
        with pytest.raises(OSError):
            retention_walk.unlink_verified_entry(directory, expected)

    assert swapped
    assert target.is_dir()
    assert target.joinpath("live.txt").read_bytes() == b"live-directory"
    assert displaced.read_bytes() == b"original"
    assert not any(
        path.name.startswith(
            (".retention-quarantine-", "retention-preserved-")
        )
        for path in refs_dir.iterdir()
    )


@pytest.mark.skipif(
    not retention_walk.descriptor_relative_traversal_available(),
    reason="descriptor-relative traversal is unavailable",
)
def test_mismatched_quarantine_collision_is_visible_and_never_swept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    target = refs_dir / "old-token.png"
    target.write_bytes(b"original")
    displaced = refs_dir / "old-token.displaced"
    real_rename = retention_walk._rename_entry
    swapped = False

    def replace_and_recreate(
        source: str,
        quarantine_name: str,
        source_fd: int,
        quarantine_fd: int,
    ) -> None:
        nonlocal swapped
        if not swapped and source == target.name:
            target.rename(displaced)
            target.mkdir()
            target.joinpath("live.txt").write_bytes(b"preserved-directory")
            real_rename(
                source,
                quarantine_name,
                source_fd,
                quarantine_fd,
            )
            target.write_bytes(b"newer-visible-entry")
            swapped = True
            return
        real_rename(
            source,
            quarantine_name,
            source_fd,
            quarantine_fd,
        )

    monkeypatch.setattr(
        retention_walk,
        "_rename_entry",
        replace_and_recreate,
    )
    with retention_walk.open_directory(refs_dir) as directory:
        expected = retention_walk.directory_entry_snapshot(
            directory,
            target.name,
        )
        with pytest.raises(OSError):
            retention_walk.unlink_verified_entry(directory, expected)

    preserved = next(
        path
        for path in refs_dir.iterdir()
        if path.name.startswith("retention-preserved-")
    )
    assert target.read_bytes() == b"newer-visible-entry"
    assert preserved.joinpath("entry", "live.txt").read_bytes() == (
        b"preserved-directory"
    )
    os.utime(target, (3, 3))
    os.utime(preserved, (1, 1))
    facade = _facade(
        tmp_path,
        ref_scan_max_entries=20,
        scan_time_budget_s=60,
    )

    for _pass in range(3):
        facade.sweep_dir(refs_dir, 2)

    assert target.read_bytes() == b"newer-visible-entry"
    assert preserved.joinpath("entry", "live.txt").read_bytes() == (
        b"preserved-directory"
    )


def test_ref_leaf_recreated_after_detach_keeps_file_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    target = refs_dir / "old-token.png"
    target.write_bytes(b"original")
    os.utime(target, (1, 1))
    db_path, open_db = _reference_db(tmp_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            """
            INSERT INTO refs(auth_hash, sha256, token, ext, size, created_at)
            VALUES ('owner', 'sha', 'old-token', 'png', 8, '1960-01-01T00:00:00+00:00')
            """
        )
    finally:
        conn.close()
    real_rename = retention_walk._rename_entry
    replaced = False

    def recreate_after_detach(
        source: str,
        quarantine_name: str,
        source_fd: int,
        quarantine_fd: int,
    ) -> None:
        nonlocal replaced
        real_rename(
            source,
            quarantine_name,
            source_fd,
            quarantine_fd,
        )
        if not replaced and source == target.name:
            target.write_bytes(b"replacement")
            replaced = True

    monkeypatch.setattr(
        retention_walk,
        "_rename_entry",
        recreate_after_detach,
    )
    facade = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
    )

    assert facade.sweep_filesystem(2) == (0, 0)
    assert replaced
    assert target.read_bytes() == b"replacement"
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 1
    finally:
        check.close()


def test_ref_root_replacement_before_metadata_delete_keeps_live_tree_and_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    target = refs_dir / "old-token.png"
    target.write_bytes(b"original")
    os.utime(target, (1, 1))
    displaced_root = tmp_path / "refs-displaced"
    live_root = tmp_path / "refs-live"
    live_root.mkdir()
    live_target = live_root / target.name
    live_target.write_bytes(b"live")
    db_path, open_db = _reference_db(tmp_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            """
            INSERT INTO refs(auth_hash, sha256, token, ext, size, created_at)
            VALUES ('owner', 'sha', 'old-token', 'png', 8, '1960-01-01T00:00:00+00:00')
            """
        )
    finally:
        conn.close()
    real_unlink = reference_retention.unlink_verified_entry
    swapped = False

    def swap_root_after_unlink(
        directory: retention_walk.DirectoryHandle,
        entry: retention_walk.EntrySnapshot,
    ) -> None:
        nonlocal swapped
        real_unlink(directory, entry)
        refs_dir.rename(displaced_root)
        refs_dir.symlink_to(live_root, target_is_directory=True)
        swapped = True

    monkeypatch.setattr(
        reference_retention,
        "unlink_verified_entry",
        swap_root_after_unlink,
    )
    facade = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
    )

    assert facade.sweep_filesystem(2) == (0, 0)
    assert swapped
    assert live_target.read_bytes() == b"live"
    assert not (displaced_root / target.name).exists()
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 1
    finally:
        check.close()


def test_ref_root_replacement_before_commit_rolls_back_metadata_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    target = refs_dir / "old-token.png"
    target.write_bytes(b"original")
    os.utime(target, (1, 1))
    displaced_root = tmp_path / "refs-displaced"
    live_root = tmp_path / "refs-live"
    live_root.mkdir()
    live_target = live_root / target.name
    live_target.write_bytes(b"live")
    db_path, open_db = _reference_db(tmp_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            """
            INSERT INTO refs(auth_hash, sha256, token, ext, size, created_at)
            VALUES ('owner', 'sha', 'old-token', 'png', 8, '1960-01-01T00:00:00+00:00')
            """
        )
    finally:
        conn.close()
    real_match = reference_retention.directory_path_matches
    checks = 0

    def swap_on_commit(guard: retention_walk.DirectoryPathGuard) -> bool:
        nonlocal checks
        checks += 1
        if checks == 2:
            refs_dir.rename(displaced_root)
            refs_dir.symlink_to(live_root, target_is_directory=True)
        return real_match(guard)

    monkeypatch.setattr(
        reference_retention,
        "directory_path_matches",
        swap_on_commit,
    )
    facade = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
    )

    assert facade.sweep_filesystem(2) == (0, 0)
    assert checks >= 2
    assert live_target.read_bytes() == b"live"
    assert not (displaced_root / target.name).exists()
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 1
    finally:
        check.close()


def test_ref_retention_normalizes_legacy_timestamps_at_exact_cutoff(
    tmp_path: Path,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    db_path, open_db = _reference_db(tmp_path)
    rows = (
        ("before", "2026-08-01 11:59:59"),
        ("exact", "2026-08-01 12:00:00"),
        ("same-day-newer", "2026-08-01 23:59:59"),
    )
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        for token, created_at in rows:
            (refs_dir / f"{token}.png").write_bytes(b"x")
            conn.execute(
                """
                INSERT INTO refs(
                    auth_hash, sha256, token, ext, size, created_at
                ) VALUES (?, ?, ?, 'png', 1, ?)
                """,
                (f"owner-{token}", f"sha-{token}", token, created_at),
            )
    finally:
        conn.close()
    cutoff = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()
    facade = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
    )

    assert facade.sweep_filesystem(cutoff) == (1, 1)
    assert not (refs_dir / "before.png").exists()
    assert (refs_dir / "exact.png").exists()
    assert (refs_dir / "same-day-newer.png").exists()
    check = sqlite3.connect(db_path)
    try:
        remaining = {
            row[0]
            for row in check.execute("SELECT token FROM refs ORDER BY token")
        }
    finally:
        check.close()
    assert remaining == {"exact", "same-day-newer"}


def test_poison_ref_is_retried_without_starving_newer_expired_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    poison = refs_dir / "poison.png"
    healthy = refs_dir / "healthy.png"
    for path in (poison, healthy):
        path.write_bytes(b"x")
        os.utime(path, (1, 1))
    db_path, open_db = _reference_db(tmp_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        for token in ("poison", "healthy"):
            conn.execute(
                """
                INSERT INTO refs(
                    auth_hash, sha256, token, ext, size, created_at
                ) VALUES (?, ?, ?, 'png', 1, '1960-01-01 00:00:00')
                """,
                (f"owner-{token}", f"sha-{token}", token),
            )
    finally:
        conn.close()
    attempts: list[str] = []
    real_unlink = reference_retention.unlink_verified_entry

    def fail_poison(
        directory: retention_walk.DirectoryHandle,
        entry: retention_walk.EntrySnapshot,
    ) -> None:
        attempts.append(entry.name)
        if entry.name == poison.name:
            raise PermissionError("injected undeletable reference")
        real_unlink(directory, entry)

    monkeypatch.setattr(
        reference_retention,
        "unlink_verified_entry",
        fail_poison,
    )
    facade = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_delete_batch_size=1,
        ref_scan_max_entries=10,
        scan_time_budget_s=60,
    )

    assert facade.sweep_filesystem(2) == (0, 0)
    assert facade.sweep_filesystem(2) == (1, 1)
    assert poison.exists()
    assert not healthy.exists()
    assert facade.sweep_filesystem(2) == (0, 0)
    assert attempts == ["poison.png", "healthy.png", "poison.png"]
    check = sqlite3.connect(db_path)
    try:
        remaining = check.execute("SELECT token FROM refs").fetchall()
    finally:
        check.close()
    assert remaining == [("poison",)]


def test_reference_row_cursor_survives_facade_rebuild_after_poison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    poison = refs_dir / "a-poison.png"
    healthy = refs_dir / "b-healthy.png"
    for path in (poison, healthy):
        path.write_bytes(b"x")
        os.utime(path, (1, 1))
    db_path, open_db = _reference_db(tmp_path)
    conn = open_db()
    try:
        for token in ("a-poison", "b-healthy"):
            conn.execute(
                """
                INSERT INTO refs(
                    auth_hash, sha256, token, ext, size, created_at
                ) VALUES (?, ?, ?, 'png', 1, '1960-01-01T00:00:00+00:00')
                """,
                (f"owner-{token}", f"sha-{token}", token),
            )
    finally:
        conn.close()
    real_unlink = reference_retention.unlink_verified_entry
    attempts: list[str] = []

    def fail_poison(
        directory: retention_walk.DirectoryHandle,
        entry: retention_walk.EntrySnapshot,
    ) -> None:
        attempts.append(entry.name)
        if entry.name == poison.name:
            raise PermissionError("injected undeletable reference")
        real_unlink(directory, entry)

    monkeypatch.setattr(
        reference_retention,
        "unlink_verified_entry",
        fail_poison,
    )
    for _pass in range(3):
        facade = _facade(
            tmp_path,
            refs_dir=lambda: refs_dir,
            open_db=open_db,
            ref_delete_batch_size=1,
            ref_scan_max_entries=1,
            scan_time_budget_s=60,
        )
        facade.sweep_filesystem(2)

    assert attempts == [
        "a-poison.png",
        "b-healthy.png",
        "a-poison.png",
    ]
    assert poison.exists()
    assert not healthy.exists()
    check = sqlite3.connect(db_path)
    try:
        state = check.execute(
            """
            SELECT generation, cursor_rowid
            FROM retention_row_cursors
            WHERE scope = 'reference-db-cleanup'
            """
        ).fetchone()
    finally:
        check.close()
    assert state is not None
    assert state[0] == 1


def test_reference_row_cursor_advances_past_512_poison_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    poison_tokens = [f"p-{index:04d}" for index in range(512)]
    healthy_token = "z-healthy"
    db_path, open_db = _reference_db(tmp_path)
    conn = open_db()
    try:
        rows = []
        for token in (*poison_tokens, healthy_token):
            path = refs_dir / f"{token}.png"
            path.write_bytes(b"x")
            os.utime(path, (1, 1))
            rows.append(
                (
                    f"owner-{token}",
                    f"sha-{token}",
                    token,
                    "png",
                    1,
                    "1960-01-01T00:00:00+00:00",
                )
            )
        conn.executemany(
            """
            INSERT INTO refs(
                auth_hash, sha256, token, ext, size, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    finally:
        conn.close()
    real_unlink = reference_retention.unlink_verified_entry

    def fail_poison(
        directory: retention_walk.DirectoryHandle,
        entry: retention_walk.EntrySnapshot,
    ) -> None:
        if entry.name != f"{healthy_token}.png":
            raise PermissionError("injected poison batch")
        real_unlink(directory, entry)

    monkeypatch.setattr(
        reference_retention,
        "unlink_verified_entry",
        fail_poison,
    )
    first = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_delete_batch_size=512,
        ref_scan_max_entries=1,
        scan_time_budget_s=60,
    )
    assert first.sweep_filesystem(2) == (0, 0)
    assert (refs_dir / f"{healthy_token}.png").exists()

    second = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_delete_batch_size=512,
        ref_scan_max_entries=1,
        scan_time_budget_s=60,
    )
    assert second.sweep_filesystem(2) == (1, 1)
    assert not (refs_dir / f"{healthy_token}.png").exists()
    assert all((refs_dir / f"{token}.png").exists() for token in poison_tokens)
    check = sqlite3.connect(db_path)
    try:
        remaining = check.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        cursor_rows = check.execute(
            "SELECT COUNT(*) FROM retention_row_cursors"
        ).fetchone()[0]
    finally:
        check.close()
    assert remaining == 512
    assert cursor_rows == 1


def test_reference_row_cursor_wraps_fairly_by_created_time(
    tmp_path: Path,
) -> None:
    _db_path, open_db = _reference_db(tmp_path)
    conn = open_db()
    try:
        conn.execute(
            """
            INSERT INTO refs(
                rowid, auth_hash, sha256, token, ext, size, created_at
            ) VALUES (100, 'owner-newer', 'sha-newer', 'newer', 'png', 1,
                      '1960-01-02T00:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO refs(
                rowid, auth_hash, sha256, token, ext, size, created_at
            ) VALUES (200, 'owner-older', 'sha-older', 'older', 'png', 1,
                      '1960-01-01T00:00:00+00:00')
            """
        )
        generations: list[int] = []
        rowids: list[int] = []
        for _pass in range(4):
            claimed = retention_row_cursor.DurableReferenceRowCursor(
                scope="test-fair-wrap"
            ).claim(
                conn,
                "1970-01-01T00:00:00+00:00",
                1,
            )
            assert len(claimed) == 1
            rowids.append(claimed[0].rowid)
            generations.append(claimed[0].generation)
    finally:
        conn.close()

    assert rowids == [200, 100, 200, 100]
    assert generations == [0, 0, 1, 1]


def test_reference_row_cursor_handles_deleted_cursor_and_rowid_wrap(
    tmp_path: Path,
) -> None:
    _db_path, open_db = _reference_db(tmp_path)
    conn = open_db()
    try:
        high_rowid = 9_223_372_036_854_775_806
        conn.execute(
            """
            INSERT INTO refs(
                rowid, auth_hash, sha256, token, ext, size, created_at
            ) VALUES (?, 'owner-high', 'sha-high', 'high', 'png', 1,
                      '1960-01-01T00:00:00+00:00')
            """,
            (high_rowid,),
        )
        cursor = retention_row_cursor.DurableReferenceRowCursor(
            scope="test-rowid-wrap"
        )
        first = cursor.claim(
            conn,
            "1970-01-01T00:00:00+00:00",
            1,
        )
        assert [candidate.rowid for candidate in first] == [high_rowid]
        conn.execute("DELETE FROM refs WHERE rowid = ?", (high_rowid,))
        conn.execute(
            """
            INSERT INTO refs(
                rowid, auth_hash, sha256, token, ext, size, created_at
            ) VALUES (1, 'owner-low', 'sha-low', 'low', 'png', 1,
                      '1960-01-01T00:00:00+00:00')
            """
        )
        second = retention_row_cursor.DurableReferenceRowCursor(
            scope="test-rowid-wrap"
        ).claim(
            conn,
            "1970-01-01T00:00:00+00:00",
            1,
        )
    finally:
        conn.close()

    assert [candidate.rowid for candidate in second] == [1]
    assert second[0].generation == 1


def test_reference_row_claim_does_not_delete_reused_identity(
    tmp_path: Path,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    old_file = refs_dir / "old.png"
    replacement_file = refs_dir / "replacement.png"
    old_file.write_bytes(b"old")
    replacement_file.write_bytes(b"replacement")
    os.utime(old_file, (1, 1))
    os.utime(replacement_file, (1, 1))
    _db_path, open_db = _reference_db(tmp_path)
    conn = open_db()
    try:
        conn.execute(
            """
            INSERT INTO refs(
                rowid, auth_hash, sha256, token, ext, size, created_at
            ) VALUES (1, 'owner-old', 'sha-old', 'old', 'png', 3,
                      '1960-01-01T00:00:00+00:00')
            """
        )
        cursor = retention_row_cursor.DurableReferenceRowCursor(
            scope="reference-db-cleanup"
        )
        claimed = cursor.claim(
            conn,
            "1970-01-01T00:00:00+00:00",
            1,
        )
        conn.execute("DELETE FROM refs WHERE rowid = 1")
        conn.execute(
            """
            INSERT INTO refs(
                rowid, auth_hash, sha256, token, ext, size, created_at
            ) VALUES (1, 'owner-new', 'sha-new', 'replacement', 'png', 11,
                      '1960-01-01T00:00:00+00:00')
            """
        )
        facade = _facade(
            tmp_path,
            refs_dir=lambda: refs_dir,
            open_db=open_db,
        )
        with retention_walk.open_directory(refs_dir) as refs:
            assert (
                facade._retire_reference_row(  # noqa: SLF001
                    conn,
                    claimed[0].rowid,
                    claimed[0].created_at,
                    claimed[0].auth_hash,
                    claimed[0].sha256,
                    claimed[0].token,
                    claimed[0].ext,
                    "1970-01-01T00:00:00+00:00",
                    refs,
                )
                == (0, 0, True)
            )
        assert replacement_file.read_bytes() == b"replacement"
        assert conn.execute(
            "SELECT token FROM refs WHERE rowid = 1"
        ).fetchone()[0] == "replacement"

        replacement_claim = retention_row_cursor.DurableReferenceRowCursor(
            scope="reference-db-cleanup"
        ).claim(
            conn,
            "1970-01-01T00:00:00+00:00",
            1,
        )
        with retention_walk.open_directory(refs_dir) as refs:
            assert (
                facade._retire_reference_row(  # noqa: SLF001
                    conn,
                    replacement_claim[0].rowid,
                    replacement_claim[0].created_at,
                    replacement_claim[0].auth_hash,
                    replacement_claim[0].sha256,
                    replacement_claim[0].token,
                    replacement_claim[0].ext,
                    "1970-01-01T00:00:00+00:00",
                    refs,
                )
                == (1, len(b"replacement"), True)
            )
    finally:
        conn.close()

    assert old_file.read_bytes() == b"old"
    assert not replacement_file.exists()


def test_reference_row_cursor_claim_transaction_rolls_back_on_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, open_db = _reference_db(tmp_path)
    conn = open_db()
    try:
        conn.execute(
            """
            INSERT INTO refs(
                auth_hash, sha256, token, ext, size, created_at
            ) VALUES ('owner', 'sha', 'candidate', 'png', 1,
                      '1960-01-01T00:00:00+00:00')
            """
        )
        real_store = (
            retention_row_cursor.DurableReferenceRowCursor._store_claim
        )

        def crash_after_update(
            self: retention_row_cursor.DurableReferenceRowCursor,
            connection: sqlite3.Connection,
            generation: int,
            last_created_jd: float,
            last_rowid: int,
            current_time: float,
        ) -> None:
            real_store(
                self,
                connection,
                generation,
                last_created_jd,
                last_rowid,
                current_time,
            )
            raise RuntimeError("injected cursor transaction crash")

        monkeypatch.setattr(
            retention_row_cursor.DurableReferenceRowCursor,
            "_store_claim",
            crash_after_update,
        )
        cursor = retention_row_cursor.DurableReferenceRowCursor(
            scope="test-claim-crash"
        )
        with pytest.raises(RuntimeError, match="transaction crash"):
            cursor.claim(
                conn,
                "1970-01-01T00:00:00+00:00",
                1,
            )
    finally:
        conn.close()

    check = sqlite3.connect(db_path)
    try:
        state_count = check.execute(
            """
            SELECT COUNT(*)
            FROM retention_row_cursors
            WHERE scope = 'test-claim-crash'
            """
        ).fetchone()[0]
    finally:
        check.close()
    assert state_count == 0

    monkeypatch.setattr(
        retention_row_cursor.DurableReferenceRowCursor,
        "_store_claim",
        real_store,
    )
    conn = open_db()
    try:
        claimed = retention_row_cursor.DurableReferenceRowCursor(
            scope="test-claim-crash"
        ).claim(
            conn,
            "1970-01-01T00:00:00+00:00",
            1,
        )
    finally:
        conn.close()
    assert [candidate.rowid for candidate in claimed] == [1]
    assert claimed[0].generation == 0


def test_reference_row_cursor_state_is_bounded_and_pruned(
    tmp_path: Path,
) -> None:
    db_path, open_db = _reference_db(tmp_path)
    conn = open_db()
    try:
        for index in range(40):
            retention_row_cursor.DurableReferenceRowCursor(
                scope=f"test-scope-{index:02d}",
                now=lambda value=float(index): value,
            ).claim(
                conn,
                "1970-01-01T00:00:00+00:00",
                1,
            )
    finally:
        conn.close()
    check = sqlite3.connect(db_path)
    try:
        bounded_count = check.execute(
            "SELECT COUNT(*) FROM retention_row_cursors"
        ).fetchone()[0]
    finally:
        check.close()
    assert bounded_count <= 16

    conn = open_db()
    try:
        retention_row_cursor.DurableReferenceRowCursor(
            scope="test-current",
            now=lambda: 8 * 24 * 60 * 60,
        ).claim(
            conn,
            "1970-01-01T00:00:00+00:00",
            1,
        )
    finally:
        conn.close()
    check = sqlite3.connect(db_path)
    try:
        remaining = check.execute(
            "SELECT scope FROM retention_row_cursors"
        ).fetchall()
    finally:
        check.close()
    assert remaining == [("test-current",)]


def test_reference_row_cursor_advances_while_root_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    for token in ("a-first", "b-second"):
        path = refs_dir / f"{token}.png"
        path.write_bytes(b"x")
        os.utime(path, (1, 1))
    _db_path, open_db = _reference_db(tmp_path)
    conn = open_db()
    try:
        for token in ("a-first", "b-second"):
            conn.execute(
                """
                INSERT INTO refs(
                    auth_hash, sha256, token, ext, size, created_at
                ) VALUES (?, ?, ?, 'png', 1,
                          '1960-01-01T00:00:00+00:00')
                """,
                (f"owner-{token}", f"sha-{token}", token),
            )
    finally:
        conn.close()
    displaced = refs_dir.with_name("refs-displaced")
    refs_dir.rename(displaced)
    first = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_delete_batch_size=1,
        ref_scan_max_entries=1,
        scan_time_budget_s=60,
    )
    assert first.sweep_filesystem(2) == (0, 0)
    displaced.rename(refs_dir)

    real_unlink = reference_retention.unlink_verified_entry
    attempts: list[str] = []

    def record_unlink(
        directory: retention_walk.DirectoryHandle,
        entry: retention_walk.EntrySnapshot,
    ) -> None:
        attempts.append(entry.name)
        real_unlink(directory, entry)

    monkeypatch.setattr(
        reference_retention,
        "unlink_verified_entry",
        record_unlink,
    )
    for _pass in range(2):
        facade = _facade(
            tmp_path,
            refs_dir=lambda: refs_dir,
            open_db=open_db,
            ref_delete_batch_size=1,
            ref_scan_max_entries=1,
            scan_time_budget_s=60,
        )
        facade.sweep_filesystem(2)

    assert attempts == ["b-second.png", "a-first.png"]
    assert not list(refs_dir.glob("*.png"))


def test_ref_orphan_sweep_reclaims_crash_gap_publish(tmp_path: Path) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    target = refs_dir / "crash-gap.png"
    temporary = refs_dir / "crash-gap.png.tmp-publish"
    durable_files.atomic_write_bytes(
        target,
        b"crash-gap",
        temporary_path=temporary,
    )
    os.utime(target, (1, 1))
    _db_path, open_db = _reference_db(tmp_path)
    facade = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_scan_max_entries=10,
        scan_time_budget_s=60,
    )

    assert facade.sweep_filesystem(2) == (1, len(b"crash-gap"))
    assert not target.exists()


def test_ref_orphan_sweep_preserves_new_exact_cutoff_and_staging_files(
    tmp_path: Path,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    cutoff = 2.0
    new_file = refs_dir / "new-token.png"
    exact_file = refs_dir / "exact-token.jpg"
    staging_file = refs_dir / "staged-token.webp.tmp-active"
    readiness_file = refs_dir / ".readiness"
    for path, mtime in (
        (new_file, cutoff + 1),
        (exact_file, cutoff),
        (staging_file, 1),
        (readiness_file, 1),
    ):
        path.write_bytes(b"x")
        os.utime(path, (mtime, mtime))
    _db_path, open_db = _reference_db(tmp_path)
    facade = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_scan_max_entries=10,
        scan_time_budget_s=60,
    )

    assert facade.sweep_filesystem(cutoff) == (0, 0)
    assert {
        path.name
        for path in refs_dir.iterdir()
    } == {
        ".readiness",
        "exact-token.jpg",
        "new-token.png",
        "staged-token.webp.tmp-active",
    }


@pytest.mark.asyncio
async def test_job_metadata_survives_artifact_parent_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = _old_job_dir(tmp_path, "job-fsync")
    row = {
        "job_id": "job-fsync",
        "status": "succeeded",
        "created_at": "2026-07-01T00:00:00+00:00",
        "finished_at": "2026-07-02T00:00:00+00:00",
        "retention_days": 1,
        "images_json": "[]",
        "retention_expires_at": "2026-07-02T00:00:00+00:00",
    }
    statements: list[str] = []

    async def db_all(
        sql: str,
        _params: tuple[Any, ...],
    ) -> list[Any]:
        if "retention_expires_at IS NULL" in sql:
            return []
        return [row]

    async def db_exec(sql: str, _params: tuple[Any, ...]) -> int:
        statements.append(" ".join(sql.split()).upper())
        return 1

    job_info = job_dir.stat()
    real_sync = retention_walk.fsync_directory_fd

    def fail_sync(directory_fd: int) -> None:
        info = os.fstat(directory_fd)
        if (info.st_dev, info.st_ino) == (
            job_info.st_dev,
            job_info.st_ino,
        ):
            raise OSError("injected job directory fsync failure")
        real_sync(directory_fd)

    monkeypatch.setattr(
        retention_walk,
        "fsync_directory_fd",
        fail_sync,
    )
    facade = _facade(
        tmp_path,
        db_all=db_all,
        db_exec=db_exec,
        sweep_filesystem_fn=lambda _cutoff: (0, 0),
    )

    await facade.run_pass()

    assert not any(statement.startswith("DELETE FROM JOBS") for statement in statements)


def test_temp_root_replacement_before_job_row_commit_keeps_live_tree_and_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = _old_job_dir(tmp_path, "expired-job")
    temp_root = tmp_path / "data" / "images" / "temp"
    row = {
        "job_id": "expired-job",
        "status": "succeeded",
        "created_at": "2026-07-01T00:00:00+00:00",
        "finished_at": "2026-07-02T00:00:00+00:00",
        "retention_days": 1,
        "images_json": "[]",
        "retention_expires_at": "2026-07-02T00:00:00+00:00",
    }
    db_path = tmp_path / "jobs.sqlite3"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                finished_at TEXT,
                retention_expires_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO jobs(job_id, status, finished_at, retention_expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                row["job_id"],
                row["status"],
                row["finished_at"],
                row["retention_expires_at"],
            ),
        )
    finally:
        conn.close()

    def open_db() -> sqlite3.Connection:
        return sqlite3.connect(db_path, isolation_level=None)

    facade = _facade(tmp_path, open_db=open_db)
    files, freed, cleaned, guard, relative_parts = (
        facade._remove_job_artifacts_status(row)  # noqa: SLF001
    )
    assert (files, freed, cleaned) == (1, 1, True)
    assert guard is not None
    assert relative_parts is not None
    assert not job_dir.exists()

    displaced_root = temp_root.with_name("temp-displaced")
    live_root = tmp_path / "live-temp"
    live_job = live_root.joinpath(*relative_parts)
    live_job.mkdir(parents=True)
    live_artifact = live_job / "live.png"
    live_artifact.write_bytes(b"live")
    real_absent = retention_module.verified_relative_path_absent
    checks = 0

    def swap_before_commit(
        pinned_guard: retention_walk.DirectoryPathGuard,
        pinned_parts: tuple[str, ...],
    ) -> bool:
        nonlocal checks
        checks += 1
        if checks == 2:
            temp_root.rename(displaced_root)
            temp_root.symlink_to(live_root, target_is_directory=True)
        return real_absent(pinned_guard, pinned_parts)

    monkeypatch.setattr(
        retention_module,
        "verified_relative_path_absent",
        swap_before_commit,
    )

    assert (
        facade._delete_job_row_if_guard_valid(  # noqa: SLF001
            row["job_id"],
            row["status"],
            row["retention_expires_at"],
            guard,
            relative_parts,
        )
        == 0
    )
    assert checks == 2
    assert live_artifact.read_bytes() == b"live"
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        check.close()


@pytest.mark.skipif(
    not retention_walk.descriptor_relative_traversal_available(),
    reason="descriptor-relative traversal is unavailable",
)
def test_job_scan_uses_verified_fd_after_path_is_swapped_to_live_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = _old_job_dir(tmp_path, "expired-job")
    live_dir = _old_job_dir(tmp_path, "live-job")
    live_artifact = live_dir / "image-0.png"
    displaced = job_dir.with_name("expired-job-displaced")
    job_info = job_dir.stat()
    real_read_page = retention_walk._read_directory_page
    swapped = False

    def force_swap(
        directory_fd: int,
        **kwargs: Any,
    ) -> retention_walk.DirectoryPage:
        nonlocal swapped
        info = os.fstat(directory_fd)
        if (
            not swapped
            and (info.st_dev, info.st_ino)
            == (job_info.st_dev, job_info.st_ino)
        ):
            job_dir.rename(displaced)
            job_dir.symlink_to(live_dir, target_is_directory=True)
            swapped = True
        return real_read_page(directory_fd, **kwargs)

    monkeypatch.setattr(
        retention_walk,
        "_read_directory_page",
        force_swap,
    )
    facade = _facade(
        tmp_path,
        job_dir_scan_max_entries=10,
        scan_time_budget_s=60,
    )
    temp_root = tmp_path / "data" / "images" / "temp"

    assert facade.remove_job_dir(job_dir, temp_root) == (1, 1, False)
    assert swapped
    assert job_dir.is_symlink()
    assert live_artifact.read_bytes() == b"x"
    assert not (displaced / "image-0.png").exists()


@pytest.mark.skipif(
    not retention_walk.descriptor_relative_traversal_available(),
    reason="descriptor-relative traversal is unavailable",
)
def test_nested_directory_swap_to_live_symlink_is_never_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = _old_job_dir(
        tmp_path,
        "expired-job",
        file_count=0,
    )
    nested = job_dir / "nested"
    nested.mkdir()
    nested_artifact = nested / "nested.png"
    nested_artifact.write_bytes(b"expired")
    displaced = job_dir / "nested-displaced"
    live_dir = _old_job_dir(tmp_path, "live-job")
    live_artifact = live_dir / "image-0.png"
    job_info = job_dir.stat()
    real_open = retention_walk._open_directory_at
    swapped = False

    def force_swap(name: str, flags: int, parent_fd: int) -> int:
        nonlocal swapped
        parent_info = os.fstat(parent_fd)
        if (
            not swapped
            and name == nested.name
            and (parent_info.st_dev, parent_info.st_ino)
            == (job_info.st_dev, job_info.st_ino)
        ):
            nested.rename(displaced)
            nested.symlink_to(live_dir, target_is_directory=True)
            swapped = True
        return real_open(name, flags, parent_fd)

    monkeypatch.setattr(
        retention_walk,
        "_open_directory_at",
        force_swap,
    )
    facade = _facade(
        tmp_path,
        job_dir_scan_max_entries=10,
        scan_time_budget_s=60,
    )
    temp_root = tmp_path / "data" / "images" / "temp"

    assert facade.remove_job_dir(job_dir, temp_root) == (0, 0, False)
    assert swapped
    assert nested.is_symlink()
    assert live_artifact.read_bytes() == b"x"
    assert (displaced / nested_artifact.name).read_bytes() == b"expired"


def test_job_cleanup_fails_closed_without_descriptor_primitives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = _old_job_dir(tmp_path, "expired-job")
    artifact = job_dir / "image-0.png"
    monkeypatch.setattr(
        retention_module,
        "descriptor_relative_traversal_available",
        lambda: False,
    )
    facade = _facade(tmp_path)
    temp_root = tmp_path / "data" / "images" / "temp"

    assert facade.remove_job_dir(job_dir, temp_root) == (0, 0, False)
    assert artifact.read_bytes() == b"x"


def test_descriptor_relative_cleanup_removes_normal_nested_tree(
    tmp_path: Path,
) -> None:
    job_dir = _old_job_dir(tmp_path, "expired-job")
    nested = job_dir / "nested"
    nested.mkdir()
    nested_artifact = nested / "nested.png"
    nested_artifact.write_bytes(b"nested")
    deeper = nested / "deeper"
    deeper.mkdir()
    deep_artifact = deeper / "deep.webp"
    deep_artifact.write_bytes(b"deep")
    facade = _facade(
        tmp_path,
        job_dir_scan_max_entries=20,
        scan_time_budget_s=60,
    )
    temp_root = tmp_path / "data" / "images" / "temp"

    assert facade.remove_job_dir(job_dir, temp_root) == (
        3,
        1 + len(b"nested") + len(b"deep"),
        True,
    )
    assert not job_dir.exists()


def test_job_directory_cleanup_is_incremental_at_entry_budget(
    tmp_path: Path,
) -> None:
    job_dir = _old_job_dir(tmp_path, "job-large", file_count=3)
    temp_root = tmp_path / "data" / "images" / "temp"
    facade = _facade(
        tmp_path,
        job_dir_scan_max_entries=2,
        scan_time_budget_s=60,
    )

    first = facade.remove_job_dir(job_dir, temp_root)

    assert first == (1, 1, False)
    assert job_dir.is_dir()
    assert len(list(job_dir.iterdir())) == 2

    second = facade.remove_job_dir(job_dir, temp_root)

    assert second == (2, 2, False)
    assert job_dir.is_dir()
    assert list(job_dir.iterdir()) == []

    third = facade.remove_job_dir(job_dir, temp_root)

    assert third == (0, 0, True)
    assert not job_dir.exists()


def test_job_directory_cursor_advances_past_poison_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = _old_job_dir(
        tmp_path,
        "job-fairness",
        file_count=0,
    )
    for name in ("a.png", "b.png", "c.png"):
        (job_dir / name).write_bytes(name.encode())
    temp_root = tmp_path / "data" / "images" / "temp"
    real_scandir = retention_walk._scandir_directory
    real_unlink = retention_walk.unlink_verified_entry
    attempts: list[str] = []

    def fail_first(
        directory: retention_walk.DirectoryHandle,
        entry: retention_walk.EntrySnapshot,
    ) -> None:
        attempts.append(entry.name)
        if entry.name == "a.png":
            raise PermissionError("injected poison job artifact")
        real_unlink(directory, entry)

    monkeypatch.setattr(
        retention_walk,
        "_read_directory_page",
        _ordered_page_reader(real_scandir),
    )
    monkeypatch.setattr(
        retention_walk,
        "unlink_verified_entry",
        fail_first,
    )
    facade = _facade(
        tmp_path,
        job_dir_scan_max_entries=1,
        scan_time_budget_s=60,
    )

    for _pass in range(8):
        facade.remove_job_dir(job_dir, temp_root)

    assert attempts == ["a.png", "b.png", "c.png", "a.png"]
    assert (job_dir / "a.png").exists()
    assert not (job_dir / "b.png").exists()
    assert not (job_dir / "c.png").exists()


def test_directory_scan_cursor_closes_iterator_after_every_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    for name in ("a.png", "b.png", "c.png"):
        path = refs_dir / name
        path.write_bytes(name.encode())
        os.utime(path, (1, 1))
    real_read_page = retention_walk._read_directory_page
    real_unlink = retention_walk.unlink_verified_entry
    active_reads = 0
    opened_reads = 0
    closed_reads = 0

    def tracking_read(
        directory_fd: int,
        **kwargs: Any,
    ) -> retention_walk.DirectoryPage:
        nonlocal active_reads, opened_reads, closed_reads
        active_reads += 1
        opened_reads += 1
        try:
            return real_read_page(directory_fd, **kwargs)
        finally:
            active_reads -= 1
            closed_reads += 1

    def fail_first(
        directory: retention_walk.DirectoryHandle,
        entry: retention_walk.EntrySnapshot,
    ) -> None:
        if entry.name == "a.png":
            raise PermissionError("injected poison reference")
        real_unlink(directory, entry)

    monkeypatch.setattr(
        retention_walk,
        "_read_directory_page",
        tracking_read,
    )
    monkeypatch.setattr(
        retention_walk,
        "unlink_verified_entry",
        fail_first,
    )
    facade = _facade(
        tmp_path,
        ref_scan_max_entries=1,
        scan_time_budget_s=60,
    )

    for _pass in range(24):
        facade.sweep_dir(refs_dir, 2)
        assert active_reads == 0

    assert opened_reads == closed_reads
    assert (refs_dir / "a.png").exists()
    assert not (refs_dir / "b.png").exists()
    assert not (refs_dir / "c.png").exists()


def test_directory_cursor_pages_30k_entries_without_progress_degradation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_count = 30_000
    names = [f"entry-{index:05d}" for index in range(entry_count)]
    scanner_calls = 0

    def fake_read_page(
        _directory_fd: int,
        *,
        device: int,
        inode: int,
        offset: int,
        buffer_bytes: int,
    ) -> retention_walk.DirectoryPage:
        nonlocal scanner_calls
        scanner_calls += 1
        assert (device, inode) != (0, 0)
        stop = min(offset + 64, entry_count)
        reached_end = stop == entry_count
        return retention_walk.DirectoryPage(
            names=tuple(
                os.fsencode(name)
                for name in names[offset:stop]
            ),
            next_offset=stop,
            reached_end=reached_end,
            bytes_read=buffer_bytes - 1 if reached_end else buffer_bytes,
        )

    def fake_snapshot(
        _directory: retention_walk.DirectoryHandle,
        name: str,
    ) -> retention_walk.EntrySnapshot:
        index = int(name.rsplit("-", 1)[-1])
        return retention_walk.EntrySnapshot(
            name=name,
            device=1,
            inode=index + 1,
            mode=0o100600,
            size=1,
            mtime_ns=1,
            ctime_ns=1,
        )

    monkeypatch.setattr(
        retention_walk,
        "_read_directory_page",
        fake_read_page,
    )
    monkeypatch.setattr(
        retention_walk,
        "directory_entry_snapshot",
        fake_snapshot,
    )
    scan_root = tmp_path / "scan-root"
    scan_root.mkdir()
    cursor = retention_walk.DirectoryScanCursor(page_size=64)
    processed: list[str] = []
    pass_progress: list[int] = []
    max_buffered = 0

    with retention_walk.open_directory(scan_root) as directory:
        while len(processed) < entry_count:
            before = len(processed)
            budget = retention_walk.new_traversal_budget(
                3,
                time_budget_s=60,
                monotonic=lambda: 0.0,
            )
            while True:
                step = cursor.next_entry(directory, budget)
                if step.entry is not None:
                    processed.append(step.entry.name)
                    max_buffered = max(
                        max_buffered,
                        cursor.buffered_entries,
                    )
                    continue
                if step.exhausted or step.reached_end:
                    break
            cursor.close_pass()
            pass_progress.append(len(processed) - before)

    assert processed == names
    assert all(0 < progress <= 3 for progress in pass_progress)
    assert scanner_calls == (entry_count + 63) // 64
    assert max_buffered <= 63


def test_directory_cursor_500k_cookie_has_constant_bounded_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offsets: list[int] = []

    def fake_read_page(
        _directory_fd: int,
        *,
        device: int,
        inode: int,
        offset: int,
        buffer_bytes: int,
    ) -> retention_walk.DirectoryPage:
        assert (device, inode) != (0, 0)
        offsets.append(offset)
        return retention_walk.DirectoryPage(
            names=tuple(
                os.fsencode(f"entry-{index}")
                for index in range(offset, offset + 64)
            ),
            next_offset=offset + 64,
            reached_end=False,
            bytes_read=buffer_bytes,
        )

    monkeypatch.setattr(
        retention_walk,
        "_read_directory_page",
        fake_read_page,
    )
    root = tmp_path / "scan-root"
    root.mkdir()
    ticks = iter((0.0, 0.0, 0.0, 2.0))
    budget = retention_walk.new_traversal_budget(
        64,
        time_budget_s=1,
        monotonic=lambda: next(ticks, 2.0),
    )
    cursor = retention_walk.DirectoryScanCursor(page_size=64)
    started = time.perf_counter()
    with retention_walk.open_directory(root) as directory:
        cursor._device = directory.device  # noqa: SLF001
        cursor._inode = directory.inode  # noqa: SLF001
        cursor._scan_offset = 500_000  # noqa: SLF001
        step = cursor.next_entry(directory, budget)
    elapsed = time.perf_counter() - started

    assert step.exhausted
    assert offsets == [500_000]
    assert cursor.buffered_entries == 64
    assert elapsed < 0.1


@pytest.mark.skipif(
    not retention_dir_reader.directory_offsets_available(),
    reason="resumable directory offsets are unavailable",
)
def test_directory_cookie_resumes_across_reopen_with_inode_guard(
    tmp_path: Path,
) -> None:
    root = tmp_path / "scan-root"
    root.mkdir()
    expected = {f"entry-{index:04d}" for index in range(1_500)}
    for name in expected:
        root.joinpath(name).write_bytes(b"x")

    seen: set[str] = set()
    offset = 0
    identity: tuple[int, int] | None = None
    while True:
        with retention_walk.open_directory(root) as directory:
            identity = (directory.device, directory.inode)
            page = retention_dir_reader.read_directory_page(
                directory.fd,
                device=directory.device,
                inode=directory.inode,
                offset=offset,
            )
        page_names = {os.fsdecode(name) for name in page.names}
        assert seen.isdisjoint(page_names)
        seen.update(page_names)
        offset = page.next_offset
        if page.reached_end:
            break

    assert seen == expected
    assert identity is not None
    displaced = root.with_name("scan-root-displaced")
    root.rename(displaced)
    root.mkdir()
    with retention_walk.open_directory(root) as replacement:
        with pytest.raises(OSError):
            retention_dir_reader.read_directory_page(
                replacement.fd,
                device=identity[0],
                inode=identity[1],
                offset=offset,
            )


@pytest.mark.skipif(
    not retention_dir_reader.directory_offsets_available(),
    reason="resumable directory offsets are unavailable",
)
def test_directory_cookie_survives_process_restart(tmp_path: Path) -> None:
    root = tmp_path / "scan-root"
    root.mkdir()
    for index in range(1_500):
        root.joinpath(f"entry-{index:04d}").write_bytes(b"x")
    script = """
import json
import os
import sys
from pathlib import Path
from image_job.retention_dir_reader import read_directory_page

path = Path(sys.argv[1])
offset = int(sys.argv[2])
expected_device = int(sys.argv[3])
expected_inode = int(sys.argv[4])
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
fd = os.open(path, flags)
try:
    info = os.fstat(fd)
    device = info.st_dev if expected_device < 0 else expected_device
    inode = info.st_ino if expected_inode < 0 else expected_inode
    page = read_directory_page(
        fd,
        device=device,
        inode=inode,
        offset=offset,
    )
finally:
    os.close(fd)
print(json.dumps({
    "device": device,
    "inode": inode,
    "offset": page.next_offset,
    "names": [os.fsdecode(name) for name in page.names],
}))
"""
    first = json.loads(
        subprocess.check_output(  # noqa: S603
            [
                sys.executable,
                "-c",
                script,
                os.fspath(root),
                "0",
                "-1",
                "-1",
            ],
            text=True,
        )
    )
    second = json.loads(
        subprocess.check_output(  # noqa: S603
            [
                sys.executable,
                "-c",
                script,
                os.fspath(root),
                str(first["offset"]),
                str(first["device"]),
                str(first["inode"]),
            ],
            text=True,
        )
    )

    assert first["names"]
    assert second["names"]
    assert set(first["names"]).isdisjoint(second["names"])
    assert second["offset"] != first["offset"]


def test_persistent_reference_scan_retries_page_after_precommit_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    orphan = refs_dir / "orphan.png"
    orphan.write_bytes(b"x")
    os.utime(orphan, (1, 1))
    _db_path, open_db = _reference_db(tmp_path)
    real_read_page = retention_scan_store.read_directory_page
    real_commit = retention_scan_store.DurableDirectoryScan._commit_directory_page
    offsets: list[int] = []
    crashed = False

    def tracked_read_page(
        directory_fd: int,
        **kwargs: Any,
    ) -> retention_walk.DirectoryPage:
        offsets.append(int(kwargs["offset"]))
        return real_read_page(directory_fd, **kwargs)

    def crash_once(
        self: retention_scan_store.DurableDirectoryScan,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("injected crash before page commit")
        real_commit(self, *args, **kwargs)

    monkeypatch.setattr(
        retention_scan_store,
        "read_directory_page",
        tracked_read_page,
    )
    monkeypatch.setattr(
        retention_scan_store.DurableDirectoryScan,
        "_commit_directory_page",
        crash_once,
    )
    first = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_scan_max_entries=20,
        scan_time_budget_s=60,
    )

    with pytest.raises(RuntimeError, match="injected crash"):
        first.sweep_filesystem(2)

    assert orphan.exists()
    second = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_scan_max_entries=20,
        scan_time_budget_s=60,
    )
    assert second.sweep_filesystem(2) == (1, 1)
    assert offsets[:2] == [0, 0]
    assert not orphan.exists()


def test_persistent_candidate_poison_does_not_block_later_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    poison = refs_dir / "a-poison.png"
    healthy = refs_dir / "b-healthy.png"
    for path in (poison, healthy):
        path.write_bytes(b"x")
        os.utime(path, (1, 1))
    _db_path, open_db = _reference_db(tmp_path)
    real_scandir = retention_walk._scandir_directory
    real_unlink = reference_retention.unlink_verified_entry
    attempts: list[str] = []

    def fail_poison(
        directory: retention_walk.DirectoryHandle,
        entry: retention_walk.EntrySnapshot,
    ) -> None:
        attempts.append(entry.name)
        if entry.name == poison.name:
            raise PermissionError("injected persistent poison")
        real_unlink(directory, entry)

    monkeypatch.setattr(
        retention_scan_store,
        "read_directory_page",
        _ordered_page_reader(real_scandir),
    )
    monkeypatch.setattr(
        reference_retention,
        "unlink_verified_entry",
        fail_poison,
    )
    for _pass in range(4):
        facade = _facade(
            tmp_path,
            refs_dir=lambda: refs_dir,
            open_db=open_db,
            ref_scan_max_entries=20,
            ref_delete_batch_size=1,
            scan_time_budget_s=60,
        )
        facade.sweep_filesystem(2)

    assert poison.exists()
    assert not healthy.exists()
    assert attempts[:2] == [poison.name, healthy.name]


def test_persistent_reference_scan_eventually_covers_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    keep = refs_dir / "a-keep.png"
    deleted = refs_dir / "b-delete.png"
    renamed = refs_dir / "c-rename.png"
    for path in (keep, deleted, renamed):
        path.write_bytes(path.name.encode())
        os.utime(path, (1, 1))
    _db_path, open_db = _reference_db(tmp_path)
    conn = open_db()
    try:
        conn.execute(
            """
            INSERT INTO refs(auth_hash, sha256, token, ext, size, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "auth",
                "keep",
                "a-keep",
                "png",
                keep.stat().st_size,
                "2026-07-01T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()
    real_scandir = retention_walk._scandir_directory
    monkeypatch.setattr(
        retention_scan_store,
        "read_directory_page",
        _ordered_page_reader(real_scandir),
    )

    first = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_scan_max_entries=2,
        scan_time_budget_s=60,
    )
    first.sweep_filesystem(2)
    deleted.unlink()
    renamed.rename(refs_dir / "d-renamed.png")
    inserted = refs_dir / "e-inserted.png"
    inserted.write_bytes(b"inserted")
    os.utime(inserted, (1, 1))

    for _pass in range(16):
        facade = _facade(
            tmp_path,
            refs_dir=lambda: refs_dir,
            open_db=open_db,
            ref_scan_max_entries=3,
            scan_time_budget_s=60,
        )
        facade.sweep_filesystem(2)
        if not (refs_dir / "d-renamed.png").exists() and not inserted.exists():
            break

    assert keep.read_bytes() == b"a-keep.png"
    assert not deleted.exists()
    assert not (refs_dir / "c-rename.png").exists()
    assert not (refs_dir / "d-renamed.png").exists()
    assert not inserted.exists()


def test_persistent_scan_root_replacement_discards_old_generation(
    tmp_path: Path,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    old_orphan = refs_dir / "old.png"
    old_orphan.write_bytes(b"old")
    os.utime(old_orphan, (1, 1))
    db_path, open_db = _reference_db(tmp_path)
    first = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_scan_max_entries=1,
        scan_time_budget_s=60,
    )
    first.sweep_filesystem(2)

    displaced = refs_dir.with_name("refs-displaced")
    refs_dir.rename(displaced)
    refs_dir.mkdir()
    live = refs_dir / "live.png"
    live.write_bytes(b"live")
    os.utime(live, (1, 1))
    conn = open_db()
    try:
        conn.execute(
            """
            INSERT INTO refs(auth_hash, sha256, token, ext, size, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "auth",
                "live",
                "live",
                "png",
                4,
                "2026-07-01T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()
    second = _facade(
        tmp_path,
        refs_dir=lambda: refs_dir,
        open_db=open_db,
        ref_scan_max_entries=10,
        scan_time_budget_s=60,
    )
    second.sweep_filesystem(2)

    assert live.read_bytes() == b"live"
    assert (displaced / old_orphan.name).read_bytes() == b"old"
    check = sqlite3.connect(db_path)
    try:
        root_row = check.execute(
            """
            SELECT root_device, root_inode
            FROM retention_scan_roots
            """
        ).fetchone()
        current = refs_dir.stat()
        assert root_row == (current.st_dev, current.st_ino)
        old_info = displaced.stat()
        assert (
            check.execute(
                """
                SELECT COUNT(*)
                FROM retention_scan_candidates
                WHERE root_device = ? AND root_inode = ?
                """,
                (old_info.st_dev, old_info.st_ino),
            ).fetchone()[0]
            == 0
        )
    finally:
        check.close()


def test_persistent_scan_tables_remain_bounded_across_generations(
    tmp_path: Path,
) -> None:
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    db_path, open_db = _reference_db(tmp_path)
    conn = open_db()
    try:
        rows = []
        for index in range(300):
            token = f"live-{index:04d}"
            path = refs_dir / f"{token}.png"
            path.write_bytes(b"x")
            os.utime(path, (1, 1))
            rows.append(
                (
                    f"auth-{index}",
                    f"sha-{index}",
                    token,
                    "png",
                    1,
                    "2026-07-01T00:00:00+00:00",
                )
            )
        conn.executemany(
            """
            INSERT INTO refs(auth_hash, sha256, token, ext, size, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    finally:
        conn.close()

    for _pass in range(40):
        facade = _facade(
            tmp_path,
            refs_dir=lambda: refs_dir,
            open_db=open_db,
            ref_scan_max_entries=25,
            scan_time_budget_s=60,
        )
        facade.sweep_filesystem(2)
        check = sqlite3.connect(db_path)
        try:
            counts = {
                table: check.execute(
                    f"SELECT COUNT(*) FROM {table}"  # nosec B608
                ).fetchone()[0]
                for table in (
                    "retention_scan_roots",
                    "retention_scan_directories",
                    "retention_scan_entries",
                    "retention_scan_candidates",
                )
            }
            generations = check.execute(
                """
                SELECT generation FROM retention_scan_directories
                UNION
                SELECT generation FROM retention_scan_entries
                """
            ).fetchall()
        finally:
            check.close()
        assert counts["retention_scan_roots"] == 1
        assert counts["retention_scan_directories"] <= 4_096
        assert counts["retention_scan_entries"] <= 1_024
        assert counts["retention_scan_candidates"] <= 4_096
        assert len(generations) <= 1


def test_directory_cursor_eventually_covers_insert_delete_and_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_root = tmp_path / "scan-root"
    scan_root.mkdir()
    for name in ("a", "b", "c", "d"):
        scan_root.joinpath(name).write_bytes(name.encode())
    real_scandir = retention_walk._scandir_directory

    monkeypatch.setattr(
        retention_walk,
        "_read_directory_page",
        _ordered_page_reader(real_scandir, page_entries=2),
    )
    cursor = retention_walk.DirectoryScanCursor(page_size=2)
    observed: set[str] = set()
    completed_cycles = 0

    with retention_walk.open_directory(scan_root) as directory:
        first_budget = retention_walk.new_traversal_budget(
            2,
            time_budget_s=60,
            monotonic=lambda: 0.0,
        )
        first = cursor.next_entry(directory, first_budget)
        assert first.entry is not None
        observed.add(first.entry.name)

        scan_root.joinpath("b").unlink()
        scan_root.joinpath("c").rename(scan_root / "x")
        scan_root.joinpath("d").rename(scan_root / "z")
        scan_root.joinpath("aa").write_bytes(b"inserted")
        current_names = {"a", "aa", "x", "z"}

        for _pass in range(32):
            budget = retention_walk.new_traversal_budget(
                1,
                time_budget_s=60,
                monotonic=lambda: 0.0,
            )
            while True:
                step = cursor.next_entry(directory, budget)
                if step.entry is not None:
                    observed.add(step.entry.name)
                    continue
                if step.reached_end:
                    completed_cycles += 1
                if step.exhausted or step.reached_end:
                    break
            cursor.close_pass()
            if current_names <= observed and completed_cycles:
                break

    assert current_names <= observed
    assert completed_cycles >= 1
    assert cursor.buffered_entries <= cursor.page_size


def test_scan_registry_replaces_same_path_inode_and_resets_old_cursor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    root.joinpath("a").write_bytes(b"a")
    registry = retention_walk.DirectoryScanRegistry.create(
        retention_walk.DirectoryScanCursor,
        max_cursors=4,
        max_idle_accesses=10,
    )

    with retention_walk.open_directory(root) as original:
        old_cursor = registry.cursor_for(original)
        budget = retention_walk.new_traversal_budget(
            2,
            time_budget_s=60,
            monotonic=lambda: 0.0,
        )
        assert old_cursor.next_entry(original, budget).entry is not None
        assert old_cursor._scan_offset != 0  # noqa: SLF001

    displaced = root.with_name("root-displaced")
    root.rename(displaced)
    root.mkdir()
    root.joinpath("replacement").write_bytes(b"new")
    with retention_walk.open_directory(root) as replacement:
        new_cursor = registry.cursor_for(replacement)

    assert new_cursor is not old_cursor
    assert old_cursor._scan_offset == 0  # noqa: SLF001
    assert old_cursor.buffered_entries == 0
    assert registry.cursor_count == 1


def test_scan_registry_caps_and_evicts_idle_cursor_state(
    tmp_path: Path,
) -> None:
    registry = retention_walk.DirectoryScanRegistry.create(
        retention_walk.DirectoryScanCursor,
        max_cursors=2,
        max_idle_accesses=2,
    )
    roots = [tmp_path / f"root-{index}" for index in range(3)]
    for root in roots:
        root.mkdir()
        root.joinpath("entry").write_bytes(b"x")

    handles = [retention_walk.open_directory(root) for root in roots]
    try:
        first_cursor = registry.cursor_for(handles[0])
        first_budget = retention_walk.new_traversal_budget(
            2,
            time_budget_s=60,
            monotonic=lambda: 0.0,
        )
        assert first_cursor.next_entry(
            handles[0],
            first_budget,
        ).entry is not None
        registry.cursor_for(handles[1])
        registry.cursor_for(handles[1])

        assert first_cursor._scan_offset == 0  # noqa: SLF001
        assert first_cursor.buffered_entries == 0

        registry.cursor_for(handles[2])
        assert registry.cursor_count <= 2
    finally:
        for handle in handles:
            handle.close()


def test_orphan_discovery_stops_at_partition_budget(
    tmp_path: Path,
) -> None:
    for day in ("01", "02", "03"):
        _old_job_dir(tmp_path, f"job-{day}", day=day)
    facade = _facade(
        tmp_path,
        orphan_scan_max_entries=100,
        orphan_scan_max_partitions=1,
        orphan_scan_max_job_dirs=100,
        scan_time_budget_s=60,
    )

    candidates = facade.orphan_job_dir_candidates_sync(2)

    assert len(candidates) == 1
    assert len({path.parent for path in candidates}) == 1


def test_orphan_discovery_advances_across_partition_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for day in ("01", "02", "03"):
        _old_job_dir(tmp_path, f"job-{day}", day=day)
    real_scandir = retention_walk._scandir_directory

    monkeypatch.setattr(
        retention_walk,
        "_read_directory_page",
        _ordered_page_reader(real_scandir),
    )
    facade = _facade(
        tmp_path,
        orphan_scan_max_entries=100,
        orphan_scan_max_partitions=1,
        orphan_scan_max_job_dirs=100,
        scan_time_budget_s=60,
    )

    passes = [
        {
            path.name
            for path in facade.orphan_job_dir_candidates_sync(2)
        }
        for _pass in range(3)
    ]

    assert passes == [{"job-01"}, {"job-02"}, {"job-03"}]


def test_orphan_discovery_resets_when_temp_root_inode_changes(
    tmp_path: Path,
) -> None:
    _old_job_dir(tmp_path, "old-job", day="01")
    facade = _facade(
        tmp_path,
        orphan_scan_max_entries=100,
        orphan_scan_max_partitions=1,
        orphan_scan_max_job_dirs=100,
        scan_time_budget_s=60,
    )

    first = facade.orphan_job_dir_candidates_sync(2)
    temp_root = tmp_path / "data" / "images" / "temp"
    displaced = temp_root.with_name("temp-displaced")
    temp_root.rename(displaced)
    _old_job_dir(tmp_path, "new-job", day="09")
    second = facade.orphan_job_dir_candidates_sync(2)

    assert {path.name for path in first} == {"old-job"}
    assert {path.name for path in second} == {"new-job"}


@pytest.mark.asyncio
async def test_persistent_orphan_scan_survives_facade_rebuilds(
    tmp_path: Path,
) -> None:
    day_dir = tmp_path / "data" / "images" / "temp" / "2026" / "07" / "01"
    day_dir.mkdir(parents=True)
    live_job_ids = [f"live-{index:04d}" for index in range(2_001)]
    for job_id in live_job_ids:
        job_dir = day_dir / job_id
        job_dir.mkdir()
        os.utime(job_dir, (1, 1))
    first_marker = day_dir / live_job_ids[0] / "keep"
    last_marker = day_dir / live_job_ids[-1] / "keep"
    first_marker.write_bytes(b"first")
    last_marker.write_bytes(b"last")
    orphan = day_dir / "zzzz-tail-orphan"
    orphan.mkdir()
    (orphan / "image.png").write_bytes(b"x")
    os.utime(orphan, (1, 1))

    db_path = tmp_path / "jobs.sqlite3"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO jobs(job_id) VALUES (?)",
            ((job_id,) for job_id in live_job_ids),
        )
    finally:
        conn.close()

    def open_db() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    async def db_all(
        sql: str,
        params: tuple[Any, ...],
    ) -> list[sqlite3.Row]:
        connection = open_db()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    removed: list[tuple[int, int]] = []
    for _pass in range(6):
        facade = _facade(
            tmp_path,
            open_db=open_db,
            db_all=db_all,
            orphan_scan_max_entries=400,
            orphan_scan_max_partitions=1,
            orphan_scan_max_job_dirs=400,
            job_dir_scan_max_entries=100,
            scan_time_budget_s=60,
        )
        removed.append(await facade.sweep_orphan_job_dirs(2))

    assert sum(files for files, _bytes in removed) == 1
    assert sum(freed for _files, freed in removed) == 1
    assert not orphan.exists()
    assert first_marker.read_bytes() == b"first"
    assert last_marker.read_bytes() == b"last"


@pytest.mark.asyncio
async def test_orphan_lookup_is_candidate_bounded_and_keeps_live_job(
    tmp_path: Path,
) -> None:
    job_dirs = [
        _old_job_dir(tmp_path, f"job-{suffix}")
        for suffix in ("a", "b", "c")
    ]
    calls: list[tuple[str, tuple[Any, ...]]] = []
    live_job_id: str | None = None

    async def db_all(
        sql: str,
        params: tuple[Any, ...],
    ) -> list[dict[str, str]]:
        nonlocal live_job_id
        calls.append((sql, params))
        live_job_id = str(params[0])
        return [{"job_id": live_job_id}]

    facade = _facade(
        tmp_path,
        db_all=db_all,
        orphan_scan_max_entries=100,
        orphan_scan_max_partitions=10,
        orphan_scan_max_job_dirs=2,
        job_dir_scan_max_entries=100,
        scan_time_budget_s=60,
    )

    removed_files, removed_bytes = await facade.sweep_orphan_job_dirs(2)

    assert (removed_files, removed_bytes) == (1, 1)
    assert live_job_id is not None
    existing = {path.name for path in job_dirs if path.exists()}
    assert live_job_id in existing
    assert len(existing) == 2
    assert len(calls) == 1
    sql, params = calls[0]
    normalized = " ".join(sql.split()).upper()
    assert "WHERE JOB_ID IN (" in normalized
    assert "ORDER BY JOB_ID ASC" in normalized
    assert "LIMIT ?" in normalized
    assert params[-1] == 2
    assert normalized != "SELECT JOB_ID FROM JOBS"


@pytest.mark.asyncio
async def test_orphan_candidate_swap_during_db_lookup_preserves_live_tree(
    tmp_path: Path,
) -> None:
    orphan_dir = _old_job_dir(tmp_path, "orphan-job")
    orphan_artifact = orphan_dir / "image-0.png"
    displaced = orphan_dir.with_name("orphan-job-displaced")
    live_dir = _old_job_dir(tmp_path, "live-job")
    live_artifact = live_dir / "image-0.png"
    os.utime(live_dir, (3, 3))
    swapped = False

    async def db_all(
        _sql: str,
        _params: tuple[Any, ...],
    ) -> list[Any]:
        nonlocal swapped
        orphan_dir.rename(displaced)
        orphan_dir.symlink_to(live_dir, target_is_directory=True)
        swapped = True
        return []

    facade = _facade(
        tmp_path,
        db_all=db_all,
        orphan_scan_max_entries=100,
        orphan_scan_max_partitions=10,
        orphan_scan_max_job_dirs=10,
        job_dir_scan_max_entries=20,
        scan_time_budget_s=60,
    )

    assert await facade.sweep_orphan_job_dirs(2) == (0, 0)
    assert swapped
    assert orphan_dir.is_symlink()
    assert live_artifact.read_bytes() == b"x"
    assert (displaced / orphan_artifact.name).read_bytes() == b"x"


@pytest.mark.asyncio
async def test_missing_expiry_cursor_advances_past_malformed_head_row(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "job_id": "a-poison",
            "status": "succeeded",
            "created_at": None,
            "finished_at": "2026-07-02T00:00:00+00:00",
            "retention_days": 1,
            "images_json": "[]",
        },
        {
            "job_id": "b-healthy",
            "status": "succeeded",
            "created_at": "2026-07-01T00:00:00+00:00",
            "finished_at": "2026-07-02T00:00:00+00:00",
            "retention_days": 1,
            "images_json": "[]",
        },
    ]
    updated: list[str] = []
    selected: list[str] = []

    async def db_all(
        sql: str,
        params: tuple[Any, ...],
    ) -> list[Any]:
        if "retention_expires_at IS NULL" not in sql:
            return []
        cursor = (str(params[0]), str(params[2]))
        ordered = sorted(
            rows,
            key=lambda row: (row["finished_at"], row["job_id"]),
        )
        wrapped = [
            row
            for row in ordered
            if (row["finished_at"], row["job_id"]) > cursor
        ] + [
            row
            for row in ordered
            if (row["finished_at"], row["job_id"]) <= cursor
        ]
        if wrapped:
            selected.append(str(wrapped[0]["job_id"]))
        return wrapped[:1]

    async def db_exec(sql: str, params: tuple[Any, ...]) -> int:
        if "SET retention_expires_at = ?" in sql:
            job_id = str(params[1])
            updated.append(job_id)
            rows[:] = [row for row in rows if row["job_id"] != job_id]
        return 1

    facade = _facade(
        tmp_path,
        db_all=db_all,
        db_exec=db_exec,
        finished_row_batch_size=1,
        sweep_filesystem_fn=lambda _cutoff: (0, 0),
    )

    await facade.run_pass()
    await facade.run_pass()
    await facade.run_pass()

    assert updated == ["b-healthy"]
    assert selected == ["a-poison", "b-healthy", "a-poison"]


@pytest.mark.asyncio
async def test_expired_job_cursor_advances_past_poison_head_row(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "job_id": job_id,
            "status": "succeeded",
            "created_at": "2026-07-01T00:00:00+00:00",
            "finished_at": "2026-07-02T00:00:00+00:00",
            "retention_days": 1,
            "images_json": "[]",
            "retention_expires_at": "2026-07-02T00:00:00+00:00",
        }
        for job_id in (".", "b-healthy", "c-healthy")
    ]
    deleted: list[str] = []
    selected: list[str] = []

    async def db_all(
        sql: str,
        params: tuple[Any, ...],
    ) -> list[Any]:
        if "retention_expires_at IS NULL" in sql:
            return []
        cursor = (str(params[1]), str(params[3]))
        ordered = sorted(
            rows,
            key=lambda row: (
                row["retention_expires_at"],
                row["job_id"],
            ),
        )
        wrapped = [
            row
            for row in ordered
            if (
                row["retention_expires_at"],
                row["job_id"],
            )
            > cursor
        ] + [
            row
            for row in ordered
            if (
                row["retention_expires_at"],
                row["job_id"],
            )
            <= cursor
        ]
        if wrapped:
            selected.append(str(wrapped[0]["job_id"]))
        return wrapped[:1]

    async def db_exec(sql: str, params: tuple[Any, ...]) -> int:
        if sql.lstrip().startswith("DELETE FROM jobs"):
            job_id = str(params[0])
            deleted.append(job_id)
            rows[:] = [row for row in rows if row["job_id"] != job_id]
        return 1

    facade = _facade(
        tmp_path,
        db_all=db_all,
        db_exec=db_exec,
        finished_row_batch_size=1,
        sweep_filesystem_fn=lambda _cutoff: (0, 0),
    )

    for _pass in range(4):
        await facade.run_pass()

    assert deleted == ["b-healthy", "c-healthy"]
    assert selected == [".", "b-healthy", "c-healthy", "."]
