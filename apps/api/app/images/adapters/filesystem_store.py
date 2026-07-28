from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Set
from pathlib import Path, PurePosixPath
from typing import Any

from ..domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    PublishedArtifact,
    StagedArtifact,
    StagedSweepBudget,
    StagedSweepResult,
    UploadTicket,
)
from .filesystem_staging import (
    ArtifactIdentityMismatch,
    ArtifactStoreError,
    FileFingerprint as _FileFingerprint,
    HashAttempt as _HashAttempt,
    LegacyPage as _LegacyPage,
    RecordOutcome as _RecordOutcome,
    ScanPage as _ScanPage,
    StagedRecord as _StagedRecord,
    SweepCursor as _SweepCursor,
    SweepProgress as _SweepProgress,
    file_fingerprint as _fingerprint,
    hash_staged_file as _hash_staged_file,
)


_CHUNK_SIZE = 256 * 1024
_STAGE_FILE_PATTERN = re.compile(
    r"^artifact-v1-(?P<created>\d+)-(?P<size>\d+)-"
    r"(?P<sha256>[0-9a-f]{64})-(?P<nonce>[0-9a-f]+)\.source$"
)
_STAGED_INDEX_DIRECTORY = ".upload-staged-index"
_STAGED_QUARANTINE_DIRECTORY = ".upload-staged-quarantine"
_STAGED_CURSOR_FILE = ".upload-staged-cursor.json"
_STAGED_SHARD_COUNT = 4
_STAGED_LEGACY_SLOT = _STAGED_SHARD_COUNT
_STAGED_SLOT_COUNT = _STAGED_SHARD_COUNT + 1
_MAX_METADATA_BYTES = 16 * 1024
_LINK_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EXDEV,
        errno.EPERM,
        errno.EACCES,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while staging artifact")
        view = view[written:]


def _identity(path: Path) -> ArtifactIdentity:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ArtifactStoreError("artifact path is not a regular file")
    return ArtifactIdentity(
        sha256=_hash_file(path),
        size_bytes=info.st_size,
        device=info.st_dev,
        inode=info.st_ino,
    )


class FileSystemArtifactStore:
    def __init__(
        self,
        root: str | Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._monotonic = monotonic

    def _ensure_directory(self, path: Path) -> None:
        relative = path.relative_to(self.root)
        current = self.root
        root_info = current.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ArtifactStoreError("artifact storage root is unsafe")
        for part in relative.parts:
            current = current / part
            try:
                current.mkdir(mode=0o700, exist_ok=True)
            except FileExistsError:
                pass
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ArtifactStoreError("artifact parent is not a safe directory")

    def _validate_directory(self, path: Path) -> None:
        relative = path.relative_to(self.root)
        current = self.root
        root_info = current.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ArtifactStoreError("artifact storage root is unsafe")
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ArtifactStoreError("artifact parent is not a safe directory")

    def _path(self, key: ArtifactKey, *, create_parent: bool = False) -> Path:
        path = self.root.joinpath(*key.value.split("/"))
        parent = path.parent
        if create_parent:
            self._ensure_directory(parent)
        else:
            try:
                self._validate_directory(parent)
            except ValueError as exc:
                raise ArtifactStoreError("artifact path escapes storage root") from exc
        return path

    def _stage_dir(self, ticket: UploadTicket) -> Path:
        temp_root = self.root / ".upload-tmp"
        self._ensure_directory(temp_root)
        ticket_dir = temp_root / ticket.value
        try:
            ticket_dir.mkdir(mode=0o700)
        except FileExistsError:
            info = ticket_dir.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ArtifactStoreError("upload ticket directory is unsafe") from None
        return ticket_dir

    @staticmethod
    def _stage_filename_metadata(
        name: str,
    ) -> tuple[float, int, str] | None:
        match = _STAGE_FILE_PATTERN.fullmatch(name)
        if match is None:
            return None
        created_ns = int(match.group("created"))
        size_bytes = int(match.group("size"))
        return created_ns / 1_000_000_000, size_bytes, match.group("sha256")

    def _staged_relative_path(
        self,
        path: Path,
        *,
        ticket: UploadTicket | None = None,
    ) -> PurePosixPath:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactStoreError("staged artifact escapes storage root") from exc
        parts = relative.parts
        if (
            len(parts) != 3
            or parts[0] != ".upload-tmp"
            or not parts[2]
            or "\\" in parts[2]
        ):
            raise ArtifactStoreError("invalid staged artifact path")
        actual_ticket = UploadTicket(parts[1])
        if ticket is not None and actual_ticket != ticket:
            raise ArtifactStoreError("staged artifact ticket does not match its path")
        return PurePosixPath(*parts)

    def _validated_staged_relative_path(
        self,
        path: Path,
        *,
        ticket: UploadTicket | None = None,
    ) -> PurePosixPath:
        relative_path = self._staged_relative_path(path, ticket=ticket)
        self._validate_directory(path.parent)
        return relative_path

    @staticmethod
    def _metadata_entry_name(relative_path: PurePosixPath) -> str:
        digest = hashlib.sha256(relative_path.as_posix().encode("utf-8")).hexdigest()
        return f"{digest}.json"

    @staticmethod
    def _initial_metadata_shard(entry_name: str) -> int:
        return int(entry_name[:8], 16) % _STAGED_SHARD_COUNT

    def _metadata_shard_path(self, shard: int) -> Path:
        if shard < 0 or shard >= _STAGED_SHARD_COUNT:
            raise ValueError("invalid staged metadata shard")
        return self.root / _STAGED_INDEX_DIRECTORY / f"{shard:02x}"

    def _metadata_path(
        self,
        relative_path: PurePosixPath,
        *,
        shard: int,
    ) -> Path:
        return self._metadata_shard_path(shard) / self._metadata_entry_name(
            relative_path
        )

    def _find_metadata_path(
        self,
        relative_path: PurePosixPath,
    ) -> Path | None:
        entry_name = self._metadata_entry_name(relative_path)
        for shard in range(_STAGED_SHARD_COUNT):
            candidate = self._metadata_shard_path(shard) / entry_name
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ArtifactStoreError("staged metadata entry is unsafe")
            return candidate
        return None

    def _write_json_atomic(self, path: Path, value: dict[str, Any]) -> None:
        self._ensure_directory(path.parent)
        payload = json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > _MAX_METADATA_BYTES:
            raise ArtifactStoreError("staged metadata exceeds maximum bytes")
        fd, raw_path = tempfile.mkstemp(
            ".tmp",
            f".{path.name}.",
            str(path.parent),
        )
        temp_path = Path(raw_path)
        os.fchmod(fd, 0o600)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            temp_path.unlink(missing_ok=True)
            raise
        os.close(fd)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)

    def _create_metadata_record(
        self,
        record: _StagedRecord,
        *,
        shard: int | None = None,
    ) -> Path:
        relative_path = self._validated_staged_relative_path(
            record.path,
            ticket=record.ticket,
        )
        existing = self._find_metadata_path(relative_path)
        if existing is not None:
            return existing
        entry_name = self._metadata_entry_name(relative_path)
        target_shard = (
            self._initial_metadata_shard(entry_name) if shard is None else shard
        )
        metadata_path = self._metadata_path(relative_path, shard=target_shard)
        self._write_json_atomic(
            metadata_path,
            {
                "version": 1,
                "ticket": record.ticket.value,
                "relative_path": relative_path.as_posix(),
                "created_at": record.created_at,
                "identity": (
                    None if record.expected is None else record.expected.to_json()
                ),
            },
        )
        return metadata_path

    def _read_json_file(self, path: Path) -> dict[str, Any]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArtifactStoreError("staged metadata entry is unsafe")
        if info.st_size > _MAX_METADATA_BYTES:
            raise ArtifactStoreError("staged metadata exceeds maximum bytes")
        with path.open("rb") as handle:
            payload = handle.read(_MAX_METADATA_BYTES + 1)
        if len(payload) > _MAX_METADATA_BYTES:
            raise ArtifactStoreError("staged metadata exceeds maximum bytes")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ArtifactStoreError("invalid staged metadata")
        return value

    def _record_from_metadata(self, metadata_path: Path) -> _StagedRecord:
        value = self._read_json_file(metadata_path)
        if value.get("version") != 1:
            raise ArtifactStoreError("unsupported staged metadata version")
        ticket_value = value.get("ticket")
        relative_value = value.get("relative_path")
        created_at = value.get("created_at")
        if (
            not isinstance(ticket_value, str)
            or not isinstance(relative_value, str)
            or "\\" in relative_value
            or not isinstance(created_at, int | float)
        ):
            raise ArtifactStoreError("invalid staged metadata")
        relative_path = PurePosixPath(relative_value)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ArtifactStoreError("invalid staged metadata path")
        ticket = UploadTicket(ticket_value)
        path = self.root.joinpath(*relative_path.parts)
        self._validated_staged_relative_path(path, ticket=ticket)
        identity_value = value.get("identity")
        expected = (
            None
            if identity_value is None
            else ArtifactIdentity.from_json(identity_value)
        )
        if identity_value is not None and expected is None:
            raise ArtifactStoreError("invalid staged metadata identity")
        return _StagedRecord(
            ticket=ticket,
            path=path,
            created_at=float(created_at),
            expected=expected,
            metadata_path=metadata_path,
        )

    def _record_from_legacy_path(
        self,
        path: Path,
        *,
        shard: int,
    ) -> _StagedRecord:
        relative_path = self._validated_staged_relative_path(path)
        ticket = UploadTicket(relative_path.parts[1])
        info = path.lstat()
        filename_metadata = self._stage_filename_metadata(path.name)
        if filename_metadata is None:
            created_at = info.st_mtime
            expected = None
        else:
            created_at, size_bytes, sha256 = filename_metadata
            expected = ArtifactIdentity(
                sha256=sha256,
                size_bytes=size_bytes,
                device=info.st_dev,
                inode=info.st_ino,
            )
        provisional = _StagedRecord(
            ticket=ticket,
            path=path,
            created_at=created_at,
            expected=expected,
            metadata_path=Path(),
        )
        metadata_path = self._create_metadata_record(provisional, shard=shard)
        return _StagedRecord(
            ticket=ticket,
            path=path,
            created_at=created_at,
            expected=expected,
            metadata_path=metadata_path,
        )

    def _load_sweep_cursor(self) -> _SweepCursor:
        path = self.root / _STAGED_CURSOR_FILE
        try:
            value = self._read_json_file(path)
        except FileNotFoundError:
            return _SweepCursor()
        slot = value.get("slot")
        legacy_after = value.get("legacy_after")
        if (
            not isinstance(slot, int)
            or slot < 0
            or slot >= _STAGED_SLOT_COUNT
            or (legacy_after is not None and not isinstance(legacy_after, str))
        ):
            raise ArtifactStoreError("invalid staged sweep cursor")
        return _SweepCursor(slot=slot, legacy_after=legacy_after)

    def _persist_sweep_cursor(self, cursor: _SweepCursor) -> None:
        self._write_json_atomic(
            self.root / _STAGED_CURSOR_FILE,
            {
                "version": 1,
                "slot": cursor.slot,
                "legacy_after": cursor.legacy_after,
            },
        )

    @staticmethod
    def _cursor_token(cursor: _SweepCursor) -> str:
        suffix = "" if cursor.legacy_after is None else cursor.legacy_after
        return f"v1:{cursor.slot}:{suffix}"

    def _scan_metadata_shard(
        self,
        shard: int,
        *,
        max_files: int,
        deadline: float,
    ) -> _ScanPage:
        shard_path = self._metadata_shard_path(shard)
        try:
            self._validate_directory(shard_path)
        except FileNotFoundError:
            return _ScanPage(paths=(), complete=True)
        paths: list[Path] = []
        with os.scandir(shard_path) as entries:
            for entry in entries:
                if self._monotonic() >= deadline:
                    return _ScanPage(
                        paths=tuple(paths),
                        complete=False,
                        time_exhausted=True,
                    )
                if len(paths) >= max_files:
                    return _ScanPage(paths=tuple(paths), complete=False)
                paths.append(Path(entry.path))
        return _ScanPage(paths=tuple(paths), complete=True)

    def _legacy_cursor_exists(self, cursor: str | None) -> bool:
        if cursor is None or "\\" in cursor:
            return False
        relative = PurePosixPath(cursor)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        path = self.root.joinpath(*relative.parts)
        try:
            self._validated_staged_relative_path(path)
            path.lstat()
        except (FileNotFoundError, ArtifactStoreError, ValueError):
            return False
        return True

    @staticmethod
    def _legacy_ticket_path(ticket_entry: Any) -> Path | None:
        try:
            info = ticket_entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            return None
        try:
            UploadTicket(ticket_entry.name)
        except ValueError:
            return None
        return Path(ticket_entry.path)

    @staticmethod
    def _legacy_entry_token(ticket_name: str, file_name: str) -> str:
        return PurePosixPath(
            ".upload-tmp",
            ticket_name,
            file_name,
        ).as_posix()

    def _legacy_record_from_entry(
        self,
        *,
        token: str,
        file_entry: Any,
        shard: int,
    ) -> _StagedRecord | None:
        relative_path = PurePosixPath(token)
        if self._find_metadata_path(relative_path) is not None:
            return None
        try:
            record = self._record_from_legacy_path(
                Path(file_entry.path),
                shard=shard,
            )
        except (FileNotFoundError, ArtifactStoreError):
            return None
        return record

    def _scan_legacy_page(
        self,
        *,
        shard: int,
        after: str | None,
        max_files: int,
        deadline: float,
    ) -> _LegacyPage:
        temp_root = self.root / ".upload-tmp"
        try:
            self._validate_directory(temp_root)
        except FileNotFoundError:
            return _LegacyPage((), 0, True, None)
        resume = after is None or not self._legacy_cursor_exists(after)
        records: list[_StagedRecord] = []
        scanned = 0
        last_cursor = after
        with os.scandir(temp_root) as ticket_entries:
            for ticket_entry in ticket_entries:
                if self._monotonic() >= deadline:
                    return _LegacyPage(
                        tuple(records),
                        scanned,
                        False,
                        last_cursor,
                        time_exhausted=True,
                    )
                ticket_path = self._legacy_ticket_path(ticket_entry)
                if ticket_path is None:
                    continue
                with os.scandir(ticket_path) as file_entries:
                    for file_entry in file_entries:
                        if self._monotonic() >= deadline:
                            return _LegacyPage(
                                tuple(records),
                                scanned,
                                False,
                                last_cursor,
                                time_exhausted=True,
                            )
                        token = self._legacy_entry_token(
                            ticket_entry.name,
                            file_entry.name,
                        )
                        if not resume:
                            if token == after:
                                resume = True
                            continue
                        record = self._legacy_record_from_entry(
                            token=token,
                            file_entry=file_entry,
                            shard=shard,
                        )
                        scanned += 1
                        last_cursor = token
                        if record is not None:
                            records.append(record)
                        if scanned >= max_files:
                            return _LegacyPage(
                                tuple(records),
                                scanned,
                                False,
                                last_cursor,
                            )
        return _LegacyPage(tuple(records), scanned, True, None)

    def _rotate_metadata(self, path: Path, *, current_shard: int) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArtifactStoreError("staged metadata entry is unsafe")
        next_shard = (current_shard + 1) % _STAGED_SHARD_COUNT
        destination_dir = self._metadata_shard_path(next_shard)
        self._ensure_directory(destination_dir)
        destination = destination_dir / path.name
        os.replace(path, destination)
        _fsync_directory(path.parent)
        if destination.parent != path.parent:
            _fsync_directory(destination.parent)

    def _quarantine_metadata(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArtifactStoreError("staged metadata entry is unsafe")
        destination_dir = self.root / _STAGED_QUARANTINE_DIRECTORY
        self._ensure_directory(destination_dir)
        destination = destination_dir / path.name
        os.replace(path, destination)
        _fsync_directory(path.parent)
        _fsync_directory(destination.parent)

    def _remove_metadata_for_path(
        self,
        path: Path,
        *,
        preferred: Path | None = None,
    ) -> None:
        relative_path = self._staged_relative_path(path)
        entry_name = self._metadata_entry_name(relative_path)
        candidates: list[Path] = []
        valid_parents = {
            self._metadata_shard_path(shard) for shard in range(_STAGED_SHARD_COUNT)
        }
        if (
            preferred is not None
            and preferred.name == entry_name
            and preferred.parent in valid_parents
        ):
            candidates.append(preferred)
        for shard in range(_STAGED_SHARD_COUNT):
            candidate = self._metadata_shard_path(shard) / entry_name
            if candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ArtifactStoreError("staged metadata entry is unsafe")
            candidate.unlink()
            _fsync_directory(candidate.parent)

    def _unlink_staged_if_unchanged(
        self,
        record: _StagedRecord,
        fingerprint: _FileFingerprint,
    ) -> bool:
        self._validated_staged_relative_path(record.path, ticket=record.ticket)
        try:
            info = record.path.lstat()
        except FileNotFoundError:
            self._remove_metadata_for_path(
                record.path,
                preferred=record.metadata_path,
            )
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArtifactIdentityMismatch(
                "refusing to delete non-regular staged artifact"
            )
        if _fingerprint(info) != fingerprint:
            raise ArtifactIdentityMismatch("refusing to delete changed staged artifact")
        record.path.unlink()
        _fsync_directory(record.path.parent)
        self._remove_metadata_for_path(
            record.path,
            preferred=record.metadata_path,
        )
        try:
            record.path.parent.rmdir()
        except OSError:
            pass
        return True

    async def stage(
        self,
        ticket: UploadTicket,
        source: AsyncIterator[bytes],
        *,
        max_bytes: int,
    ) -> StagedArtifact:
        ticket_dir = await asyncio.to_thread(self._stage_dir, ticket)
        fd, raw_path = await asyncio.to_thread(
            tempfile.mkstemp,
            ".pending",
            "artifact-",
            str(ticket_dir),
        )
        path = Path(raw_path)
        os.fchmod(fd, 0o600)
        digest = hashlib.sha256()
        size = 0
        try:
            async for chunk in source:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ArtifactStoreError("upload exceeds maximum bytes")
                digest.update(chunk)
                await asyncio.to_thread(_write_all, fd, chunk)
            if size <= 0:
                raise ArtifactStoreError("empty upload")
            await asyncio.to_thread(os.fsync, fd)
        except BaseException:
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        os.close(fd)
        created_ns = time.time_ns()
        final_path = path.with_name(
            "artifact-v1-"
            f"{created_ns}-{size}-{digest.hexdigest()}-"
            f"{secrets.token_hex(8)}.source"
        )
        try:
            await asyncio.to_thread(os.replace, path, final_path)
            await asyncio.to_thread(_fsync_directory, ticket_dir)
            info = await asyncio.to_thread(final_path.lstat)
            identity = ArtifactIdentity(
                sha256=digest.hexdigest(),
                size_bytes=size,
                device=info.st_dev,
                inode=info.st_ino,
            )
            record = _StagedRecord(
                ticket=ticket,
                path=final_path,
                created_at=created_ns / 1_000_000_000,
                expected=identity,
                metadata_path=Path(),
            )
            metadata_path = await asyncio.to_thread(
                self._create_metadata_record,
                record,
            )
        except BaseException:
            path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            try:
                ticket_dir.rmdir()
            except OSError:
                pass
            raise
        return StagedArtifact(
            ticket=ticket,
            path=str(final_path),
            identity=identity,
            modified_at=info.st_mtime,
            created_at=record.created_at,
            metadata_path=str(metadata_path),
        )

    @staticmethod
    def _copy_exclusive(source: Path, destination: Path) -> None:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst, length=_CHUNK_SIZE)
                dst.flush()
                os.fsync(dst.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def _publish_path_sync(
        self,
        source: Path,
        key: ArtifactKey,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        source_identity = _identity(source)
        if not expected.matches(source_identity):
            raise ArtifactIdentityMismatch("source artifact identity changed")
        destination = self._path(key, create_parent=True)
        try:
            existing = _identity(destination)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.sha256 == expected.sha256
                and existing.size_bytes == expected.size_bytes
            ):
                return PublishedArtifact(key=key, identity=existing, created=False)
            raise FileExistsError(destination)
        try:
            os.link(source, destination)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                return self._publish_path_sync(source, key, expected)
            if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
                raise
            self._copy_exclusive(source, destination)
        _fsync_directory(destination.parent)
        published_identity = _identity(destination)
        if (
            published_identity.sha256 != expected.sha256
            or published_identity.size_bytes != expected.size_bytes
        ):
            destination.unlink(missing_ok=True)
            raise ArtifactIdentityMismatch("published artifact verification failed")
        return PublishedArtifact(key=key, identity=published_identity, created=True)

    async def publish(
        self,
        staged: StagedArtifact,
        key: ArtifactKey,
    ) -> PublishedArtifact:
        return await self.publish_path(
            Path(staged.path),
            key,
            expected=staged.identity,
        )

    async def publish_path(
        self,
        source: Path,
        key: ArtifactKey,
        *,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        return await asyncio.to_thread(
            self._publish_path_sync,
            source,
            key,
            expected,
        )

    async def identity(self, key: ArtifactKey) -> ArtifactIdentity | None:
        try:
            return await asyncio.to_thread(_identity, self._path(key))
        except FileNotFoundError:
            return None

    async def exists(self, key: ArtifactKey) -> bool:
        return await self.identity(key) is not None

    async def open(self, key: ArtifactKey) -> AsyncIterator[bytes]:
        path = self._path(key)
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            while chunk := await asyncio.to_thread(handle.read, _CHUNK_SIZE):
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def delete(
        self,
        key: ArtifactKey,
        expected: ArtifactIdentity | None = None,
    ) -> bool:
        path = self._path(key)
        try:
            current = await asyncio.to_thread(_identity, path)
        except FileNotFoundError:
            return False
        if expected is not None and not expected.matches(current):
            raise ArtifactIdentityMismatch("refusing to delete changed artifact")
        await asyncio.to_thread(path.unlink)
        await asyncio.to_thread(_fsync_directory, path.parent)
        return True

    async def delete_staged(self, staged: StagedArtifact) -> bool:
        path = Path(staged.path)
        try:
            relative_path = await asyncio.to_thread(
                self._validated_staged_relative_path,
                path,
                ticket=staged.ticket,
            )
            attempt = await asyncio.to_thread(
                _hash_staged_file,
                path,
                max_bytes=max(staged.identity.size_bytes, 1),
                deadline=float("inf"),
                monotonic=self._monotonic,
            )
        except FileNotFoundError:
            await asyncio.to_thread(self._remove_metadata_for_path, path)
            return False
        if (
            attempt.budget_exhausted
            or attempt.changed
            or attempt.identity is None
            or attempt.fingerprint is None
            or not staged.identity.matches(attempt.identity)
        ):
            raise ArtifactIdentityMismatch("refusing to delete changed staged artifact")
        metadata_path = (
            Path(staged.metadata_path)
            if staged.metadata_path is not None
            else self._metadata_path(
                relative_path,
                shard=self._initial_metadata_shard(
                    self._metadata_entry_name(relative_path)
                ),
            )
        )
        record = _StagedRecord(
            ticket=staged.ticket,
            path=path,
            created_at=staged.created_at or staged.modified_at or 0.0,
            expected=staged.identity,
            metadata_path=metadata_path,
        )
        return await asyncio.to_thread(
            self._unlink_staged_if_unchanged,
            record,
            attempt.fingerprint,
        )

    async def _rotate_record_metadata(
        self,
        record: _StagedRecord,
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
        record: _StagedRecord,
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
        record: _StagedRecord,
        *,
        current_shard: int,
        active_tickets: Set[str],
        stale_before: float,
    ) -> tuple[os.stat_result | None, _RecordOutcome | None]:
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
            return None, _RecordOutcome()
        except ArtifactStoreError:
            return None, _RecordOutcome(deferred=1)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            await self._rotate_record_metadata(record, current_shard=current_shard)
            return None, _RecordOutcome(deferred=1)
        if record.ticket.value in active_tickets or record.created_at > stale_before:
            rotated = await self._rotate_record_metadata(
                record,
                current_shard=current_shard,
            )
            return None, _RecordOutcome(deferred=int(not rotated))
        if not self._record_matches_info(record, info):
            await self._rotate_record_metadata(record, current_shard=current_shard)
            return None, _RecordOutcome(deferred=1)
        return info, None

    async def _hash_record_with_budget(
        self,
        record: _StagedRecord,
        *,
        current_shard: int,
        info: os.stat_result,
        remaining_hash_bytes: int,
        max_hash_bytes_per_pass: int,
        deadline: float,
    ) -> tuple[_HashAttempt | None, _RecordOutcome | None]:
        if info.st_size > max_hash_bytes_per_pass:
            try:
                await asyncio.to_thread(
                    self._quarantine_metadata,
                    record.metadata_path,
                )
            except ArtifactStoreError:
                pass
            return None, _RecordOutcome(deferred=1, quarantined=1)
        if info.st_size > remaining_hash_bytes:
            return None, _RecordOutcome(budget_exhausted=True)
        attempt = await asyncio.to_thread(
            _hash_staged_file,
            record.path,
            max_bytes=remaining_hash_bytes,
            deadline=deadline,
            monotonic=self._monotonic,
        )
        if attempt.budget_exhausted:
            deadline_exhausted = (
                attempt.deadline_exhausted or self._monotonic() >= deadline
            )
            return None, _RecordOutcome(
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
        return None, _RecordOutcome(
            hashed_bytes=attempt.bytes_hashed,
            deferred=1,
        )

    async def _delete_verified_staged(
        self,
        record: _StagedRecord,
        *,
        current_shard: int,
        attempt: _HashAttempt,
        deadline: float,
        before_delete: Callable[[], Awaitable[None]] | None,
    ) -> _RecordOutcome:
        remaining_seconds = deadline - self._monotonic()
        if remaining_seconds <= 0:
            return _RecordOutcome(
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
                return _RecordOutcome(
                    hashed_bytes=attempt.bytes_hashed,
                    budget_exhausted=True,
                    deadline_exhausted=True,
                )
        if self._monotonic() >= deadline:
            return _RecordOutcome(
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
            return _RecordOutcome(
                hashed_bytes=attempt.bytes_hashed,
                deferred=1,
            )
        return _RecordOutcome(
            hashed_bytes=attempt.bytes_hashed,
            deleted=int(deleted),
        )

    async def _process_staged_record(
        self,
        record: _StagedRecord,
        *,
        current_shard: int,
        active_tickets: Set[str],
        stale_before: float,
        remaining_hash_bytes: int,
        max_hash_bytes_per_pass: int,
        deadline: float,
        before_delete: Callable[[], Awaitable[None]] | None,
    ) -> _RecordOutcome:
        if self._monotonic() >= deadline:
            return _RecordOutcome(
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
            return outcome or _RecordOutcome()
        attempt, outcome = await self._hash_record_with_budget(
            record,
            current_shard=current_shard,
            info=info,
            remaining_hash_bytes=remaining_hash_bytes,
            max_hash_bytes_per_pass=max_hash_bytes_per_pass,
            deadline=deadline,
        )
        if outcome is not None or attempt is None:
            return outcome or _RecordOutcome()
        return await self._delete_verified_staged(
            record,
            current_shard=current_shard,
            attempt=attempt,
            deadline=deadline,
            before_delete=before_delete,
        )

    async def _resolve_active_tickets_for_records(
        self,
        records: list[_StagedRecord],
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
        progress: _SweepProgress,
        outcome: _RecordOutcome,
    ) -> None:
        progress.hashed_bytes += outcome.hashed_bytes
        progress.deleted += outcome.deleted
        progress.deferred += outcome.deferred
        progress.quarantined += outcome.quarantined

    async def _process_sweep_records(
        self,
        records: list[_StagedRecord],
        *,
        current_shard: int,
        active_tickets: Set[str] | None,
        load_active_tickets: Callable[[Set[str]], Awaitable[Set[str]]] | None,
        stale_before: float,
        budget: StagedSweepBudget,
        deadline: float,
        before_delete: Callable[[], Awaitable[None]] | None,
        progress: _SweepProgress,
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

    async def _load_metadata_page_records(
        self,
        page: _ScanPage,
        *,
        current_shard: int,
        deadline: float,
        progress: _SweepProgress,
    ) -> tuple[list[_StagedRecord], int]:
        records: list[_StagedRecord] = []
        consumed = 0
        for metadata_path in page.paths:
            if self._monotonic() >= deadline:
                progress.budget_exhausted = True
                progress.slot_complete = False
                break
            progress.scanned += 1
            consumed += 1
            try:
                record = await asyncio.to_thread(
                    self._record_from_metadata,
                    metadata_path,
                )
            except (ArtifactStoreError, json.JSONDecodeError, UnicodeError):
                progress.deferred += 1
                try:
                    await asyncio.to_thread(
                        self._rotate_metadata,
                        metadata_path,
                        current_shard=current_shard,
                    )
                except ArtifactStoreError:
                    pass
                continue
            records.append(record)
        return records, consumed

    async def _sweep_metadata_slot(
        self,
        cursor: _SweepCursor,
        *,
        active_tickets: Set[str] | None,
        load_active_tickets: Callable[[Set[str]], Awaitable[Set[str]]] | None,
        stale_before: float,
        budget: StagedSweepBudget,
        deadline: float,
        before_delete: Callable[[], Awaitable[None]] | None,
        progress: _SweepProgress,
    ) -> None:
        page = await asyncio.to_thread(
            self._scan_metadata_shard,
            cursor.slot,
            max_files=budget.max_files_per_pass,
            deadline=deadline,
        )
        records, consumed = await self._load_metadata_page_records(
            page,
            current_shard=cursor.slot,
            deadline=deadline,
            progress=progress,
        )
        processed = await self._process_sweep_records(
            records,
            current_shard=cursor.slot,
            active_tickets=active_tickets,
            load_active_tickets=load_active_tickets,
            stale_before=stale_before,
            budget=budget,
            deadline=deadline,
            before_delete=before_delete,
            progress=progress,
        )
        if consumed < len(page.paths) or processed < len(records):
            progress.slot_complete = False
        if not page.complete:
            progress.slot_complete = False
            progress.budget_exhausted = True
        if page.time_exhausted:
            progress.budget_exhausted = True

    async def _sweep_legacy_slot(
        self,
        cursor: _SweepCursor,
        *,
        active_tickets: Set[str] | None,
        load_active_tickets: Callable[[Set[str]], Awaitable[Set[str]]] | None,
        stale_before: float,
        budget: StagedSweepBudget,
        deadline: float,
        before_delete: Callable[[], Awaitable[None]] | None,
        progress: _SweepProgress,
    ) -> None:
        page = await asyncio.to_thread(
            self._scan_legacy_page,
            shard=0,
            after=cursor.legacy_after,
            max_files=budget.max_files_per_pass,
            deadline=deadline,
        )
        progress.scanned += page.scanned
        records = list(page.records)
        processed = await self._process_sweep_records(
            records,
            current_shard=0,
            active_tickets=active_tickets,
            load_active_tickets=load_active_tickets,
            stale_before=stale_before,
            budget=budget,
            deadline=deadline,
            before_delete=before_delete,
            progress=progress,
        )
        if processed < len(records):
            progress.slot_complete = False
        if not page.complete:
            progress.slot_complete = False
            progress.budget_exhausted = True
            progress.legacy_after = page.last_cursor
        else:
            progress.legacy_after = None
        if page.time_exhausted:
            progress.budget_exhausted = True

    async def sweep_staged(
        self,
        *,
        active_tickets: Set[str] | None,
        stale_before: float,
        budget: StagedSweepBudget,
        load_active_tickets: Callable[[Set[str]], Awaitable[Set[str]]] | None = None,
        before_delete: Callable[[], Awaitable[None]] | None = None,
    ) -> StagedSweepResult:
        started_at = self._monotonic()
        deadline = started_at + budget.max_seconds_per_pass
        cursor = await asyncio.to_thread(self._load_sweep_cursor)
        progress = _SweepProgress(legacy_after=cursor.legacy_after)

        if cursor.slot < _STAGED_SHARD_COUNT:
            await self._sweep_metadata_slot(
                cursor,
                active_tickets=active_tickets,
                load_active_tickets=load_active_tickets,
                stale_before=stale_before,
                budget=budget,
                deadline=deadline,
                before_delete=before_delete,
                progress=progress,
            )
        else:
            await self._sweep_legacy_slot(
                cursor,
                active_tickets=active_tickets,
                load_active_tickets=load_active_tickets,
                stale_before=stale_before,
                budget=budget,
                deadline=deadline,
                before_delete=before_delete,
                progress=progress,
            )

        next_slot = (
            (cursor.slot + 1) % _STAGED_SLOT_COUNT
            if progress.slot_complete
            else cursor.slot
        )
        next_cursor_state = _SweepCursor(
            slot=next_slot,
            legacy_after=progress.legacy_after,
        )
        await asyncio.to_thread(
            self._persist_sweep_cursor,
            next_cursor_state,
        )
        return StagedSweepResult(
            scanned=progress.scanned,
            hashed_bytes=progress.hashed_bytes,
            deleted=progress.deleted,
            deferred=progress.deferred,
            quarantined=progress.quarantined,
            budget_exhausted=progress.budget_exhausted,
            next_cursor=self._cursor_token(next_cursor_state),
        )

    def processing_path(self, key: ArtifactKey) -> Path:
        return self._path(key)


def publish_file_sync(
    source: Path,
    destination: Path,
) -> None:
    root = destination.parent
    while root.parent != root and not root.exists():
        root = root.parent
    store = FileSystemArtifactStore(root)
    relative = destination.relative_to(root).as_posix()
    store._publish_path_sync(source, ArtifactKey(relative), _identity(source))
