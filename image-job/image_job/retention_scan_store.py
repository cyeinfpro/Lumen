"""Durable bounded directory-scan queues for retention discovery."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .retention_dir_reader import (
    DIRECTORY_PAGE_BYTES,
    read_directory_page,
)
from .retention_scan_schema import (
    begin_immediate as _begin_immediate,
    decode_scan_parts as _decode_parts,
    encode_scan_parts as _encode_parts,
    ensure_retention_scan_schema,
    rollback as _rollback,
)
from .retention_walk import (
    DirectoryHandle,
    DirectoryPathGuard,
    EntrySnapshot,
    TraversalBudget,
    directory_entry_snapshot,
    directory_path_matches,
    open_child_directory,
)


SCAN_IGNORE = "ignore"
SCAN_DESCEND = "descend"
SCAN_CANDIDATE = "candidate"
SCAN_RETRY = "retry"
_SCAN_ACTIONS = frozenset(
    {SCAN_IGNORE, SCAN_DESCEND, SCAN_CANDIDATE, SCAN_RETRY}
)
_MAX_PENDING_ENTRIES = 1_024
_MAX_PENDING_DIRECTORIES = 4_096
_MAX_PENDING_CANDIDATES = 4_096
_MAX_SCAN_SCOPES = 16
_STALE_SCOPE_SECONDS = 7 * 24 * 60 * 60
_DIRECTORY_PAGE_MAX_ENTRIES = DIRECTORY_PAGE_BYTES // 8
_MAX_CANDIDATE_ATTEMPTS = 8

ScanClassifier = Callable[[int, tuple[str, ...], EntrySnapshot], str]


@dataclass(frozen=True)
class PersistentScanCandidate:
    candidate_id: int
    relative_parts: tuple[str, ...]
    entry: EntrySnapshot
    eligible_mtime_ns: int


@dataclass(frozen=True)
class _RootState:
    generation: int
    work_sequence: int


@dataclass(frozen=True)
class _QueuedEntry:
    entry_id: int
    parent_parts: tuple[str, ...]
    parent_device: int
    parent_inode: int
    name: str
    depth: int


@dataclass(frozen=True)
class _QueuedDirectory:
    relative_parts: tuple[str, ...]
    depth: int
    device: int
    inode: int
    scan_offset: int


@dataclass(frozen=True)
class DurableDirectoryScan:
    open_db: Callable[[], sqlite3.Connection]
    scope: str
    root_path: Path
    now: Callable[[], float] = time.time
    max_pending_entries: int = _MAX_PENDING_ENTRIES
    max_pending_directories: int = _MAX_PENDING_DIRECTORIES
    max_pending_candidates: int = _MAX_PENDING_CANDIDATES

    def __post_init__(self) -> None:
        if self.max_pending_entries <= _DIRECTORY_PAGE_MAX_ENTRIES:
            raise ValueError("retention scan queue limits are too small")
        if (
            self.max_pending_directories <= 0
            or self.max_pending_candidates <= 0
        ):
            raise ValueError("retention scan queue limits must be positive")

    @staticmethod
    def _delete_scopes(
        conn: sqlite3.Connection,
        scopes: tuple[str, ...],
    ) -> None:
        if not scopes:
            return
        placeholders = ", ".join("?" for _scope in scopes)
        for table in (
            "retention_scan_entries",
            "retention_scan_directories",
            "retention_scan_candidates",
            "retention_scan_roots",
        ):
            conn.execute(
                f"DELETE FROM {table} WHERE scope IN ({placeholders})",  # nosec B608
                scopes,
            )

    def _prune_scopes(self, conn: sqlite3.Connection, current_time: float) -> None:
        rows = conn.execute(
            """
            SELECT scope, updated_at
            FROM retention_scan_roots
            WHERE scope <> ?
            ORDER BY updated_at DESC, scope ASC
            """,
            (self.scope,),
        ).fetchall()
        stale_before = current_time - _STALE_SCOPE_SECONDS
        stale = {
            str(row[0])
            for row in rows
            if float(row[1]) < stale_before
        }
        keep_other = max(0, _MAX_SCAN_SCOPES - 1)
        stale.update(str(row[0]) for row in rows[keep_other:])
        self._delete_scopes(conn, tuple(sorted(stale)))

    def _clear_scope(self, conn: sqlite3.Connection) -> None:
        self._delete_scopes(conn, (self.scope,))

    def invalidate(self) -> None:
        conn = self.open_db()
        try:
            ensure_retention_scan_schema(conn)
            _begin_immediate(conn)
            self._clear_scope(conn)
            conn.execute("COMMIT")
        except sqlite3.Error:
            _rollback(conn)
        finally:
            conn.close()

    @staticmethod
    def _root_directory_row(
        root: DirectoryHandle,
        generation: int,
    ) -> tuple[object, ...]:
        return (
            generation,
            b"",
            0,
            root.device,
            root.inode,
        )

    def _seed_cycle(
        self,
        conn: sqlite3.Connection,
        root: DirectoryHandle,
        generation: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO retention_scan_directories (
                scope, generation, relative_path, depth,
                device, inode, scan_offset
            ) VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (self.scope, *self._root_directory_row(root, generation)),
        )

    def _replace_root_state(
        self,
        conn: sqlite3.Connection,
        root: DirectoryHandle,
        generation: int,
        current_time: float,
    ) -> _RootState:
        self._clear_scope(conn)
        conn.execute(
            """
            INSERT INTO retention_scan_roots (
                scope, root_path, root_device, root_inode,
                generation, work_sequence, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                self.scope,
                os.fspath(self.root_path.absolute()),
                root.device,
                root.inode,
                generation,
                current_time,
            ),
        )
        self._seed_cycle(conn, root, generation)
        return _RootState(generation=generation, work_sequence=0)

    def _cycle_is_empty(
        self,
        conn: sqlite3.Connection,
        generation: int,
    ) -> bool:
        directory = conn.execute(
            """
            SELECT 1
            FROM retention_scan_directories
            WHERE scope = ? AND generation = ?
            LIMIT 1
            """,
            (self.scope, generation),
        ).fetchone()
        if directory is not None:
            return False
        entry = conn.execute(
            """
            SELECT 1
            FROM retention_scan_entries
            WHERE scope = ? AND generation = ?
            LIMIT 1
            """,
            (self.scope, generation),
        ).fetchone()
        return entry is None

    def _pin_root(
        self,
        conn: sqlite3.Connection,
        root: DirectoryHandle,
    ) -> _RootState:
        current_time = self.now()
        _begin_immediate(conn)
        try:
            self._prune_scopes(conn, current_time)
            row = conn.execute(
                """
                SELECT root_path, root_device, root_inode,
                       generation, work_sequence
                FROM retention_scan_roots
                WHERE scope = ?
                """,
                (self.scope,),
            ).fetchone()
            root_path = os.fspath(self.root_path.absolute())
            if row is None:
                state = self._replace_root_state(
                    conn,
                    root,
                    1,
                    current_time,
                )
            elif (
                str(row[0]) != root_path
                or int(row[1]) != root.device
                or int(row[2]) != root.inode
            ):
                state = self._replace_root_state(
                    conn,
                    root,
                    int(row[3]) + 1,
                    current_time,
                )
            else:
                generation = int(row[3])
                sequence = int(row[4])
                if self._cycle_is_empty(conn, generation):
                    generation += 1
                    conn.execute(
                        """
                        UPDATE retention_scan_roots
                        SET generation = ?, updated_at = ?
                        WHERE scope = ?
                        """,
                        (generation, current_time, self.scope),
                    )
                    self._seed_cycle(conn, root, generation)
                else:
                    conn.execute(
                        """
                        UPDATE retention_scan_roots
                        SET updated_at = ?
                        WHERE scope = ?
                        """,
                        (current_time, self.scope),
                    )
                state = _RootState(generation, sequence)
            conn.execute("COMMIT")
            return state
        except BaseException:
            _rollback(conn)
            raise

    @staticmethod
    def _open_relative_directory(
        root: DirectoryHandle,
        parts: tuple[str, ...],
        device: int,
        inode: int,
    ) -> tuple[DirectoryHandle, list[DirectoryHandle]]:
        handles: list[DirectoryHandle] = []
        current = root
        try:
            for part in parts:
                entry = directory_entry_snapshot(current, part)
                child = open_child_directory(
                    current,
                    entry,
                    include_metadata=False,
                )
                handles.append(child)
                current = child
            info = os.fstat(current.fd)
            if info.st_dev != device or info.st_ino != inode:
                raise OSError("retention scan directory changed")
            return current, handles
        except BaseException:
            for handle in reversed(handles):
                handle.close()
            raise

    @staticmethod
    def _close_handles(handles: list[DirectoryHandle]) -> None:
        for handle in reversed(handles):
            handle.close()

    def _next_entry(
        self,
        conn: sqlite3.Connection,
        generation: int,
    ) -> _QueuedEntry | None:
        row = conn.execute(
            """
            SELECT entry_id, parent_path, parent_device,
                   parent_inode, name, depth
            FROM retention_scan_entries
            WHERE scope = ? AND generation = ?
            ORDER BY attempt_order ASC, entry_id ASC
            LIMIT 1
            """,
            (self.scope, generation),
        ).fetchone()
        if row is None:
            return None
        return _QueuedEntry(
            entry_id=int(row[0]),
            parent_parts=_decode_parts(bytes(row[1])),
            parent_device=int(row[2]),
            parent_inode=int(row[3]),
            name=os.fsdecode(bytes(row[4])),
            depth=int(row[5]),
        )

    def _next_directory(
        self,
        conn: sqlite3.Connection,
        generation: int,
    ) -> _QueuedDirectory | None:
        row = conn.execute(
            """
            SELECT relative_path, depth, device, inode, scan_offset
            FROM retention_scan_directories
            WHERE scope = ? AND generation = ?
            ORDER BY depth DESC, attempt_order ASC, relative_path ASC
            LIMIT 1
            """,
            (self.scope, generation),
        ).fetchone()
        if row is None:
            return None
        return _QueuedDirectory(
            relative_parts=_decode_parts(bytes(row[0])),
            depth=int(row[1]),
            device=int(row[2]),
            inode=int(row[3]),
            scan_offset=int(row[4]),
        )

    def _next_sequence(
        self,
        conn: sqlite3.Connection,
        generation: int,
    ) -> int:
        row = conn.execute(
            """
            SELECT work_sequence
            FROM retention_scan_roots
            WHERE scope = ? AND generation = ?
            """,
            (self.scope, generation),
        ).fetchone()
        if row is None:
            raise sqlite3.OperationalError("retention scan root changed")
        sequence = int(row[0]) + 1
        conn.execute(
            """
            UPDATE retention_scan_roots
            SET work_sequence = ?, updated_at = ?
            WHERE scope = ? AND generation = ?
            """,
            (sequence, self.now(), self.scope, generation),
        )
        return sequence

    def _rotate_entry(
        self,
        conn: sqlite3.Connection,
        generation: int,
        entry_id: int,
    ) -> None:
        _begin_immediate(conn)
        try:
            sequence = self._next_sequence(conn, generation)
            conn.execute(
                """
                UPDATE retention_scan_entries
                SET attempt_order = ?
                WHERE entry_id = ? AND scope = ? AND generation = ?
                """,
                (sequence, entry_id, self.scope, generation),
            )
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    def _directory_capacity(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM retention_scan_directories
            WHERE scope = ?
            """,
            (self.scope,),
        ).fetchone()
        return int(row[0]) < self.max_pending_directories

    def _candidate_capacity(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM retention_scan_candidates
            WHERE scope = ?
            """,
            (self.scope,),
        ).fetchone()
        return int(row[0]) < self.max_pending_candidates

    def _commit_entry_action(
        self,
        conn: sqlite3.Connection,
        root: DirectoryHandle,
        generation: int,
        queued: _QueuedEntry,
        snapshot: EntrySnapshot | None,
        action: str,
    ) -> bool:
        _begin_immediate(conn)
        try:
            if action == SCAN_DESCEND and not self._directory_capacity(conn):
                _rollback(conn)
                return False
            if action == SCAN_CANDIDATE and not self._candidate_capacity(conn):
                _rollback(conn)
                return False
            if snapshot is not None and action == SCAN_DESCEND:
                parts = (*queued.parent_parts, snapshot.name)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO retention_scan_directories (
                        scope, generation, relative_path, depth,
                        device, inode, scan_offset
                    ) VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        self.scope,
                        generation,
                        _encode_parts(parts),
                        queued.depth + 1,
                        snapshot.device,
                        snapshot.inode,
                    ),
                )
            elif snapshot is not None and action == SCAN_CANDIDATE:
                parts = (*queued.parent_parts, snapshot.name)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO retention_scan_candidates (
                        scope, root_device, root_inode, relative_path,
                        device, inode, mode, size, mtime_ns, ctime_ns,
                        eligible_mtime_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.scope,
                        root.device,
                        root.inode,
                        _encode_parts(parts),
                        snapshot.device,
                        snapshot.inode,
                        snapshot.mode,
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.ctime_ns,
                        snapshot.mtime_ns,
                    ),
                )
            deleted = conn.execute(
                """
                DELETE FROM retention_scan_entries
                WHERE entry_id = ? AND scope = ? AND generation = ?
                """,
                (queued.entry_id, self.scope, generation),
            ).rowcount
            if deleted != 1:
                _rollback(conn)
                return True
            conn.execute(
                """
                UPDATE retention_scan_roots
                SET updated_at = ?
                WHERE scope = ? AND generation = ?
                  AND root_device = ? AND root_inode = ?
                """,
                (
                    self.now(),
                    self.scope,
                    generation,
                    root.device,
                    root.inode,
                ),
            )
            conn.execute("COMMIT")
            return True
        except BaseException:
            _rollback(conn)
            raise

    def _classify_entry(
        self,
        conn: sqlite3.Connection,
        root: DirectoryHandle,
        generation: int,
        queued: _QueuedEntry,
        classifier: ScanClassifier,
        budget: TraversalBudget,
    ) -> bool:
        if not budget.consume():
            return False
        handles: list[DirectoryHandle] = []
        try:
            parent, handles = self._open_relative_directory(
                root,
                queued.parent_parts,
                queued.parent_device,
                queued.parent_inode,
            )
            snapshot = directory_entry_snapshot(parent, queued.name)
        except (OSError, ValueError):
            snapshot = None
        finally:
            self._close_handles(handles)
        action = (
            SCAN_IGNORE
            if snapshot is None
            else classifier(queued.depth, queued.parent_parts, snapshot)
        )
        if action not in _SCAN_ACTIONS:
            raise ValueError(f"invalid retention scan action: {action}")
        if action == SCAN_RETRY:
            self._rotate_entry(conn, generation, queued.entry_id)
            return False
        completed = self._commit_entry_action(
            conn,
            root,
            generation,
            queued,
            snapshot,
            action,
        )
        if not completed:
            self._rotate_entry(conn, generation, queued.entry_id)
        return completed

    def _pending_entry_capacity(
        self,
        conn: sqlite3.Connection,
        generation: int,
    ) -> bool:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM retention_scan_entries
            WHERE scope = ? AND generation = ?
            """,
            (self.scope, generation),
        ).fetchone()
        return (
            int(row[0]) + _DIRECTORY_PAGE_MAX_ENTRIES
            <= self.max_pending_entries
        )

    def _drop_directory(
        self,
        conn: sqlite3.Connection,
        generation: int,
        queued: _QueuedDirectory,
    ) -> None:
        conn.execute(
            """
            DELETE FROM retention_scan_directories
            WHERE scope = ? AND generation = ? AND relative_path = ?
              AND device = ? AND inode = ? AND scan_offset = ?
            """,
            (
                self.scope,
                generation,
                _encode_parts(queued.relative_parts),
                queued.device,
                queued.inode,
                queued.scan_offset,
            ),
        )

    def _commit_directory_page(
        self,
        conn: sqlite3.Connection,
        generation: int,
        queued: _QueuedDirectory,
        names: tuple[bytes, ...],
        next_offset: int,
        reached_end: bool,
    ) -> None:
        _begin_immediate(conn)
        try:
            row = conn.execute(
                """
                SELECT 1
                FROM retention_scan_directories
                WHERE scope = ? AND generation = ? AND relative_path = ?
                  AND device = ? AND inode = ? AND scan_offset = ?
                """,
                (
                    self.scope,
                    generation,
                    _encode_parts(queued.relative_parts),
                    queued.device,
                    queued.inode,
                    queued.scan_offset,
                ),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return
            conn.executemany(
                """
                INSERT OR IGNORE INTO retention_scan_entries (
                    scope, generation, parent_path, parent_device,
                    parent_inode, name, depth
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        self.scope,
                        generation,
                        _encode_parts(queued.relative_parts),
                        queued.device,
                        queued.inode,
                        name,
                        queued.depth,
                    )
                    for name in names
                ),
            )
            if reached_end:
                self._drop_directory(conn, generation, queued)
            else:
                sequence = self._next_sequence(conn, generation)
                conn.execute(
                    """
                    UPDATE retention_scan_directories
                    SET scan_offset = ?, attempt_order = ?
                    WHERE scope = ? AND generation = ?
                      AND relative_path = ? AND device = ? AND inode = ?
                      AND scan_offset = ?
                    """,
                    (
                        next_offset,
                        sequence,
                        self.scope,
                        generation,
                        _encode_parts(queued.relative_parts),
                        queued.device,
                        queued.inode,
                        queued.scan_offset,
                    ),
                )
            conn.execute(
                """
                UPDATE retention_scan_roots
                SET updated_at = ?
                WHERE scope = ? AND generation = ?
                """,
                (self.now(), self.scope, generation),
            )
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    def _discover_page(
        self,
        conn: sqlite3.Connection,
        root: DirectoryHandle,
        generation: int,
        queued: _QueuedDirectory,
        budget: TraversalBudget,
    ) -> bool:
        if not self._pending_entry_capacity(conn, generation):
            return False
        if not budget.consume():
            return False
        handles: list[DirectoryHandle] = []
        try:
            directory, handles = self._open_relative_directory(
                root,
                queued.relative_parts,
                queued.device,
                queued.inode,
            )
            page = read_directory_page(
                directory.fd,
                device=queued.device,
                inode=queued.inode,
                offset=queued.scan_offset,
                buffer_bytes=DIRECTORY_PAGE_BYTES,
            )
        except OSError:
            _begin_immediate(conn)
            try:
                self._drop_directory(conn, generation, queued)
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
            return True
        finally:
            self._close_handles(handles)
        self._commit_directory_page(
            conn,
            generation,
            queued,
            page.names,
            page.next_offset,
            page.reached_end,
        )
        return True

    def advance(
        self,
        root: DirectoryHandle,
        budget: TraversalBudget,
        classifier: ScanClassifier,
        *,
        directory_limits: dict[int, int] | None = None,
    ) -> None:
        guard = DirectoryPathGuard.from_handle(root)
        if not directory_path_matches(guard):
            self.invalidate()
            return
        directories_by_depth: dict[int, set[bytes]] = {}
        conn = self.open_db()
        try:
            ensure_retention_scan_schema(conn)
            state = self._pin_root(conn, root)
            while budget.available():
                queued_entry = self._next_entry(conn, state.generation)
                if queued_entry is not None and self._classify_entry(
                    conn,
                    root,
                    state.generation,
                    queued_entry,
                    classifier,
                    budget,
                ):
                    continue
                queued_directory = self._next_directory(
                    conn,
                    state.generation,
                )
                if (
                    queued_directory is not None
                    and directory_limits is not None
                ):
                    limit = directory_limits.get(queued_directory.depth)
                    visited = directories_by_depth.setdefault(
                        queued_directory.depth,
                        set(),
                    )
                    directory_key = _encode_parts(
                        queued_directory.relative_parts
                    )
                    if (
                        limit is not None
                        and directory_key not in visited
                        and len(visited) >= limit
                    ):
                        break
                    visited.add(directory_key)
                if queued_directory is None or not self._discover_page(
                    conn,
                    root,
                    state.generation,
                    queued_directory,
                    budget,
                ):
                    break
                if not directory_path_matches(guard):
                    break
        finally:
            conn.close()
        if not directory_path_matches(guard):
            self.invalidate()

    def candidates(
        self,
        root: DirectoryHandle,
        limit: int,
    ) -> tuple[PersistentScanCandidate, ...]:
        guard = DirectoryPathGuard.from_handle(root)
        if not directory_path_matches(guard):
            self.invalidate()
            return ()
        conn = self.open_db()
        try:
            ensure_retention_scan_schema(conn)
            self._pin_root(conn, root)
            rows = conn.execute(
                """
                SELECT candidate_id, relative_path, device, inode,
                       mode, size, mtime_ns, ctime_ns, eligible_mtime_ns
                FROM retention_scan_candidates
                WHERE scope = ? AND root_device = ? AND root_inode = ?
                ORDER BY attempt_order ASC, candidate_id ASC
                LIMIT ?
                """,
                (self.scope, root.device, root.inode, limit),
            ).fetchall()
        finally:
            conn.close()
        if not directory_path_matches(guard):
            self.invalidate()
            return ()
        return tuple(
            PersistentScanCandidate(
                candidate_id=int(row[0]),
                relative_parts=_decode_parts(bytes(row[1])),
                entry=EntrySnapshot(
                    name=_decode_parts(bytes(row[1]))[-1],
                    device=int(row[2]),
                    inode=int(row[3]),
                    mode=int(row[4]),
                    size=int(row[5]),
                    mtime_ns=int(row[6]),
                    ctime_ns=int(row[7]),
                ),
                eligible_mtime_ns=int(row[8]),
            )
            for row in rows
        )

    def acknowledge(self, candidate_ids: tuple[int, ...]) -> None:
        if not candidate_ids:
            return
        placeholders = ", ".join("?" for _candidate_id in candidate_ids)
        conn = self.open_db()
        try:
            ensure_retention_scan_schema(conn)
            conn.execute(
                f"""
                DELETE FROM retention_scan_candidates
                WHERE scope = ? AND candidate_id IN ({placeholders})
                """,  # nosec B608
                (self.scope, *candidate_ids),
            )
        finally:
            conn.close()

    def defer(self, candidate_id: int) -> None:
        conn = self.open_db()
        try:
            ensure_retention_scan_schema(conn)
            _begin_immediate(conn)
            row = conn.execute(
                """
                SELECT roots.generation, candidates.attempts
                FROM retention_scan_roots AS roots
                JOIN retention_scan_candidates AS candidates
                  ON candidates.scope = roots.scope
                WHERE roots.scope = ? AND candidates.candidate_id = ?
                """,
                (self.scope, candidate_id),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return
            if int(row[1]) + 1 >= _MAX_CANDIDATE_ATTEMPTS:
                conn.execute(
                    """
                    DELETE FROM retention_scan_candidates
                    WHERE scope = ? AND candidate_id = ?
                    """,
                    (self.scope, candidate_id),
                )
                conn.execute("COMMIT")
                return
            sequence = self._next_sequence(conn, int(row[0]))
            conn.execute(
                """
                UPDATE retention_scan_candidates
                SET attempts = attempts + 1, attempt_order = ?
                WHERE scope = ? AND candidate_id = ?
                """,
                (sequence, self.scope, candidate_id),
            )
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise
        finally:
            conn.close()

    def refresh(
        self,
        candidate_id: int,
        entry: EntrySnapshot,
    ) -> bool:
        conn = self.open_db()
        try:
            ensure_retention_scan_schema(conn)
            updated = conn.execute(
                """
                UPDATE retention_scan_candidates
                SET mode = ?, size = ?, mtime_ns = ?, ctime_ns = ?,
                    attempts = 0
                WHERE scope = ? AND candidate_id = ?
                  AND device = ? AND inode = ?
                """,
                (
                    entry.mode,
                    entry.size,
                    entry.mtime_ns,
                    entry.ctime_ns,
                    self.scope,
                    candidate_id,
                    entry.device,
                    entry.inode,
                ),
            ).rowcount
            return updated == 1
        finally:
            conn.close()
