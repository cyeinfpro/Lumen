"""Backup catalog parsing and paired-file validation."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel, Field

from lumen_core.backup_integrity import (
    BackupPairBinding,
    BackupPairInvalid,
    TIMESTAMP_RE as COMMITTED_TIMESTAMP_RE,
    validate_backup_pair,
    validate_backup_pair_async,
    validate_backup_pair_metadata,
)


TIMESTAMP_RE = COMMITTED_TIMESTAMP_RE
PAIR_MTIME_WINDOW_SEC = 600
_MAX_RECEIPT_BYTES = 64 * 1024


class BackupPair(NamedTuple):
    pg: Path
    redis: Path
    pg_stat: os.stat_result
    redis_stat: os.stat_result


class BackupItem(BaseModel):
    timestamp: str
    created_at: datetime
    pg_size: int
    redis_size: int
    mtime_skew_sec: int
    consistent: bool


class BackupListOut(BaseModel):
    items: list[BackupItem]
    total: int


class RestoreIn(BaseModel):
    timestamp: str = Field(min_length=15, max_length=15, pattern=r"^[0-9]{8}-[0-9]{6}$")


def parse_ts(name: str, suffix: str) -> str | None:
    if not name.endswith(suffix):
        return None
    stem = name[: -len(suffix)]
    if not TIMESTAMP_RE.fullmatch(stem):
        return None
    return stem


def resolved_backup_dir(backup_root: Path, name: str) -> Path:
    directory = (backup_root / name).resolve(strict=True)
    directory.relative_to(backup_root)
    if not directory.is_dir():
        raise ValueError(f"{name} backup path is not a directory")
    return directory


def regular_file_lstat(path: Path) -> os.stat_result:
    file_stat = path.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"{path.name} is not a regular backup file")
    return file_stat


def backup_pair_for_timestamp(backup_root: Path, timestamp: str) -> BackupPair:
    try:
        binding = validate_backup_pair(backup_root, timestamp)
    except BackupPairInvalid as exc:
        if "missing" in str(exc):
            raise FileNotFoundError(str(exc)) from exc
        raise
    return _backup_pair_from_binding(backup_root, binding)


async def backup_pair_for_timestamp_async(
    backup_root: Path,
    timestamp: str,
) -> BackupPair:
    try:
        binding = await validate_backup_pair_async(backup_root, timestamp)
    except BackupPairInvalid as exc:
        if "missing" in str(exc):
            raise FileNotFoundError(str(exc)) from exc
        raise
    return _backup_pair_from_binding(backup_root, binding)


def _backup_pair_from_binding(
    backup_root: Path,
    binding: BackupPairBinding,
) -> BackupPair:
    pg_dir = resolved_backup_dir(backup_root, "pg")
    redis_dir = resolved_backup_dir(backup_root, "redis")
    pg = binding.pg_path
    redis = binding.redis_path
    pg.resolve(strict=True).relative_to(pg_dir)
    redis.resolve(strict=True).relative_to(redis_dir)

    return BackupPair(
        pg=pg,
        redis=redis,
        pg_stat=regular_file_lstat(pg),
        redis_stat=regular_file_lstat(redis),
    )


def find_backup_pair_metadata_for_operation(
    backup_root: Path,
    operation_id: str,
    started_at: datetime,
) -> BackupPairBinding | None:
    if not backup_root.is_absolute() or not backup_root.is_dir():
        return None
    receipt_path = backup_root / ".backup.last-success.json"
    try:
        receipt_stat = regular_file_lstat(receipt_path)
        if receipt_stat.st_size <= 0 or receipt_stat.st_size > _MAX_RECEIPT_BYTES:
            return None
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(receipt, dict) or receipt.get("operation_id") != operation_id:
        return None
    marker_name = receipt.get("pair_marker")
    if not isinstance(marker_name, str):
        return None
    timestamp = marker_name.removeprefix(".backup-pair.").removesuffix(".json")
    if (
        marker_name != f".backup-pair.{timestamp}.json"
        or not TIMESTAMP_RE.fullmatch(timestamp)
        or receipt.get("completed_at") != timestamp
    ):
        return None
    started_second = started_at.astimezone(timezone.utc).replace(microsecond=0)
    try:
        created_at = datetime.strptime(timestamp, "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc
        )
        if created_at < started_second:
            return None
        binding = validate_backup_pair_metadata(backup_root, timestamp)
    except (BackupPairInvalid, OSError, ValueError):
        return None
    if binding.operation_id != operation_id:
        return None
    return binding


def list_backup_items(backup_root: Path) -> BackupListOut:
    if not backup_root.is_absolute() or not backup_root.is_dir():
        return BackupListOut(items=[], total=0)

    items: list[BackupItem] = []
    for marker_path in sorted(
        backup_root.glob(".backup-pair.*.json"),
        reverse=True,
    ):
        timestamp = marker_path.name.removeprefix(".backup-pair.").removesuffix(".json")
        if not TIMESTAMP_RE.fullmatch(timestamp):
            continue
        try:
            created_at = datetime.strptime(timestamp, "%Y%m%d-%H%M%S").replace(
                tzinfo=timezone.utc
            )
            binding = validate_backup_pair_metadata(backup_root, timestamp)
            pg_stat = regular_file_lstat(binding.pg_path)
            redis_stat = regular_file_lstat(binding.redis_path)
        except (BackupPairInvalid, OSError, ValueError):
            continue
        skew = int(abs(pg_stat.st_mtime - redis_stat.st_mtime))
        items.append(
            BackupItem(
                timestamp=timestamp,
                created_at=created_at,
                pg_size=binding.pg_size,
                redis_size=binding.redis_size,
                mtime_skew_sec=skew,
                consistent=True,
            )
        )
    return BackupListOut(items=items, total=len(items))
