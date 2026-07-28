"""Bounded filesystem traversal primitives for retention cleanup."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TraversalBudget:
    remaining_entries: int
    deadline: float
    monotonic: Callable[[], float]
    exhausted: bool = False

    def available(self) -> bool:
        if self.remaining_entries <= 0 or self.monotonic() >= self.deadline:
            self.exhausted = True
            return False
        return True

    def consume(self) -> bool:
        if not self.available():
            return False
        self.remaining_entries -= 1
        return True


def new_traversal_budget(
    max_entries: int,
    *,
    time_budget_s: float,
    monotonic: Callable[[], float],
) -> TraversalBudget:
    started = monotonic()
    return TraversalBudget(
        remaining_entries=max_entries,
        deadline=started + time_budget_s,
        monotonic=monotonic,
    )


def _entry_info(entry: os.DirEntry[str]) -> os.stat_result | None:
    try:
        return entry.stat(follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return None


def _unlink_entry(
    entry: os.DirEntry[str],
    info: os.stat_result,
    cutoff_ts: float | None,
) -> tuple[int, int, bool]:
    if cutoff_ts is not None and info.st_mtime >= cutoff_ts:
        return 0, 0, True
    try:
        os.unlink(entry.path)
    except FileNotFoundError:
        return 0, 0, True
    except OSError:
        return 0, 0, False
    if stat.S_ISREG(info.st_mode):
        return 1, info.st_size, True
    return 0, 0, True


def _remove_empty_directory(directory: Path) -> bool:
    try:
        directory.rmdir()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def sweep_tree_bounded(
    directory: Path,
    budget: TraversalBudget,
    *,
    cutoff_ts: float | None,
    remove_directory: bool,
) -> tuple[int, int, bool]:
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return 0, 0, True
    except OSError:
        return 0, 0, False

    removed_files = 0
    removed_bytes = 0
    complete = True
    with entries:
        for entry in entries:
            if not budget.consume():
                return removed_files, removed_bytes, False
            info = _entry_info(entry)
            if info is None:
                complete = False
                continue

            if stat.S_ISDIR(info.st_mode):
                child_files, child_bytes, child_complete = sweep_tree_bounded(
                    Path(entry.path),
                    budget,
                    cutoff_ts=cutoff_ts,
                    remove_directory=True,
                )
                removed_files += child_files
                removed_bytes += child_bytes
                complete = complete and child_complete
                if budget.exhausted:
                    return removed_files, removed_bytes, False
                continue

            files, freed, entry_complete = _unlink_entry(
                entry,
                info,
                cutoff_ts,
            )
            removed_files += files
            removed_bytes += freed
            complete = complete and entry_complete

    if remove_directory and complete:
        complete = _remove_empty_directory(directory)
    return removed_files, removed_bytes, complete


def iter_child_dirs(
    directory: Path,
    budget: TraversalBudget,
) -> Iterator[Path]:
    try:
        entries = os.scandir(directory)
    except OSError:
        return
    with entries:
        for entry in entries:
            if not budget.consume():
                return
            try:
                if entry.is_dir(follow_symlinks=False):
                    yield Path(entry.path)
            except OSError:
                continue
