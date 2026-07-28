from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from image_job.persistence import RetentionFacade


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
    assert "ORDER BY FINISHED_AT ASC, JOB_ID ASC" in normalized_backfill
    assert "LIMIT ?" in normalized_backfill
    assert backfill_params == (7,)

    expiry_sql, expiry_params = calls[1]
    normalized_expiry = " ".join(expiry_sql.split()).upper()
    assert "RETENTION_EXPIRES_AT <= ?" in normalized_expiry
    assert (
        "ORDER BY RETENTION_EXPIRES_AT ASC, JOB_ID ASC"
        in normalized_expiry
    )
    assert "LIMIT ?" in normalized_expiry
    assert expiry_params == ("2026-07-27T00:00:00+00:00", 7)


@pytest.mark.asyncio
async def test_long_lived_old_row_cannot_starve_indexed_expired_row(
    tmp_path: Path,
) -> None:
    calls = 0
    expired_row = {
        "job_id": "expired-later",
        "created_at": "2026-07-01T00:00:00+00:00",
        "finished_at": "2026-07-20T00:00:00+00:00",
        "retention_days": 1,
        "images_json": "[]",
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
        return 0

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

    assert (removed_files, removed_bytes) == (2, 2)
    assert sum(path.exists() for path in paths) == 1
    sql, params = statements[-1]
    normalized = " ".join(sql.split()).upper()
    assert "ORDER BY CREATED_AT ASC, ROWID ASC" in normalized
    assert "LIMIT ?" in normalized
    assert params[1] == 5


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

    assert (removed_files, removed_bytes) == (1, 1)
    assert sum(path.exists() for path in paths) == 2


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

    assert first == (2, 2, False)
    assert job_dir.is_dir()
    assert len(list(job_dir.iterdir())) == 1

    second = facade.remove_job_dir(job_dir, temp_root)

    assert second == (1, 1, True)
    assert not job_dir.exists()


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
