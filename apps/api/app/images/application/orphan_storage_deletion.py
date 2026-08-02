from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, Protocol, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import User


class FileCandidate(Protocol):
    key: str


CandidateT = TypeVar("CandidateT", bound=FileCandidate)


@dataclass(frozen=True)
class OrphanDeletionResult(Generic[CandidateT]):
    confirmed: tuple[CandidateT, ...]
    changed: tuple[str, ...]
    failed: tuple[str, ...]
    deleted: int
    incomplete: bool


def _remaining_seconds(
    *,
    max_seconds: float,
    started: float,
    monotonic: Callable[[], float],
) -> float:
    return max_seconds - (monotonic() - started)


def _candidate_user_id(storage_key: str) -> str | None:
    parts = storage_key.split("/")
    if len(parts) < 3 or parts[0] != "u" or not parts[1] or parts[1] in {".", ".."}:
        return None
    return parts[1]


async def delete_orphan_candidates(
    db: AsyncSession,
    candidates: list[CandidateT],
    *,
    max_seconds: float,
    started: float,
    monotonic: Callable[[], float],
    assert_owned: Callable[[], Awaitable[None]] | None,
    known_storage_keys: Callable[
        [AsyncSession, set[str]],
        Awaitable[set[str]],
    ],
    unlink_if_unchanged: Callable[[CandidateT], bool],
) -> OrphanDeletionResult[CandidateT]:
    confirmed: list[CandidateT] = []
    changed: list[str] = []
    failed: list[str] = []
    deleted = 0
    incomplete = False
    for candidate in candidates:
        remaining = _remaining_seconds(
            max_seconds=max_seconds,
            started=started,
            monotonic=monotonic,
        )
        if remaining <= 0:
            incomplete = True
            break
        if assert_owned is not None:
            await assert_owned()
            remaining = _remaining_seconds(
                max_seconds=max_seconds,
                started=started,
                monotonic=monotonic,
            )
            if remaining <= 0:
                incomplete = True
                break
        user_id = _candidate_user_id(candidate.key)
        if user_id is None:
            confirmed.append(candidate)
            changed.append(candidate.key)
            continue
        # Plain snapshot read, never FOR UPDATE: this sweep never mutates the
        # user row, and row locks acquired here would pile up across the whole
        # candidate batch inside one uncommitted transaction, stalling account
        # deletion and session revocation for the scan's duration. The per-file
        # live-reference recheck below plus the inode guard still protect the
        # unlink.
        try:
            await asyncio.wait_for(
                db.execute(select(User.id).where(User.id == user_id)),
                timeout=remaining,
            )
        except TimeoutError:
            incomplete = True
            break
        remaining = _remaining_seconds(
            max_seconds=max_seconds,
            started=started,
            monotonic=monotonic,
        )
        if remaining <= 0:
            incomplete = True
            break
        try:
            now_known = await asyncio.wait_for(
                known_storage_keys(db, {candidate.key}),
                timeout=remaining,
            )
        except TimeoutError:
            incomplete = True
            break
        if candidate.key in now_known:
            continue

        remaining = _remaining_seconds(
            max_seconds=max_seconds,
            started=started,
            monotonic=monotonic,
        )
        if remaining <= 0:
            incomplete = True
            break
        if assert_owned is not None:
            await assert_owned()
            remaining = _remaining_seconds(
                max_seconds=max_seconds,
                started=started,
                monotonic=monotonic,
            )
            if remaining <= 0:
                incomplete = True
                break
        try:
            removed = await asyncio.wait_for(
                asyncio.to_thread(unlink_if_unchanged, candidate),
                timeout=remaining,
            )
        except TimeoutError:
            incomplete = True
            break
        except OSError:
            confirmed.append(candidate)
            failed.append(candidate.key)
            incomplete = True
            continue
        confirmed.append(candidate)
        deleted += int(removed)
        changed.extend([candidate.key] * int(not removed))
    return OrphanDeletionResult(
        confirmed=tuple(confirmed),
        changed=tuple(changed),
        failed=tuple(failed),
        deleted=deleted,
        incomplete=incomplete,
    )
