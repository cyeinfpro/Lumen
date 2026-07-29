from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable, Set

from ...domain.artifact import StagedSweepBudget
from ..filesystem_staging import (
    ArtifactIdentityMismatch,
    ArtifactStoreError,
    HashAttempt,
    RecordOutcome,
    StagedRecord,
    SweepProgress,
)


class FileSystemReconcileMixin:
    async def _rotate_record_metadata(
        self,
        record: StagedRecord,
        *,
        current_shard: int,
    ) -> bool:
        try:
            await asyncio.to_thread(
                self._rotate_metadata,
                record.metadata_path,
                current_shard=current_shard,
            )
        except ArtifactStoreError:
            return False
        return True

    @staticmethod
    def _record_matches_info(
        record: StagedRecord,
        info: os.stat_result,
    ) -> bool:
        expected = record.expected
        if expected is None:
            return True
        if expected.size_bytes != info.st_size:
            return False
        if expected.device is not None and expected.device != info.st_dev:
            return False
        return expected.inode is None or expected.inode == info.st_ino

    async def _preflight_staged_record(
        self,
        record: StagedRecord,
        *,
        current_shard: int,
        active_tickets: Set[str],
        stale_before: float,
    ) -> tuple[os.stat_result | None, RecordOutcome | None]:
        try:
            await asyncio.to_thread(
                self._validated_staged_relative_path,
                record.path,
                ticket=record.ticket,
            )
            info = await asyncio.to_thread(record.path.lstat)
        except FileNotFoundError:
            await asyncio.to_thread(
                self._remove_metadata_for_path,
                record.path,
                preferred=record.metadata_path,
            )
            return None, RecordOutcome()
        except ArtifactStoreError:
            return None, RecordOutcome(deferred=1)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            await self._rotate_record_metadata(record, current_shard=current_shard)
            return None, RecordOutcome(deferred=1)
        if record.ticket.value in active_tickets or record.created_at > stale_before:
            rotated = await self._rotate_record_metadata(
                record,
                current_shard=current_shard,
            )
            return None, RecordOutcome(deferred=int(not rotated))
        if not self._record_matches_info(record, info):
            await self._rotate_record_metadata(record, current_shard=current_shard)
            return None, RecordOutcome(deferred=1)
        return info, None

    async def _hash_record_with_budget(
        self,
        record: StagedRecord,
        *,
        current_shard: int,
        info: os.stat_result,
        remaining_hash_bytes: int,
        max_hash_bytes_per_pass: int,
        deadline: float,
    ) -> tuple[HashAttempt | None, RecordOutcome | None]:
        if info.st_size > max_hash_bytes_per_pass:
            try:
                await asyncio.to_thread(
                    self._quarantine_metadata,
                    record.metadata_path,
                )
            except ArtifactStoreError:
                pass
            return None, RecordOutcome(deferred=1, quarantined=1)
        if info.st_size > remaining_hash_bytes:
            return None, RecordOutcome(budget_exhausted=True)
        attempt = await asyncio.to_thread(
            self._hash_staged_file,
            record.path,
            max_bytes=remaining_hash_bytes,
            deadline=deadline,
            monotonic=self._monotonic,
        )
        if attempt.budget_exhausted:
            deadline_exhausted = (
                attempt.deadline_exhausted or self._monotonic() >= deadline
            )
            return None, RecordOutcome(
                hashed_bytes=attempt.bytes_hashed,
                budget_exhausted=True,
                deadline_exhausted=deadline_exhausted,
            )
        valid = (
            not attempt.changed
            and attempt.identity is not None
            and attempt.fingerprint is not None
            and (record.expected is None or record.expected.matches(attempt.identity))
        )
        if valid:
            return attempt, None
        await self._rotate_record_metadata(
            record,
            current_shard=current_shard,
        )
        return None, RecordOutcome(
            hashed_bytes=attempt.bytes_hashed,
            deferred=1,
        )

    async def _delete_verified_staged(
        self,
        record: StagedRecord,
        *,
        current_shard: int,
        attempt: HashAttempt,
        deadline: float,
        before_delete: Callable[[], Awaitable[None]] | None,
    ) -> RecordOutcome:
        remaining_seconds = deadline - self._monotonic()
        if remaining_seconds <= 0:
            return RecordOutcome(
                hashed_bytes=attempt.bytes_hashed,
                budget_exhausted=True,
                deadline_exhausted=True,
            )
        if before_delete is not None:
            try:
                await asyncio.wait_for(
                    before_delete(),
                    timeout=remaining_seconds,
                )
            except TimeoutError:
                return RecordOutcome(
                    hashed_bytes=attempt.bytes_hashed,
                    budget_exhausted=True,
                    deadline_exhausted=True,
                )
        if self._monotonic() >= deadline:
            return RecordOutcome(
                hashed_bytes=attempt.bytes_hashed,
                budget_exhausted=True,
                deadline_exhausted=True,
            )
        try:
            deleted = await asyncio.to_thread(
                self._unlink_staged_if_unchanged,
                record,
                attempt.fingerprint,
            )
        except FileNotFoundError:
            deleted = False
        except ArtifactIdentityMismatch:
            await self._rotate_record_metadata(
                record,
                current_shard=current_shard,
            )
            return RecordOutcome(
                hashed_bytes=attempt.bytes_hashed,
                deferred=1,
            )
        return RecordOutcome(
            hashed_bytes=attempt.bytes_hashed,
            deleted=int(deleted),
        )

    async def _process_staged_record(
        self,
        record: StagedRecord,
        *,
        current_shard: int,
        active_tickets: Set[str],
        stale_before: float,
        remaining_hash_bytes: int,
        max_hash_bytes_per_pass: int,
        deadline: float,
        before_delete: Callable[[], Awaitable[None]] | None,
    ) -> RecordOutcome:
        if self._monotonic() >= deadline:
            return RecordOutcome(
                budget_exhausted=True,
                deadline_exhausted=True,
            )
        info, outcome = await self._preflight_staged_record(
            record,
            current_shard=current_shard,
            active_tickets=active_tickets,
            stale_before=stale_before,
        )
        if outcome is not None or info is None:
            return outcome or RecordOutcome()
        attempt, outcome = await self._hash_record_with_budget(
            record,
            current_shard=current_shard,
            info=info,
            remaining_hash_bytes=remaining_hash_bytes,
            max_hash_bytes_per_pass=max_hash_bytes_per_pass,
            deadline=deadline,
        )
        if outcome is not None or attempt is None:
            return outcome or RecordOutcome()
        return await self._delete_verified_staged(
            record,
            current_shard=current_shard,
            attempt=attempt,
            deadline=deadline,
            before_delete=before_delete,
        )

    async def _resolve_active_tickets_for_records(
        self,
        records: list[StagedRecord],
        *,
        active_tickets: Set[str] | None,
        load_active_tickets: Callable[[Set[str]], Awaitable[Set[str]]] | None,
        deadline: float,
    ) -> Set[str] | None:
        if load_active_tickets is None:
            return set(active_tickets or ())
        remaining_seconds = deadline - self._monotonic()
        if remaining_seconds <= 0:
            return None
        candidates = {record.ticket.value for record in records}
        try:
            return await asyncio.wait_for(
                load_active_tickets(candidates),
                timeout=remaining_seconds,
            )
        except TimeoutError:
            return None

    @staticmethod
    def _accumulate_record_outcome(
        progress: SweepProgress,
        outcome: RecordOutcome,
    ) -> None:
        progress.hashed_bytes += outcome.hashed_bytes
        progress.deleted += outcome.deleted
        progress.deferred += outcome.deferred
        progress.quarantined += outcome.quarantined

    async def _process_sweep_records(
        self,
        records: list[StagedRecord],
        *,
        current_shard: int,
        active_tickets: Set[str] | None,
        load_active_tickets: Callable[[Set[str]], Awaitable[Set[str]]] | None,
        stale_before: float,
        budget: StagedSweepBudget,
        deadline: float,
        before_delete: Callable[[], Awaitable[None]] | None,
        progress: SweepProgress,
    ) -> int:
        resolved_active = await self._resolve_active_tickets_for_records(
            records,
            active_tickets=active_tickets,
            load_active_tickets=load_active_tickets,
            deadline=deadline,
        )
        if resolved_active is None:
            progress.budget_exhausted = True
            progress.slot_complete = False
            return 0
        processed = 0
        for record in records:
            if self._monotonic() >= deadline:
                progress.budget_exhausted = True
                progress.slot_complete = False
                break
            processed += 1
            outcome = await self._process_staged_record(
                record,
                current_shard=current_shard,
                active_tickets=resolved_active,
                stale_before=stale_before,
                remaining_hash_bytes=(
                    budget.max_bytes_hashed_per_pass - progress.hashed_bytes
                ),
                max_hash_bytes_per_pass=budget.max_bytes_hashed_per_pass,
                deadline=deadline,
                before_delete=before_delete,
            )
            self._accumulate_record_outcome(progress, outcome)
            if outcome.budget_exhausted:
                if outcome.deadline_exhausted:
                    rotated = await self._rotate_record_metadata(
                        record,
                        current_shard=current_shard,
                    )
                    progress.deferred += int(not rotated)
                progress.budget_exhausted = True
                progress.slot_complete = False
                break
        return processed
