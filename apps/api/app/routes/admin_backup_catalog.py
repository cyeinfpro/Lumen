"""Backup catalog parsing and paired-file validation."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel, Field

from lumen_core.backup_integrity import (
    BackupPairInvalid,
    TIMESTAMP_RE as COMMITTED_TIMESTAMP_RE,
    validate_backup_pair,
)


TIMESTAMP_RE = COMMITTED_TIMESTAMP_RE
PAIR_MTIME_WINDOW_SEC = 600


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


def list_backup_items(backup_root: Path) -> BackupListOut:
    pg_dir = backup_root / "pg"
    redis_dir = backup_root / "redis"
    if not pg_dir.is_dir() or not redis_dir.is_dir():
        return BackupListOut(items=[], total=0)

    pg_map: dict[str, tuple[int, float]] = {}
    for path in pg_dir.iterdir():
        timestamp = parse_ts(path.name, ".pg.dump.gz")
        if timestamp is None:
            continue
        try:
            file_stat = regular_file_lstat(path)
            pg_map[timestamp] = (file_stat.st_size, file_stat.st_mtime)
        except (OSError, ValueError):
            continue

    redis_map: dict[str, tuple[int, float]] = {}
    for path in redis_dir.iterdir():
        timestamp = parse_ts(path.name, ".redis.tgz")
        if timestamp is None:
            continue
        try:
            file_stat = regular_file_lstat(path)
            redis_map[timestamp] = (file_stat.st_size, file_stat.st_mtime)
        except (OSError, ValueError):
            continue

    items: list[BackupItem] = []
    for timestamp in sorted(set(pg_map) & set(redis_map), reverse=True):
        try:
            created_at = datetime.strptime(timestamp, "%Y%m%d-%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        try:
            binding = validate_backup_pair(backup_root, timestamp)
        except (BackupPairInvalid, OSError, ValueError):
            # Same-timestamp files without a durable pair marker are not a
            # restore point and must not appear as a valid catalog item.
            continue
        pg_size, pg_mtime = binding.pg_size, binding.pg_path.stat().st_mtime
        redis_size, redis_mtime = (
            binding.redis_size,
            binding.redis_path.stat().st_mtime,
        )
        skew = int(abs(pg_mtime - redis_mtime))
        items.append(
            BackupItem(
                timestamp=timestamp,
                created_at=created_at,
                pg_size=pg_size,
                redis_size=redis_size,
                mtime_skew_sec=skew,
                consistent=True,
            )
        )
    return BackupListOut(items=items, total=len(items))
