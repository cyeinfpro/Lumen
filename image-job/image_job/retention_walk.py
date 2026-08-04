"""Bounded descriptor-relative filesystem traversal for retention cleanup."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .durable_files import (
    fsync_directory_fd,
    rename_entry_noreplace,
    rename_noreplace_available,
)
from .retention_dir_reader import (
    DIRECTORY_PAGE_BYTES,
    MAX_DIRECTORY_PAGE_BYTES,
    DirectoryPage,
    directory_offsets_available,
    read_directory_page,
)
from .retention_budget import (
    TraversalBudget as TraversalBudget,
    new_traversal_budget as new_traversal_budget,
)
from .retention_guards import DirectoryPathGuard
from .retention_scan_registry import DirectoryScanRegistry


_QUARANTINE_PREFIX = ".retention-quarantine-"
_PRESERVED_PREFIX = "retention-preserved-"
_QUARANTINE_ENTRY = "entry"
_DEFAULT_SCAN_PAGE_SIZE = 64
_MAX_SCAN_PAGE_SIZE = 1_024


@dataclass(frozen=True)
class EntrySnapshot:
    name: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, name: str, info: os.stat_result) -> EntrySnapshot:
        return cls(
            name=name,
            device=info.st_dev,
            inode=info.st_ino,
            mode=info.st_mode,
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            ctime_ns=info.st_ctime_ns,
        )

    def matches(
        self,
        info: os.stat_result,
        *,
        include_metadata: bool,
    ) -> bool:
        if (
            self.device != info.st_dev
            or self.inode != info.st_ino
            or stat.S_IFMT(self.mode) != stat.S_IFMT(info.st_mode)
        ):
            return False
        return not include_metadata or (
            self.mode == info.st_mode
            and self.size == info.st_size
            and self.mtime_ns == info.st_mtime_ns
            and self.ctime_ns == info.st_ctime_ns
        )

    def matches_detached(self, info: os.stat_result) -> bool:
        return (
            self.matches(info, include_metadata=False)
            and self.mode == info.st_mode
            and self.size == info.st_size
            and self.mtime_ns == info.st_mtime_ns
        )

    @property
    def is_directory(self) -> bool:
        return stat.S_ISDIR(self.mode)


@dataclass
class DirectoryHandle:
    fd: int
    path: Path
    device: int
    inode: int
    mode: int

    @classmethod
    def from_fd(cls, fd: int, path: Path) -> DirectoryHandle:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(path)
        return cls(
            fd=fd,
            path=path,
            device=info.st_dev,
            inode=info.st_ino,
            mode=info.st_mode,
        )

    def matches(self, info: os.stat_result) -> bool:
        return (
            self.device == info.st_dev
            and self.inode == info.st_ino
            and stat.S_IFMT(self.mode) == stat.S_IFMT(info.st_mode)
        )

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self) -> DirectoryHandle:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        self.close()


@dataclass(frozen=True)
class DirectoryScanStep:
    entry: EntrySnapshot | None = None
    reached_end: bool = False
    cycle_complete: bool = False
    exhausted: bool = False


@dataclass
class DirectoryScanCursor:
    page_size: int = _DEFAULT_SCAN_PAGE_SIZE
    _device: int | None = None
    _inode: int | None = None
    _pending_names: deque[str] = field(default_factory=deque)
    _scan_offset: int = 0
    _page_reached_end: bool = False
    _cycle_failed: bool = False
    _needs_eof_probe: bool = False
    _short_page_changed: bool = False

    def __post_init__(self) -> None:
        if self.page_size <= 0 or self.page_size > _MAX_SCAN_PAGE_SIZE:
            raise ValueError(
                f"page_size must be between 1 and {_MAX_SCAN_PAGE_SIZE}"
            )

    def _same_directory(self, directory: DirectoryHandle) -> bool:
        return (
            self._device == directory.device
            and self._inode == directory.inode
        )

    def reset(self) -> None:
        self._device = None
        self._inode = None
        self._pending_names.clear()
        self._scan_offset = 0
        self._page_reached_end = False
        self._cycle_failed = False
        self._needs_eof_probe = False
        self._short_page_changed = False

    def close_pass(self) -> None:
        return None

    def mark_failed(self) -> None:
        self._cycle_failed = True

    def mark_directory_changed(self) -> None:
        if self._needs_eof_probe:
            self._short_page_changed = True
        elif not self._page_reached_end:
            self._cycle_failed = True

    def _finish_cycle(self) -> DirectoryScanStep:
        cycle_complete = not self._cycle_failed
        self._pending_names.clear()
        self._scan_offset = 0
        self._page_reached_end = False
        self._cycle_failed = False
        self._needs_eof_probe = False
        self._short_page_changed = False
        return DirectoryScanStep(
            reached_end=True,
            cycle_complete=cycle_complete,
        )

    def _page_bytes(self) -> int:
        return min(
            MAX_DIRECTORY_PAGE_BYTES,
            max(DIRECTORY_PAGE_BYTES, self.page_size * 64),
        )

    def _read_page(self, directory: DirectoryHandle) -> DirectoryPage:
        return _read_directory_page(
            directory.fd,
            device=directory.device,
            inode=directory.inode,
            offset=self._scan_offset,
            buffer_bytes=self._page_bytes(),
        )

    def _fill_page(
        self,
        directory: DirectoryHandle,
        budget: TraversalBudget,
    ) -> None:
        if not budget.consume():
            return
        try:
            page = self._read_page(directory)
        except OSError:
            self.mark_failed()
            self._page_reached_end = True
            return
        was_eof_probe = self._needs_eof_probe
        if was_eof_probe and not page.reached_end and self._short_page_changed:
            self.mark_failed()
        if was_eof_probe:
            self._needs_eof_probe = False
            self._short_page_changed = False
        self._pending_names.extend(os.fsdecode(name) for name in page.names)
        self._scan_offset = page.next_offset
        self._page_reached_end = page.reached_end
        if not page.reached_end and page.bytes_read < self._page_bytes():
            self._needs_eof_probe = True

    def next_entry(
        self,
        directory: DirectoryHandle,
        budget: TraversalBudget,
    ) -> DirectoryScanStep:
        if not self._same_directory(directory):
            self.reset()
            self._device = directory.device
            self._inode = directory.inode
        if not budget.available():
            return DirectoryScanStep(exhausted=True)
        if not self._pending_names and self._page_reached_end:
            return self._finish_cycle()
        if not self._pending_names:
            self._fill_page(directory, budget)
        if not budget.available():
            return DirectoryScanStep(exhausted=True)
        if not self._pending_names and self._page_reached_end:
            return self._finish_cycle()
        if not self._pending_names:
            return DirectoryScanStep()

        name = self._pending_names.popleft()
        budget.remaining_entries -= 1
        try:
            return DirectoryScanStep(
                entry=directory_entry_snapshot(directory, name)
            )
        except (FileNotFoundError, OSError, ValueError):
            self.mark_failed()
            return DirectoryScanStep()

    @property
    def buffered_entries(self) -> int:
        return len(self._pending_names)


def descriptor_relative_traversal_available() -> bool:
    supports_dir_fd = getattr(os, "supports_dir_fd", ())
    supports_fd = getattr(os, "supports_fd", ())
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in supports_dir_fd
        and os.stat in supports_dir_fd
        and os.unlink in supports_dir_fd
        and os.rmdir in supports_dir_fd
        and os.mkdir in supports_dir_fd
        and os.rename in supports_dir_fd
        and os.scandir in supports_fd
        and directory_offsets_available()
        and rename_noreplace_available()
    )


def _unsupported_error() -> OSError:
    return OSError(
        errno.ENOTSUP,
        "descriptor-relative retention traversal is unavailable",
    )


def _directory_open_flags() -> int:
    if not descriptor_relative_traversal_available():
        raise _unsupported_error()
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _validate_entry_name(name: str) -> None:
    separators = {os.sep}
    if os.altsep is not None:
        separators.add(os.altsep)
    if (
        not name
        or name in {".", ".."}
        or "\x00" in name
        or any(separator in name for separator in separators)
    ):
        raise ValueError(f"invalid directory entry name: {name!r}")


def _open_directory_at(name: str, flags: int, parent_fd: int) -> int:
    return os.open(name, flags, dir_fd=parent_fd)


def _rename_entry(
    source: str,
    target: str,
    source_fd: int,
    target_fd: int,
) -> None:
    os.rename(
        source,
        target,
        src_dir_fd=source_fd,
        dst_dir_fd=target_fd,
    )


def _rename_entry_noreplace(
    source: str,
    target: str,
    source_fd: int,
    target_fd: int,
) -> None:
    rename_entry_noreplace(
        source,
        target,
        source_fd,
        target_fd,
    )


def _scandir_directory(directory_fd: int) -> os.ScandirIterator[str]:
    return os.scandir(directory_fd)


def _read_directory_page(
    directory_fd: int,
    *,
    device: int,
    inode: int,
    offset: int,
    buffer_bytes: int,
) -> DirectoryPage:
    return read_directory_page(
        directory_fd,
        device=device,
        inode=inode,
        offset=offset,
        buffer_bytes=buffer_bytes,
    )


def _stale_entry_error(name: str) -> OSError:
    return OSError(errno.ESTALE, f"filesystem entry changed: {name}")


def open_directory(path: Path) -> DirectoryHandle:
    flags = _directory_open_flags()
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise NotADirectoryError(path)
    fd = os.open(path, flags)
    try:
        handle = DirectoryHandle.from_fd(fd, path)
        if not handle.matches(before):
            raise _stale_entry_error(str(path))
        after = os.stat(path, follow_symlinks=False)
        if not handle.matches(after):
            raise _stale_entry_error(str(path))
        return handle
    except BaseException:
        os.close(fd)
        raise


def directory_path_matches(guard: DirectoryPathGuard) -> bool:
    try:
        current = os.stat(guard.path, follow_symlinks=False)
    except FileNotFoundError:
        return guard.device is None
    except OSError:
        return False
    return guard.matches(current)


def directory_entry_snapshot(
    directory: DirectoryHandle,
    name: str,
) -> EntrySnapshot:
    _validate_entry_name(name)
    info = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    return EntrySnapshot.from_stat(name, info)


def verified_entry_absent(
    directory: DirectoryHandle,
    name: str,
) -> bool:
    guard = DirectoryPathGuard.from_handle(directory)
    if not directory_path_matches(guard):
        return False
    try:
        os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    except FileNotFoundError:
        return directory_path_matches(guard)
    except (OSError, ValueError):
        return False
    return False


def verified_relative_path_absent(
    guard: DirectoryPathGuard,
    relative_parts: tuple[str, ...],
) -> bool:
    if not relative_parts:
        return False
    if guard.device is None:
        return directory_path_matches(guard)
    if any(
        not part or part in {".", ".."} or "\x00" in part
        for part in relative_parts
    ):
        return False

    handles: list[DirectoryHandle] = []
    try:
        root = open_directory(guard.path)
        handles.append(root)
        current_info = os.fstat(root.fd)
        if not guard.matches(current_info) or not directory_path_matches(guard):
            return False
        for part in relative_parts[:-1]:
            parent = handles[-1]
            try:
                entry = directory_entry_snapshot(parent, part)
            except FileNotFoundError:
                return directory_path_matches(guard)
            if not entry.is_directory:
                return False
            handles.append(open_child_directory(parent, entry))
        try:
            directory_entry_snapshot(handles[-1], relative_parts[-1])
        except FileNotFoundError:
            return directory_path_matches(guard)
        except OSError:
            return False
        return False
    except OSError:
        return False
    finally:
        for handle in reversed(handles):
            handle.close()


def open_child_directory(
    parent: DirectoryHandle,
    expected: EntrySnapshot,
    *,
    include_metadata: bool = True,
) -> DirectoryHandle:
    _validate_entry_name(expected.name)
    before = os.stat(
        expected.name,
        dir_fd=parent.fd,
        follow_symlinks=False,
    )
    if not expected.matches(before, include_metadata=include_metadata):
        raise _stale_entry_error(expected.name)
    if not stat.S_ISDIR(before.st_mode):
        raise NotADirectoryError(expected.name)

    fd = _open_directory_at(
        expected.name,
        _directory_open_flags(),
        parent.fd,
    )
    try:
        path = parent.path / expected.name
        handle = DirectoryHandle.from_fd(fd, path)
        opened = os.fstat(fd)
        if (
            not handle.matches(before)
            or not expected.matches(
                opened,
                include_metadata=include_metadata,
            )
        ):
            raise _stale_entry_error(expected.name)
        after = os.stat(
            expected.name,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if not handle.matches(after):
            raise _stale_entry_error(expected.name)
        return handle
    except BaseException:
        os.close(fd)
        raise


def iter_directory_entries(
    directory: DirectoryHandle,
    budget: TraversalBudget,
    cursor: DirectoryScanCursor | None = None,
) -> Iterator[EntrySnapshot]:
    active_cursor = cursor or DirectoryScanCursor()
    owns_cursor = cursor is None
    try:
        while True:
            step = active_cursor.next_entry(directory, budget)
            if step.entry is not None:
                yield step.entry
                continue
            if step.reached_end or step.exhausted:
                return
    finally:
        if owns_cursor:
            active_cursor.reset()
        else:
            active_cursor.close_pass()


def fsync_open_directory(
    directory: DirectoryHandle,
    budget: TraversalBudget | None = None,
) -> bool:
    try:
        current = os.fstat(directory.fd)
        if not directory.matches(current):
            raise _stale_entry_error(str(directory.path))
        fsync_directory_fd(directory.fd)
    except OSError:
        if budget is not None:
            budget.durability_failed = True
        return False
    return True


def is_retention_quarantine_directory(entry: EntrySnapshot) -> bool:
    return entry.is_directory and entry.name.startswith(_QUARANTINE_PREFIX)


def is_retention_internal_directory(entry: EntrySnapshot) -> bool:
    return entry.is_directory and entry.name.startswith(
        (_QUARANTINE_PREFIX, _PRESERVED_PREFIX)
    )


def _create_quarantine_directory(
    directory: DirectoryHandle,
) -> tuple[str, DirectoryHandle]:
    for _attempt in range(8):
        name = f"{_QUARANTINE_PREFIX}{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=directory.fd)
        except FileExistsError:
            continue
        try:
            entry = directory_entry_snapshot(directory, name)
            return name, open_child_directory(directory, entry)
        except BaseException:
            try:
                os.rmdir(name, dir_fd=directory.fd)
                fsync_directory_fd(directory.fd)
            except OSError:
                pass
            raise
    raise FileExistsError("could not allocate retention quarantine")


def _persist_quarantine_state(
    directory: DirectoryHandle,
    quarantine: DirectoryHandle,
) -> None:
    fsync_directory_fd(quarantine.fd)
    fsync_directory_fd(directory.fd)


def _preserve_quarantine(
    directory: DirectoryHandle,
    quarantine_name: str,
    quarantine: DirectoryHandle,
) -> None:
    for _attempt in range(8):
        preserved_name = f"{_PRESERVED_PREFIX}{secrets.token_hex(16)}"
        try:
            _rename_entry_noreplace(
                quarantine_name,
                preserved_name,
                directory.fd,
                directory.fd,
            )
        except FileExistsError:
            continue
        except OSError:
            break
        fsync_directory_fd(directory.fd)
        return
    _persist_quarantine_state(directory, quarantine)


def _restore_quarantined_entry(
    directory: DirectoryHandle,
    quarantine_name: str,
    quarantine: DirectoryHandle,
    expected: EntrySnapshot,
) -> bool:
    try:
        detached = directory_entry_snapshot(
            quarantine,
            _QUARANTINE_ENTRY,
        )
        _rename_entry_noreplace(
            _QUARANTINE_ENTRY,
            expected.name,
            quarantine.fd,
            directory.fd,
        )
        restored = os.stat(
            expected.name,
            dir_fd=directory.fd,
            follow_symlinks=False,
        )
        if not detached.matches_detached(restored):
            fsync_directory_fd(directory.fd)
            return False
        os.rmdir(quarantine_name, dir_fd=directory.fd)
        fsync_directory_fd(directory.fd)
    except OSError:
        _preserve_quarantine(
            directory,
            quarantine_name,
            quarantine,
        )
        return False
    return True


def unlink_verified_entry(
    directory: DirectoryHandle,
    expected: EntrySnapshot,
) -> None:
    quarantine_name, quarantine = _create_quarantine_directory(directory)
    moved = False
    quarantine_removed = False
    try:
        _rename_entry(
            expected.name,
            _QUARANTINE_ENTRY,
            directory.fd,
            quarantine.fd,
        )
        moved = True
        detached_info = os.stat(
            _QUARANTINE_ENTRY,
            dir_fd=quarantine.fd,
            follow_symlinks=False,
        )
        if not expected.matches_detached(detached_info):
            quarantine_removed = _restore_quarantined_entry(
                directory,
                quarantine_name,
                quarantine,
                expected,
            )
            raise _stale_entry_error(expected.name)
        detached = EntrySnapshot.from_stat(
            _QUARANTINE_ENTRY,
            detached_info,
        )
        current = os.stat(
            _QUARANTINE_ENTRY,
            dir_fd=quarantine.fd,
            follow_symlinks=False,
        )
        if not detached.matches(current, include_metadata=True):
            raise _stale_entry_error(expected.name)
        os.unlink(_QUARANTINE_ENTRY, dir_fd=quarantine.fd)
        fsync_directory_fd(quarantine.fd)
        os.rmdir(quarantine_name, dir_fd=directory.fd)
        quarantine_removed = True
    except BaseException:
        if not quarantine_removed:
            if not moved:
                try:
                    os.rmdir(quarantine_name, dir_fd=directory.fd)
                    quarantine_removed = True
                except OSError:
                    pass
            if not quarantine_removed:
                _persist_quarantine_state(directory, quarantine)
            else:
                fsync_directory_fd(directory.fd)
        raise
    finally:
        quarantine.close()


def remove_verified_directory(
    parent: DirectoryHandle,
    name: str,
    child: DirectoryHandle,
    *,
    budget: TraversalBudget | None = None,
) -> bool:
    _validate_entry_name(name)
    try:
        current = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        if not child.matches(current):
            return False
        os.rmdir(name, dir_fd=parent.fd)
    except OSError:
        return False
    return fsync_open_directory(parent, budget)


def _sync_if_changed(
    directory: DirectoryHandle,
    changed: bool,
    budget: TraversalBudget,
) -> bool:
    return not changed or fsync_open_directory(directory, budget)


def _unlink_snapshot(
    directory: DirectoryHandle,
    entry: EntrySnapshot,
    cutoff_ts: float | None,
) -> tuple[int, int, bool, bool]:
    if cutoff_ts is not None and entry.mtime_ns >= int(cutoff_ts * 1e9):
        return 0, 0, True, False
    try:
        unlink_verified_entry(directory, entry)
    except FileNotFoundError:
        return 0, 0, True, False
    except OSError:
        return 0, 0, False, False
    if stat.S_ISREG(entry.mode):
        return 1, entry.size, True, True
    return 0, 0, True, True


def _sweep_child_directory(
    directory: DirectoryHandle,
    entry: EntrySnapshot,
    budget: TraversalBudget,
    cursor: DirectoryScanCursor,
    *,
    cutoff_ts: float | None,
    scan_cursors: DirectoryScanRegistry[DirectoryScanCursor] | None,
) -> tuple[int, int, bool]:
    try:
        child = open_child_directory(directory, entry)
    except OSError:
        cursor.mark_failed()
        return 0, 0, False
    with child:
        removed_files, removed_bytes, complete = _sweep_open_tree(
            child,
            budget,
            cutoff_ts=cutoff_ts,
            scan_cursors=scan_cursors,
        )
        removed = complete and remove_verified_directory(
            directory,
            entry.name,
            child,
            budget=budget,
        )
        if removed:
            cursor.mark_directory_changed()
        if removed and scan_cursors is not None:
            scan_cursors.discard(child)
        if not removed:
            cursor.mark_failed()
    return removed_files, removed_bytes, removed


def _sweep_open_tree(
    directory: DirectoryHandle,
    budget: TraversalBudget,
    *,
    cutoff_ts: float | None,
    scan_cursors: DirectoryScanRegistry[DirectoryScanCursor] | None,
) -> tuple[int, int, bool]:
    try:
        current = os.fstat(directory.fd)
        if not directory.matches(current):
            return 0, 0, False
    except OSError:
        return 0, 0, False

    removed_files = 0
    removed_bytes = 0
    directory_changed = False
    cursor = (
        scan_cursors.cursor_for(directory)
        if scan_cursors is not None
        else DirectoryScanCursor()
    )
    owns_cursor = scan_cursors is None
    try:
        while True:
            step = cursor.next_entry(directory, budget)
            if step.exhausted:
                _sync_if_changed(directory, directory_changed, budget)
                return removed_files, removed_bytes, False
            if step.reached_end:
                complete = step.cycle_complete and _sync_if_changed(
                    directory,
                    directory_changed,
                    budget,
                )
                return removed_files, removed_bytes, complete
            entry = step.entry
            if entry is None:
                continue

            if entry.is_directory:
                if is_retention_internal_directory(entry):
                    cursor.mark_failed()
                    continue
                child_files, child_bytes, removed = _sweep_child_directory(
                    directory,
                    entry,
                    budget,
                    cursor,
                    cutoff_ts=cutoff_ts,
                    scan_cursors=scan_cursors,
                )
                removed_files += child_files
                removed_bytes += child_bytes
                if removed:
                    directory_changed = False
                if budget.exhausted or budget.durability_failed:
                    _sync_if_changed(directory, directory_changed, budget)
                    return removed_files, removed_bytes, False
                continue

            files, freed, entry_complete, entry_changed = _unlink_snapshot(
                directory,
                entry,
                cutoff_ts,
            )
            removed_files += files
            removed_bytes += freed
            directory_changed = directory_changed or entry_changed
            if entry_changed:
                cursor.mark_directory_changed()
            if not entry_complete:
                cursor.mark_failed()
            if budget.durability_failed:
                _sync_if_changed(directory, directory_changed, budget)
                return removed_files, removed_bytes, False
    finally:
        if owns_cursor:
            cursor.reset()
        else:
            cursor.close_pass()


def sweep_directory_entry_bounded(
    parent: DirectoryHandle,
    expected: EntrySnapshot,
    budget: TraversalBudget,
    *,
    cutoff_ts: float | None,
    scan_cursors: DirectoryScanRegistry[DirectoryScanCursor] | None = None,
) -> tuple[int, int, bool]:
    try:
        child = open_child_directory(parent, expected)
    except FileNotFoundError:
        return 0, 0, False
    except OSError:
        return 0, 0, False
    with child:
        removed_files, removed_bytes, complete = _sweep_open_tree(
            child,
            budget,
            cutoff_ts=cutoff_ts,
            scan_cursors=scan_cursors,
        )
        if complete:
            complete = remove_verified_directory(
                parent,
                expected.name,
                child,
                budget=budget,
            )
            if complete and scan_cursors is not None:
                scan_cursors.discard(child)
    return removed_files, removed_bytes, complete


def sweep_open_directory_bounded(
    directory: DirectoryHandle,
    budget: TraversalBudget,
    *,
    cutoff_ts: float | None,
    scan_cursors: DirectoryScanRegistry[DirectoryScanCursor] | None = None,
) -> tuple[int, int, bool]:
    return _sweep_open_tree(
        directory,
        budget,
        cutoff_ts=cutoff_ts,
        scan_cursors=scan_cursors,
    )


def sweep_tree_bounded(
    directory: Path,
    budget: TraversalBudget,
    *,
    cutoff_ts: float | None,
    remove_directory: bool,
    scan_cursors: DirectoryScanRegistry[DirectoryScanCursor] | None = None,
) -> tuple[int, int, bool]:
    if not descriptor_relative_traversal_available():
        return 0, 0, False
    if not remove_directory:
        try:
            root = open_directory(directory)
        except FileNotFoundError:
            return 0, 0, True
        except OSError:
            return 0, 0, False
        with root:
            return _sweep_open_tree(
                root,
                budget,
                cutoff_ts=cutoff_ts,
                scan_cursors=scan_cursors,
            )

    try:
        parent = open_directory(directory.parent)
    except FileNotFoundError:
        return 0, 0, True
    except OSError:
        return 0, 0, False
    with parent:
        try:
            expected = directory_entry_snapshot(parent, directory.name)
        except FileNotFoundError:
            return 0, 0, True
        except (OSError, ValueError):
            return 0, 0, False
        if not expected.is_directory:
            return 0, 0, False
        return sweep_directory_entry_bounded(
            parent,
            expected,
            budget,
            cutoff_ts=cutoff_ts,
            scan_cursors=scan_cursors,
        )
