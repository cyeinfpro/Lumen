from __future__ import annotations

import asyncio
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Iterator

from sqlalchemy.ext.asyncio import AsyncSession

from .deleted_media_references import known_live_media_storage_keys
from .orphan_storage_deletion import delete_orphan_candidates
from .storage_discovery_parts import (
    is_attempt_segment,
    is_completion_attempt_segment,
    is_image_file_storage_key,
    is_safe_directory_path,
    is_safe_storage_segment,
    is_storage_leaf_directory_key,
)


@dataclass(frozen=True)
class OrphanSweepBudget:
    max_files: int = 500
    max_entries: int = 5_000
    max_bytes: int = 10 * 1024 * 1024 * 1024
    max_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_files < 1:
            raise ValueError("orphan sweep max_files must be positive")
        if self.max_entries < self.max_files:
            raise ValueError("orphan sweep max_entries must cover max_files")
        if self.max_bytes < 1:
            raise ValueError("orphan sweep max_bytes must be positive")
        if self.max_seconds <= 0:
            raise ValueError("orphan sweep max_seconds must be positive")


@dataclass(frozen=True)
class _FileCandidate:
    key: str
    root: Path
    path: Path
    size_bytes: int
    device: int
    inode: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _SweepCursor:
    directory: str
    entry: str | None


@dataclass
class _DiscoveryProgress:
    candidates: list[_FileCandidate]
    oversized: list[str]
    too_young: list[str]
    bytes_scanned: int = 0
    last_cursor: _SweepCursor | None = None

    def advance(self, directory: str, entry: str | None) -> None:
        self.last_cursor = _SweepCursor(directory=directory, entry=entry)


@dataclass(frozen=True)
class _Discovery:
    candidates: tuple[_FileCandidate, ...]
    entries_scanned: int
    bytes_scanned: int
    next_cursor: str | None
    budget_exhausted: bool
    oversized: tuple[str, ...]
    too_young: tuple[str, ...]


class _StopDiscovery(Exception):
    pass


_STORAGE_LEAF_CATEGORIES = (
    "uploads",
    "g",
    "completion-tools",
    "v",
    "vref",
    "storyboards",
)


def _iter_directory_entries(
    directory: Path,
    *,
    root: Path,
    account_entry: Callable[[], None] | None,
) -> Iterator[tuple[os.DirEntry[str], os.stat_result]]:
    if not is_safe_directory_path(root, directory):
        return
    try:
        entries = os.scandir(directory)
    except OSError:
        return
    collected: list[tuple[os.DirEntry[str], os.stat_result]] = []
    with entries:
        for entry in entries:
            if account_entry is not None:
                account_entry()
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            collected.append((entry, info))
    yield from sorted(collected, key=lambda item: item[0].name)


def _file_entry(
    entry: os.DirEntry[str],
    info: os.stat_result,
    *,
    directory: Path,
    root: Path,
) -> tuple[str, Path, os.stat_result] | None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    path = directory / entry.name
    try:
        key = path.relative_to(root).as_posix()
    except ValueError:
        return None
    if not is_image_file_storage_key(key):
        return None
    return key, path, info


def _iter_leaf_files(
    directory: Path,
    *,
    root: Path,
    account_entry: Callable[[], None],
) -> Iterator[tuple[str, Path, os.stat_result]]:
    for entry, info in _iter_directory_entries(
        directory,
        root=root,
        account_entry=account_entry,
    ):
        item = _file_entry(
            entry,
            info,
            directory=directory,
            root=root,
        )
        if item is not None:
            yield item


def _iter_child_directories(
    directory: Path,
    *,
    root: Path,
    account_entry: Callable[[], None],
    allowed_name: Callable[[str], bool] = is_safe_storage_segment,
    minimum_name: str | None = None,
) -> Iterator[Path]:
    for entry, info in _iter_directory_entries(
        directory,
        root=root,
        account_entry=account_entry,
    ):
        if minimum_name is not None and entry.name < minimum_name:
            continue
        if stat.S_ISDIR(info.st_mode) and allowed_name(entry.name):
            yield directory / entry.name


def _named_child_directory(
    directory: Path,
    name: str,
    *,
    root: Path,
    account_entry: Callable[[], None],
) -> Path | None:
    for child in _iter_child_directories(
        directory,
        root=root,
        account_entry=account_entry,
    ):
        if child.name == name:
            return child
    return None


def _iter_generation_leaf_directories(
    user_path: Path,
    *,
    root: Path,
    account_entry: Callable[[], None],
    minimum_name: str | None = None,
) -> Iterator[Path]:
    for generation_path in _iter_child_directories(
        user_path / "g",
        root=root,
        account_entry=account_entry,
        minimum_name=minimum_name,
    ):
        yield generation_path
        yield from _iter_child_directories(
            generation_path / "attempts",
            root=root,
            account_entry=account_entry,
            allowed_name=is_attempt_segment,
        )
        for execution_path in _iter_child_directories(
            generation_path / "executions",
            root=root,
            account_entry=account_entry,
            allowed_name=is_attempt_segment,
        ):
            yield from _iter_child_directories(
                execution_path / "attempts",
                root=root,
                account_entry=account_entry,
                allowed_name=is_attempt_segment,
            )


def _iter_completion_leaf_directories(
    user_path: Path,
    *,
    root: Path,
    account_entry: Callable[[], None],
    minimum_name: str | None = None,
) -> Iterator[Path]:
    for task_path in _iter_child_directories(
        user_path / "completion-tools",
        root=root,
        account_entry=account_entry,
        minimum_name=minimum_name,
    ):
        attempts_path = _named_child_directory(
            task_path,
            "attempts",
            root=root,
            account_entry=account_entry,
        )
        if attempts_path is not None:
            for attempt_path in _iter_child_directories(
                attempts_path,
                root=root,
                account_entry=account_entry,
                allowed_name=is_completion_attempt_segment,
            ):
                yield from _iter_child_directories(
                    attempt_path,
                    root=root,
                    account_entry=account_entry,
                )
        for execution_path in _iter_child_directories(
            task_path / "executions",
            root=root,
            account_entry=account_entry,
            allowed_name=is_attempt_segment,
        ):
            execution_attempts_path = _named_child_directory(
                execution_path,
                "attempts",
                root=root,
                account_entry=account_entry,
            )
            if execution_attempts_path is None:
                continue
            for attempt_path in _iter_child_directories(
                execution_attempts_path,
                root=root,
                account_entry=account_entry,
                allowed_name=is_attempt_segment,
            ):
                yield from _iter_child_directories(
                    attempt_path,
                    root=root,
                    account_entry=account_entry,
                )


def _iter_video_leaf_directories(
    user_path: Path,
    *,
    root: Path,
    account_entry: Callable[[], None],
    minimum_name: str | None = None,
) -> Iterator[Path]:
    for generation_path in _iter_child_directories(
        user_path / "v",
        root=root,
        account_entry=account_entry,
        minimum_name=minimum_name,
    ):
        yield generation_path
        final_path = _named_child_directory(
            generation_path,
            "final",
            root=root,
            account_entry=account_entry,
        )
        if final_path is None:
            continue
        yield from _iter_child_directories(
            final_path,
            root=root,
            account_entry=account_entry,
        )


def _iter_reference_video_leaf_directories(
    user_path: Path,
    *,
    root: Path,
    account_entry: Callable[[], None],
    minimum_name: str | None = None,
) -> Iterator[Path]:
    yield from _iter_child_directories(
        user_path / "vref",
        root=root,
        account_entry=account_entry,
        minimum_name=minimum_name,
    )


def _iter_storyboard_leaf_directories(
    user_path: Path,
    *,
    root: Path,
    account_entry: Callable[[], None],
    minimum_name: str | None = None,
) -> Iterator[Path]:
    for run_path in _iter_child_directories(
        user_path / "storyboards",
        root=root,
        account_entry=account_entry,
        minimum_name=minimum_name,
    ):
        assembly_path = _named_child_directory(
            run_path,
            "assembly",
            root=root,
            account_entry=account_entry,
        )
        if assembly_path is None:
            continue
        yield from _iter_child_directories(
            assembly_path,
            root=root,
            account_entry=account_entry,
        )


def _iter_user_category_leaf_directories(
    user_path: Path,
    category: str,
    *,
    root: Path,
    account_entry: Callable[[], None],
    minimum_name: str | None,
) -> Iterator[Path]:
    if category == "uploads":
        uploads_path = user_path / "uploads"
        if is_safe_directory_path(root, uploads_path):
            yield uploads_path
        return
    if category == "g":
        yield from _iter_generation_leaf_directories(
            user_path,
            root=root,
            account_entry=account_entry,
            minimum_name=minimum_name,
        )
        return
    if category == "completion-tools":
        yield from _iter_completion_leaf_directories(
            user_path,
            root=root,
            account_entry=account_entry,
            minimum_name=minimum_name,
        )
        return
    if category == "v":
        yield from _iter_video_leaf_directories(
            user_path,
            root=root,
            account_entry=account_entry,
            minimum_name=minimum_name,
        )
        return
    if category == "vref":
        yield from _iter_reference_video_leaf_directories(
            user_path,
            root=root,
            account_entry=account_entry,
            minimum_name=minimum_name,
        )
        return
    yield from _iter_storyboard_leaf_directories(
        user_path,
        root=root,
        account_entry=account_entry,
        minimum_name=minimum_name,
    )


def _iter_image_leaf_directories(
    root: Path,
    *,
    account_entry: Callable[[], None],
    cursor_state: _SweepCursor | None = None,
) -> Iterator[Path]:
    cursor_parts = (
        PurePosixPath(cursor_state.directory).parts if cursor_state is not None else ()
    )
    cursor_user = cursor_parts[1] if len(cursor_parts) > 1 else None
    cursor_category = cursor_parts[2] if len(cursor_parts) > 2 else None
    cursor_primary = cursor_parts[3] if len(cursor_parts) > 3 else None
    cursor_category_rank = (
        _STORAGE_LEAF_CATEGORIES.index(cursor_category)
        if cursor_category in _STORAGE_LEAF_CATEGORIES
        else 0
    )
    users_root = root / "u"
    for user_path in _iter_child_directories(
        users_root,
        root=root,
        account_entry=account_entry,
        minimum_name=cursor_user,
    ):
        same_user = cursor_user is not None and user_path.name == cursor_user
        minimum_category_rank = cursor_category_rank if same_user else 0
        for category_rank, category in enumerate(_STORAGE_LEAF_CATEGORIES):
            if category_rank < minimum_category_rank:
                continue
            same_category = same_user and category == cursor_category
            minimum_name = cursor_primary if same_category else None
            yield from _iter_user_category_leaf_directories(
                user_path,
                category,
                root=root,
                account_entry=account_entry,
                minimum_name=minimum_name,
            )


def _iter_image_files(
    root: Path,
    *,
    account_entry: Callable[[], None],
    skip_directory: Path | None = None,
) -> Iterator[tuple[str, Path, os.stat_result]]:
    for directory in _iter_image_leaf_directories(
        root,
        account_entry=account_entry,
    ):
        if directory == skip_directory:
            continue
        yield from _iter_leaf_files(
            directory,
            root=root,
            account_entry=account_entry,
        )


def _decode_cursor(cursor: str | None) -> _SweepCursor | None:
    if cursor is None:
        return None
    if cursor.startswith("v2:"):
        length_text, separator, payload = cursor[3:].partition(":")
        if not separator or not length_text.isdigit():
            return None
        directory_length = int(length_text)
        if directory_length < 1 or directory_length > len(payload):
            return None
        directory = payload[:directory_length]
        entry = payload[directory_length:] or None
        if not is_storage_leaf_directory_key(directory):
            return None
        if entry is not None and not is_safe_storage_segment(entry):
            return None
        return _SweepCursor(directory=directory, entry=entry)
    if not cursor.startswith("v1:"):
        if is_image_file_storage_key(cursor):
            path = PurePosixPath(cursor)
            return _SweepCursor(directory=path.parent.as_posix(), entry=path.name)
        return None
    length_text, separator, payload = cursor[3:].partition(":")
    if not separator or not length_text.isdigit():
        return None
    boundary_length = int(length_text)
    if boundary_length < 1 or boundary_length >= len(payload):
        return None
    boundary = payload[:boundary_length]
    anchor = payload[boundary_length:]
    if not (
        is_image_file_storage_key(anchor) and is_image_file_storage_key(boundary)
    ):
        return None
    anchor_path = PurePosixPath(anchor)
    return _SweepCursor(
        directory=anchor_path.parent.as_posix(),
        entry=anchor_path.name,
    )


def _encode_cursor(cursor: _SweepCursor) -> str:
    entry = cursor.entry or ""
    return f"v2:{len(cursor.directory)}:{cursor.directory}{entry}"


def _record_discovered_file(
    progress: _DiscoveryProgress,
    *,
    root: Path,
    key: str,
    path: Path,
    info: os.stat_result,
    budget: OrphanSweepBudget,
    minimum_modified_at: float | None,
) -> bool:
    size_bytes = max(0, int(info.st_size))
    if minimum_modified_at is not None and float(info.st_mtime) > minimum_modified_at:
        if len(progress.too_young) < budget.max_files:
            progress.too_young.append(key)
        return False
    if size_bytes > budget.max_bytes:
        if len(progress.oversized) < budget.max_files:
            progress.oversized.append(key)
        return False
    if progress.bytes_scanned + size_bytes > budget.max_bytes:
        raise _StopDiscovery
    progress.candidates.append(
        _FileCandidate(
            key=key,
            root=root,
            path=path,
            size_bytes=size_bytes,
            device=info.st_dev,
            inode=info.st_ino,
            modified_ns=info.st_mtime_ns,
            changed_ns=info.st_ctime_ns,
        )
    )
    progress.bytes_scanned += size_bytes
    return len(progress.candidates) >= budget.max_files


def _next_discovery_cursor(
    progress: _DiscoveryProgress,
    *,
    cursor_state: _SweepCursor | None,
    budget_exhausted: bool,
) -> str | None:
    if not budget_exhausted:
        return None
    next_state = progress.last_cursor or cursor_state
    if next_state is None:
        return None
    return _encode_cursor(next_state)


def _leaf_directory_sort_key(key: str) -> tuple[str, int, tuple[str, ...]]:
    parts = PurePosixPath(key).parts
    category_ranks = {
        "uploads": 0,
        "g": 1,
        "completion-tools": 2,
        "v": 3,
        "vref": 4,
        "storyboards": 5,
    }
    return (
        parts[1],
        category_ranks.get(parts[2], len(category_ranks)),
        tuple(parts[3:]),
    )


def _discover_candidates(
    root: Path,
    *,
    cursor: str | None,
    budget: OrphanSweepBudget,
    monotonic: Callable[[], float],
    minimum_modified_at: float | None,
) -> _Discovery:
    started = monotonic()
    entries_scanned = 0
    progress = _DiscoveryProgress(
        candidates=[],
        oversized=[],
        too_young=[],
    )
    cursor_state = _decode_cursor(cursor)
    budget_exhausted = False

    def account_entry() -> None:
        nonlocal entries_scanned
        if entries_scanned >= budget.max_entries:
            raise _StopDiscovery
        entries_scanned += 1

    def stop_if_time_exhausted() -> None:
        if monotonic() - started >= budget.max_seconds:
            raise _StopDiscovery

    try:
        cursor_sort_key = (
            None
            if cursor_state is None
            else _leaf_directory_sort_key(cursor_state.directory)
        )
        for directory in _iter_image_leaf_directories(
            root,
            account_entry=lambda: None,
            cursor_state=cursor_state,
        ):
            directory_key = directory.relative_to(root).as_posix()
            directory_sort_key = _leaf_directory_sort_key(directory_key)
            if cursor_sort_key is not None and directory_sort_key < cursor_sort_key:
                continue
            if (
                cursor_state is not None
                and directory_key == cursor_state.directory
                and cursor_state.entry is None
            ):
                continue
            for entry, info in _iter_directory_entries(
                directory,
                root=root,
                account_entry=None,
            ):
                if (
                    cursor_state is not None
                    and directory_key == cursor_state.directory
                    and cursor_state.entry is not None
                    and entry.name <= cursor_state.entry
                ):
                    continue
                account_entry()
                item = _file_entry(
                    entry,
                    info,
                    directory=directory,
                    root=root,
                )
                if item is None:
                    progress.advance(directory_key, entry.name)
                    stop_if_time_exhausted()
                    continue
                key, path, file_info = item
                stop_after_file = _record_discovered_file(
                    progress,
                    root=root,
                    key=key,
                    path=path,
                    info=file_info,
                    budget=budget,
                    minimum_modified_at=minimum_modified_at,
                )
                progress.advance(directory_key, entry.name)
                if stop_after_file:
                    raise _StopDiscovery
                stop_if_time_exhausted()
            account_entry()
            progress.advance(directory_key, None)
            stop_if_time_exhausted()
    except _StopDiscovery:
        budget_exhausted = True

    return _Discovery(
        candidates=tuple(sorted(progress.candidates, key=lambda item: item.key)),
        entries_scanned=entries_scanned,
        bytes_scanned=progress.bytes_scanned,
        next_cursor=_next_discovery_cursor(
            progress,
            cursor_state=cursor_state,
            budget_exhausted=budget_exhausted,
        ),
        budget_exhausted=budget_exhausted,
        oversized=tuple(sorted(progress.oversized)),
        too_young=tuple(sorted(progress.too_young)),
    )


def _storage_key_sort_key(key: str) -> tuple[str, int, str]:
    parts = PurePosixPath(key).parts
    user = parts[1] if len(parts) > 1 else ""
    category_rank = 0 if len(parts) > 2 and parts[2] == "uploads" else 1
    return user, category_rank, key


async def _known_storage_keys(
    db: AsyncSession,
    candidates: set[str],
) -> set[str]:
    return await known_live_media_storage_keys(db, candidates)


def _remaining_seconds(
    *,
    max_seconds: float,
    started: float,
    monotonic: Callable[[], float],
) -> float:
    return max_seconds - (monotonic() - started)


async def _load_known_storage_keys(
    db: AsyncSession,
    candidate_keys: set[str],
    *,
    max_seconds: float,
    started: float,
    monotonic: Callable[[], float],
    assert_owned: Callable[[], Awaitable[None]] | None,
) -> tuple[set[str], bool]:
    if not candidate_keys:
        return set(), False
    remaining = _remaining_seconds(
        max_seconds=max_seconds,
        started=started,
        monotonic=monotonic,
    )
    if remaining <= 0:
        return set(), True
    try:
        if assert_owned is not None:
            await assert_owned()
            remaining = _remaining_seconds(
                max_seconds=max_seconds,
                started=started,
                monotonic=monotonic,
            )
            if remaining <= 0:
                return set(), True
        known_keys = await asyncio.wait_for(
            _known_storage_keys(db, candidate_keys),
            timeout=remaining,
        )
    except TimeoutError:
        return set(), True
    return known_keys, False


def _unlink_if_unchanged(candidate: _FileCandidate) -> bool:
    expected_path = candidate.root.joinpath(*PurePosixPath(candidate.key).parts)
    if candidate.path != expected_path or not is_safe_directory_path(
        candidate.root,
        candidate.path.parent,
    ):
        return False
    try:
        info = candidate.path.lstat()
    except FileNotFoundError:
        return False
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size != candidate.size_bytes
        or info.st_dev != candidate.device
        or info.st_ino != candidate.inode
        or info.st_mtime_ns != candidate.modified_ns
        or info.st_ctime_ns != candidate.changed_ns
    ):
        return False
    candidate.path.unlink()
    return True


async def sweep_orphan_image_files(
    db: AsyncSession,
    *,
    storage_root: str,
    dry_run: bool = True,
    cursor: str | None = None,
    max_files: int = 500,
    max_entries: int = 5_000,
    max_bytes: int = 10 * 1024 * 1024 * 1024,
    max_seconds: float = 2.0,
    minimum_age_seconds: float = 3600.0,
    monotonic: Callable[[], float] = time.monotonic,
    wall_time: Callable[[], float] = time.time,
    assert_owned: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    root = Path(storage_root).resolve()
    budget = OrphanSweepBudget(
        max_files=max_files,
        max_entries=max_entries,
        max_bytes=max_bytes,
        max_seconds=max_seconds,
    )
    started = monotonic()
    if not root.exists():
        return {
            "dry_run": dry_run,
            "storage_root": str(root),
            "cursor": cursor,
            "next_cursor": None,
            "budget_exhausted": False,
            "entries_scanned": 0,
            "scanned": 0,
            "bytes_scanned": 0,
            "orphans": [],
            "oversized": [],
            "too_young": [],
            "changed": [],
            "failed": [],
            "deleted": 0,
            "elapsed_ms": 0,
        }

    discovery = await asyncio.to_thread(
        _discover_candidates,
        root,
        cursor=cursor,
        budget=budget,
        monotonic=monotonic,
        minimum_modified_at=(
            None if dry_run else wall_time() - max(0.0, minimum_age_seconds)
        ),
    )
    candidate_keys = {candidate.key for candidate in discovery.candidates}
    known_keys, database_timed_out = await _load_known_storage_keys(
        db,
        candidate_keys,
        max_seconds=max_seconds,
        started=started,
        monotonic=monotonic,
        assert_owned=assert_owned,
    )

    possible_orphans = [
        candidate
        for candidate in discovery.candidates
        if candidate.key not in known_keys
    ]
    orphan_candidates = list(possible_orphans)
    changed: list[str] = []
    failed: list[str] = []
    deleted = 0
    deletion_incomplete = False
    if not dry_run and not database_timed_out:
        deletion = await delete_orphan_candidates(
            db,
            possible_orphans,
            max_seconds=max_seconds,
            started=started,
            monotonic=monotonic,
            assert_owned=assert_owned,
            known_storage_keys=_known_storage_keys,
            unlink_if_unchanged=_unlink_if_unchanged,
        )
        orphan_candidates = list(deletion.confirmed)
        changed = list(deletion.changed)
        failed = list(deletion.failed)
        deleted = deletion.deleted
        deletion_incomplete = deletion.incomplete

    elapsed = monotonic() - started
    exhausted = (
        discovery.budget_exhausted
        or database_timed_out
        or deletion_incomplete
        or elapsed >= max_seconds
        or (not dry_run and deleted + len(changed) < len(orphan_candidates))
    )
    next_cursor = discovery.next_cursor
    return {
        "dry_run": dry_run,
        "storage_root": str(root),
        "cursor": cursor,
        "next_cursor": next_cursor,
        "budget_exhausted": exhausted,
        "database_timed_out": database_timed_out,
        "entries_scanned": discovery.entries_scanned,
        "scanned": len(discovery.candidates),
        "bytes_scanned": discovery.bytes_scanned,
        "orphans": sorted(
            (candidate.key for candidate in orphan_candidates),
            key=_storage_key_sort_key,
        ),
        "oversized": list(discovery.oversized),
        "too_young": list(discovery.too_young),
        "changed": changed,
        "failed": failed,
        "deleted": deleted,
        "elapsed_ms": max(0, int(elapsed * 1000)),
    }
