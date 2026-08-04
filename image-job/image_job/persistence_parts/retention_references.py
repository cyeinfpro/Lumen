"""Reference-file retention with pinned roots and fair bounded scans."""

from __future__ import annotations

import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..retention_row_cursor import (
    DurableReferenceRowCursor,
    ReferenceRowCandidate,
)
from ..retention_walk import (
    DirectoryHandle,
    DirectoryPathGuard,
    EntrySnapshot,
    directory_entry_snapshot,
    directory_path_matches,
    fsync_open_directory,
    is_retention_internal_directory,
    iter_directory_entries,
    open_directory,
    sweep_open_directory_bounded,
    unlink_verified_entry,
    verified_entry_absent,
)
from ..retention_scan_store import (
    SCAN_CANDIDATE,
    SCAN_IGNORE,
    DurableDirectoryScan,
    PersistentScanCandidate,
)


_REFERENCE_FILE_RE = re.compile(
    r"(?P<token>[A-Za-z0-9_-]+)\.(?P<ext>png|jpg|webp)\Z"
)


@dataclass(frozen=True)
class _ReferenceFileSnapshot:
    token: str
    ext: str
    entry: EntrySnapshot
    candidate_id: int | None = None


class ReferenceRetentionMixin:
    def sweep_dir(self, base: Path, cutoff_ts: float) -> tuple[int, int]:
        removed_files, removed_bytes, _durable, _guard = self._sweep_dir_status(
            base,
            cutoff_ts,
        )
        return removed_files, removed_bytes

    def _sweep_dir_status(
        self,
        base: Path,
        cutoff_ts: float,
    ) -> tuple[int, int, bool, DirectoryPathGuard | None]:
        budget = self._new_budget(self.ref_scan_max_entries)
        try:
            root = open_directory(base)
        except FileNotFoundError:
            return 0, 0, True, DirectoryPathGuard.absent(base)
        except OSError:
            return 0, 0, False, None
        with root:
            guard = DirectoryPathGuard.from_handle(root)
            removed_files, removed_bytes, complete = (
                sweep_open_directory_bounded(
                    root,
                    budget,
                    cutoff_ts=cutoff_ts,
                    scan_cursors=self._reference_scan_cursors,
                )
            )
            root_still_pinned = directory_path_matches(guard)
            if not root_still_pinned:
                self._reference_scan_cursors.reset_all()
        return (
            removed_files,
            removed_bytes,
            complete
            and not budget.durability_failed
            and root_still_pinned,
            guard,
        )

    def _reference_path(self, token: Any, ext: Any) -> Path | None:
        if not isinstance(token, str) or not isinstance(ext, str):
            return None
        filename = f"{token}.{ext}"
        match = _REFERENCE_FILE_RE.fullmatch(filename)
        if (
            match is None
            or match.group("token") != token
            or match.group("ext") != ext
        ):
            return None
        root = self.refs_dir()
        target = root / filename
        return target if target.parent == root else None

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        if conn.in_transaction:
            conn.execute("ROLLBACK")

    @staticmethod
    def _reference_delete_boundary_valid(
        refs: DirectoryHandle,
        root_guard: DirectoryPathGuard,
        target_name: str | None,
    ) -> bool:
        return directory_path_matches(root_guard) and (
            target_name is None
            or verified_entry_absent(refs, target_name)
        )

    def _delete_reference_metadata(
        self,
        conn: sqlite3.Connection,
        ref_rowid: int,
        created_at: Any,
        refs: DirectoryHandle,
        root_guard: DirectoryPathGuard,
        target_name: str | None,
    ) -> bool:
        if not self._reference_delete_boundary_valid(
            refs,
            root_guard,
            target_name,
        ):
            return False
        deleted = conn.execute(
            "DELETE FROM refs WHERE rowid = ? AND created_at = ?",
            (ref_rowid, created_at),
        ).rowcount
        return (
            deleted == 1
            and self._reference_delete_boundary_valid(
                refs,
                root_guard,
                target_name,
            )
        )

    def _retire_reference_row(
        self,
        conn: sqlite3.Connection,
        ref_rowid: int,
        expected_created_at: Any,
        expected_auth_hash: str,
        expected_sha256: str,
        expected_token: str,
        expected_ext: str,
        cutoff_iso: str,
        refs: DirectoryHandle | None,
    ) -> tuple[int, int, bool]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT rowid AS ref_rowid, token, ext, size, created_at
                FROM refs
                WHERE rowid = ?
                  AND created_at = ?
                  AND auth_hash = ?
                  AND sha256 = ?
                  AND token = ?
                  AND ext = ?
                  AND julianday(created_at) < julianday(?)
                """,
                (
                    ref_rowid,
                    expected_created_at,
                    expected_auth_hash,
                    expected_sha256,
                    expected_token,
                    expected_ext,
                    cutoff_iso,
                ),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return 0, 0, True
            if refs is None:
                self._rollback(conn)
                return 0, 0, False
            root_guard = DirectoryPathGuard.from_handle(refs)
            target = self._reference_path(row["token"], row["ext"])
            if target is None:
                deleted = self._delete_reference_metadata(
                    conn,
                    ref_rowid,
                    row["created_at"],
                    refs,
                    root_guard,
                    None,
                )
                if not deleted:
                    self._rollback(conn)
                    return 0, 0, False
                conn.execute("COMMIT")
                return 0, 0, True

            try:
                entry = directory_entry_snapshot(refs, target.name)
            except FileNotFoundError:
                entry = None
            except (OSError, ValueError):
                self._rollback(conn)
                return 0, 0, False
            if entry is None:
                removed = False
                durable = fsync_open_directory(refs)
            else:
                try:
                    unlink_verified_entry(refs, entry)
                except OSError:
                    self._rollback(conn)
                    return 0, 0, False
                removed = True
                durable = fsync_open_directory(refs)
            if not durable or not self._delete_reference_metadata(
                conn,
                ref_rowid,
                row["created_at"],
                refs,
                root_guard,
                target.name,
            ):
                self._rollback(conn)
                return 0, 0, False
            conn.execute("COMMIT")
        except BaseException:
            self._rollback(conn)
            raise

        if not removed or entry is None or not stat.S_ISREG(entry.mode):
            return 0, 0, True
        return 1, entry.size, True

    @staticmethod
    def _reference_row_cursor() -> DurableReferenceRowCursor:
        return DurableReferenceRowCursor(scope="reference-db-cleanup")

    def _reference_candidates(
        self,
        conn: sqlite3.Connection,
        cutoff_iso: str,
    ) -> tuple[ReferenceRowCandidate, ...]:
        return self._reference_row_cursor().claim(
            conn,
            cutoff_iso,
            self.ref_delete_batch_size,
        )

    @staticmethod
    def _reference_file_snapshot(
        entry: EntrySnapshot,
        cutoff_ts: float,
    ) -> _ReferenceFileSnapshot | None:
        match = _REFERENCE_FILE_RE.fullmatch(entry.name)
        if match is None:
            return None
        if (
            not stat.S_ISREG(entry.mode)
            or entry.mtime_ns >= cutoff_ts * 1_000_000_000
        ):
            return None
        return _ReferenceFileSnapshot(
            token=match.group("token"),
            ext=match.group("ext"),
            entry=entry,
        )

    def _orphan_reference_candidates(
        self,
        refs: DirectoryHandle,
        cutoff_ts: float,
    ) -> tuple[_ReferenceFileSnapshot, ...]:
        budget = self._new_budget(self.ref_scan_max_entries)
        candidates: list[_ReferenceFileSnapshot] = []
        cursor = self._reference_scan_cursors.cursor_for(refs)
        for entry in iter_directory_entries(refs, budget, cursor):
            if is_retention_internal_directory(entry):
                continue
            snapshot = self._reference_file_snapshot(entry, cutoff_ts)
            if snapshot is not None:
                candidates.append(snapshot)
        return tuple(candidates)

    def _reference_scan_store(self) -> DurableDirectoryScan | None:
        open_db = self.open_db
        if open_db is None:
            return None
        root = self.refs_dir()
        return DurableDirectoryScan(
            open_db=open_db,
            scope=f"reference-orphans:{root.absolute()}",
            root_path=root,
        )

    def _classify_reference_scan_entry(
        self,
        cutoff_ts: float,
        _depth: int,
        _parts: tuple[str, ...],
        entry: EntrySnapshot,
    ) -> str:
        if is_retention_internal_directory(entry):
            return SCAN_IGNORE
        return (
            SCAN_CANDIDATE
            if self._reference_file_snapshot(entry, cutoff_ts) is not None
            else SCAN_IGNORE
        )

    def _persistent_orphan_reference_candidates(
        self,
        refs: DirectoryHandle,
        cutoff_ts: float,
    ) -> tuple[
        DurableDirectoryScan,
        tuple[_ReferenceFileSnapshot, ...],
    ] | None:
        store = self._reference_scan_store()
        if store is None:
            return None
        budget = self._new_budget(self.ref_scan_max_entries)
        store.advance(
            refs,
            budget,
            lambda depth, parts, entry: self._classify_reference_scan_entry(
                cutoff_ts,
                depth,
                parts,
                entry,
            ),
        )
        persistent = store.candidates(
            refs,
            self.ref_delete_batch_size,
        )
        candidates: list[_ReferenceFileSnapshot] = []
        stale_ids: list[int] = []
        for candidate in persistent:
            snapshot = self._persistent_reference_snapshot(
                candidate,
                cutoff_ts,
            )
            if snapshot is None:
                stale_ids.append(candidate.candidate_id)
            else:
                candidates.append(snapshot)
        store.acknowledge(tuple(stale_ids))
        return store, tuple(candidates)

    @staticmethod
    def _persistent_reference_snapshot(
        candidate: PersistentScanCandidate,
        cutoff_ts: float,
    ) -> _ReferenceFileSnapshot | None:
        if len(candidate.relative_parts) != 1:
            return None
        snapshot = ReferenceRetentionMixin._reference_file_snapshot(
            candidate.entry,
            cutoff_ts,
        )
        if snapshot is None:
            return None
        return _ReferenceFileSnapshot(
            token=snapshot.token,
            ext=snapshot.ext,
            entry=snapshot.entry,
            candidate_id=candidate.candidate_id,
        )

    def _retire_orphan_reference(
        self,
        conn: sqlite3.Connection,
        refs: DirectoryHandle,
        candidate: _ReferenceFileSnapshot,
    ) -> tuple[int, int, bool]:
        root_guard = DirectoryPathGuard.from_handle(refs)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = directory_entry_snapshot(
                    refs,
                    candidate.entry.name,
                )
            except OSError:
                current = None
            if current != candidate.entry:
                conn.execute("COMMIT")
                return 0, 0, True
            row = conn.execute(
                """
                SELECT 1
                FROM refs
                WHERE token = ? AND ext = ?
                LIMIT 1
                """,
                (candidate.token, candidate.ext),
            ).fetchone()
            if row is not None:
                conn.execute("COMMIT")
                return 0, 0, True
            current = directory_entry_snapshot(
                refs,
                candidate.entry.name,
            )
            if current != candidate.entry:
                conn.execute("COMMIT")
                return 0, 0, True
            unlink_verified_entry(refs, candidate.entry)
            if (
                not fsync_open_directory(refs)
                or not directory_path_matches(root_guard)
            ):
                self._rollback(conn)
                return 0, 0, False
            conn.execute("COMMIT")
        except (OSError, sqlite3.Error):
            self._rollback(conn)
            return 0, 0, False
        return 1, candidate.entry.size, True

    def _sweep_persisted_references(
        self,
        cutoff_iso: str,
        cutoff_ts: float,
    ) -> tuple[int, int]:
        open_db = self.open_db
        if open_db is None:
            return 0, 0
        removed_files = 0
        removed_bytes = 0
        with self._reference_sweep_lock:
            refs_guard: DirectoryPathGuard | None = None
            try:
                refs = open_directory(self.refs_dir())
            except OSError:
                refs = None
            else:
                refs_guard = DirectoryPathGuard.from_handle(refs)
            conn = open_db()
            try:
                for candidate in self._reference_candidates(
                    conn,
                    cutoff_iso,
                ):
                    files, freed, _durable = self._retire_reference_row(
                        conn,
                        candidate.rowid,
                        candidate.created_at,
                        candidate.auth_hash,
                        candidate.sha256,
                        candidate.token,
                        candidate.ext,
                        cutoff_iso,
                        refs,
                    )
                    removed_files += files
                    removed_bytes += freed
                if refs is not None:
                    persistent = self._persistent_orphan_reference_candidates(
                        refs,
                        cutoff_ts,
                    )
                    if persistent is None:
                        store = None
                        candidates = self._orphan_reference_candidates(
                            refs,
                            cutoff_ts,
                        )
                    else:
                        store, candidates = persistent
                    acknowledged_ids: list[int] = []
                    deferred_ids: list[int] = []
                    for candidate in candidates:
                        files, freed, _durable = (
                            self._retire_orphan_reference(
                                conn,
                                refs,
                                candidate,
                            )
                        )
                        removed_files += files
                        removed_bytes += freed
                        if store is None or candidate.candidate_id is None:
                            continue
                        if _durable:
                            acknowledged_ids.append(candidate.candidate_id)
                        else:
                            deferred_ids.append(candidate.candidate_id)
                    if store is not None:
                        store.acknowledge(tuple(acknowledged_ids))
                        for candidate_id in deferred_ids:
                            store.defer(candidate_id)
            except sqlite3.OperationalError:
                self._rollback(conn)
            finally:
                conn.close()
                if refs is not None:
                    if (
                        refs_guard is not None
                        and not directory_path_matches(refs_guard)
                    ):
                        self._reference_scan_cursors.reset_all()
                    refs.close()
        return removed_files, removed_bytes

    def sweep_filesystem(self, cutoff_ts: float) -> tuple[int, int]:
        cutoff_iso = datetime.fromtimestamp(
            cutoff_ts,
            tz=timezone.utc,
        ).isoformat()
        if self.open_db is not None:
            return self._sweep_persisted_references(cutoff_iso, cutoff_ts)

        if self.sweep_dir_fn is None:
            (
                total_files,
                total_bytes,
                durable,
                root_guard,
            ) = self._sweep_dir_status(self.refs_dir(), cutoff_ts)
        else:
            root_path = self.refs_dir()
            try:
                root = open_directory(root_path)
            except FileNotFoundError:
                root_guard = DirectoryPathGuard.absent(root_path)
            except OSError:
                return 0, 0
            else:
                root_guard = DirectoryPathGuard.from_handle(root)
                root.close()
            total_files, total_bytes = self.sweep_dir_fn(
                root_path,
                cutoff_ts,
            )
            durable = directory_path_matches(root_guard)
        if (
            not durable
            or root_guard is None
            or not directory_path_matches(root_guard)
        ):
            return total_files, total_bytes
        try:
            self.db_exec_sync(
                """
                DELETE FROM refs
                WHERE rowid IN (
                    SELECT rowid
                    FROM refs
                    WHERE julianday(created_at) < julianday(?)
                    ORDER BY julianday(created_at) ASC, rowid ASC
                    LIMIT ?
                )
                """,
                (cutoff_iso, self.ref_delete_batch_size),
            )
        except sqlite3.OperationalError:
            pass
        return total_files, total_bytes
