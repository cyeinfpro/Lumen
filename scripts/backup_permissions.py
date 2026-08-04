#!/usr/bin/env python3
"""Securely provision shared backup data and private recovery journals."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import errno
import fcntl
import grp
import hashlib
import hmac
import os
import pwd
import re
import stat
import subprocess
import sys


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_SHARED_DIRECTORY_MODE = 0o770
_SHARED_FILE_MODE = 0o660
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_RECOVERY_NAME = ".recovery"
_SHARED_DATA_NAMES = ("pg", "redis")
_LOCK_RECORD_LIMIT = 4096
_OWNER_TOKEN_PATTERN = re.compile(r"\.owner\.[A-Za-z0-9]+")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class BackupPermissionError(RuntimeError):
    """Raised when the backup layout cannot be migrated safely."""


@dataclass(frozen=True)
class _NodeSnapshot:
    device: int
    inode: int
    mode: int
    links: int
    user_id: int
    group_id: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _DirectorySnapshot:
    directory: _NodeSnapshot
    entries: tuple[tuple[str, _NodeSnapshot], ...]


@dataclass
class _WatchedDirectory:
    descriptor: int
    label: str
    baseline: _DirectorySnapshot
    final: _DirectorySnapshot | None = None


@dataclass
class _DirectoryPathBinding:
    raw_path: str
    parent_path: str
    parent_fd: int
    name: str
    directory_fd: int
    parent_identity: tuple[int, int]
    directory_identity: tuple[int, int]

    def verify(self, *, label: str) -> None:
        current_parent_fd = _open_existing_directory(
            _path_parts_allow_root(self.parent_path)
        )
        try:
            current_parent = os.fstat(current_parent_fd)
            retained_parent = os.fstat(self.parent_fd)
            if (
                (current_parent.st_dev, current_parent.st_ino)
                != self.parent_identity
                or (retained_parent.st_dev, retained_parent.st_ino)
                != self.parent_identity
            ):
                raise BackupPermissionError(f"{label} parent path changed")
        finally:
            os.close(current_parent_fd)
        metadata = _entry_metadata(self.parent_fd, self.name)
        opened = os.fstat(self.directory_fd)
        if (
            metadata is None
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (metadata.st_dev, metadata.st_ino) != self.directory_identity
            or (opened.st_dev, opened.st_ino) != self.directory_identity
        ):
            raise BackupPermissionError(f"{label} path entry changed")

    def token(self) -> str:
        return "v1:{}:{}:{}:{}".format(
            self.parent_identity[0],
            self.parent_identity[1],
            self.directory_identity[0],
            self.directory_identity[1],
        )

    def close(self) -> None:
        os.close(self.directory_fd)
        os.close(self.parent_fd)


def _node_snapshot(metadata: os.stat_result) -> _NodeSnapshot:
    return _NodeSnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        links=metadata.st_nlink,
        user_id=metadata.st_uid,
        group_id=metadata.st_gid,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _stable_node_identity(node: _NodeSnapshot) -> tuple[int, ...]:
    return (
        node.device,
        node.inode,
        stat.S_IFMT(node.mode),
        node.links,
        node.size,
        node.modified_ns,
    )


def _stable_snapshot_identity(
    snapshot: _DirectorySnapshot,
) -> tuple[tuple[int, ...], tuple[tuple[str, tuple[int, ...]], ...]]:
    return (
        _stable_node_identity(snapshot.directory),
        tuple((name, _stable_node_identity(node)) for name, node in snapshot.entries),
    )


def _entry_snapshots(
    directory_fd: int,
    names: list[str],
    *,
    label: str,
) -> tuple[tuple[str, _NodeSnapshot], ...]:
    entries: list[tuple[str, _NodeSnapshot]] = []
    for name in names:
        metadata = _entry_metadata(directory_fd, name)
        if metadata is None:
            raise BackupPermissionError(f"{label} changed while scanning: {name}")
        entries.append((name, _node_snapshot(metadata)))
    return tuple(entries)


def _capture_directory_snapshot(
    directory_fd: int,
    *,
    label: str,
) -> _DirectorySnapshot:
    before = _node_snapshot(os.fstat(directory_fd))
    if not stat.S_ISDIR(before.mode):
        raise BackupPermissionError(f"{label} is not a directory")
    names_before = _iter_entry_names(directory_fd)
    entries_before = _entry_snapshots(
        directory_fd,
        names_before,
        label=label,
    )
    names_after = _iter_entry_names(directory_fd)
    entries_after = _entry_snapshots(
        directory_fd,
        names_after,
        label=label,
    )
    after = _node_snapshot(os.fstat(directory_fd))
    if (
        before != after
        or names_before != names_after
        or entries_before != entries_after
    ):
        raise BackupPermissionError(f"{label} changed while scanning")
    return _DirectorySnapshot(directory=after, entries=entries_after)


def _assert_snapshot_matches(
    current: _DirectorySnapshot,
    expected: _DirectorySnapshot,
    *,
    label: str,
    strict: bool,
) -> None:
    if strict:
        matches = current == expected
    else:
        matches = _stable_snapshot_identity(current) == _stable_snapshot_identity(
            expected
        )
    if not matches:
        raise BackupPermissionError(
            f"{label} changed during backup permission migration"
        )


class _TreeStabilityGuard:
    # Retained directory FDs bind commit checks to the exact inodes observed
    # during preflight, even if a path component is concurrently replaced.
    def __init__(self) -> None:
        self._directories: list[_WatchedDirectory] = []
        self._by_identity: dict[tuple[int, int], _WatchedDirectory] = {}

    def __enter__(self) -> _TreeStabilityGuard:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        self.close()

    @staticmethod
    def _identity(directory_fd: int) -> tuple[int, int]:
        metadata = os.fstat(directory_fd)
        return metadata.st_dev, metadata.st_ino

    def watch(self, directory_fd: int, *, label: str) -> _DirectorySnapshot:
        identity = self._identity(directory_fd)
        watched = self._by_identity.get(identity)
        if watched is not None:
            return watched.baseline
        baseline = _capture_directory_snapshot(directory_fd, label=label)
        descriptor = os.dup(directory_fd)
        watched = _WatchedDirectory(
            descriptor=descriptor,
            label=label,
            baseline=baseline,
        )
        self._directories.append(watched)
        self._by_identity[identity] = watched
        return baseline

    def baseline_for(self, directory_fd: int) -> _DirectorySnapshot:
        watched = self._by_identity.get(self._identity(directory_fd))
        if watched is None:
            raise BackupPermissionError(
                "backup directory was not included in the migration baseline"
            )
        return watched.baseline

    def assert_unchanged(
        self,
        directory_fd: int,
        *,
        strict: bool,
    ) -> _DirectorySnapshot:
        watched = self._by_identity.get(self._identity(directory_fd))
        if watched is None:
            raise BackupPermissionError(
                "backup directory was not included in the migration baseline"
            )
        current = _capture_directory_snapshot(
            directory_fd,
            label=watched.label,
        )
        _assert_snapshot_matches(
            current,
            watched.baseline,
            label=watched.label,
            strict=strict,
        )
        return current

    def record_final(
        self,
        directory_fd: int,
        snapshot: _DirectorySnapshot,
    ) -> None:
        watched = self._by_identity.get(self._identity(directory_fd))
        if watched is None:
            raise BackupPermissionError(
                "backup directory was not included in the migration baseline"
            )
        _assert_snapshot_matches(
            snapshot,
            watched.baseline,
            label=watched.label,
            strict=False,
        )
        watched.final = snapshot

    def verify_baseline(self, *, strict: bool) -> None:
        self._verify(
            expected=lambda watched: watched.baseline,
            strict=strict,
        )

    def verify_final(self) -> None:
        def expected(watched: _WatchedDirectory) -> _DirectorySnapshot:
            if watched.final is None:
                raise BackupPermissionError(
                    f"{watched.label} lacks a final migration snapshot"
                )
            return watched.final

        self._verify(expected=expected, strict=True)

    def _verify(
        self,
        *,
        expected: Callable[[_WatchedDirectory], _DirectorySnapshot],
        strict: bool,
    ) -> None:
        orders = (self._directories, list(reversed(self._directories)))
        for directories in orders:
            for watched in directories:
                current = _capture_directory_snapshot(
                    watched.descriptor,
                    label=watched.label,
                )
                _assert_snapshot_matches(
                    current,
                    expected(watched),
                    label=watched.label,
                    strict=strict,
                )

    def close(self) -> None:
        while self._directories:
            watched = self._directories.pop()
            os.close(watched.descriptor)
        self._by_identity.clear()


def _coerce_path(raw_path: str | os.PathLike[str]) -> str:
    value = os.fspath(raw_path)
    if not isinstance(value, str):
        raise BackupPermissionError("path must be text")
    return value


def _path_parts(raw_path: str | os.PathLike[str]) -> tuple[str, ...]:
    path = _coerce_path(raw_path)
    if not path or not path.startswith("/"):
        raise BackupPermissionError("backup root must be an absolute path")
    if any(ord(character) < 32 for character in path):
        raise BackupPermissionError("backup root contains control characters")
    normalized = path.rstrip("/") or "/"
    if normalized == "/":
        raise BackupPermissionError("backup root cannot be the filesystem root")
    parts = tuple(normalized.split("/")[1:])
    if any(part in {"", ".", ".."} for part in parts):
        raise BackupPermissionError("backup root contains unsafe path components")
    return parts


def _path_parts_allow_root(
    raw_path: str | os.PathLike[str],
) -> tuple[str, ...]:
    if _coerce_path(raw_path) == "/":
        return ()
    return _path_parts(raw_path)


def _entry_metadata(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_child_directory(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> int:
    if stat.S_ISLNK(expected.st_mode):
        raise BackupPermissionError(f"path component is a symlink: {name}")
    if not stat.S_ISDIR(expected.st_mode):
        raise BackupPermissionError(f"path component is not a directory: {name}")
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or not _same_inode(expected, opened):
        os.close(descriptor)
        raise BackupPermissionError(f"directory changed while opening: {name}")
    return descriptor


def _open_existing_directory(parts: tuple[str, ...]) -> int:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    traversed: list[str] = []
    try:
        for part in parts:
            traversed.append(part)
            metadata = _entry_metadata(descriptor, part)
            if metadata is None:
                raise BackupPermissionError(
                    f"backup root parent does not exist: /{'/'.join(traversed)}"
                )
            child = _open_child_directory(descriptor, part, metadata)
            os.close(descriptor)
            descriptor = child
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_or_create_leaf_directory(raw_path: str, mode: int) -> int:
    parts = _path_parts(raw_path)
    parent_fd = _open_existing_directory(parts[:-1])
    name = parts[-1]
    try:
        metadata = _entry_metadata(parent_fd, name)
        if metadata is None:
            os.mkdir(name, mode=mode, dir_fd=parent_fd)
            metadata = _entry_metadata(parent_fd, name)
            if metadata is None:
                raise BackupPermissionError("backup root disappeared after creation")
        return _open_child_directory(parent_fd, name, metadata)
    finally:
        os.close(parent_fd)


def _open_directory_path_binding(
    raw_path: str | os.PathLike[str],
    *,
    create_mode: int | None = None,
) -> _DirectoryPathBinding:
    path = _coerce_path(raw_path)
    parts = _path_parts(path)
    parent_parts = parts[:-1]
    parent_path = "/" + "/".join(parent_parts) if parent_parts else "/"
    parent_fd = _open_existing_directory(parent_parts)
    name = parts[-1]
    directory_fd = -1
    try:
        metadata = _entry_metadata(parent_fd, name)
        if metadata is None:
            if create_mode is None:
                raise BackupPermissionError(f"directory does not exist: {path}")
            os.mkdir(name, mode=create_mode, dir_fd=parent_fd)
            metadata = _entry_metadata(parent_fd, name)
            if metadata is None:
                raise BackupPermissionError(
                    f"directory disappeared after creation: {path}"
                )
        directory_fd = _open_child_directory(parent_fd, name, metadata)
        parent_metadata = os.fstat(parent_fd)
        directory_metadata = os.fstat(directory_fd)
        binding = _DirectoryPathBinding(
            raw_path=path.rstrip("/"),
            parent_path=parent_path,
            parent_fd=parent_fd,
            name=name,
            directory_fd=directory_fd,
            parent_identity=(parent_metadata.st_dev, parent_metadata.st_ino),
            directory_identity=(
                directory_metadata.st_dev,
                directory_metadata.st_ino,
            ),
        )
        binding.verify(label="directory")
        return binding
    except Exception:
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(parent_fd)
        raise


def _parse_directory_binding_token(token: str) -> tuple[int, int, int, int]:
    fields = token.split(":")
    if len(fields) != 5 or fields[0] != "v1":
        raise BackupPermissionError("directory binding token is invalid")
    try:
        values = tuple(int(field) for field in fields[1:])
    except ValueError as exc:
        raise BackupPermissionError("directory binding token is invalid") from exc
    if any(value < 0 for value in values):
        raise BackupPermissionError("directory binding token is invalid")
    return values[0], values[1], values[2], values[3]


def _verify_directory_binding_token(
    raw_path: str | os.PathLike[str],
    token: str,
) -> None:
    expected = _parse_directory_binding_token(token)
    binding = _open_directory_path_binding(raw_path)
    try:
        binding.verify(label="directory")
        actual = (
            binding.parent_identity[0],
            binding.parent_identity[1],
            binding.directory_identity[0],
            binding.directory_identity[1],
        )
        if actual != expected:
            raise BackupPermissionError("directory path binding changed")
    finally:
        binding.close()


def _iter_entry_names(directory_fd: int) -> list[str]:
    return sorted(os.listdir(directory_fd))


def _entry_metadata_for_snapshot(
    directory_fd: int,
    name: str,
    expected: _NodeSnapshot,
    *,
    label: str,
    strict: bool,
) -> os.stat_result:
    metadata = _entry_metadata(directory_fd, name)
    if metadata is None:
        raise BackupPermissionError(f"{label} disappeared during migration")
    current = _node_snapshot(metadata)
    if strict:
        matches = current == expected
    else:
        matches = _stable_node_identity(current) == _stable_node_identity(expected)
    if not matches:
        raise BackupPermissionError(f"{label} changed during migration")
    return metadata


def _fsync(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        unsupported = {errno.EINVAL}
        if hasattr(errno, "ENOTSUP"):
            unsupported.add(errno.ENOTSUP)
        if exc.errno not in unsupported:
            raise


def _open_regular_file(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> int:
    if stat.S_ISLNK(expected.st_mode):
        raise BackupPermissionError(f"backup entry is a symlink: {name}")
    if not stat.S_ISREG(expected.st_mode):
        raise BackupPermissionError(f"backup entry is not a regular file: {name}")
    if expected.st_nlink != 1:
        raise BackupPermissionError(f"backup entry has multiple hard links: {name}")
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or not _same_inode(expected, opened)
    ):
        os.close(descriptor)
        raise BackupPermissionError(f"backup file changed while opening: {name}")
    return descriptor


def _read_bounded_file(descriptor: int, *, label: str) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = os.read(descriptor, _LOCK_RECORD_LIMIT + 1)
    if not payload or len(payload) > _LOCK_RECORD_LIMIT:
        raise BackupPermissionError(f"{label} is empty or oversized")
    return payload


def _parse_lock_record(payload: bytes, *, label: str) -> dict[str, str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise BackupPermissionError(f"{label} is not ASCII") from exc
    record: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if (
            separator != "="
            or not re.fullmatch(r"[a-z0-9_]+", key)
            or key in record
            or not value
        ):
            raise BackupPermissionError(f"{label} has an invalid record")
        record[key] = value
    return record


def _process_start_token(process_id: int) -> str:
    try:
        raw = open(f"/proc/{process_id}/stat", encoding="ascii").read()
    except OSError:
        raw = ""
    if raw and ") " in raw:
        fields = raw.rsplit(") ", 1)[1].split()
        if len(fields) > 19 and fields[19].isdecimal():
            return f"proc:{fields[19]}"
    completed = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(process_id)],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    started = completed.stdout.strip()
    if completed.returncode != 0 or not started:
        raise BackupPermissionError("maintenance lock owner is not running")
    return f"ps:{started}"


def _process_parent_id(process_id: int) -> int:
    completed = subprocess.run(
        ["ps", "-o", "ppid=", "-p", str(process_id)],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    raw = completed.stdout.strip()
    if completed.returncode != 0 or not raw.isdecimal():
        return 0
    return int(raw)


def _process_is_ancestor(ancestor_id: int, descendant_id: int) -> bool:
    current = descendant_id
    seen: set[int] = set()
    for _ in range(64):
        if current == ancestor_id:
            return True
        if current <= 1 or current in seen:
            return False
        seen.add(current)
        current = _process_parent_id(current)
    return False


def _validate_lock_record(
    payload: bytes,
    *,
    expected_owner_id: str,
    expected_owner_pid: str,
    expected_start_token: str,
    capability: str,
    label: str,
) -> None:
    record = _parse_lock_record(payload, label=label)
    if (
        record.get("owner_id") != expected_owner_id
        or record.get("pid") != expected_owner_pid
        or record.get("start_token") != expected_start_token
    ):
        raise BackupPermissionError(f"{label} owner record mismatch")
    if not expected_owner_pid.isdecimal():
        raise BackupPermissionError(f"{label} owner PID is invalid")
    owner_pid = int(expected_owner_pid)
    if _process_start_token(owner_pid) != expected_start_token:
        raise BackupPermissionError(f"{label} owner process changed")
    if not _process_is_ancestor(owner_pid, os.getpid()):
        raise BackupPermissionError(f"{label} owner is not a caller ancestor")
    expected_hash = record.get("capability_sha256", "")
    if not _SHA256_PATTERN.fullmatch(expected_hash):
        raise BackupPermissionError(f"{label} capability hash is invalid")
    try:
        actual_hash = hashlib.sha256(capability.encode("ascii")).hexdigest()
    except UnicodeEncodeError as exc:
        raise BackupPermissionError(f"{label} capability is invalid") from exc
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise BackupPermissionError(f"{label} capability mismatch")


def _open_lock_regular_file(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> int:
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
        raise BackupPermissionError(f"maintenance lock is not a regular file: {name}")
    if expected.st_nlink != 1:
        raise BackupPermissionError(f"maintenance lock has multiple hard links: {name}")
    descriptor = os.open(
        name,
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or not _same_inode(expected, opened)
    ):
        os.close(descriptor)
        raise BackupPermissionError("maintenance lock changed while opening")
    return descriptor


@dataclass
class _MaintenanceLockProof:
    root_binding: _DirectoryPathBinding
    root_fd: int
    kind: str
    lock_parent_fd: int
    lock_name: str
    lock_fd: int
    lock_snapshot: _NodeSnapshot
    flock_anchor_fd: int | None
    owner_name: str
    owner_fd: int | None
    owner_snapshot: _NodeSnapshot | None
    record_fd: int
    record_snapshot: _NodeSnapshot
    record_payload: bytes
    owner_pid: str
    owner_start_token: str
    capability: str
    local_lock_name: str | None = None
    local_lock_fd: int | None = None
    local_lock_snapshot: _NodeSnapshot | None = None
    local_owner_name: str | None = None
    local_owner_fd: int | None = None
    local_owner_snapshot: _NodeSnapshot | None = None
    local_record_fd: int | None = None
    local_record_snapshot: _NodeSnapshot | None = None
    local_record_payload: bytes | None = None
    local_capability: str | None = None

    def __enter__(self) -> _MaintenanceLockProof:
        self.verify()
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        self.close()

    def _verify_entry(
        self,
        parent_fd: int,
        name: str,
        descriptor: int,
        expected: _NodeSnapshot,
        *,
        label: str,
    ) -> None:
        metadata = _entry_metadata(parent_fd, name)
        if metadata is None:
            raise BackupPermissionError(f"{label} disappeared")
        if _node_snapshot(metadata) != expected:
            raise BackupPermissionError(f"{label} path changed")
        if _node_snapshot(os.fstat(descriptor)) != expected:
            raise BackupPermissionError(f"{label} descriptor changed")

    def verify(self) -> None:
        self.root_binding.verify(label="maintenance root")
        self._verify_entry(
            self.lock_parent_fd,
            self.lock_name,
            self.lock_fd,
            self.lock_snapshot,
            label="maintenance lock",
        )
        record_parent_fd = self.root_fd
        if self.kind == "mkdir":
            if self.owner_fd is None or self.owner_snapshot is None:
                raise BackupPermissionError("maintenance lock owner proof is incomplete")
            self._verify_entry(
                self.lock_fd,
                self.owner_name,
                self.owner_fd,
                self.owner_snapshot,
                label="maintenance lock owner directory",
            )
            record_parent_fd = self.owner_fd
        self._verify_entry(
            record_parent_fd,
            "owner" if self.kind == "mkdir" else self.lock_name,
            self.record_fd,
            self.record_snapshot,
            label="maintenance lock owner record",
        )
        if _read_bounded_file(
            self.record_fd,
            label="maintenance lock owner record",
        ) != self.record_payload:
            raise BackupPermissionError("maintenance lock owner record changed")
        _validate_lock_record(
            self.record_payload,
            expected_owner_id=self.owner_name,
            expected_owner_pid=self.owner_pid,
            expected_start_token=self.owner_start_token,
            capability=self.capability,
            label="maintenance lock owner record",
        )
        if self.local_lock_fd is not None:
            if (
                self.local_lock_name is None
                or self.local_lock_snapshot is None
                or self.local_owner_name is None
                or self.local_owner_fd is None
                or self.local_owner_snapshot is None
                or self.local_record_fd is None
                or self.local_record_snapshot is None
                or self.local_record_payload is None
                or self.local_capability is None
            ):
                raise BackupPermissionError(
                    "root-local maintenance lock proof is incomplete"
                )
            self._verify_entry(
                self.root_fd,
                self.local_lock_name,
                self.local_lock_fd,
                self.local_lock_snapshot,
                label="root-local maintenance lock",
            )
            self._verify_entry(
                self.local_lock_fd,
                self.local_owner_name,
                self.local_owner_fd,
                self.local_owner_snapshot,
                label="root-local maintenance owner directory",
            )
            self._verify_entry(
                self.local_owner_fd,
                "owner",
                self.local_record_fd,
                self.local_record_snapshot,
                label="root-local maintenance owner record",
            )
            if _read_bounded_file(
                self.local_record_fd,
                label="root-local maintenance owner record",
            ) != self.local_record_payload:
                raise BackupPermissionError(
                    "root-local maintenance owner record changed"
                )
            _validate_lock_record(
                self.local_record_payload,
                expected_owner_id=self.local_owner_name,
                expected_owner_pid=self.owner_pid,
                expected_start_token=self.owner_start_token,
                capability=self.local_capability,
                label="root-local maintenance owner record",
            )
        if self.kind == "flock":
            if self.flock_anchor_fd is None:
                raise BackupPermissionError(
                    "maintenance parent anchor proof is incomplete"
                )
            self._assert_flock_held(
                self.flock_anchor_fd,
                label="maintenance parent anchor flock",
            )
            self._assert_flock_held(
                self.lock_fd,
                label="maintenance root-local flock",
            )

    @staticmethod
    def _assert_flock_held(descriptor: int, *, label: str) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            raise BackupPermissionError(f"{label} is not held")

    def close(self) -> None:
        if self.local_record_fd is not None:
            os.close(self.local_record_fd)
        if self.local_owner_fd is not None:
            os.close(self.local_owner_fd)
        if self.local_lock_fd is not None:
            os.close(self.local_lock_fd)
        os.close(self.record_fd)
        if self.owner_fd is not None:
            os.close(self.owner_fd)
        os.close(self.lock_fd)
        if self.flock_anchor_fd is not None:
            os.close(self.flock_anchor_fd)
        self.root_binding.close()


def _required_lock_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or any(ord(character) < 32 for character in value):
        raise BackupPermissionError(f"maintenance lock proof is missing {name}")
    return value


def _open_maintenance_lock_proof(root: str) -> _MaintenanceLockProof:
    kind = _required_lock_environment("LUMEN_BORROWED_MAINTENANCE_LOCK_KIND")
    borrowed_root = _required_lock_environment(
        "LUMEN_BORROWED_MAINTENANCE_LOCK_ROOT"
    )
    if borrowed_root != root:
        raise BackupPermissionError("maintenance lock root mismatch")
    root_binding = _open_directory_path_binding(root)
    root_fd = root_binding.directory_fd
    lock_fd = -1
    flock_anchor_fd: int | None = None
    owner_fd: int | None = None
    record_fd = -1
    local_lock_name: str | None = None
    local_lock_fd: int | None = None
    local_lock_snapshot: _NodeSnapshot | None = None
    local_owner_name: str | None = None
    local_owner_fd: int | None = None
    local_owner_snapshot: _NodeSnapshot | None = None
    local_record_fd: int | None = None
    local_record_snapshot: _NodeSnapshot | None = None
    local_record_payload: bytes | None = None
    local_capability: str | None = None
    try:
        expected_root_binding = (
            _required_lock_environment(
                "LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_PATH"
            ),
            _required_lock_environment("LUMEN_BORROWED_MAINTENANCE_ROOT_NAME"),
            _required_lock_environment(
                "LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_DEV"
            ),
            _required_lock_environment(
                "LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_INO"
            ),
            _required_lock_environment("LUMEN_BORROWED_MAINTENANCE_ROOT_DEV"),
            _required_lock_environment("LUMEN_BORROWED_MAINTENANCE_ROOT_INO"),
        )
        actual_root_binding = (
            root_binding.parent_path,
            root_binding.name,
            str(root_binding.parent_identity[0]),
            str(root_binding.parent_identity[1]),
            str(root_binding.directory_identity[0]),
            str(root_binding.directory_identity[1]),
        )
        if expected_root_binding != actual_root_binding:
            raise BackupPermissionError("maintenance root path binding mismatch")
        anchor_key = _required_lock_environment(
            "LUMEN_BORROWED_MAINTENANCE_ROOT_ANCHOR_KEY"
        )
        expected_anchor_key = hashlib.sha256(
            root_binding.raw_path.encode("utf-8")
        ).hexdigest()[:32]
        if anchor_key != expected_anchor_key:
            raise BackupPermissionError("maintenance root anchor key mismatch")
        capability = _required_lock_environment(
            "LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY"
        )
        owner_pid = _required_lock_environment(
            "LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID"
        )
        owner_start_token = _required_lock_environment(
            "LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_START_TOKEN"
        )
        if kind == "flock":
            lock_name = ".lumen-maintenance.lock"
            lock_parent_fd = root_fd
            owner_name = "flock"
            expected_path = f"{root.rstrip('/')}/{lock_name}"
            if (
                _required_lock_environment(
                    "LUMEN_BORROWED_MAINTENANCE_LOCK_PATH"
                )
                != expected_path
            ):
                raise BackupPermissionError("maintenance flock path mismatch")
            metadata = _entry_metadata(root_fd, lock_name)
            if metadata is None:
                raise BackupPermissionError("maintenance flock disappeared")
            lock_fd = _open_lock_regular_file(root_fd, lock_name, metadata)
            lock_snapshot = _node_snapshot(os.fstat(lock_fd))
            if stat.S_IMODE(lock_snapshot.mode) not in {0o600, 0o660}:
                raise BackupPermissionError(
                    "maintenance flock mode is not 0600 or 0660"
                )
            expected_identity = (
                _required_lock_environment(
                    "LUMEN_BORROWED_MAINTENANCE_LOCK_DEV"
                ),
                _required_lock_environment(
                    "LUMEN_BORROWED_MAINTENANCE_LOCK_INO"
                ),
            )
            if expected_identity != (
                str(lock_snapshot.device),
                str(lock_snapshot.inode),
            ):
                raise BackupPermissionError("maintenance flock identity mismatch")
            anchor_path = _required_lock_environment(
                "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH"
            )
            if anchor_path != root_binding.parent_path:
                raise BackupPermissionError(
                    "maintenance parent anchor path mismatch"
                )
            flock_anchor_fd = os.dup(root_binding.parent_fd)
            anchor_snapshot = _node_snapshot(os.fstat(flock_anchor_fd))
            expected_anchor_identity = (
                _required_lock_environment(
                    "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_DEV"
                ),
                _required_lock_environment(
                    "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_INO"
                ),
            )
            if expected_anchor_identity != (
                str(anchor_snapshot.device),
                str(anchor_snapshot.inode),
            ):
                raise BackupPermissionError(
                    "maintenance parent anchor identity mismatch"
                )
            record_fd = os.dup(lock_fd)
            record_snapshot = _node_snapshot(os.fstat(record_fd))
            owner_snapshot = None
        elif kind == "mkdir":
            expected_anchor_path = (
                f"{root_binding.parent_path.rstrip('/')}/"
                f".lumen-maintenance.{anchor_key}.lock.d"
            )
            if root_binding.parent_path == "/":
                expected_anchor_path = (
                    f"/.lumen-maintenance.{anchor_key}.lock.d"
                )
            if (
                _required_lock_environment(
                    "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH"
                )
                != expected_anchor_path
                or _required_lock_environment(
                    "LUMEN_BORROWED_MAINTENANCE_LOCK_PATH"
                )
                != expected_anchor_path
            ):
                raise BackupPermissionError(
                    "maintenance parent anchor directory path mismatch"
                )
            lock_name = os.path.basename(expected_anchor_path)
            lock_parent_fd = root_binding.parent_fd
            owner_name = _required_lock_environment(
                "LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_TOKEN"
            )
            if not _OWNER_TOKEN_PATTERN.fullmatch(owner_name):
                raise BackupPermissionError("maintenance lock owner token is invalid")
            metadata = _entry_metadata(lock_parent_fd, lock_name)
            if metadata is None:
                raise BackupPermissionError(
                    "maintenance parent anchor directory disappeared"
                )
            lock_fd = _open_child_directory(lock_parent_fd, lock_name, metadata)
            lock_snapshot = _node_snapshot(os.fstat(lock_fd))
            expected_anchor_identity = (
                _required_lock_environment(
                    "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_DEV"
                ),
                _required_lock_environment(
                    "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_INO"
                ),
            )
            if expected_anchor_identity != (
                str(lock_snapshot.device),
                str(lock_snapshot.inode),
            ):
                raise BackupPermissionError(
                    "maintenance parent anchor identity mismatch"
                )
            owner_metadata = _entry_metadata(lock_fd, owner_name)
            if owner_metadata is None:
                raise BackupPermissionError(
                    "maintenance lock owner directory disappeared"
                )
            owner_fd = _open_child_directory(lock_fd, owner_name, owner_metadata)
            owner_snapshot = _node_snapshot(os.fstat(owner_fd))
            record_metadata = _entry_metadata(owner_fd, "owner")
            if record_metadata is None:
                raise BackupPermissionError("maintenance lock owner record disappeared")
            record_fd = _open_regular_file(owner_fd, "owner", record_metadata)
            record_snapshot = _node_snapshot(os.fstat(record_fd))
            if (
                stat.S_IMODE(lock_snapshot.mode) != 0o700
                or stat.S_IMODE(owner_snapshot.mode) != 0o700
                or stat.S_IMODE(record_snapshot.mode) != 0o600
                or lock_snapshot.user_id != owner_snapshot.user_id
                or owner_snapshot.user_id != record_snapshot.user_id
            ):
                raise BackupPermissionError("maintenance lock permissions are unsafe")
            local_lock_name = ".lumen-maintenance.lock.d"
            expected_local_path = (
                f"{root_binding.raw_path}/{local_lock_name}"
            )
            if (
                _required_lock_environment(
                    "LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH"
                )
                != expected_local_path
            ):
                raise BackupPermissionError(
                    "root-local maintenance lock path mismatch"
                )
            local_owner_name = _required_lock_environment(
                "LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_OWNER_TOKEN"
            )
            if not _OWNER_TOKEN_PATTERN.fullmatch(local_owner_name):
                raise BackupPermissionError(
                    "root-local maintenance owner token is invalid"
                )
            local_capability = _required_lock_environment(
                "LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_CAPABILITY"
            )
            local_metadata = _entry_metadata(root_fd, local_lock_name)
            if local_metadata is None:
                raise BackupPermissionError(
                    "root-local maintenance lock disappeared"
                )
            local_lock_fd = _open_child_directory(
                root_fd,
                local_lock_name,
                local_metadata,
            )
            local_lock_snapshot = _node_snapshot(os.fstat(local_lock_fd))
            local_owner_metadata = _entry_metadata(
                local_lock_fd,
                local_owner_name,
            )
            if local_owner_metadata is None:
                raise BackupPermissionError(
                    "root-local maintenance owner directory disappeared"
                )
            local_owner_fd = _open_child_directory(
                local_lock_fd,
                local_owner_name,
                local_owner_metadata,
            )
            local_owner_snapshot = _node_snapshot(os.fstat(local_owner_fd))
            local_record_metadata = _entry_metadata(local_owner_fd, "owner")
            if local_record_metadata is None:
                raise BackupPermissionError(
                    "root-local maintenance owner record disappeared"
                )
            local_record_fd = _open_regular_file(
                local_owner_fd,
                "owner",
                local_record_metadata,
            )
            local_record_snapshot = _node_snapshot(os.fstat(local_record_fd))
            if (
                stat.S_IMODE(local_lock_snapshot.mode) != 0o700
                or stat.S_IMODE(local_owner_snapshot.mode) != 0o700
                or stat.S_IMODE(local_record_snapshot.mode) != 0o600
                or local_lock_snapshot.user_id
                != local_owner_snapshot.user_id
                or local_owner_snapshot.user_id
                != local_record_snapshot.user_id
                or local_record_snapshot.user_id != record_snapshot.user_id
            ):
                raise BackupPermissionError(
                    "root-local maintenance lock permissions are unsafe"
                )
            local_record_payload = _read_bounded_file(
                local_record_fd,
                label="root-local maintenance owner record",
            )
        else:
            raise BackupPermissionError("maintenance lock kind is invalid")
        record_payload = _read_bounded_file(
            record_fd,
            label="maintenance lock owner record",
        )
        proof = _MaintenanceLockProof(
            root_binding=root_binding,
            root_fd=root_fd,
            kind=kind,
            lock_parent_fd=lock_parent_fd,
            lock_name=lock_name,
            lock_fd=lock_fd,
            lock_snapshot=lock_snapshot,
            flock_anchor_fd=flock_anchor_fd,
            owner_name=owner_name,
            owner_fd=owner_fd,
            owner_snapshot=owner_snapshot,
            record_fd=record_fd,
            record_snapshot=record_snapshot,
            record_payload=record_payload,
            owner_pid=owner_pid,
            owner_start_token=owner_start_token,
            capability=capability,
            local_lock_name=local_lock_name,
            local_lock_fd=local_lock_fd,
            local_lock_snapshot=local_lock_snapshot,
            local_owner_name=local_owner_name,
            local_owner_fd=local_owner_fd,
            local_owner_snapshot=local_owner_snapshot,
            local_record_fd=local_record_fd,
            local_record_snapshot=local_record_snapshot,
            local_record_payload=local_record_payload,
            local_capability=local_capability,
        )
        proof.verify()
        return proof
    except Exception:
        if local_record_fd is not None:
            os.close(local_record_fd)
        if local_owner_fd is not None:
            os.close(local_owner_fd)
        if local_lock_fd is not None:
            os.close(local_lock_fd)
        if record_fd >= 0:
            os.close(record_fd)
        if owner_fd is not None:
            os.close(owner_fd)
        if lock_fd >= 0:
            os.close(lock_fd)
        if flock_anchor_fd is not None:
            os.close(flock_anchor_fd)
        root_binding.close()
        raise


def _verify_child_directory_binding(
    parent_fd: int,
    name: str,
    child_fd: int,
) -> None:
    metadata = _entry_metadata(parent_fd, name)
    opened = os.fstat(child_fd)
    if (
        metadata is None
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or not _same_inode(metadata, opened)
    ):
        raise BackupPermissionError(
            f"private recovery path no longer names the opened directory: {name}"
        )


def _validate_shared_owner(
    metadata: os.stat_result,
    *,
    target_user_id: int,
    legacy_owner_id: int,
    label: str,
) -> None:
    if metadata.st_uid not in {target_user_id, legacy_owner_id}:
        raise BackupPermissionError(f"shared backup {label} owner mismatch")


def _set_shared_directory_permissions(
    directory_fd: int,
    *,
    target_user_id: int,
    target_group_id: int,
    legacy_owner_id: int,
    label: str,
) -> None:
    _validate_shared_owner(
        os.fstat(directory_fd),
        target_user_id=target_user_id,
        legacy_owner_id=legacy_owner_id,
        label=label,
    )
    os.fchmod(directory_fd, _SHARED_DIRECTORY_MODE)
    os.fchown(directory_fd, target_user_id, target_group_id)
    os.fchmod(directory_fd, _SHARED_DIRECTORY_MODE)
    final = os.fstat(directory_fd)
    if (
        final.st_uid != target_user_id
        or final.st_gid != target_group_id
        or stat.S_IMODE(final.st_mode) != _SHARED_DIRECTORY_MODE
    ):
        raise BackupPermissionError(
            f"shared backup directory migration did not converge: {label}"
        )
    _fsync(directory_fd)


def _set_shared_file_permissions(
    file_fd: int,
    *,
    target_user_id: int,
    target_group_id: int,
    legacy_owner_id: int,
    label: str,
) -> None:
    _validate_shared_owner(
        os.fstat(file_fd),
        target_user_id=target_user_id,
        legacy_owner_id=legacy_owner_id,
        label=label,
    )
    os.fchmod(file_fd, _SHARED_FILE_MODE)
    os.fchown(file_fd, target_user_id, target_group_id)
    os.fchmod(file_fd, _SHARED_FILE_MODE)
    final = os.fstat(file_fd)
    if (
        final.st_uid != target_user_id
        or final.st_gid != target_group_id
        or stat.S_IMODE(final.st_mode) != _SHARED_FILE_MODE
    ):
        raise BackupPermissionError(
            f"shared backup file migration did not converge: {label}"
        )
    _fsync(file_fd)


def _shared_entry_label(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _shared_directory_label(relative_path: str) -> str:
    return f"shared backup {relative_path or 'root'}"


def _validate_shared_tree(
    directory_fd: int,
    *,
    target_user_id: int,
    legacy_owner_id: int,
    guard: _TreeStabilityGuard,
    skip_recovery: bool = False,
    relative_path: str = "",
) -> None:
    directory_label = _shared_directory_label(relative_path)
    snapshot = guard.watch(directory_fd, label=directory_label)
    for name, expected in snapshot.entries:
        if skip_recovery and name == _PRIVATE_RECOVERY_NAME:
            continue
        label = _shared_entry_label(relative_path, name)
        metadata = _entry_metadata_for_snapshot(
            directory_fd,
            name,
            expected,
            label=f"shared backup {label}",
            strict=True,
        )
        if stat.S_ISLNK(metadata.st_mode):
            raise BackupPermissionError(f"backup entry is a symlink: {name}")
        if stat.S_ISDIR(metadata.st_mode):
            _validate_shared_owner(
                metadata,
                target_user_id=target_user_id,
                legacy_owner_id=legacy_owner_id,
                label=label,
            )
            child_fd = _open_child_directory(directory_fd, name, metadata)
            try:
                _validate_shared_owner(
                    os.fstat(child_fd),
                    target_user_id=target_user_id,
                    legacy_owner_id=legacy_owner_id,
                    label=label,
                )
                _validate_shared_tree(
                    child_fd,
                    target_user_id=target_user_id,
                    legacy_owner_id=legacy_owner_id,
                    guard=guard,
                    relative_path=label,
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupPermissionError(f"backup entry is not a regular file: {name}")
        _validate_shared_owner(
            metadata,
            target_user_id=target_user_id,
            legacy_owner_id=legacy_owner_id,
            label=label,
        )
        file_fd = _open_regular_file(directory_fd, name, metadata)
        try:
            _validate_shared_owner(
                os.fstat(file_fd),
                target_user_id=target_user_id,
                legacy_owner_id=legacy_owner_id,
                label=label,
            )
        finally:
            os.close(file_fd)
    guard.assert_unchanged(directory_fd, strict=True)


def _harden_shared_tree(
    directory_fd: int,
    *,
    target_user_id: int,
    target_group_id: int,
    legacy_owner_id: int,
    guard: _TreeStabilityGuard,
    skip_recovery: bool = False,
    relative_path: str = "",
) -> None:
    snapshot = guard.baseline_for(directory_fd)
    for name, expected in snapshot.entries:
        if skip_recovery and name == _PRIVATE_RECOVERY_NAME:
            continue
        label = _shared_entry_label(relative_path, name)
        metadata = _entry_metadata_for_snapshot(
            directory_fd,
            name,
            expected,
            label=f"shared backup {label}",
            strict=False,
        )
        if stat.S_ISLNK(metadata.st_mode):
            raise BackupPermissionError(f"backup entry is a symlink: {name}")
        if stat.S_ISDIR(metadata.st_mode):
            _validate_shared_owner(
                metadata,
                target_user_id=target_user_id,
                legacy_owner_id=legacy_owner_id,
                label=label,
            )
            child_fd = _open_child_directory(directory_fd, name, metadata)
            try:
                _validate_shared_owner(
                    os.fstat(child_fd),
                    target_user_id=target_user_id,
                    legacy_owner_id=legacy_owner_id,
                    label=label,
                )
                _harden_shared_tree(
                    child_fd,
                    target_user_id=target_user_id,
                    target_group_id=target_group_id,
                    legacy_owner_id=legacy_owner_id,
                    guard=guard,
                    relative_path=label,
                )
                _set_shared_directory_permissions(
                    child_fd,
                    target_user_id=target_user_id,
                    target_group_id=target_group_id,
                    legacy_owner_id=legacy_owner_id,
                    label=label,
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupPermissionError(f"backup entry is not a regular file: {name}")
        _validate_shared_owner(
            metadata,
            target_user_id=target_user_id,
            legacy_owner_id=legacy_owner_id,
            label=label,
        )
        file_fd = _open_regular_file(directory_fd, name, metadata)
        try:
            _set_shared_file_permissions(
                file_fd,
                target_user_id=target_user_id,
                target_group_id=target_group_id,
                legacy_owner_id=legacy_owner_id,
                label=label,
            )
        finally:
            os.close(file_fd)
    guard.assert_unchanged(directory_fd, strict=False)


def _validate_private_owner(
    metadata: os.stat_result,
    *,
    target_user_id: int,
    legacy_owner_id: int,
    label: str,
) -> None:
    if metadata.st_uid not in {target_user_id, legacy_owner_id}:
        raise BackupPermissionError(f"{label} owner mismatch")


def _open_or_create_private_recovery(
    backup_root_fd: int,
    *,
    target_user_id: int,
    legacy_owner_id: int,
) -> int:
    metadata = _entry_metadata(backup_root_fd, _PRIVATE_RECOVERY_NAME)
    if metadata is None:
        os.mkdir(
            _PRIVATE_RECOVERY_NAME,
            mode=_PRIVATE_DIRECTORY_MODE,
            dir_fd=backup_root_fd,
        )
        metadata = _entry_metadata(backup_root_fd, _PRIVATE_RECOVERY_NAME)
        if metadata is None:
            raise BackupPermissionError("private recovery directory disappeared")
    _validate_private_owner(
        metadata,
        target_user_id=target_user_id,
        legacy_owner_id=legacy_owner_id,
        label="private recovery directory",
    )
    recovery_fd = _open_child_directory(
        backup_root_fd,
        _PRIVATE_RECOVERY_NAME,
        metadata,
    )
    try:
        _validate_private_owner(
            os.fstat(recovery_fd),
            target_user_id=target_user_id,
            legacy_owner_id=legacy_owner_id,
            label="private recovery directory",
        )
    except Exception:
        os.close(recovery_fd)
        raise
    return recovery_fd


def _validate_private_recovery(
    recovery_fd: int,
    *,
    target_user_id: int,
    legacy_owner_id: int,
    guard: _TreeStabilityGuard,
) -> None:
    snapshot = guard.watch(
        recovery_fd,
        label="private recovery directory",
    )
    for name, expected in snapshot.entries:
        label = f"recovery journal {name}"
        child = _entry_metadata_for_snapshot(
            recovery_fd,
            name,
            expected,
            label=label,
            strict=True,
        )
        _validate_private_owner(
            child,
            target_user_id=target_user_id,
            legacy_owner_id=legacy_owner_id,
            label=label,
        )
        journal_fd = _open_regular_file(recovery_fd, name, child)
        try:
            _validate_private_owner(
                os.fstat(journal_fd),
                target_user_id=target_user_id,
                legacy_owner_id=legacy_owner_id,
                label=label,
            )
        finally:
            os.close(journal_fd)
    guard.assert_unchanged(recovery_fd, strict=True)


def _set_private_file_permissions(
    journal_fd: int,
    *,
    target_user_id: int,
    target_group_id: int,
    legacy_owner_id: int,
    label: str,
) -> None:
    _validate_private_owner(
        os.fstat(journal_fd),
        target_user_id=target_user_id,
        legacy_owner_id=legacy_owner_id,
        label=label,
    )
    os.fchmod(journal_fd, _PRIVATE_FILE_MODE)
    os.fchown(journal_fd, target_user_id, target_group_id)
    os.fchmod(journal_fd, _PRIVATE_FILE_MODE)
    final = os.fstat(journal_fd)
    if (
        final.st_uid != target_user_id
        or final.st_gid != target_group_id
        or stat.S_IMODE(final.st_mode) != _PRIVATE_FILE_MODE
    ):
        raise BackupPermissionError(f"{label} migration did not converge")
    _fsync(journal_fd)


def _set_private_directory_permissions(
    recovery_fd: int,
    *,
    target_user_id: int,
    target_group_id: int,
    legacy_owner_id: int,
) -> None:
    _validate_private_owner(
        os.fstat(recovery_fd),
        target_user_id=target_user_id,
        legacy_owner_id=legacy_owner_id,
        label="private recovery directory",
    )
    os.fchmod(recovery_fd, _PRIVATE_DIRECTORY_MODE)
    os.fchown(recovery_fd, target_user_id, target_group_id)
    os.fchmod(recovery_fd, _PRIVATE_DIRECTORY_MODE)
    final = os.fstat(recovery_fd)
    if (
        final.st_uid != target_user_id
        or final.st_gid != target_group_id
        or stat.S_IMODE(final.st_mode) != _PRIVATE_DIRECTORY_MODE
    ):
        raise BackupPermissionError(
            "private recovery directory migration did not converge"
        )
    _fsync(recovery_fd)


def _migrate_private_recovery(
    recovery_fd: int,
    *,
    target_user_id: int,
    target_group_id: int,
    legacy_owner_id: int,
    guard: _TreeStabilityGuard,
) -> None:
    snapshot = guard.baseline_for(recovery_fd)
    for name, expected in snapshot.entries:
        label = f"recovery journal {name}"
        child = _entry_metadata_for_snapshot(
            recovery_fd,
            name,
            expected,
            label=label,
            strict=False,
        )
        _validate_private_owner(
            child,
            target_user_id=target_user_id,
            legacy_owner_id=legacy_owner_id,
            label=label,
        )
        journal_fd = _open_regular_file(recovery_fd, name, child)
        try:
            _set_private_file_permissions(
                journal_fd,
                target_user_id=target_user_id,
                target_group_id=target_group_id,
                legacy_owner_id=legacy_owner_id,
                label=label,
            )
        finally:
            os.close(journal_fd)
    guard.assert_unchanged(recovery_fd, strict=False)
    _set_private_directory_permissions(
        recovery_fd,
        target_user_id=target_user_id,
        target_group_id=target_group_id,
        legacy_owner_id=legacy_owner_id,
    )
    guard.assert_unchanged(recovery_fd, strict=False)


def _validate_final_node(
    node: _NodeSnapshot,
    *,
    expected_mode: int,
    target_user_id: int,
    target_group_id: int,
    directory: bool,
    label: str,
) -> None:
    type_matches = stat.S_ISDIR(node.mode) if directory else stat.S_ISREG(node.mode)
    if (
        not type_matches
        or node.links != 1
        and not directory
        or node.user_id != target_user_id
        or node.group_id != target_group_id
        or stat.S_IMODE(node.mode) != expected_mode
    ):
        raise BackupPermissionError(f"{label} final permissions are unsafe")


def _record_final_shared_tree(
    directory_fd: int,
    *,
    target_user_id: int,
    target_group_id: int,
    guard: _TreeStabilityGuard,
    skip_recovery: bool = False,
    relative_path: str = "",
) -> None:
    directory_label = _shared_directory_label(relative_path)
    snapshot = _capture_directory_snapshot(
        directory_fd,
        label=directory_label,
    )
    _validate_final_node(
        snapshot.directory,
        expected_mode=_SHARED_DIRECTORY_MODE,
        target_user_id=target_user_id,
        target_group_id=target_group_id,
        directory=True,
        label=directory_label,
    )
    for name, node in snapshot.entries:
        if skip_recovery and name == _PRIVATE_RECOVERY_NAME:
            continue
        label = _shared_entry_label(relative_path, name)
        metadata = _entry_metadata_for_snapshot(
            directory_fd,
            name,
            node,
            label=f"shared backup {label}",
            strict=True,
        )
        if stat.S_ISDIR(node.mode):
            _validate_final_node(
                node,
                expected_mode=_SHARED_DIRECTORY_MODE,
                target_user_id=target_user_id,
                target_group_id=target_group_id,
                directory=True,
                label=f"shared backup {label}",
            )
            child_fd = _open_child_directory(directory_fd, name, metadata)
            try:
                _record_final_shared_tree(
                    child_fd,
                    target_user_id=target_user_id,
                    target_group_id=target_group_id,
                    guard=guard,
                    relative_path=label,
                )
            finally:
                os.close(child_fd)
            continue
        _validate_final_node(
            node,
            expected_mode=_SHARED_FILE_MODE,
            target_user_id=target_user_id,
            target_group_id=target_group_id,
            directory=False,
            label=f"shared backup {label}",
        )
        file_fd = _open_regular_file(directory_fd, name, metadata)
        try:
            if _node_snapshot(os.fstat(file_fd)) != node:
                raise BackupPermissionError(
                    f"shared backup {label} changed during final validation"
                )
        finally:
            os.close(file_fd)
    guard.record_final(directory_fd, snapshot)


def _record_final_private_recovery(
    recovery_fd: int,
    *,
    target_user_id: int,
    target_group_id: int,
    guard: _TreeStabilityGuard,
) -> None:
    snapshot = _capture_directory_snapshot(
        recovery_fd,
        label="private recovery directory",
    )
    _validate_final_node(
        snapshot.directory,
        expected_mode=_PRIVATE_DIRECTORY_MODE,
        target_user_id=target_user_id,
        target_group_id=target_group_id,
        directory=True,
        label="private recovery directory",
    )
    for name, node in snapshot.entries:
        label = f"recovery journal {name}"
        _validate_final_node(
            node,
            expected_mode=_PRIVATE_FILE_MODE,
            target_user_id=target_user_id,
            target_group_id=target_group_id,
            directory=False,
            label=label,
        )
        metadata = _entry_metadata_for_snapshot(
            recovery_fd,
            name,
            node,
            label=label,
            strict=True,
        )
        journal_fd = _open_regular_file(recovery_fd, name, metadata)
        try:
            if _node_snapshot(os.fstat(journal_fd)) != node:
                raise BackupPermissionError(f"{label} changed during final validation")
        finally:
            os.close(journal_fd)
    guard.record_final(recovery_fd, snapshot)


def _resolve_user(name: str) -> pwd.struct_passwd:
    try:
        if name.isdecimal():
            return pwd.getpwuid(int(name))
        return pwd.getpwnam(name)
    except KeyError as exc:
        raise BackupPermissionError(
            f"backup service user does not exist: {name}"
        ) from exc


def _resolve_group(name: str) -> grp.struct_group:
    try:
        if name.isdecimal():
            return grp.getgrgid(int(name))
        return grp.getgrnam(name)
    except KeyError as exc:
        raise BackupPermissionError(
            f"backup service group does not exist: {name}"
        ) from exc


def _ensure_backup_layout_locked(args: argparse.Namespace) -> str:
    target_user = _resolve_user(args.service_user)
    target_group = _resolve_group(args.service_group)
    legacy_owner = _resolve_user(args.legacy_owner_user)
    backup_root_binding = _open_directory_path_binding(
        args.backup_root,
        create_mode=_SHARED_DIRECTORY_MODE,
    )
    backup_root_fd = backup_root_binding.directory_fd
    recovery_fd: int | None = None
    try:
        backup_root_binding.verify(label="backup root")
        _validate_shared_owner(
            os.fstat(backup_root_fd),
            target_user_id=target_user.pw_uid,
            legacy_owner_id=legacy_owner.pw_uid,
            label="root",
        )
        for name in _SHARED_DATA_NAMES:
            metadata = _entry_metadata(backup_root_fd, name)
            if metadata is None:
                os.mkdir(name, mode=_SHARED_DIRECTORY_MODE, dir_fd=backup_root_fd)
                metadata = _entry_metadata(backup_root_fd, name)
                if metadata is None:
                    raise BackupPermissionError(
                        f"shared backup directory disappeared: {name}"
                    )
            child_fd = _open_child_directory(backup_root_fd, name, metadata)
            os.close(child_fd)
        recovery_fd = _open_or_create_private_recovery(
            backup_root_fd,
            target_user_id=target_user.pw_uid,
            legacy_owner_id=legacy_owner.pw_uid,
        )
        with _TreeStabilityGuard() as guard:
            guard.watch(backup_root_fd, label=_shared_directory_label(""))
            _verify_child_directory_binding(
                backup_root_fd,
                _PRIVATE_RECOVERY_NAME,
                recovery_fd,
            )
            _validate_shared_tree(
                backup_root_fd,
                target_user_id=target_user.pw_uid,
                legacy_owner_id=legacy_owner.pw_uid,
                guard=guard,
                skip_recovery=True,
            )
            _validate_private_recovery(
                recovery_fd,
                target_user_id=target_user.pw_uid,
                legacy_owner_id=legacy_owner.pw_uid,
                guard=guard,
            )
            guard.verify_baseline(strict=True)
            _migrate_private_recovery(
                recovery_fd,
                target_user_id=target_user.pw_uid,
                target_group_id=target_group.gr_gid,
                legacy_owner_id=legacy_owner.pw_uid,
                guard=guard,
            )
            _harden_shared_tree(
                backup_root_fd,
                target_user_id=target_user.pw_uid,
                target_group_id=target_group.gr_gid,
                legacy_owner_id=legacy_owner.pw_uid,
                guard=guard,
                skip_recovery=True,
            )
            _set_shared_directory_permissions(
                backup_root_fd,
                target_user_id=target_user.pw_uid,
                target_group_id=target_group.gr_gid,
                legacy_owner_id=legacy_owner.pw_uid,
                label="root",
            )
            guard.verify_baseline(strict=False)
            _record_final_shared_tree(
                backup_root_fd,
                target_user_id=target_user.pw_uid,
                target_group_id=target_group.gr_gid,
                guard=guard,
                skip_recovery=True,
            )
            _record_final_private_recovery(
                recovery_fd,
                target_user_id=target_user.pw_uid,
                target_group_id=target_group.gr_gid,
                guard=guard,
            )
            guard.verify_final()
            _verify_child_directory_binding(
                backup_root_fd,
                _PRIVATE_RECOVERY_NAME,
                recovery_fd,
            )
            backup_root_binding.verify(label="backup root")
        backup_root_binding.verify(label="backup root")
        return backup_root_binding.token()
    finally:
        if recovery_fd is not None:
            os.close(recovery_fd)
        backup_root_binding.close()


def ensure_backup_layout(args: argparse.Namespace) -> str:
    with _open_maintenance_lock_proof(args.maintenance_lock_root) as lock_proof:
        binding_token = _ensure_backup_layout_locked(args)
        lock_proof.verify()
        _verify_directory_binding_token(args.backup_root, binding_token)
        return binding_token


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    ensure = subparsers.add_parser("ensure-backup-layout")
    ensure.add_argument("backup_root")
    ensure.add_argument("--service-user", required=True)
    ensure.add_argument("--service-group", required=True)
    ensure.add_argument("--legacy-owner-user", default="root")
    ensure.add_argument("--maintenance-lock-root", required=True)
    ensure.add_argument("--emit-binding-token", action="store_true")
    verify_binding = subparsers.add_parser("verify-path-binding")
    verify_binding.add_argument("path")
    verify_binding.add_argument("--token", required=True)
    args = parser.parse_args()

    try:
        if args.command == "ensure-backup-layout":
            binding_token = ensure_backup_layout(args)
            if args.emit_binding_token:
                print(binding_token)
        elif args.command == "verify-path-binding":
            _verify_directory_binding_token(args.path, args.token)
        else:
            raise BackupPermissionError("unknown backup permission command")
    except (BackupPermissionError, OSError) as exc:
        print(f"backup permission error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
