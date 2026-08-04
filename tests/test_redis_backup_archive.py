from __future__ import annotations

import io
import os
import tarfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import BinaryIO, Optional

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "redis_backup_archive.py"
SPEC = spec_from_file_location("redis_backup_archive", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
REDIS_BACKUP_ARCHIVE = module_from_spec(SPEC)
SPEC.loader.exec_module(REDIS_BACKUP_ARCHIVE)

ArchiveEntry = tuple[tarfile.TarInfo, Optional[BinaryIO]]


def _file(name: str, payload: bytes) -> ArchiveEntry:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    return member, io.BytesIO(payload)


def _directory(name: str) -> ArchiveEntry:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o700
    return member, None


def _special(
    name: str,
    member_type: bytes,
    *,
    linkname: str = "",
) -> ArchiveEntry:
    member = tarfile.TarInfo(name)
    member.type = member_type
    member.linkname = linkname
    if member_type in {tarfile.CHRTYPE, tarfile.BLKTYPE}:
        member.devmajor = 1
        member.devminor = 3
    return member, None


def _write_archive(path: Path, entries: list[ArchiveEntry]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for member, source in entries:
            archive.addfile(member, source)
    return path


def _assert_empty_or_missing(path: Path) -> None:
    if not path.exists():
        return
    assert path.is_dir()
    assert list(path.iterdir()) == []


def _extract(archive: Path, destination: Path) -> None:
    REDIS_BACKUP_ARCHIVE.extract_archive(archive, destination)


def test_extracts_valid_dump_and_appendonly_payloads(tmp_path: Path) -> None:
    archive = _write_archive(
        tmp_path / "valid.tgz",
        [
            _file("dump.rdb", b"redis-dump"),
            _file("appendonly.aof", b"legacy-aof"),
            _directory("appendonlydir"),
            _directory("appendonlydir/nested"),
            _file("appendonlydir/nested/part.aof", b"multipart-aof"),
        ],
    )
    destination = tmp_path / "restore"
    destination.mkdir()

    _extract(archive, destination)

    assert (destination / "dump.rdb").read_bytes() == b"redis-dump"
    assert (destination / "appendonly.aof").read_bytes() == b"legacy-aof"
    assert (
        destination / "appendonlydir" / "nested" / "part.aof"
    ).read_bytes() == b"multipart-aof"


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [_file("dump.rdb", b"")],
        [_file("appendonly.aof", b"aof-only")],
        [
            _directory("appendonlydir"),
            _file("appendonlydir/part.aof", b"aof-only"),
        ],
    ],
    ids=["empty-archive", "empty-dump", "legacy-aof-only", "multipart-aof-only"],
)
def test_rejects_archives_without_nonempty_dump(
    tmp_path: Path,
    entries: list[ArchiveEntry],
) -> None:
    archive = _write_archive(tmp_path / "missing-dump.tgz", entries)
    destination = tmp_path / "restore"

    with pytest.raises(ValueError, match="dump.rdb"):
        _extract(archive, destination)

    _assert_empty_or_missing(destination)


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../escaped",
        "appendonlydir/../../escaped",
    ],
)
def test_rejects_path_traversal_without_writing_outside_destination(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    archive = _write_archive(
        tmp_path / "traversal.tgz",
        [
            _file("dump.rdb", b"redis-dump"),
            _file(unsafe_name, b"escaped"),
        ],
    )
    destination = tmp_path / "restore"
    escaped = tmp_path / "escaped"

    with pytest.raises(ValueError, match="unsafe redis archive path"):
        _extract(archive, destination)

    assert not escaped.exists()
    _assert_empty_or_missing(destination)


def test_rejects_absolute_path_without_writing_outside_destination(
    tmp_path: Path,
) -> None:
    escaped = tmp_path / "absolute-escape"
    archive = _write_archive(
        tmp_path / "absolute.tgz",
        [
            _file("dump.rdb", b"redis-dump"),
            _file(str(escaped), b"escaped"),
        ],
    )
    destination = tmp_path / "restore"

    with pytest.raises(ValueError, match="unsafe redis archive path"):
        _extract(archive, destination)

    assert not escaped.exists()
    _assert_empty_or_missing(destination)


@pytest.mark.parametrize(
    ("member_type", "linkname"),
    [
        (tarfile.SYMTYPE, "../../escaped"),
        (tarfile.LNKTYPE, "dump.rdb"),
        (tarfile.CHRTYPE, ""),
        (tarfile.BLKTYPE, ""),
        (tarfile.FIFOTYPE, ""),
    ],
    ids=["symlink", "hardlink", "character-device", "block-device", "fifo"],
)
def test_rejects_special_member_types_before_extraction(
    tmp_path: Path,
    member_type: bytes,
    linkname: str,
) -> None:
    archive = _write_archive(
        tmp_path / "special.tgz",
        [
            _file("dump.rdb", b"redis-dump"),
            _special(
                "appendonlydir/unsafe",
                member_type,
                linkname=linkname,
            ),
        ],
    )
    destination = tmp_path / "restore"

    with pytest.raises(ValueError, match="unsupported redis archive entry type"):
        _extract(archive, destination)

    _assert_empty_or_missing(destination)


@pytest.mark.parametrize(
    "entry",
    [
        _file("redis.conf", b"unexpected"),
        _file("appendonlydir", b"not-a-directory"),
        _directory("appendonly.aof"),
    ],
    ids=["unexpected-root", "appendonlydir-file", "appendonly-aof-directory"],
)
def test_rejects_unexpected_roots_and_invalid_root_types(
    tmp_path: Path,
    entry: ArchiveEntry,
) -> None:
    archive = _write_archive(
        tmp_path / "bad-root.tgz",
        [_file("dump.rdb", b"redis-dump"), entry],
    )
    destination = tmp_path / "restore"

    with pytest.raises(ValueError):
        _extract(archive, destination)

    _assert_empty_or_missing(destination)


@pytest.mark.parametrize(
    "entries",
    [
        [
            _file("dump.rdb", b"first"),
            _file("dump.rdb", b"second"),
        ],
        [
            _file("dump.rdb", b"first"),
            _file("./dump.rdb", b"second"),
        ],
        [
            _file("dump.rdb", b"redis-dump"),
            _file("appendonlydir/segment", b"parent-file"),
            _file("appendonlydir/segment/child.aof", b"child"),
        ],
        [
            _file("dump.rdb", b"redis-dump"),
            _file("appendonlydir/segment/child.aof", b"child"),
            _file("appendonlydir/segment", b"parent-file"),
        ],
    ],
    ids=[
        "duplicate-member",
        "normalized-duplicate-member",
        "file-before-child",
        "file-after-child",
    ],
)
def test_rejects_duplicate_members_and_parent_path_conflicts_before_writing(
    tmp_path: Path,
    entries: list[ArchiveEntry],
) -> None:
    archive = _write_archive(tmp_path / "conflict.tgz", entries)
    destination = tmp_path / "restore"

    with pytest.raises(ValueError):
        _extract(archive, destination)

    _assert_empty_or_missing(destination)


def test_rejects_existing_destination_file_without_modifying_it(
    tmp_path: Path,
) -> None:
    archive = _write_archive(
        tmp_path / "valid.tgz",
        [_file("dump.rdb", b"redis-dump")],
    )
    destination = tmp_path / "restore"
    destination.write_bytes(b"keep-me")

    with pytest.raises(ValueError, match="destination"):
        _extract(archive, destination)

    assert destination.read_bytes() == b"keep-me"


def test_rejects_destination_symlink_without_writing_to_target(
    tmp_path: Path,
) -> None:
    archive = _write_archive(
        tmp_path / "valid.tgz",
        [_file("dump.rdb", b"redis-dump")],
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "restore"
    destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        _extract(archive, destination)

    assert list(outside.iterdir()) == []


def test_rejects_nonempty_destination_before_writing_any_member(
    tmp_path: Path,
) -> None:
    archive = _write_archive(
        tmp_path / "valid.tgz",
        [_file("dump.rdb", b"redis-dump")],
    )
    destination = tmp_path / "restore"
    destination.mkdir()
    marker = destination / "existing"
    marker.write_bytes(b"keep-me")

    with pytest.raises(ValueError, match="empty"):
        _extract(archive, destination)

    assert marker.read_bytes() == b"keep-me"
    assert sorted(path.name for path in destination.iterdir()) == ["existing"]


def test_existing_parent_symlink_cannot_escape_destination(
    tmp_path: Path,
) -> None:
    archive = _write_archive(
        tmp_path / "valid.tgz",
        [
            _file("dump.rdb", b"redis-dump"),
            _file("appendonlydir/part.aof", b"must-not-escape"),
        ],
    )
    destination = tmp_path / "restore"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "appendonlydir").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(ValueError, match="empty"):
        _extract(archive, destination)

    assert list(outside.iterdir()) == []
    assert sorted(path.name for path in destination.iterdir()) == ["appendonlydir"]


def test_destination_scan_treats_broken_symlink_as_existing_content(
    tmp_path: Path,
) -> None:
    archive = _write_archive(
        tmp_path / "valid.tgz",
        [_file("dump.rdb", b"redis-dump")],
    )
    destination = tmp_path / "restore"
    destination.mkdir()
    broken_link = destination / "dump.rdb"
    broken_link.symlink_to(tmp_path / "missing")

    with pytest.raises(ValueError, match="empty"):
        _extract(archive, destination)

    assert broken_link.is_symlink()
    assert not os.path.exists(broken_link)


def test_write_failure_removes_all_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_archive(
        tmp_path / "valid.tgz",
        [
            _file("dump.rdb", b"redis-dump"),
            _file("appendonlydir/part.aof", b"appendonly"),
        ],
    )
    destination = tmp_path / "restore"
    real_fsync = REDIS_BACKUP_ARCHIVE.os.fsync
    failed = False

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(
        REDIS_BACKUP_ARCHIVE.os,
        "fsync",
        fail_first_fsync,
    )

    with pytest.raises(OSError, match="injected fsync failure"):
        _extract(archive, destination)

    _assert_empty_or_missing(destination)
