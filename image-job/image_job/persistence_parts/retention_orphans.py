"""Bounded in-memory fallback and durable orphan job-directory scans."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from ..retention_scan_store import (
    SCAN_CANDIDATE,
    SCAN_DESCEND,
    SCAN_IGNORE,
    SCAN_RETRY,
    DurableDirectoryScan,
    PersistentScanCandidate,
)
from ..retention_walk import (
    DirectoryHandle,
    DirectoryPathGuard,
    DirectoryScanCursor,
    EntrySnapshot,
    TraversalBudget,
    descriptor_relative_traversal_available,
    directory_entry_snapshot,
    directory_path_matches,
    is_retention_internal_directory,
    open_child_directory,
    open_directory,
)


@dataclass(frozen=True)
class JobDirectorySnapshot:
    path: Path
    relative_parts: tuple[str, ...]
    entry: EntrySnapshot
    candidate_id: int | None = None
    eligible_mtime_ns: int | None = None


@dataclass
class OrphanScanState:
    root_identity: tuple[int, int] | None = None
    year: EntrySnapshot | None = None
    month: EntrySnapshot | None = None
    day: EntrySnapshot | None = None
    root_cursor: DirectoryScanCursor = field(
        default_factory=DirectoryScanCursor
    )
    year_cursor: DirectoryScanCursor = field(
        default_factory=DirectoryScanCursor
    )
    month_cursor: DirectoryScanCursor = field(
        default_factory=DirectoryScanCursor
    )
    day_cursor: DirectoryScanCursor = field(
        default_factory=DirectoryScanCursor
    )

    def close_pass(self) -> None:
        self.root_cursor.close_pass()
        self.year_cursor.close_pass()
        self.month_cursor.close_pass()
        self.day_cursor.close_pass()

    def clear_day(self) -> None:
        self.day = None
        self.day_cursor.reset()

    def clear_month(self) -> None:
        self.clear_day()
        self.month = None
        self.month_cursor.reset()

    def clear_year(self) -> None:
        self.clear_month()
        self.year = None
        self.year_cursor.reset()

    def reset(self, root_identity: tuple[int, int] | None = None) -> None:
        self.close_pass()
        self.clear_year()
        self.root_cursor.reset()
        self.root_identity = root_identity

    def pin_root(self, root: DirectoryHandle) -> None:
        identity = (root.device, root.inode)
        if self.root_identity != identity:
            self.reset(identity)


class OrphanRetentionMixin:
    @staticmethod
    def _open_partition_child(
        parent: DirectoryHandle,
        entry: EntrySnapshot,
    ) -> DirectoryHandle | None:
        if not entry.is_directory:
            return None
        try:
            return open_child_directory(
                parent,
                entry,
                include_metadata=False,
            )
        except OSError:
            return None

    @staticmethod
    def _next_partition_directory(
        directory: DirectoryHandle,
        cursor: DirectoryScanCursor,
        budget: TraversalBudget,
    ) -> tuple[EntrySnapshot | None, bool]:
        while True:
            step = cursor.next_entry(directory, budget)
            if step.entry is not None:
                if step.entry.is_directory:
                    return step.entry, False
                continue
            return None, step.reached_end

    def _open_next_orphan_partition(
        self,
        root: DirectoryHandle,
        budget: TraversalBudget,
    ) -> tuple[
        tuple[str, str, str],
        tuple[DirectoryHandle, DirectoryHandle, DirectoryHandle],
    ] | None:
        state = self._orphan_scan_state
        while budget.available():
            if state.year is None:
                year_entry, reached_end = self._next_partition_directory(
                    root,
                    state.root_cursor,
                    budget,
                )
                if year_entry is None:
                    if reached_end:
                        state.clear_year()
                    return None
                state.year = year_entry
                state.clear_month()
            year = self._open_partition_child(root, state.year)
            if year is None:
                state.root_cursor.mark_failed()
                state.clear_year()
                continue

            if state.month is None:
                month_entry, reached_end = self._next_partition_directory(
                    year,
                    state.year_cursor,
                    budget,
                )
                if month_entry is None:
                    year.close()
                    if reached_end:
                        state.clear_year()
                        continue
                    return None
                state.month = month_entry
                state.clear_day()
            month = self._open_partition_child(year, state.month)
            if month is None:
                year.close()
                state.year_cursor.mark_failed()
                state.clear_month()
                continue

            if state.day is None:
                day_entry, reached_end = self._next_partition_directory(
                    month,
                    state.month_cursor,
                    budget,
                )
                if day_entry is None:
                    month.close()
                    year.close()
                    if reached_end:
                        state.clear_month()
                        continue
                    return None
                state.day = day_entry
                state.day_cursor.reset()
            day = self._open_partition_child(month, state.day)
            if day is None:
                month.close()
                year.close()
                state.month_cursor.mark_failed()
                state.clear_day()
                continue
            return (
                (state.year.name, state.month.name, state.day.name),
                (year, month, day),
            )
        return None

    def _scan_orphan_partition(
        self,
        temp_root: Path,
        day_parts: tuple[str, str, str],
        day: DirectoryHandle,
        cutoff_ts: float,
        budget: TraversalBudget,
        candidates: list[JobDirectorySnapshot],
        job_dirs_scanned: int,
    ) -> tuple[int, bool]:
        state = self._orphan_scan_state
        while job_dirs_scanned < self.orphan_scan_max_job_dirs:
            step = state.day_cursor.next_entry(day, budget)
            if step.exhausted:
                return job_dirs_scanned, False
            if step.reached_end:
                state.clear_day()
                return job_dirs_scanned, True
            job_entry = step.entry
            if job_entry is None or not job_entry.is_directory:
                continue
            job_dirs_scanned += 1
            if job_entry.mtime_ns < cutoff_ts * 1_000_000_000:
                relative_parts = (*day_parts, job_entry.name)
                candidates.append(
                    JobDirectorySnapshot(
                        path=temp_root.joinpath(*relative_parts),
                        relative_parts=relative_parts,
                        entry=job_entry,
                    )
                )
        return job_dirs_scanned, False

    def _fallback_orphan_candidates(
        self,
        root: DirectoryHandle,
        temp_root: Path,
        cutoff_ts: float,
    ) -> tuple[JobDirectorySnapshot, ...]:
        budget = self._new_budget(self.orphan_scan_max_entries)
        partitions_scanned = 0
        job_dirs_scanned = 0
        candidates: list[JobDirectorySnapshot] = []
        state = self._orphan_scan_state
        state.pin_root(root)
        try:
            while (
                partitions_scanned < self.orphan_scan_max_partitions
                and job_dirs_scanned < self.orphan_scan_max_job_dirs
                and budget.available()
            ):
                opened = self._open_next_orphan_partition(root, budget)
                if opened is None:
                    break
                day_parts, handles = opened
                year, month, day = handles
                partitions_scanned += 1
                try:
                    job_dirs_scanned, complete = self._scan_orphan_partition(
                        temp_root,
                        day_parts,
                        day,
                        cutoff_ts,
                        budget,
                        candidates,
                        job_dirs_scanned,
                    )
                finally:
                    day.close()
                    month.close()
                    year.close()
                if not complete:
                    break
        finally:
            state.close_pass()
        return tuple(candidates)

    def _job_scan_store(self, temp_root: Path) -> DurableDirectoryScan | None:
        open_db = self.open_db
        if open_db is None:
            return None
        return DurableDirectoryScan(
            open_db=open_db,
            scope=f"job-orphans:{temp_root.absolute()}",
            root_path=temp_root,
        )

    def _persistent_orphan_candidates(
        self,
        store: DurableDirectoryScan,
        root: DirectoryHandle,
        temp_root: Path,
        cutoff_ts: float,
    ) -> tuple[JobDirectorySnapshot, ...]:
        budget = self._new_budget(self.orphan_scan_max_entries)
        job_dirs_scanned = 0

        def classify(
            depth: int,
            _parts: tuple[str, ...],
            entry: EntrySnapshot,
        ) -> str:
            nonlocal job_dirs_scanned
            if is_retention_internal_directory(entry):
                return SCAN_IGNORE
            child_depth = depth + 1
            if child_depth <= 3:
                return SCAN_DESCEND if entry.is_directory else SCAN_IGNORE
            if child_depth != 4 or not entry.is_directory:
                return SCAN_IGNORE
            if job_dirs_scanned >= self.orphan_scan_max_job_dirs:
                budget.remaining_entries = 0
                budget.exhausted = True
                return SCAN_RETRY
            job_dirs_scanned += 1
            return (
                SCAN_CANDIDATE
                if entry.mtime_ns < cutoff_ts * 1_000_000_000
                else SCAN_IGNORE
            )

        store.advance(
            root,
            budget,
            classify,
            directory_limits={3: self.orphan_scan_max_partitions},
        )
        candidates: list[JobDirectorySnapshot] = []
        stale_ids: list[int] = []
        for candidate in store.candidates(
            root,
            self.orphan_scan_max_job_dirs,
        ):
            snapshot = self._persistent_job_snapshot(
                temp_root,
                candidate,
                cutoff_ts,
            )
            if snapshot is None:
                stale_ids.append(candidate.candidate_id)
            else:
                candidates.append(snapshot)
        store.acknowledge(tuple(stale_ids))
        return tuple(candidates)

    @staticmethod
    def _persistent_job_snapshot(
        temp_root: Path,
        candidate: PersistentScanCandidate,
        cutoff_ts: float,
    ) -> JobDirectorySnapshot | None:
        if (
            len(candidate.relative_parts) != 4
            or not candidate.entry.is_directory
            or candidate.eligible_mtime_ns
            >= cutoff_ts * 1_000_000_000
        ):
            return None
        return JobDirectorySnapshot(
            path=temp_root.joinpath(*candidate.relative_parts),
            relative_parts=candidate.relative_parts,
            entry=candidate.entry,
            candidate_id=candidate.candidate_id,
            eligible_mtime_ns=candidate.eligible_mtime_ns,
        )

    def _orphan_job_dir_candidate_snapshots_sync(
        self,
        cutoff_ts: float,
    ) -> tuple[JobDirectorySnapshot, ...]:
        temp_root = self.data_dir() / "images" / "temp"
        if not descriptor_relative_traversal_available():
            return ()
        store = self._job_scan_store(temp_root)
        try:
            root = open_directory(temp_root)
        except OSError:
            if store is not None:
                store.invalidate()
            return ()
        with root:
            if store is None:
                return self._fallback_orphan_candidates(
                    root,
                    temp_root,
                    cutoff_ts,
                )
            return self._persistent_orphan_candidates(
                store,
                root,
                temp_root,
                cutoff_ts,
            )

    def orphan_job_dir_candidates_sync(
        self,
        cutoff_ts: float,
    ) -> tuple[Path, ...]:
        return tuple(
            candidate.path
            for candidate in self._orphan_job_dir_candidate_snapshots_sync(
                cutoff_ts
            )
        )

    def _job_candidate_current(
        self,
        temp_root: Path,
        relative_parts: tuple[str, ...],
    ) -> EntrySnapshot | None:
        handles: list[DirectoryHandle] = []
        try:
            root = open_directory(temp_root)
            handles.append(root)
            guard = DirectoryPathGuard.from_handle(root)
            for part in relative_parts[:-1]:
                entry = directory_entry_snapshot(handles[-1], part)
                handles.append(
                    open_child_directory(
                        handles[-1],
                        entry,
                        include_metadata=False,
                    )
                )
            current = directory_entry_snapshot(
                handles[-1],
                relative_parts[-1],
            )
            return current if directory_path_matches(guard) else None
        except OSError:
            return None
        finally:
            self._close_directory_handles(handles)

    def _record_candidate_result(
        self,
        store: DurableDirectoryScan | None,
        candidate: JobDirectorySnapshot,
        *,
        acknowledge: bool,
    ) -> None:
        if store is None or candidate.candidate_id is None:
            return
        if acknowledge:
            store.acknowledge((candidate.candidate_id,))
        else:
            store.defer(candidate.candidate_id)

    def sweep_orphan_job_dirs_sync(
        self,
        known_job_ids: set[str],
        cutoff_ts: float,
        candidates: (
            tuple[Path, ...]
            | tuple[JobDirectorySnapshot, ...]
            | None
        ) = None,
    ) -> tuple[int, int]:
        """Remove old artifact directories with no matching jobs row."""
        temp_root = self.data_dir() / "images" / "temp"
        job_dirs = (
            self._orphan_job_dir_candidate_snapshots_sync(cutoff_ts)
            if candidates is None
            else candidates
        )
        store = self._job_scan_store(temp_root)
        cleanup_budget = self._new_budget(self.job_dir_scan_max_entries)
        removed_files = 0
        removed_bytes = 0
        for candidate in job_dirs:
            if not cleanup_budget.available():
                break
            job_dir = (
                candidate.path
                if isinstance(candidate, JobDirectorySnapshot)
                else candidate
            )
            if job_dir.name in known_job_ids:
                if isinstance(candidate, JobDirectorySnapshot):
                    self._record_candidate_result(
                        store,
                        candidate,
                        acknowledge=True,
                    )
                continue
            relative_parts = self._job_relative_parts(job_dir, temp_root)
            if relative_parts is None:
                continue
            expected = (
                candidate.entry
                if isinstance(candidate, JobDirectorySnapshot)
                else None
            )
            files, freed, cleaned, _root_guard = (
                self._remove_job_directory_parts(
                    temp_root,
                    relative_parts,
                    cleanup_budget,
                    expected=expected,
                    cutoff_ts=(
                        None
                        if isinstance(candidate, JobDirectorySnapshot)
                        and candidate.candidate_id is not None
                        else cutoff_ts
                    ),
                )
            )
            removed_files += files
            removed_bytes += freed
            if isinstance(candidate, JobDirectorySnapshot):
                current = self._job_candidate_current(
                    temp_root,
                    relative_parts,
                )
                same_inode = (
                    expected is not None
                    and current is not None
                    and expected.device == current.device
                    and expected.inode == current.inode
                )
                if (
                    not cleaned
                    and same_inode
                    and current is not None
                    and current != expected
                    and store is not None
                    and candidate.candidate_id is not None
                ):
                    store.refresh(candidate.candidate_id, current)
                self._record_candidate_result(
                    store,
                    candidate,
                    acknowledge=cleaned or not same_inode,
                )
            if cleanup_budget.exhausted:
                break
        return removed_files, removed_bytes

    async def sweep_orphan_job_dirs(self, cutoff_ts: float) -> tuple[int, int]:
        candidates = await asyncio.to_thread(
            self._orphan_job_dir_candidate_snapshots_sync,
            cutoff_ts,
        )
        candidate_ids = sorted(
            {candidate.path.name for candidate in candidates}
        )
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
