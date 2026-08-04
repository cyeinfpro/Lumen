"""Retention cleanup implementation for persisted jobs and references."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..retention_walk import (
    DirectoryHandle,
    DirectoryPathGuard,
    DirectoryScanCursor,
    DirectoryScanRegistry,
    EntrySnapshot,
    TraversalBudget,
    descriptor_relative_traversal_available,
    directory_path_matches,
    directory_entry_snapshot,
    fsync_open_directory,
    new_traversal_budget,
    open_child_directory,
    open_directory,
    remove_verified_directory,
    sweep_directory_entry_bounded,
    verified_relative_path_absent,
)
from .common import DbAll, DbExec, parse_utc_datetime, strict_json_loads
from .retention_orphans import OrphanRetentionMixin, OrphanScanState
from .retention_references import ReferenceRetentionMixin


@dataclass(frozen=True)
class RetentionFacade(OrphanRetentionMixin, ReferenceRetentionMixin):
    data_dir: Callable[[], Path]
    refs_dir: Callable[[], Path]
    db_exec_sync: Callable[[str, tuple[Any, ...]], int]
    db_exec: DbExec
    db_all: DbAll
    utc_now: Callable[[], datetime]
    max_retention_days: Callable[[], int]
    job_ttl_days: Callable[[], int]
    log: logging.Logger
    open_db: Callable[[], sqlite3.Connection] | None = None
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
    _reference_sweep_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )
    _reference_scan_cursors: DirectoryScanRegistry[DirectoryScanCursor] = field(
        default_factory=lambda: DirectoryScanRegistry.create(
            DirectoryScanCursor
        ),
        init=False,
        repr=False,
        compare=False,
    )
    _job_scan_cursors: DirectoryScanRegistry[DirectoryScanCursor] = field(
        default_factory=lambda: DirectoryScanRegistry.create(
            DirectoryScanCursor
        ),
        init=False,
        repr=False,
        compare=False,
    )
    _orphan_scan_state: OrphanScanState = field(
        default_factory=OrphanScanState,
        init=False,
        repr=False,
        compare=False,
    )
    _missing_expiry_cursor: list[str] = field(
        default_factory=lambda: ["", ""],
        init=False,
        repr=False,
        compare=False,
    )
    _expired_job_cursor: list[str] = field(
        default_factory=lambda: ["", ""],
        init=False,
        repr=False,
        compare=False,
    )

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

    @staticmethod
    def _job_relative_parts(
        job_dir: Path,
        temp_root: Path,
    ) -> tuple[str, ...] | None:
        try:
            parts = job_dir.relative_to(temp_root).parts
        except ValueError:
            return None
        if (
            not parts
            or any(
                not part or part in {".", ".."} or "\x00" in part
                for part in parts
            )
            or temp_root.joinpath(*parts) != job_dir
        ):
            return None
        return parts

    @staticmethod
    def _close_directory_handles(handles: list[DirectoryHandle]) -> None:
        for handle in reversed(handles):
            handle.close()

    @staticmethod
    def _prune_empty_job_parents(
        handles: list[DirectoryHandle],
        parent_parts: tuple[str, ...],
        budget: TraversalBudget,
    ) -> bool:
        for index in range(len(handles) - 1, 0, -1):
            if not remove_verified_directory(
                handles[index - 1],
                parent_parts[index - 1],
                handles[index],
                budget=budget,
            ):
                return not budget.durability_failed
        return True

    def _remove_job_directory_parts(
        self,
        temp_root: Path,
        relative_parts: tuple[str, ...],
        budget: TraversalBudget,
        *,
        expected: EntrySnapshot | None = None,
        cutoff_ts: float | None = None,
    ) -> tuple[int, int, bool, DirectoryPathGuard | None]:
        if (
            not relative_parts
            or not descriptor_relative_traversal_available()
        ):
            return 0, 0, False, None

        handles: list[DirectoryHandle] = []
        root_guard: DirectoryPathGuard | None = None
        try:
            try:
                handles.append(open_directory(temp_root))
            except FileNotFoundError:
                root_guard = DirectoryPathGuard.absent(temp_root)
                return 0, 0, directory_path_matches(root_guard), root_guard
            root_guard = DirectoryPathGuard.from_handle(handles[0])

            for part in relative_parts[:-1]:
                parent = handles[-1]
                try:
                    entry = directory_entry_snapshot(parent, part)
                except FileNotFoundError:
                    return (
                        0,
                        0,
                        fsync_open_directory(parent, budget)
                        and directory_path_matches(root_guard),
                        root_guard,
                    )
                if not entry.is_directory:
                    return 0, 0, False, root_guard
                handles.append(open_child_directory(parent, entry))

            parent = handles[-1]
            try:
                current = directory_entry_snapshot(
                    parent,
                    relative_parts[-1],
                )
            except FileNotFoundError:
                return (
                    0,
                    0,
                    fsync_open_directory(parent, budget)
                    and directory_path_matches(root_guard),
                    root_guard,
                )
            if (
                not current.is_directory
                or expected is not None
                and current != expected
                or cutoff_ts is not None
                and current.mtime_ns >= cutoff_ts * 1_000_000_000
            ):
                return 0, 0, False, root_guard

            removed_files, removed_bytes, cleaned = (
                sweep_directory_entry_bounded(
                    parent,
                    current,
                    budget,
                    cutoff_ts=None,
                    scan_cursors=self._job_scan_cursors,
                )
            )
            if not cleaned:
                return removed_files, removed_bytes, False, root_guard
            durable = self._prune_empty_job_parents(
                handles,
                relative_parts[:-1],
                budget,
            )
            return (
                removed_files,
                removed_bytes,
                durable and directory_path_matches(root_guard),
                root_guard,
            )
        except (OSError, ValueError):
            return 0, 0, False, root_guard
        finally:
            if (
                root_guard is not None
                and not directory_path_matches(root_guard)
            ):
                self._job_scan_cursors.reset_all()
            self._close_directory_handles(handles)

    def _remove_job_artifacts_status(
        self,
        row: Any,
        budget: TraversalBudget | None = None,
    ) -> tuple[
        int,
        int,
        bool,
        DirectoryPathGuard | None,
        tuple[str, ...] | None,
    ]:
        job_id = self._row_value(row, "job_id")
        created_at = self._parse_datetime(self._row_value(row, "created_at"))
        if (
            not isinstance(job_id, str)
            or not job_id
            or job_id in {".", ".."}
            or "/" in job_id
            or "\\" in job_id
            or "\x00" in job_id
            or created_at is None
        ):
            return 0, 0, False, None, None

        temp_root = self.data_dir() / "images" / "temp"
        relative_parts = (
            created_at.strftime("%Y"),
            created_at.strftime("%m"),
            created_at.strftime("%d"),
            job_id,
        )
        traversal_budget = budget or self._new_budget(
            self.job_dir_scan_max_entries
        )
        files, freed, cleaned, root_guard = (
            self._remove_job_directory_parts(
                temp_root,
                relative_parts,
                traversal_budget,
            )
        )
        return files, freed, cleaned, root_guard, relative_parts

    def remove_job_artifacts(
        self,
        row: Any,
        budget: TraversalBudget | None = None,
    ) -> tuple[int, int, bool]:
        files, freed, cleaned, _guard, _parts = (
            self._remove_job_artifacts_status(row, budget)
        )
        return files, freed, cleaned

    def remove_job_dir(
        self,
        job_dir: Path,
        temp_root: Path,
        *,
        budget: TraversalBudget | None = None,
    ) -> tuple[int, int, bool]:
        relative_parts = self._job_relative_parts(job_dir, temp_root)
        if relative_parts is None:
            return 0, 0, False

        traversal_budget = budget or self._new_budget(
            self.job_dir_scan_max_entries
        )
        removed_files, removed_bytes, cleaned, _root_guard = (
            self._remove_job_directory_parts(
                temp_root,
                relative_parts,
                traversal_budget,
            )
        )
        if not cleaned and not traversal_budget.exhausted:
            self.log.warning(
                "retention sweeper could not remove job directory %s",
                job_dir,
            )
        return removed_files, removed_bytes, cleaned

    def _delete_job_row_if_guard_valid(
        self,
        job_id: Any,
        status: Any,
        retention_expires_at: Any,
        root_guard: DirectoryPathGuard,
        relative_parts: tuple[str, ...],
    ) -> int:
        open_db = self.open_db
        if open_db is None:
            return 0
        conn = open_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not verified_relative_path_absent(
                root_guard,
                relative_parts,
            ):
                self._rollback(conn)
                return 0
            deleted = conn.execute(
                """
                DELETE FROM jobs
                WHERE job_id = ?
                  AND status = ?
                  AND finished_at IS NOT NULL
                  AND retention_expires_at = ?
                """,
                (job_id, status, retention_expires_at),
            ).rowcount
            if (
                deleted != 1
                or not verified_relative_path_absent(
                    root_guard,
                    relative_parts,
                )
            ):
                self._rollback(conn)
                return 0
            conn.execute("COMMIT")
            return 1
        except sqlite3.Error:
            self._rollback(conn)
            return 0
        finally:
            conn.close()

    async def _backfill_missing_expiries(self) -> None:
        missing_finished_at, missing_job_id = self._missing_expiry_cursor
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
            ORDER BY CASE
                         WHEN finished_at > ?
                           OR (finished_at = ? AND job_id > ?)
                         THEN 0 ELSE 1
                     END,
                     finished_at ASC,
                     job_id ASC
            LIMIT ?
            """,
            (
                missing_finished_at,
                missing_finished_at,
                missing_job_id,
                self.finished_row_batch_size,
            ),
        )
        for row in missing_expiry_rows:
            cursor_finished_at = self._row_value(row, "finished_at")
            cursor_job_id = self._row_value(row, "job_id")
            if cursor_finished_at is not None and cursor_job_id is not None:
                self._missing_expiry_cursor[:] = [
                    str(cursor_finished_at),
                    str(cursor_job_id),
                ]
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

    async def _cleanup_terminal_rows(
        self,
        now: datetime,
    ) -> tuple[int, int, int]:
        expired_at_cursor, expired_job_id = self._expired_job_cursor
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
            ORDER BY CASE
                         WHEN retention_expires_at > ?
                           OR (
                               retention_expires_at = ?
                               AND job_id > ?
                           )
                         THEN 0 ELSE 1
                     END,
                     retention_expires_at ASC,
                     job_id ASC
            LIMIT ?
            """,
            (
                now.isoformat(),
                expired_at_cursor,
                expired_at_cursor,
                expired_job_id,
                self.finished_row_batch_size,
            ),
        )
        removed_files = 0
        removed_bytes = 0
        removed_jobs = 0
        job_dir_budget = self._new_budget(self.job_dir_scan_max_entries)
        for row in rows:
            if not job_dir_budget.available():
                break
            cursor_expiry = self._row_value(row, "retention_expires_at")
            cursor_job_id = self._row_value(row, "job_id")
            if cursor_expiry is not None and cursor_job_id is not None:
                self._expired_job_cursor[:] = [
                    str(cursor_expiry),
                    str(cursor_job_id),
                ]
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
            (
                files,
                freed,
                cleaned,
                root_guard,
                relative_parts,
            ) = await asyncio.to_thread(
                self._remove_job_artifacts_status,
                row,
                job_dir_budget,
            )
            removed_files += files
            removed_bytes += freed
            if (
                not cleaned
                or root_guard is None
                or relative_parts is None
            ):
                continue
            if self.open_db is not None:
                removed_jobs += await asyncio.to_thread(
                    self._delete_job_row_if_guard_valid,
                    job_id,
                    status,
                    retention_expires_at,
                    root_guard,
                    relative_parts,
                )
            elif await asyncio.to_thread(
                verified_relative_path_absent,
                root_guard,
                relative_parts,
            ):
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

        return removed_files, removed_bytes, removed_jobs

    async def _sweep_filesystem_orphans_and_report(
        self,
        now: datetime,
        removed_files: int,
        removed_bytes: int,
        removed_jobs: int,
    ) -> None:
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

    async def run_pass(self) -> None:
        now = self.utc_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)

        await self._backfill_missing_expiries()
        removed_files, removed_bytes, removed_jobs = (
            await self._cleanup_terminal_rows(now)
        )
        await self._sweep_filesystem_orphans_and_report(
            now,
            removed_files,
            removed_bytes,
            removed_jobs,
        )
