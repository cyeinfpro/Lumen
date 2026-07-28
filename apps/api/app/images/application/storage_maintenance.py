from __future__ import annotations

import asyncio
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, ImageVariant


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
    path: Path
    size_bytes: int
    device: int
    inode: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _SweepCursor:
    anchor: str
    boundary: str


@dataclass
class _DiscoveryProgress:
    candidates: list[_FileCandidate]
    oversized: list[str]
    too_young: list[str]
    bytes_scanned: int = 0
    first_consumed_key: str | None = None
    last_consumed_key: str | None = None

    def consume(self, key: str) -> None:
        if self.first_consumed_key is None:
            self.first_consumed_key = key
        self.last_consumed_key = key


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


def _is_image_file_storage_key(key: str) -> bool:
    parts = PurePosixPath(key).parts
    if any(part in {".", ".."} for part in parts):
        return False
    if len(parts) < 4 or parts[0] != "u" or not parts[1]:
        return False
    if parts[2] == "uploads":
        return len(parts) == 4 and bool(parts[3])
    if parts[2] == "g":
        return len(parts) == 5 and bool(parts[3]) and bool(parts[4])
    return False


def _iter_leaf_files(
    directory: Path,
    *,
    root: Path,
    account_entry: Callable[[], None],
) -> Iterator[tuple[str, Path, os.stat_result]]:
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return
    with entries:
        for entry in entries:
            account_entry()
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            path = Path(entry.path)
            key = path.relative_to(root).as_posix()
            if _is_image_file_storage_key(key):
                yield key, path, info


def _iter_image_files(
    root: Path,
    *,
    account_entry: Callable[[], None],
    skip_directory: Path | None = None,
) -> Iterator[tuple[str, Path, os.stat_result]]:
    users_root = root / "u"
    try:
        users = os.scandir(users_root)
    except FileNotFoundError:
        return
    with users:
        for user in users:
            account_entry()
            try:
                if not user.is_dir(follow_symlinks=False):
                    continue
            except FileNotFoundError:
                continue
            user_path = Path(user.path)
            uploads_path = user_path / "uploads"
            if uploads_path != skip_directory:
                yield from _iter_leaf_files(
                    uploads_path,
                    root=root,
                    account_entry=account_entry,
                )
            generated_root = user_path / "g"
            try:
                generations = os.scandir(generated_root)
            except FileNotFoundError:
                continue
            with generations:
                for generation in generations:
                    account_entry()
                    try:
                        if not generation.is_dir(follow_symlinks=False):
                            continue
                    except FileNotFoundError:
                        continue
                    generation_path = Path(generation.path)
                    if generation_path == skip_directory:
                        continue
                    yield from _iter_leaf_files(
                        generation_path,
                        root=root,
                        account_entry=account_entry,
                    )


def _decode_cursor(cursor: str | None) -> _SweepCursor | None:
    if cursor is None:
        return None
    if not cursor.startswith("v1:"):
        if _is_image_file_storage_key(cursor):
            return _SweepCursor(anchor=cursor, boundary=cursor)
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
        _is_image_file_storage_key(anchor) and _is_image_file_storage_key(boundary)
    ):
        return None
    return _SweepCursor(anchor=anchor, boundary=boundary)


def _encode_cursor(cursor: _SweepCursor) -> str:
    if cursor.anchor == cursor.boundary:
        return cursor.anchor
    return f"v1:{len(cursor.boundary)}:{cursor.boundary}{cursor.anchor}"


def _cursor_directory(root: Path, key: str) -> Path:
    return root.joinpath(*PurePosixPath(key).parent.parts)


def _iter_image_files_circular(
    root: Path,
    *,
    anchor: str | None,
    account_entry: Callable[[], None],
) -> Iterator[tuple[str, Path, os.stat_result]]:
    if anchor is None:
        yield from _iter_image_files(root, account_entry=account_entry)
        return

    anchor_directory = _cursor_directory(root, anchor)
    before_anchor: list[tuple[str, Path, os.stat_result]] = []
    anchor_found = False
    for item in _iter_leaf_files(
        anchor_directory,
        root=root,
        account_entry=account_entry,
    ):
        if anchor_found:
            yield item
        elif item[0] == anchor:
            anchor_found = True
        else:
            before_anchor.append(item)

    if not anchor_found:
        yield from before_anchor
    yield from _iter_image_files(
        root,
        account_entry=account_entry,
        skip_directory=anchor_directory,
    )
    if anchor_found:
        yield from before_anchor


def _record_discovered_file(
    progress: _DiscoveryProgress,
    *,
    key: str,
    path: Path,
    info: os.stat_result,
    budget: OrphanSweepBudget,
    minimum_modified_at: float | None,
) -> None:
    size_bytes = max(0, int(info.st_size))
    if minimum_modified_at is not None and float(info.st_mtime) > minimum_modified_at:
        if len(progress.too_young) < budget.max_files:
            progress.too_young.append(key)
        progress.consume(key)
        return
    if size_bytes > budget.max_bytes:
        if len(progress.oversized) < budget.max_files:
            progress.oversized.append(key)
        progress.consume(key)
        return
    if progress.bytes_scanned + size_bytes > budget.max_bytes:
        raise _StopDiscovery
    progress.candidates.append(
        _FileCandidate(
            key=key,
            path=path,
            size_bytes=size_bytes,
            device=info.st_dev,
            inode=info.st_ino,
            modified_ns=info.st_mtime_ns,
            changed_ns=info.st_ctime_ns,
        )
    )
    progress.consume(key)
    progress.bytes_scanned += size_bytes
    if len(progress.candidates) >= budget.max_files:
        raise _StopDiscovery


def _next_discovery_cursor(
    progress: _DiscoveryProgress,
    *,
    cursor_state: _SweepCursor | None,
    budget_exhausted: bool,
) -> str | None:
    if not budget_exhausted:
        return None
    boundary = (
        cursor_state.boundary
        if cursor_state is not None
        else progress.first_consumed_key
    )
    anchor = (
        progress.last_consumed_key
        if progress.last_consumed_key is not None
        else (None if cursor_state is None else cursor_state.anchor)
    )
    if boundary is None or anchor is None:
        return None
    return _encode_cursor(_SweepCursor(anchor=anchor, boundary=boundary))


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
        if monotonic() - started >= budget.max_seconds:
            raise _StopDiscovery
        entries_scanned += 1

    try:
        for key, path, info in _iter_image_files_circular(
            root,
            anchor=None if cursor_state is None else cursor_state.anchor,
            account_entry=account_entry,
        ):
            if monotonic() - started >= budget.max_seconds:
                raise _StopDiscovery
            if cursor_state is not None and key == cursor_state.boundary:
                break
            _record_discovered_file(
                progress,
                key=key,
                path=path,
                info=info,
                budget=budget,
                minimum_modified_at=minimum_modified_at,
            )
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


def _metadata_storage_keys(metadata: Any) -> Iterator[str]:
    if not isinstance(metadata, dict):
        return
    normalized_ref = metadata.get("normalized_ref")
    if isinstance(normalized_ref, dict):
        storage_key = normalized_ref.get("storage_key")
        if isinstance(storage_key, str) and storage_key:
            yield storage_key


def _possible_metadata_image_ids(keys: set[str]) -> set[str]:
    image_ids: set[str] = set()
    suffix = ".ref.webp"
    for key in keys:
        filename = PurePosixPath(key).name
        if filename.endswith(suffix):
            image_id = filename[: -len(suffix)]
            if image_id:
                image_ids.add(image_id)
    return image_ids


def _storage_key_sort_key(key: str) -> tuple[str, int, str]:
    parts = PurePosixPath(key).parts
    user = parts[1] if len(parts) > 1 else ""
    category_rank = 0 if len(parts) > 2 and parts[2] == "uploads" else 1
    return user, category_rank, key


async def _known_storage_keys(
    db: AsyncSession,
    candidates: set[str],
) -> set[str]:
    if not candidates:
        return set()
    image_ids = _possible_metadata_image_ids(candidates)
    image_conditions = [Image.storage_key.in_(candidates)]
    if image_ids:
        image_conditions.append(Image.id.in_(image_ids))
    image_rows = (
        await db.execute(
            select(Image.storage_key, Image.metadata_jsonb).where(
                or_(*image_conditions)
            )
        )
    ).all()
    known: set[str] = set()
    for storage_key, metadata in image_rows:
        if isinstance(storage_key, str) and storage_key in candidates:
            known.add(storage_key)
        known.update(
            key for key in _metadata_storage_keys(metadata) if key in candidates
        )
    variant_keys = (
        (
            await db.execute(
                select(ImageVariant.storage_key).where(
                    ImageVariant.storage_key.in_(candidates)
                )
            )
        )
        .scalars()
        .all()
    )
    known.update(
        key for key in variant_keys if isinstance(key, str) and key in candidates
    )
    return known


def _unlink_if_unchanged(candidate: _FileCandidate) -> bool:
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
    remaining = max_seconds - (monotonic() - started)
    if candidate_keys and remaining <= 0:
        known_keys: set[str] = set()
        database_timed_out = True
    else:
        try:
            known_keys = await asyncio.wait_for(
                _known_storage_keys(db, candidate_keys),
                timeout=max(remaining, 0.001),
            )
            database_timed_out = False
        except TimeoutError:
            known_keys = set()
            database_timed_out = True

    possible_orphans = [
        candidate
        for candidate in discovery.candidates
        if candidate.key not in known_keys
    ]
    orphan_candidates = list(possible_orphans)
    changed: list[str] = []
    deleted = 0
    deletion_incomplete = False
    if not dry_run and not database_timed_out:
        confirmed: list[_FileCandidate] = []
        for candidate in possible_orphans:
            remaining = max_seconds - (monotonic() - started)
            if remaining <= 0:
                deletion_incomplete = True
                break
            try:
                now_known = await asyncio.wait_for(
                    _known_storage_keys(db, {candidate.key}),
                    timeout=remaining,
                )
                if candidate.key in now_known:
                    continue
                remaining = max_seconds - (monotonic() - started)
                if remaining <= 0:
                    deletion_incomplete = True
                    break
                removed = await asyncio.wait_for(
                    asyncio.to_thread(_unlink_if_unchanged, candidate),
                    timeout=remaining,
                )
            except TimeoutError:
                deletion_incomplete = True
                break
            confirmed.append(candidate)
            if removed:
                deleted += 1
            else:
                changed.append(candidate.key)
        orphan_candidates = confirmed

    elapsed = monotonic() - started
    exhausted = (
        discovery.budget_exhausted
        or database_timed_out
        or deletion_incomplete
        or elapsed >= max_seconds
        or (not dry_run and deleted + len(changed) < len(orphan_candidates))
    )
    next_cursor = discovery.next_cursor
    if database_timed_out:
        next_cursor = cursor
    elif not dry_run and deletion_incomplete:
        next_cursor = cursor
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
        "deleted": deleted,
        "elapsed_ms": max(0, int(elapsed * 1000)),
    }
