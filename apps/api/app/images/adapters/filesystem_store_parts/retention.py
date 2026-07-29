from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Awaitable, Callable, Set
from pathlib import Path, PurePosixPath
from typing import Any

from ...domain.artifact import StagedSweepBudget, StagedSweepResult, UploadTicket
from ..filesystem_staging import (
    ArtifactStoreError,
    LegacyPage,
    ScanPage,
    StagedRecord,
    SweepCursor,
    SweepProgress,
)
from .objects import fsync_directory
from .staging import (
    STAGED_CURSOR_FILE,
    STAGED_QUARANTINE_DIRECTORY,
    STAGED_SHARD_COUNT,
    STAGED_SLOT_COUNT,
)


class FileSystemRetentionMixin:
    def _load_sweep_cursor(self) -> SweepCursor:
        path = self.root / STAGED_CURSOR_FILE
        try:
            value = self._read_json_file(path)
        except FileNotFoundError:
            return SweepCursor()
        slot = value.get("slot")
        legacy_after = value.get("legacy_after")
        if (
            not isinstance(slot, int)
            or slot < 0
            or slot >= STAGED_SLOT_COUNT
            or (legacy_after is not None and not isinstance(legacy_after, str))
        ):
            raise ArtifactStoreError("invalid staged sweep cursor")
        return SweepCursor(slot=slot, legacy_after=legacy_after)

    def _persist_sweep_cursor(self, cursor: SweepCursor) -> None:
        self._write_json_atomic(
            self.root / STAGED_CURSOR_FILE,
            {
                "version": 1,
                "slot": cursor.slot,
                "legacy_after": cursor.legacy_after,
            },
        )

    @staticmethod
    def _cursor_token(cursor: SweepCursor) -> str:
        suffix = "" if cursor.legacy_after is None else cursor.legacy_after
        return f"v1:{cursor.slot}:{suffix}"

    def _scan_metadata_shard(
        self,
        shard: int,
        *,
        max_files: int,
        deadline: float,
    ) -> ScanPage:
        shard_path = self._metadata_shard_path(shard)
        try:
            self._validate_directory(shard_path)
        except FileNotFoundError:
            return ScanPage(paths=(), complete=True)
        paths: list[Path] = []
        with os.scandir(shard_path) as entries:
            for entry in entries:
                if self._monotonic() >= deadline:
                    return ScanPage(
                        paths=tuple(paths),
                        complete=False,
                        time_exhausted=True,
                    )
                if len(paths) >= max_files:
                    return ScanPage(paths=tuple(paths), complete=False)
                paths.append(Path(entry.path))
        return ScanPage(paths=tuple(paths), complete=True)

    def _legacy_cursor_exists(self, cursor: str | None) -> bool:
        if cursor is None or "\\" in cursor:
            return False
        relative = PurePosixPath(cursor)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        path = self.root.joinpath(*relative.parts)
        try:
            self._validated_staged_relative_path(path)
            path.lstat()
        except (FileNotFoundError, ArtifactStoreError, ValueError):
            return False
        return True

    @staticmethod
    def _legacy_ticket_path(ticket_entry: Any) -> Path | None:
        try:
            info = ticket_entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            return None
        try:
            UploadTicket(ticket_entry.name)
        except ValueError:
            return None
        return Path(ticket_entry.path)

    @staticmethod
    def _legacy_entry_token(ticket_name: str, file_name: str) -> str:
        return PurePosixPath(
            ".upload-tmp",
            ticket_name,
            file_name,
        ).as_posix()

    def _legacy_record_from_entry(
        self,
        *,
        token: str,
        file_entry: Any,
        shard: int,
    ) -> StagedRecord | None:
        relative_path = PurePosixPath(token)
        if self._find_metadata_path(relative_path) is not None:
            return None
        try:
            record = self._record_from_legacy_path(
                Path(file_entry.path),
                shard=shard,
            )
        except (FileNotFoundError, ArtifactStoreError):
            return None
        return record

    def _scan_legacy_page(
        self,
        *,
        shard: int,
        after: str | None,
        max_files: int,
        deadline: float,
    ) -> LegacyPage:
        temp_root = self.root / ".upload-tmp"
        try:
            self._validate_directory(temp_root)
        except FileNotFoundError:
            return LegacyPage((), 0, True, None)
        resume = after is None or not self._legacy_cursor_exists(after)
        records: list[StagedRecord] = []
        scanned = 0
        last_cursor = after
        with os.scandir(temp_root) as ticket_entries:
            for ticket_entry in ticket_entries:
                if self._monotonic() >= deadline:
                    return LegacyPage(
                        tuple(records),
                        scanned,
                        False,
                        last_cursor,
                        time_exhausted=True,
                    )
                ticket_path = self._legacy_ticket_path(ticket_entry)
                if ticket_path is None:
                    continue
                with os.scandir(ticket_path) as file_entries:
                    for file_entry in file_entries:
                        if self._monotonic() >= deadline:
                            return LegacyPage(
                                tuple(records),
                                scanned,
                                False,
                                last_cursor,
                                time_exhausted=True,
                            )
                        token = self._legacy_entry_token(
                            ticket_entry.name,
                            file_entry.name,
                        )
                        if not resume:
                            if token == after:
                                resume = True
                            continue
                        record = self._legacy_record_from_entry(
                            token=token,
                            file_entry=file_entry,
                            shard=shard,
                        )
                        scanned += 1
                        last_cursor = token
                        if record is not None:
                            records.append(record)
                        if scanned >= max_files:
                            return LegacyPage(
                                tuple(records),
                                scanned,
                                False,
                                last_cursor,
                            )
        return LegacyPage(tuple(records), scanned, True, None)

    def _rotate_metadata(self, path: Path, *, current_shard: int) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArtifactStoreError("staged metadata entry is unsafe")
        next_shard = (current_shard + 1) % STAGED_SHARD_COUNT
        destination_dir = self._metadata_shard_path(next_shard)
        self._ensure_directory(destination_dir)
        destination = destination_dir / path.name
        os.replace(path, destination)
        fsync_directory(path.parent)
        if destination.parent != path.parent:
            fsync_directory(destination.parent)

    def _quarantine_metadata(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArtifactStoreError("staged metadata entry is unsafe")
        destination_dir = self.root / STAGED_QUARANTINE_DIRECTORY
        self._ensure_directory(destination_dir)
        destination = destination_dir / path.name
        os.replace(path, destination)
        fsync_directory(path.parent)
        fsync_directory(destination.parent)

    async def _load_metadata_page_records(
        self,
        page: ScanPage,
        *,
        current_shard: int,
        deadline: float,
        progress: SweepProgress,
    ) -> tuple[list[StagedRecord], int]:
        records: list[StagedRecord] = []
        consumed = 0
        for metadata_path in page.paths:
            if self._monotonic() >= deadline:
                progress.budget_exhausted = True
                progress.slot_complete = False
                break
            progress.scanned += 1
            consumed += 1
            try:
                record = await asyncio.to_thread(
                    self._record_from_metadata,
                    metadata_path,
                )
            except FileNotFoundError:
                self._record_staged_sweep_tombstone()
                continue
            except (ArtifactStoreError, json.JSONDecodeError, UnicodeError):
                progress.deferred += 1
                try:
                    await asyncio.to_thread(
                        self._rotate_metadata,
                        metadata_path,
                        current_shard=current_shard,
                    )
                except ArtifactStoreError:
                    pass
                continue
            records.append(record)
        return records, consumed

    def _annotate_sweep_error(
        self,
        error: Exception,
        cursor: SweepCursor | None,
    ) -> None:
        error._lumen_sweep_cursor = (  # type: ignore[attr-defined]
            None if cursor is None else self._cursor_token(cursor)
        )
        error._lumen_sweep_slot = (  # type: ignore[attr-defined]
            None if cursor is None else cursor.slot
        )
        error._lumen_sweep_root = str(self.root)  # type: ignore[attr-defined]

    async def _sweep_metadata_slot(
        self,
        cursor: SweepCursor,
        *,
        active_tickets: Set[str] | None,
        load_active_tickets: Callable[[Set[str]], Awaitable[Set[str]]] | None,
        stale_before: float,
        budget: StagedSweepBudget,
        deadline: float,
        before_delete: Callable[[], Awaitable[None]] | None,
        progress: SweepProgress,
    ) -> None:
        page = await asyncio.to_thread(
            self._scan_metadata_shard,
            cursor.slot,
            max_files=budget.max_files_per_pass,
            deadline=deadline,
        )
        records, consumed = await self._load_metadata_page_records(
            page,
            current_shard=cursor.slot,
            deadline=deadline,
            progress=progress,
        )
        processed = await self._process_sweep_records(
            records,
            current_shard=cursor.slot,
            active_tickets=active_tickets,
            load_active_tickets=load_active_tickets,
            stale_before=stale_before,
            budget=budget,
            deadline=deadline,
            before_delete=before_delete,
            progress=progress,
        )
        if consumed < len(page.paths) or processed < len(records):
            progress.slot_complete = False
        if not page.complete:
            progress.slot_complete = False
            progress.budget_exhausted = True
        if page.time_exhausted:
            progress.budget_exhausted = True

    async def _sweep_legacy_slot(
        self,
        cursor: SweepCursor,
        *,
        active_tickets: Set[str] | None,
        load_active_tickets: Callable[[Set[str]], Awaitable[Set[str]]] | None,
        stale_before: float,
        budget: StagedSweepBudget,
        deadline: float,
        before_delete: Callable[[], Awaitable[None]] | None,
        progress: SweepProgress,
    ) -> None:
        page = await asyncio.to_thread(
            self._scan_legacy_page,
            shard=0,
            after=cursor.legacy_after,
            max_files=budget.max_files_per_pass,
            deadline=deadline,
        )
        progress.scanned += page.scanned
        records = list(page.records)
        processed = await self._process_sweep_records(
            records,
            current_shard=0,
            active_tickets=active_tickets,
            load_active_tickets=load_active_tickets,
            stale_before=stale_before,
            budget=budget,
            deadline=deadline,
            before_delete=before_delete,
            progress=progress,
        )
        if processed < len(records):
            progress.slot_complete = False
        if not page.complete:
            progress.slot_complete = False
            progress.budget_exhausted = True
            progress.legacy_after = page.last_cursor
        else:
            progress.legacy_after = None
        if page.time_exhausted:
            progress.budget_exhausted = True

    async def sweep_staged(
        self,
        *,
        active_tickets: Set[str] | None,
        stale_before: float,
        budget: StagedSweepBudget,
        load_active_tickets: Callable[[Set[str]], Awaitable[Set[str]]] | None = None,
        before_delete: Callable[[], Awaitable[None]] | None = None,
    ) -> StagedSweepResult:
        started_at = self._monotonic()
        deadline = started_at + budget.max_seconds_per_pass
        cursor: SweepCursor | None = None
        try:
            cursor = await asyncio.to_thread(self._load_sweep_cursor)
            progress = SweepProgress(legacy_after=cursor.legacy_after)

            if cursor.slot < STAGED_SHARD_COUNT:
                await self._sweep_metadata_slot(
                    cursor,
                    active_tickets=active_tickets,
                    load_active_tickets=load_active_tickets,
                    stale_before=stale_before,
                    budget=budget,
                    deadline=deadline,
                    before_delete=before_delete,
                    progress=progress,
                )
            else:
                await self._sweep_legacy_slot(
                    cursor,
                    active_tickets=active_tickets,
                    load_active_tickets=load_active_tickets,
                    stale_before=stale_before,
                    budget=budget,
                    deadline=deadline,
                    before_delete=before_delete,
                    progress=progress,
                )

            next_slot = (
                (cursor.slot + 1) % STAGED_SLOT_COUNT
                if progress.slot_complete
                else cursor.slot
            )
            next_cursor_state = SweepCursor(
                slot=next_slot,
                legacy_after=progress.legacy_after,
            )
            await asyncio.to_thread(
                self._persist_sweep_cursor,
                next_cursor_state,
            )
        except Exception as exc:
            self._annotate_sweep_error(exc, cursor)
            raise
        return StagedSweepResult(
            scanned=progress.scanned,
            hashed_bytes=progress.hashed_bytes,
            deleted=progress.deleted,
            deferred=progress.deferred,
            quarantined=progress.quarantined,
            budget_exhausted=progress.budget_exhausted,
            next_cursor=self._cursor_token(next_cursor_state),
        )
