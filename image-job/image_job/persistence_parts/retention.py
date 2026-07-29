"""Retention cleanup implementation for persisted jobs and references."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..retention_walk import (
    TraversalBudget,
    iter_child_dirs,
    new_traversal_budget,
    sweep_tree_bounded,
)
from .common import DbAll, DbExec, parse_utc_datetime, strict_json_loads


@dataclass(frozen=True)
class RetentionFacade:
    data_dir: Callable[[], Path]
    refs_dir: Callable[[], Path]
    db_exec_sync: Callable[[str, tuple[Any, ...]], int]
    db_exec: DbExec
    db_all: DbAll
    utc_now: Callable[[], datetime]
    max_retention_days: Callable[[], int]
    job_ttl_days: Callable[[], int]
    log: logging.Logger
    sweep_dir_fn: Callable[[Path, float], tuple[int, int]] | None = None
    sweep_filesystem_fn: Callable[[float], tuple[int, int]] | None = None
    finished_row_batch_size: int = 256
    ref_delete_batch_size: int = 512
    ref_scan_max_entries: int = 2_000
    job_dir_scan_max_entries: int = 2_000
    orphan_scan_max_entries: int = 2_000
    orphan_scan_max_partitions: int = 32
    orphan_scan_max_job_dirs: int = 256
    scan_time_budget_s: float = 1.0
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        integer_budgets = {
            "finished_row_batch_size": self.finished_row_batch_size,
            "ref_delete_batch_size": self.ref_delete_batch_size,
            "ref_scan_max_entries": self.ref_scan_max_entries,
            "job_dir_scan_max_entries": self.job_dir_scan_max_entries,
            "orphan_scan_max_entries": self.orphan_scan_max_entries,
            "orphan_scan_max_partitions": self.orphan_scan_max_partitions,
            "orphan_scan_max_job_dirs": self.orphan_scan_max_job_dirs,
        }
        for name, value in integer_budgets.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.scan_time_budget_s <= 0:
            raise ValueError("scan_time_budget_s must be positive")

    def _new_budget(self, max_entries: int) -> TraversalBudget:
        return new_traversal_budget(
            max_entries,
            time_budget_s=self.scan_time_budget_s,
            monotonic=self.monotonic,
        )

    def sweep_dir(self, base: Path, cutoff_ts: float) -> tuple[int, int]:
        if not base.is_dir():
            return 0, 0
        removed_files, removed_bytes, _complete = sweep_tree_bounded(
            base,
            self._new_budget(self.ref_scan_max_entries),
            cutoff_ts=cutoff_ts,
            remove_directory=False,
        )
        return removed_files, removed_bytes

    def sweep_filesystem(self, cutoff_ts: float) -> tuple[int, int]:
        sweep_dir = self.sweep_dir_fn or self.sweep_dir
        total_files, total_bytes = sweep_dir(self.refs_dir(), cutoff_ts)
        cutoff_iso = datetime.fromtimestamp(
            cutoff_ts,
            tz=timezone.utc,
        ).isoformat()
        try:
            self.db_exec_sync(
                """
                DELETE FROM refs
                WHERE rowid IN (
                    SELECT rowid
                    FROM refs
                    WHERE created_at < ?
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT ?
                )
                """,
                (cutoff_iso, self.ref_delete_batch_size),
            )
        except sqlite3.OperationalError:
            pass
        return total_files, total_bytes

    @staticmethod
    def _row_value(row: Any, key: str) -> Any:
        try:
            return row[key]
        except (IndexError, KeyError, TypeError):
            return None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        return parse_utc_datetime(value)

    def job_artifact_expiry(self, row: Any) -> datetime | None:
        created_at = self._parse_datetime(self._row_value(row, "created_at"))
        if created_at is None:
            return None
        try:
            retention_days = int(self._row_value(row, "retention_days"))
        except (TypeError, ValueError):
            retention_days = self.max_retention_days()
        retention_days = min(
            self.max_retention_days(),
            max(1, retention_days),
        )
        expiries = [created_at + timedelta(days=retention_days)]

        images_json = self._row_value(row, "images_json")
        if isinstance(images_json, str) and images_json:
            try:
                images = strict_json_loads(images_json)
            except (
                json.JSONDecodeError,
                RecursionError,
                TypeError,
                ValueError,
            ):
                images = []
            if isinstance(images, list):
                for image in images:
                    if not isinstance(image, dict):
                        continue
                    expires_at = self._parse_datetime(image.get("expires_at"))
                    if expires_at is not None:
                        expiries.append(expires_at)
        return min(expiries)

    def job_effective_expiry(self, row: Any) -> datetime | None:
        artifact_expiry = self.job_artifact_expiry(row)
        if artifact_expiry is None:
            return None
        finished_at = self._parse_datetime(self._row_value(row, "finished_at"))
        if finished_at is None:
            return artifact_expiry
        row_ttl_expiry = finished_at + timedelta(days=self.job_ttl_days())
        return min(artifact_expiry, row_ttl_expiry)

    def remove_job_artifacts(
        self,
        row: Any,
        budget: TraversalBudget | None = None,
    ) -> tuple[int, int, bool]:
        job_id = self._row_value(row, "job_id")
        created_at = self._parse_datetime(self._row_value(row, "created_at"))
        if (
            not isinstance(job_id, str)
            or not job_id
            or "/" in job_id
            or "\\" in job_id
            or created_at is None
        ):
            return 0, 0, False

        temp_root = self.data_dir() / "images" / "temp"
        job_dir = (
            temp_root
            / created_at.strftime("%Y")
            / created_at.strftime("%m")
            / created_at.strftime("%d")
            / job_id
        )
        return self.remove_job_dir(job_dir, temp_root, budget=budget)

    def remove_job_dir(
        self,
        job_dir: Path,
        temp_root: Path,
        *,
        budget: TraversalBudget | None = None,
    ) -> tuple[int, int, bool]:
        try:
            job_dir.relative_to(temp_root)
        except ValueError:
            return 0, 0, False
        try:
            root_info = job_dir.lstat()
        except FileNotFoundError:
            return 0, 0, True
        except OSError:
            return 0, 0, False
        if not stat.S_ISDIR(root_info.st_mode):
            return 0, 0, False

        traversal_budget = budget or self._new_budget(
            self.job_dir_scan_max_entries
        )
        removed_files, removed_bytes, cleaned = sweep_tree_bounded(
            job_dir,
            traversal_budget,
            cutoff_ts=None,
            remove_directory=True,
        )
        if not cleaned:
            if not traversal_budget.exhausted:
                self.log.warning(
                    "retention sweeper could not remove job directory %s",
                    job_dir,
                )
            return removed_files, removed_bytes, False

        parent = job_dir.parent
        while parent != temp_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return removed_files, removed_bytes, True

    def orphan_job_dir_candidates_sync(
        self,
        cutoff_ts: float,
    ) -> tuple[Path, ...]:
        temp_root = self.data_dir() / "images" / "temp"
        if not temp_root.is_dir():
            return ()

        budget = self._new_budget(self.orphan_scan_max_entries)
        partitions_scanned = 0
        job_dirs_scanned = 0
        candidates: list[Path] = []
        for year_dir in iter_child_dirs(temp_root, budget):
            for month_dir in iter_child_dirs(year_dir, budget):
                for day_dir in iter_child_dirs(month_dir, budget):
                    partitions_scanned += 1
                    for job_dir in iter_child_dirs(day_dir, budget):
                        job_dirs_scanned += 1
                        old_directory = False
                        try:
                            info = job_dir.lstat()
                        except OSError:
                            pass
                        else:
                            old_directory = (
                                stat.S_ISDIR(info.st_mode)
                                and info.st_mtime < cutoff_ts
                            )
                        if old_directory:
                            candidates.append(job_dir)
                        if (
                            budget.exhausted
                            or job_dirs_scanned >= self.orphan_scan_max_job_dirs
                        ):
                            return tuple(candidates)
                    if (
                        budget.exhausted
                        or partitions_scanned >= self.orphan_scan_max_partitions
                    ):
                        return tuple(candidates)
                if budget.exhausted:
                    return tuple(candidates)
            if budget.exhausted:
                return tuple(candidates)
        return tuple(candidates)

    def sweep_orphan_job_dirs_sync(
        self,
        known_job_ids: set[str],
        cutoff_ts: float,
        candidates: tuple[Path, ...] | None = None,
    ) -> tuple[int, int]:
        """Remove old artifact directories with no matching jobs row."""
        temp_root = self.data_dir() / "images" / "temp"
        job_dirs = (
            self.orphan_job_dir_candidates_sync(cutoff_ts)
            if candidates is None
            else candidates
        )
        cleanup_budget = self._new_budget(self.job_dir_scan_max_entries)
        removed_files = 0
        removed_bytes = 0
        for job_dir in job_dirs:
            if not cleanup_budget.available():
                break
            if job_dir.name in known_job_ids:
                continue
            try:
                info = job_dir.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_mtime >= cutoff_ts
                ):
                    continue
            except OSError:
                continue
            files, freed, _cleaned = self.remove_job_dir(
                job_dir,
                temp_root,
                budget=cleanup_budget,
            )
            removed_files += files
            removed_bytes += freed
            if cleanup_budget.exhausted:
                break
        return removed_files, removed_bytes

    async def sweep_orphan_job_dirs(self, cutoff_ts: float) -> tuple[int, int]:
        candidates = await asyncio.to_thread(
            self.orphan_job_dir_candidates_sync,
            cutoff_ts,
        )
        candidate_ids = sorted({path.name for path in candidates})
        known: set[str] = set()
        for offset in range(0, len(candidate_ids), 500):
            batch = candidate_ids[offset : offset + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = await self.db_all(
                f"""
                SELECT job_id
                FROM jobs
                WHERE job_id IN ({placeholders})
                ORDER BY job_id ASC
                LIMIT ?
                """,  # nosec B608 - placeholders are generated, not user input.
                (*batch, len(batch)),
            )
            known.update(
                str(self._row_value(row, "job_id"))
                for row in rows
                if self._row_value(row, "job_id") is not None
            )
        return await asyncio.to_thread(
            self.sweep_orphan_job_dirs_sync,
            known,
            cutoff_ts,
            candidates,
        )

    async def run_pass(self) -> None:
        now = self.utc_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        missing_expiry_rows = await self.db_all(
            """
            SELECT job_id, status, created_at, finished_at,
                   retention_days, images_json
            FROM jobs
            WHERE finished_at IS NOT NULL
              AND retention_expires_at IS NULL
              AND status IN (
                  'succeeded',
                  'failed',
                  'cancelled',
                  'cancel_requested',
                  'uncertain'
              )
            ORDER BY finished_at ASC, job_id ASC
            LIMIT ?
            """,
            (self.finished_row_batch_size,),
        )
        for row in missing_expiry_rows:
            expires_at = self.job_effective_expiry(row)
            if expires_at is None:
                continue
            await self.db_exec(
                """
                UPDATE jobs
                SET retention_expires_at = ?
                WHERE job_id = ?
                  AND finished_at IS NOT NULL
                  AND retention_expires_at IS NULL
                  AND status IN (
                      'succeeded',
                      'failed',
                      'cancelled',
                      'cancel_requested',
                      'uncertain'
                  )
                """,
                (
                    expires_at.isoformat(),
                    self._row_value(row, "job_id"),
                ),
            )

        rows = await self.db_all(
            """
            SELECT job_id, status, created_at, finished_at,
                   retention_days, images_json, retention_expires_at
            FROM jobs
            WHERE retention_expires_at IS NOT NULL
              AND retention_expires_at <= ?
              AND finished_at IS NOT NULL
              AND status IN (
                  'succeeded',
                  'failed',
                  'cancelled',
                  'cancel_requested',
                  'uncertain'
              )
            ORDER BY retention_expires_at ASC, job_id ASC
            LIMIT ?
            """,
            (now.isoformat(), self.finished_row_batch_size),
        )
        removed_files = 0
        removed_bytes = 0
        removed_jobs = 0
        job_dir_budget = self._new_budget(self.job_dir_scan_max_entries)
        for row in rows:
            if not job_dir_budget.available():
                break
            expires_at = self.job_effective_expiry(row)
            if expires_at is None or expires_at > now:
                continue
            job_id = self._row_value(row, "job_id")
            status = self._row_value(row, "status")
            retention_expires_at = self._row_value(
                row,
                "retention_expires_at",
            )
            cleared = await self.db_exec(
                """
                UPDATE jobs
                SET auth_header = NULL,
                    auth_ciphertext = NULL,
                    auth_nonce = NULL,
                    auth_key_id = NULL
                WHERE job_id = ?
                  AND status = ?
                  AND finished_at IS NOT NULL
                  AND retention_expires_at = ?
                """,
                (job_id, status, retention_expires_at),
            )
            if cleared != 1:
                continue
            files, freed, cleaned = await asyncio.to_thread(
                self.remove_job_artifacts,
                row,
                job_dir_budget,
            )
            removed_files += files
            removed_bytes += freed
            if not cleaned:
                continue
            removed_jobs += await self.db_exec(
                """
                DELETE FROM jobs
                WHERE job_id = ?
                  AND status = ?
                  AND finished_at IS NOT NULL
                  AND retention_expires_at = ?
                """,
                (job_id, status, retention_expires_at),
            )

        cutoff = now - timedelta(days=self.max_retention_days())
        sweep_filesystem = self.sweep_filesystem_fn or self.sweep_filesystem
        ref_files, ref_bytes = await asyncio.to_thread(
            sweep_filesystem,
            cutoff.timestamp(),
        )
        removed_files += ref_files
        removed_bytes += ref_bytes
        orphan_files, orphan_bytes = await self.sweep_orphan_job_dirs(
            cutoff.timestamp()
        )
        removed_files += orphan_files
        removed_bytes += orphan_bytes
        if removed_files:
            self.log.info(
                "retention sweeper removed %d files (%d bytes)",
                removed_files,
                removed_bytes,
            )

        if removed_jobs:
            self.log.info(
                "retention sweeper removed %d job rows",
                removed_jobs,
            )
