#!/usr/bin/env python3
"""Validate and safely extract Lumen Redis backup archives."""

from __future__ import annotations

import argparse
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

_ALLOWED_ROOTS = frozenset({"dump.rdb", "appendonly.aof", "appendonlydir"})
_ARCHIVE_ROOT = PurePosixPath(".")
_DUMP_PATH = PurePosixPath("dump.rdb")
_APPENDONLY_FILE_PATH = PurePosixPath("appendonly.aof")
_APPENDONLY_DIRECTORY_PATH = PurePosixPath("appendonlydir")
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_COPY_CHUNK_SIZE = 1024 * 1024

ValidatedMember = Tuple[tarfile.TarInfo, PurePosixPath]


def _normalized_member_name(raw: str) -> Optional[PurePosixPath]:
    value = raw
    while value.startswith("./"):
        value = value[2:]
    if not value or value == ".":
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe redis archive path: {raw}")
    if path.parts[0] not in _ALLOWED_ROOTS:
        raise ValueError(f"unexpected redis archive entry: {raw}")
    if path.parts[0] in {"dump.rdb", "appendonly.aof"} and len(path.parts) != 1:
        raise ValueError(f"invalid redis archive file path: {raw}")
    return path


def _validate_parent_paths(member_kinds: Dict[PurePosixPath, bool]) -> None:
    for path in member_kinds:
        parent = path.parent
        while parent != _ARCHIVE_ROOT:
            if parent in member_kinds and not member_kinds[parent]:
                raise ValueError(
                    f"redis archive file is used as a parent directory: {parent}"
                )
            parent = parent.parent


def _validated_members(archive: tarfile.TarFile) -> List[ValidatedMember]:
    validated: List[ValidatedMember] = []
    member_kinds: Dict[PurePosixPath, bool] = {}
    dump_present = False
    for member in archive.getmembers():
        path = _normalized_member_name(member.name)
        if path is None:
            if not member.isdir():
                raise ValueError("redis archive root entry is not a directory")
            continue
        if not (member.isdir() or member.isreg()):
            raise ValueError(f"unsupported redis archive entry type: {member.name}")
        if member.size < 0:
            raise ValueError(f"invalid redis archive member size: {member.name}")
        if path in member_kinds:
            raise ValueError(f"duplicate redis archive entry: {member.name}")
        if path == _DUMP_PATH:
            if not member.isreg() or member.size <= 0:
                raise ValueError("redis dump.rdb is missing or empty")
            dump_present = True
        elif path == _APPENDONLY_FILE_PATH and not member.isreg():
            raise ValueError("redis appendonly.aof must be a regular file")
        elif path == _APPENDONLY_DIRECTORY_PATH and not member.isdir():
            raise ValueError("redis appendonlydir must be a directory")
        member_kinds[path] = member.isdir()
        validated.append((member, path))
    if not dump_present:
        raise ValueError("redis archive does not contain a non-empty dump.rdb")
    _validate_parent_paths(member_kinds)
    return validated


def _required_directories(
    members: List[ValidatedMember],
) -> List[PurePosixPath]:
    required: Set[PurePosixPath] = set()
    for member, path in members:
        current = path if member.isdir() else path.parent
        while current != _ARCHIVE_ROOT:
            required.add(current)
            current = current.parent
    return sorted(required, key=lambda path: (len(path.parts), str(path)))


def _open_empty_destination(destination: Path) -> int:
    try:
        destination_status = destination.lstat()
    except FileNotFoundError:
        destination.mkdir(parents=True, mode=0o700)
        destination_status = destination.lstat()

    if stat.S_ISLNK(destination_status.st_mode):
        raise ValueError("redis extraction destination cannot be a symlink")
    if not stat.S_ISDIR(destination_status.st_mode):
        raise ValueError("redis extraction destination must be a directory")

    try:
        descriptor = os.open(destination, _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise ValueError("cannot securely open redis extraction destination") from exc
    try:
        if os.listdir(descriptor):
            raise ValueError("redis extraction destination must be empty")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_directory(root_descriptor: int, path: PurePosixPath) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for part in path.parts:
            child_descriptor = os.open(
                part,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_directories(
    root_descriptor: int,
    directories: List[PurePosixPath],
    created: List[PurePosixPath],
) -> None:
    for relative in directories:
        parent_descriptor = _open_directory(root_descriptor, relative.parent)
        try:
            os.mkdir(
                relative.name,
                mode=0o700,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ValueError(
                f"cannot securely create redis extraction directory: {relative}"
            ) from exc
        finally:
            os.close(parent_descriptor)
        created.append(relative)


def _extract_file(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    relative: PurePosixPath,
    root_descriptor: int,
    created: List[PurePosixPath],
) -> None:
    source = archive.extractfile(member)
    if source is None:
        raise ValueError(f"cannot read redis archive member: {relative}")

    parent_descriptor = _open_directory(root_descriptor, relative.parent)
    descriptor: Optional[int] = None
    try:
        try:
            descriptor = os.open(
                relative.name,
                _FILE_CREATE_FLAGS,
                stat.S_IRUSR | stat.S_IWUSR,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ValueError(
                f"cannot securely create redis extraction file: {relative}"
            ) from exc
        created.append(relative)
        with source, os.fdopen(descriptor, "wb") as output:
            descriptor = None
            copied = 0
            while True:
                chunk = source.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                copied += len(chunk)
            if copied != member.size:
                raise ValueError(f"redis archive member size mismatch: {relative}")
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def _fsync_directories(
    root_descriptor: int,
    directories: List[PurePosixPath],
) -> None:
    for relative in sorted(
        directories,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        descriptor = _open_directory(root_descriptor, relative)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    os.fsync(root_descriptor)


def _cleanup_created(
    root_descriptor: int,
    files: List[PurePosixPath],
    directories: List[PurePosixPath],
) -> None:
    for relative in reversed(files):
        try:
            parent_descriptor = _open_directory(
                root_descriptor,
                relative.parent,
            )
        except (OSError, ValueError):
            continue
        try:
            os.unlink(relative.name, dir_fd=parent_descriptor)
        except OSError:
            pass
        finally:
            os.close(parent_descriptor)

    for relative in sorted(
        directories,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            parent_descriptor = _open_directory(
                root_descriptor,
                relative.parent,
            )
        except (OSError, ValueError):
            continue
        try:
            os.rmdir(relative.name, dir_fd=parent_descriptor)
        except OSError:
            pass
        finally:
            os.close(parent_descriptor)
    try:
        os.fsync(root_descriptor)
    except OSError:
        pass


def extract_archive(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = _validated_members(archive)
        directories = _required_directories(members)
        root_descriptor = _open_empty_destination(destination)
        created_directories: List[PurePosixPath] = []
        created_files: List[PurePosixPath] = []
        try:
            _create_directories(
                root_descriptor,
                directories,
                created_directories,
            )
            for member, relative in members:
                if member.isdir():
                    continue
                _extract_file(
                    archive,
                    member,
                    relative,
                    root_descriptor,
                    created_files,
                )
            _fsync_directories(root_descriptor, directories)
        except BaseException:
            _cleanup_created(
                root_descriptor,
                created_files,
                created_directories,
            )
            raise
        finally:
            os.close(root_descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    extract_archive(args.archive, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
