"""Canonical validation for committed Lumen backup pairs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TIMESTAMP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_MARKER_BYTES = 64 * 1024


class BackupPairInvalid(ValueError):
    """The requested backup pair is absent, incomplete, or tampered with."""


@dataclass(frozen=True)
class BackupPairBinding:
    timestamp: str
    operation_id: str
    marker_path: Path
    pg_path: Path
    redis_path: Path
    pg_size: int
    redis_size: int
    pg_sha256: str
    redis_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup_operation_id": self.operation_id,
            "backup_pair_marker": str(self.marker_path),
            "pg_backup_path": str(self.pg_path),
            "redis_backup_path": str(self.redis_path),
            "pg_backup_size": self.pg_size,
            "redis_backup_size": self.redis_size,
            "pg_backup_sha256": self.pg_sha256,
            "redis_backup_sha256": self.redis_sha256,
        }


def _regular_path(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BackupPairInvalid(f"{label} is missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise BackupPairInvalid(f"{label} is not a regular file")
    return metadata


def _read_marker(path: Path) -> dict[str, Any]:
    metadata = _regular_path(path, "backup pair marker")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_MARKER_BYTES:
        raise BackupPairInvalid("backup pair marker size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BackupPairInvalid("backup pair marker cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
        ):
            raise BackupPairInvalid("backup pair marker changed while opening")
        raw = os.read(descriptor, _MAX_MARKER_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) != metadata.st_size:
        raise BackupPairInvalid("backup pair marker changed while reading")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupPairInvalid("backup pair marker is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise BackupPairInvalid("backup pair marker must be an object")
    return payload


def _positive_size(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BackupPairInvalid(f"{label} is invalid")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise BackupPairInvalid(f"{label} is invalid")
    return value


def _digest(path: Path, expected_size: int, label: str) -> str:
    metadata = _regular_path(path, label)
    if metadata.st_size != expected_size:
        raise BackupPairInvalid(f"{label} size does not match committed marker")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BackupPairInvalid(f"{label} cannot be opened safely") from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
        ):
            raise BackupPairInvalid(f"{label} changed while opening")
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def backup_pair_paths(backup_root: Path, timestamp: str) -> tuple[Path, Path, Path]:
    if not TIMESTAMP_RE.fullmatch(timestamp):
        raise BackupPairInvalid("backup pair timestamp is invalid")
    root = backup_root.resolve()
    return (
        root / "pg" / f"{timestamp}.pg.dump.gz",
        root / "redis" / f"{timestamp}.redis.tgz",
        root / f".backup-pair.{timestamp}.json",
    )


def validate_backup_pair(backup_root: Path, timestamp: str) -> BackupPairBinding:
    """Return a binding only after marker identity and both payload hashes match."""
    if not backup_root.is_absolute():
        raise BackupPairInvalid("backup root must be absolute")
    root = backup_root.resolve()
    pg_path, redis_path, marker_path = backup_pair_paths(root, timestamp)
    marker = _read_marker(marker_path)
    if marker.get("schema") != 1 or marker.get("timestamp") != timestamp:
        raise BackupPairInvalid("backup pair marker identity is invalid")
    operation_id = marker.get("operation_id")
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or len(operation_id) > 240
        or any(char in operation_id for char in "\x00\r\n")
    ):
        raise BackupPairInvalid("backup pair operation identity is invalid")
    pg_payload = marker.get("pg")
    redis_payload = marker.get("redis")
    if not isinstance(pg_payload, dict) or not isinstance(redis_payload, dict):
        raise BackupPairInvalid("backup pair marker payload is invalid")
    if pg_payload.get("name") != pg_path.name or redis_payload.get("name") != redis_path.name:
        raise BackupPairInvalid("backup pair marker paths do not match timestamp")
    pg_size = _positive_size(pg_payload.get("size"), "pg_size")
    redis_size = _positive_size(redis_payload.get("size"), "redis_size")
    pg_sha256 = _sha256(pg_payload.get("sha256"), "pg_sha256")
    redis_sha256 = _sha256(redis_payload.get("sha256"), "redis_sha256")
    if _digest(pg_path, pg_size, "postgres backup") != pg_sha256:
        raise BackupPairInvalid("postgres backup hash does not match marker")
    if _digest(redis_path, redis_size, "redis backup") != redis_sha256:
        raise BackupPairInvalid("redis backup hash does not match marker")
    return BackupPairBinding(
        timestamp=timestamp,
        operation_id=operation_id,
        marker_path=marker_path,
        pg_path=pg_path,
        redis_path=redis_path,
        pg_size=pg_size,
        redis_size=redis_size,
        pg_sha256=pg_sha256,
        redis_sha256=redis_sha256,
    )


__all__ = [
    "BackupPairBinding",
    "BackupPairInvalid",
    "TIMESTAMP_RE",
    "backup_pair_paths",
    "validate_backup_pair",
]
