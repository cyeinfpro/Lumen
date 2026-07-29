from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any

from ...domain.artifact import (
    ArtifactIdentity,
    StagedArtifact,
    UploadTicket,
)
from ..filesystem_staging import (
    ArtifactIdentityMismatch,
    ArtifactStoreError,
    FileFingerprint,
    StagedRecord,
    file_fingerprint,
)
from ..filesystem_writer import StageFileWriter, write_all
from .objects import fsync_directory


STAGE_FILE_PATTERN = re.compile(
    r"^artifact-v1-(?P<created>\d+)-(?P<size>\d+)-"
    r"(?P<sha256>[0-9a-f]{64})-(?P<nonce>[0-9a-f]+)\.source$"
)
STAGED_INDEX_DIRECTORY = ".upload-staged-index"
STAGED_QUARANTINE_DIRECTORY = ".upload-staged-quarantine"
STAGED_CURSOR_FILE = ".upload-staged-cursor.json"
STAGED_SHARD_COUNT = 4
STAGED_LEGACY_SLOT = STAGED_SHARD_COUNT
STAGED_SLOT_COUNT = STAGED_SHARD_COUNT + 1
MAX_METADATA_BYTES = 16 * 1024


class FileSystemStagingMixin:
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
        match = STAGE_FILE_PATTERN.fullmatch(name)
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
        return int(entry_name[:8], 16) % STAGED_SHARD_COUNT

    def _metadata_shard_path(self, shard: int) -> Path:
        if shard < 0 or shard >= STAGED_SHARD_COUNT:
            raise ValueError("invalid staged metadata shard")
        return self.root / STAGED_INDEX_DIRECTORY / f"{shard:02x}"

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
        for shard in range(STAGED_SHARD_COUNT):
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
        if len(payload) > MAX_METADATA_BYTES:
            raise ArtifactStoreError("staged metadata exceeds maximum bytes")
        fd, raw_path = tempfile.mkstemp(
            ".tmp",
            f".{path.name}.",
            str(path.parent),
        )
        temp_path = Path(raw_path)
        os.fchmod(fd, 0o600)
        try:
            write_all(fd, payload)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            temp_path.unlink(missing_ok=True)
            raise
        os.close(fd)
        os.replace(temp_path, path)
        fsync_directory(path.parent)

    def _create_metadata_record(
        self,
        record: StagedRecord,
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
        if info.st_size > MAX_METADATA_BYTES:
            raise ArtifactStoreError("staged metadata exceeds maximum bytes")
        with path.open("rb") as handle:
            payload = handle.read(MAX_METADATA_BYTES + 1)
        if len(payload) > MAX_METADATA_BYTES:
            raise ArtifactStoreError("staged metadata exceeds maximum bytes")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ArtifactStoreError("invalid staged metadata")
        return value

    def _record_from_metadata(self, metadata_path: Path) -> StagedRecord:
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
        return StagedRecord(
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
    ) -> StagedRecord:
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
        provisional = StagedRecord(
            ticket=ticket,
            path=path,
            created_at=created_at,
            expected=expected,
            metadata_path=Path(),
        )
        metadata_path = self._create_metadata_record(provisional, shard=shard)
        return StagedRecord(
            ticket=ticket,
            path=path,
            created_at=created_at,
            expected=expected,
            metadata_path=metadata_path,
        )

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
            self._metadata_shard_path(shard) for shard in range(STAGED_SHARD_COUNT)
        }
        if (
            preferred is not None
            and preferred.name == entry_name
            and preferred.parent in valid_parents
        ):
            candidates.append(preferred)
        for shard in range(STAGED_SHARD_COUNT):
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
            fsync_directory(candidate.parent)

    def _unlink_staged_if_unchanged(
        self,
        record: StagedRecord,
        fingerprint: FileFingerprint,
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
        if file_fingerprint(info) != fingerprint:
            raise ArtifactIdentityMismatch("refusing to delete changed staged artifact")
        record.path.unlink()
        fsync_directory(record.path.parent)
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
        writer = StageFileWriter(fd)
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
                await writer.write(chunk)
            if size <= 0:
                raise ArtifactStoreError("empty upload")
            await writer.finish()
            self._record_upload_writer(
                upload_bytes=size,
                queue_wait_seconds=writer.queue_wait_seconds,
                duration_seconds=writer.duration_seconds,
            )
        except BaseException:
            await writer.abort()
            path.unlink(missing_ok=True)
            raise
        created_ns = time.time_ns()
        final_path = path.with_name(
            "artifact-v1-"
            f"{created_ns}-{size}-{digest.hexdigest()}-"
            f"{secrets.token_hex(8)}.source"
        )
        try:
            await asyncio.to_thread(os.replace, path, final_path)
            await asyncio.to_thread(fsync_directory, ticket_dir)
            info = await asyncio.to_thread(final_path.lstat)
            identity = ArtifactIdentity(
                sha256=digest.hexdigest(),
                size_bytes=size,
                device=info.st_dev,
                inode=info.st_ino,
            )
            record = StagedRecord(
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

    async def delete_staged(self, staged: StagedArtifact) -> bool:
        path = Path(staged.path)
        try:
            relative_path = await asyncio.to_thread(
                self._validated_staged_relative_path,
                path,
                ticket=staged.ticket,
            )
            attempt = await asyncio.to_thread(
                self._hash_staged_file,
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
        record = StagedRecord(
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
