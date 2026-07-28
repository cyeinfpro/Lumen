from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..domain.artifact import ArtifactIdentity, UploadTicket


_CHUNK_SIZE = 256 * 1024


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactIdentityMismatch(ArtifactStoreError):
    pass


@dataclass(frozen=True)
class FileFingerprint:
    device: int
    inode: int
    size_bytes: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class HashAttempt:
    identity: ArtifactIdentity | None
    fingerprint: FileFingerprint | None
    bytes_hashed: int
    budget_exhausted: bool = False
    deadline_exhausted: bool = False
    changed: bool = False


@dataclass(frozen=True)
class StagedRecord:
    ticket: UploadTicket
    path: Path
    created_at: float
    expected: ArtifactIdentity | None
    metadata_path: Path


@dataclass(frozen=True)
class SweepCursor:
    slot: int = 0
    legacy_after: str | None = None


@dataclass(frozen=True)
class ScanPage:
    paths: tuple[Path, ...]
    complete: bool
    time_exhausted: bool = False


@dataclass(frozen=True)
class LegacyPage:
    records: tuple[StagedRecord, ...]
    scanned: int
    complete: bool
    last_cursor: str | None
    time_exhausted: bool = False


@dataclass(frozen=True)
class RecordOutcome:
    hashed_bytes: int = 0
    deleted: int = 0
    deferred: int = 0
    quarantined: int = 0
    budget_exhausted: bool = False
    deadline_exhausted: bool = False


@dataclass
class SweepProgress:
    scanned: int = 0
    hashed_bytes: int = 0
    deleted: int = 0
    deferred: int = 0
    quarantined: int = 0
    budget_exhausted: bool = False
    slot_complete: bool = True
    legacy_after: str | None = None


def file_fingerprint(info: os.stat_result) -> FileFingerprint:
    return FileFingerprint(
        device=info.st_dev,
        inode=info.st_ino,
        size_bytes=info.st_size,
        modified_ns=info.st_mtime_ns,
        changed_ns=info.st_ctime_ns,
    )


def hash_staged_file(
    path: Path,
    *,
    max_bytes: int,
    deadline: float,
    monotonic: Callable[[], float],
) -> HashAttempt:
    if max_bytes <= 0:
        return HashAttempt(
            identity=None,
            fingerprint=None,
            bytes_hashed=0,
            budget_exhausted=True,
        )
    if monotonic() >= deadline:
        return HashAttempt(
            identity=None,
            fingerprint=None,
            bytes_hashed=0,
            budget_exhausted=True,
            deadline_exhausted=True,
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactStoreError("staged artifact is not a regular file")
        if before.st_size > max_bytes:
            return HashAttempt(
                identity=None,
                fingerprint=None,
                bytes_hashed=0,
                budget_exhausted=True,
            )
        digest = hashlib.sha256()
        hashed_bytes = 0
        while hashed_bytes < before.st_size:
            if monotonic() >= deadline:
                return HashAttempt(
                    identity=None,
                    fingerprint=None,
                    bytes_hashed=hashed_bytes,
                    budget_exhausted=True,
                    deadline_exhausted=True,
                )
            remaining = min(
                _CHUNK_SIZE,
                before.st_size - hashed_bytes,
                max_bytes - hashed_bytes,
            )
            if remaining <= 0:
                return HashAttempt(
                    identity=None,
                    fingerprint=None,
                    bytes_hashed=hashed_bytes,
                    budget_exhausted=True,
                )
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            digest.update(chunk)
            hashed_bytes += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    before_fingerprint = file_fingerprint(before)
    after_fingerprint = file_fingerprint(after)
    if before_fingerprint != after_fingerprint or hashed_bytes != after.st_size:
        return HashAttempt(
            identity=None,
            fingerprint=after_fingerprint,
            bytes_hashed=hashed_bytes,
            changed=True,
        )
    return HashAttempt(
        identity=ArtifactIdentity(
            sha256=digest.hexdigest(),
            size_bytes=after.st_size,
            device=after.st_dev,
            inode=after.st_ino,
        ),
        fingerprint=after_fingerprint,
        bytes_hashed=hashed_bytes,
    )
