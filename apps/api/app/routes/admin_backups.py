"""Admin 备份与恢复路由。

端点：
- GET  /admin/backups           → 列所有配对备份点（PG + Redis 同时存在的 timestamp）
- POST /admin/backups/now       → 立即触发一次备份（同步，几秒）
- POST /admin/backups/restore   → 异步触发恢复脚本；API 自身随 worker 一起被重启。

恢复是破坏性操作，要求 admin 且带 CSRF。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, NamedTuple, TextIO

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..config import settings
from ..deps import AdminUser, verify_csrf
from ..services.system_lock import LockBusy, SystemOperationLockService
from ._admin_common import admin_http as _http, write_admin_audit_isolated


router = APIRouter(prefix="/admin/backups", tags=["admin"])


# YYYYMMDD-HHMMSS 严格格式：8 位日期 + 短横线 + 6 位时间。
_TIMESTAMP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")
# 备份点配对一致性窗口：PG 和 Redis 文件 mtime 偏差应 ≤ 该秒数。
_PAIR_MTIME_WINDOW_SEC = 600
_BACKUP_TIMEOUT_SECONDS = 180
_BACKUP_TRIGGER_START_TIMEOUT_SECONDS = 15
_MAINTENANCE_MARKER_STALE_AFTER_SECONDS = 24 * 60 * 60
_BACKUP_TRIGGER_NAME = ".backup.trigger"
_BACKUP_LOG_NAME = ".backup.log"
_BACKUP_RUNNING_MARKER = ".backup.running"
_RESTORE_RUNNING_MARKER = ".restore.running"
_RESTORE_TRIGGER_NAME = ".restore.trigger"
_RESTORE_LOG_NAME = ".restore.log"
_RESTORE_RUNNER_UNIT = "lumen-restore-runner.service"
_UPDATE_RUNNING_MARKER = ".update.running"


class _ScriptResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class _BackupPair(NamedTuple):
    pg: Path
    redis: Path
    pg_stat: os.stat_result
    redis_stat: os.stat_result


def _chmod_tolerate_eperm(path: Path | str, mode: int) -> None:
    """chmod that swallows EPERM from squashing mounts (CIFS/NFS).

    Production /opt/lumendata is commonly mounted CIFS with
    ``forceuid,forcegid,uid=...,gid=...,file_mode=0664``. The mount option
    pins the on-wire mode and uid; every chmod from any caller — even the
    file's apparent local owner — returns EPERM because the CIFS server
    doesn't accept the mode change. The mount itself already enforces
    file_mode, so our redundant chmod is purely defensive on local fs.
    Any other OSError still propagates so genuine faults (ENOSPC, EBADF,
    EROFS, ...) keep failing fast.
    """
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass


def _open_private_append(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except PermissionError:
            # Same EPERM-on-squashed-mount story as _chmod_tolerate_eperm; see
            # there for the full rationale. Kept inline because os.fchmod takes
            # a fd, not a path, so the helper signature doesn't fit.
            pass
        return os.fdopen(fd, "a", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def _backup_root() -> Path:
    return Path(settings.backup_root).expanduser()


def _maintenance_marker_path(name: str) -> Path:
    return _backup_root() / name


def _backup_trigger_path() -> Path:
    return _backup_root() / _BACKUP_TRIGGER_NAME


def _backup_log_path() -> Path:
    return _backup_root() / _BACKUP_LOG_NAME


def _backup_trigger_only_mode() -> bool:
    return os.environ.get("LUMEN_BACKUP_VIA_TRIGGER", "").strip() == "1"


def _restore_trigger_only_mode() -> bool:
    return os.environ.get("LUMEN_RESTORE_VIA_TRIGGER", "").strip() == "1"


def _restore_trigger_path() -> Path:
    return _backup_root() / _RESTORE_TRIGGER_NAME


def _restore_log_path() -> Path:
    return _backup_root() / _RESTORE_LOG_NAME


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _marker_is_stale(started_at: str | None) -> bool:
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - started.astimezone(timezone.utc)
    return age.total_seconds() > _MAINTENANCE_MARKER_STALE_AFTER_SECONDS


def _read_pid_marker(path: Path) -> bool:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    pid = 0
    unit: str | None = None
    started_at: str | None = None
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "pid":
            try:
                pid = int(value)
            except ValueError:
                pid = 0
        elif key == "started_at":
            started_at = value.strip() or None
        elif key == "unit":
            unit = value.strip() or None
    if unit and not _marker_is_stale(started_at):
        return True
    if pid and _pid_is_running(pid) and not _marker_is_stale(started_at):
        return True
    try:
        path.unlink()
    except OSError:
        pass
    return False


def _write_pid_marker(
    path: Path,
    pid: int,
    started_at: datetime,
    *,
    unit: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    lines = [f"pid={pid}", f"started_at={started_at.isoformat()}"]
    if unit:
        lines.append(f"unit={unit}")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _chmod_tolerate_eperm(tmp, 0o600)
    tmp.replace(path)


def _try_write_pid_marker(
    path: Path,
    pid: int,
    started_at: datetime,
    *,
    unit: str | None = None,
) -> bool:
    """Atomically create a maintenance marker.

    The Redis system lock is the cross-process guard in normal operation. When
    Redis is unavailable the service degrades to marker-file checks, so marker
    creation itself must be exclusive to close the local check/write race.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"pid={pid}", f"started_at={started_at.isoformat()}"]
    if unit:
        lines.append(f"unit={unit}")
    payload = ("\n".join(lines) + "\n").encode()
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if _read_pid_marker(path):
                return False
            try:
                path.unlink()
            except OSError:
                pass
            continue
        try:
            try:
                os.fchmod(fd, 0o600)
            except PermissionError:
                pass
            os.write(fd, payload)
            return True
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
    return False


def _unlink_marker(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _maintenance_marker_busy() -> bool:
    return any(
        _read_pid_marker(_maintenance_marker_path(name))
        for name in (
            _UPDATE_RUNNING_MARKER,
            _BACKUP_RUNNING_MARKER,
            _RESTORE_RUNNING_MARKER,
        )
    )


def _discover_scripts_dir() -> Path:
    configured = settings.lumen_scripts_dir.strip()
    if configured:
        return Path(configured).expanduser()

    candidates: list[Path] = [Path.cwd() / "scripts"]
    for parent in Path(__file__).resolve().parents:
        candidates.append(parent / "scripts")
    for candidate in candidates:
        if (candidate / "backup.sh").is_file() and (candidate / "restore.sh").is_file():
            return candidate
    return Path("/opt/lumen/scripts")


def _backup_script() -> Path:
    return _discover_scripts_dir() / "backup.sh"


def _restore_script() -> Path:
    return _discover_scripts_dir() / "restore.sh"


async def _run_script(
    script: Path,
    *args: str,
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> _ScriptResult:
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/env",
        "bash",
        str(script),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=proc_env,
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        raise
    return _ScriptResult(
        returncode=int(proc.returncode or 0),
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )


# ---- Schemas ----

class BackupItem(BaseModel):
    timestamp: str  # e.g. 20260424-123000
    created_at: datetime
    pg_size: int
    redis_size: int
    # 跨源一致性：两个文件 mtime 的偏差秒数；超出窗口则 consistent=False。
    mtime_skew_sec: int
    consistent: bool


class BackupListOut(BaseModel):
    items: list[BackupItem]
    total: int


class RestoreIn(BaseModel):
    timestamp: str = Field(min_length=15, max_length=15, pattern=r"^[0-9]{8}-[0-9]{6}$")


# ---- Listing ----

def _parse_ts(name: str, suffix: str) -> str | None:
    """'20260424-123000.pg.dump.gz' → '20260424-123000'; 不符合返回 None。"""
    if not name.endswith(suffix):
        return None
    stem = name[: -len(suffix)]
    if not _TIMESTAMP_RE.fullmatch(stem):
        return None
    return stem


def _resolved_backup_dir(backup_root: Path, name: str) -> Path:
    directory = (backup_root / name).resolve(strict=True)
    directory.relative_to(backup_root)
    if not directory.is_dir():
        raise ValueError(f"{name} backup path is not a directory")
    return directory


def _regular_file_lstat(path: Path) -> os.stat_result:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"{path.name} is not a regular backup file")
    return st


def _backup_pair_for_timestamp(backup_root: Path, ts: str) -> _BackupPair:
    pg_dir = _resolved_backup_dir(backup_root, "pg")
    redis_dir = _resolved_backup_dir(backup_root, "redis")
    pg = pg_dir / f"{ts}.pg.dump.gz"
    rd = redis_dir / f"{ts}.redis.tgz"

    pg.resolve(strict=True).relative_to(pg_dir)
    rd.resolve(strict=True).relative_to(redis_dir)

    pg_stat = _regular_file_lstat(pg)
    redis_stat = _regular_file_lstat(rd)
    return _BackupPair(pg=pg, redis=rd, pg_stat=pg_stat, redis_stat=redis_stat)


@router.get("", response_model=BackupListOut)
async def list_backups(_admin: AdminUser) -> BackupListOut:
    backup_root = _backup_root()
    pg_dir = backup_root / "pg"
    redis_dir = backup_root / "redis"
    if not pg_dir.is_dir() or not redis_dir.is_dir():
        return BackupListOut(items=[], total=0)

    pg_map: dict[str, tuple[int, float]] = {}
    for p in pg_dir.iterdir():
        ts = _parse_ts(p.name, ".pg.dump.gz")
        if ts is None:
            continue
        try:
            st = _regular_file_lstat(p)
            pg_map[ts] = (st.st_size, st.st_mtime)
        except (OSError, ValueError):
            continue

    redis_map: dict[str, tuple[int, float]] = {}
    for p in redis_dir.iterdir():
        ts = _parse_ts(p.name, ".redis.tgz")
        if ts is None:
            continue
        try:
            st = _regular_file_lstat(p)
            redis_map[ts] = (st.st_size, st.st_mtime)
        except (OSError, ValueError):
            continue

    # 只返回配对成功的
    paired = sorted(set(pg_map) & set(redis_map), reverse=True)
    items: list[BackupItem] = []
    for ts in paired:
        # 从文件名反解时间（UTC）；格式 YYYYMMDD-HHMMSS
        try:
            dt = datetime.strptime(ts, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        pg_size, pg_mtime = pg_map[ts]
        redis_size, redis_mtime = redis_map[ts]
        skew = int(abs(pg_mtime - redis_mtime))
        items.append(
            BackupItem(
                timestamp=ts,
                created_at=dt,
                pg_size=pg_size,
                redis_size=redis_size,
                mtime_skew_sec=skew,
                consistent=skew <= _PAIR_MTIME_WINDOW_SEC,
            )
        )
    return BackupListOut(items=items, total=len(items))


# ---- Trigger backup now ----

class BackupNowOut(BaseModel):
    timestamp: str | None = None
    ok: bool
    stderr_tail: str | None = None


async def _find_latest_paired_backup_after(started_at: datetime) -> str | None:
    backup_root = _backup_root()
    pg_dir = backup_root / "pg"
    redis_dir = backup_root / "redis"
    if not pg_dir.is_dir() or not redis_dir.is_dir():
        return None
    started_ts = started_at.timestamp() - 2
    candidates: list[tuple[float, str]] = []
    for p in pg_dir.iterdir():
        ts = _parse_ts(p.name, ".pg.dump.gz")
        if ts is None:
            continue
        redis_file = redis_dir / f"{ts}.redis.tgz"
        if not redis_file.is_file():
            continue
        try:
            pg_stat = p.stat()
            redis_stat = redis_file.stat()
        except OSError:
            continue
        newest_mtime = max(pg_stat.st_mtime, redis_stat.st_mtime)
        if newest_mtime >= started_ts:
            candidates.append((newest_mtime, ts))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


async def _wait_for_log_append(
    path: Path,
    *,
    initial_size: int,
    timeout_sec: float,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    while loop.time() < deadline:
        try:
            if path.stat().st_size > initial_size:
                return True
        except OSError:
            pass
        await asyncio.sleep(0.25)
    return False


async def _wait_for_latest_paired_backup_after(
    started_at: datetime,
    *,
    timeout_sec: float,
) -> str | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    while loop.time() < deadline:
        ts = await _find_latest_paired_backup_after(started_at)
        if ts is not None:
            return ts
        await asyncio.sleep(0.5)
    return await _find_latest_paired_backup_after(started_at)


def _timestamp_from_backup_stdout(stdout: str, started_at: datetime) -> str | None:
    for line in reversed((stdout or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                ts = payload.get("timestamp")
                if isinstance(ts, str) and _TIMESTAMP_RE.fullmatch(ts):
                    return ts
        if "complete" in line and "backup " in line:
            parts = line.split()
            # 形如 "[backup ...] backup 20260424-123000 complete"
            for i, token in enumerate(parts):
                if token == "backup" and i + 1 < len(parts):
                    ts = parts[i + 1].rstrip(":")
                    if _TIMESTAMP_RE.fullmatch(ts):
                        return ts
        if "complete" in line.lower():
            match = re.search(r"\b([0-9]{8}-[0-9]{6})\b", line)
            if match:
                return match.group(1)
    return None


def _backup_script_was_skipped(output: str) -> bool:
    lowered = (output or "").lower()
    return "skipped:" in lowered and (
        "maintenance lock" in lowered or "already running" in lowered
    )


def _write_backup_trigger(path: Path, started_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(started_at.isoformat() + "\n", encoding="utf-8")
    _chmod_tolerate_eperm(tmp, 0o600)
    tmp.replace(path)


def _write_restore_trigger(path: Path, timestamp: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(timestamp + "\n", encoding="utf-8")
    _chmod_tolerate_eperm(tmp, 0o600)
    tmp.replace(path)


@router.post("/now", response_model=BackupNowOut, dependencies=[Depends(verify_csrf)])
async def backup_now(request: Request, admin: AdminUser) -> BackupNowOut:
    backup_script = _backup_script()
    if not backup_script.is_file():
        raise _http("script_missing", f"missing {backup_script}", 500)
    lock_service = SystemOperationLockService(
        fallback_busy=_maintenance_marker_busy,
    )
    try:
        lock = await lock_service.acquire(
            operation="backup", owner=str(admin.id), ttl_sec=_BACKUP_TIMEOUT_SECONDS + 30
        )
    except LockBusy:
        raise _http(
            "maintenance_busy",
            "another maintenance operation is running",
            409,
        )
    marker = _maintenance_marker_path(_BACKUP_RUNNING_MARKER)
    if not _try_write_pid_marker(marker, os.getpid(), datetime.now(timezone.utc)):
        await lock_service.release(lock, succeeded=False, reason="maintenance_busy")
        raise _http(
            "maintenance_busy",
            "another maintenance operation is running",
            409,
        )
    succeeded = False
    release_reason = "backup_failed"
    started_at = datetime.now(timezone.utc)
    proc: _ScriptResult | None = None
    ts: str | None = None
    try:
        if _backup_trigger_only_mode():
            backup_root = _backup_root()
            backup_root.mkdir(parents=True, exist_ok=True)
            log_path = _backup_log_path()
            try:
                initial_log_size = log_path.stat().st_size
            except OSError:
                initial_log_size = 0
            trigger_path = _backup_trigger_path()
            _write_backup_trigger(trigger_path, started_at)
            if not await _wait_for_log_append(
                log_path,
                initial_size=initial_log_size,
                timeout_sec=_BACKUP_TRIGGER_START_TIMEOUT_SECONDS,
            ):
                release_reason = "backup_trigger_not_started"
                _unlink_marker(trigger_path)
                raise _http(
                    "backup_trigger_not_started",
                    "backup trigger was written, but host backup service did not start",
                    504,
                )
            ts = await _wait_for_latest_paired_backup_after(
                started_at,
                timeout_sec=max(
                    1,
                    _BACKUP_TIMEOUT_SECONDS - _BACKUP_TRIGGER_START_TIMEOUT_SECONDS,
                ),
            )
            if ts is None:
                release_reason = "backup_timeout"
                raise _http(
                    "backup_timeout",
                    f"backup exceeded {_BACKUP_TIMEOUT_SECONDS}s",
                    504,
                )
        else:
            backup_root = _backup_root()
            proc = await _run_script(
                backup_script,
                timeout=_BACKUP_TIMEOUT_SECONDS,
                env={
                    "BACKUP_ROOT": str(backup_root),
                    "LUMEN_BACKUP_ROOT": str(backup_root),
                    # Manual backups are already guarded by the API operation
                    # lock and marker files. In the containerized API path
                    # /opt/lumen is mounted read-only, so backup.sh cannot take
                    # the host maintenance lock and would otherwise exit 0 as
                    # "skipped".
                    "LUMEN_BACKUP_FORCE": "1",
                },
            )
    except TimeoutError:
        release_reason = "backup_timeout"
        _unlink_marker(marker)
        await lock_service.release(lock, succeeded=False, reason=release_reason)
        raise _http(
            "backup_timeout",
            f"backup exceeded {_BACKUP_TIMEOUT_SECONDS}s",
            504,
        )
    except Exception:
        _unlink_marker(marker)
        await lock_service.release(lock, succeeded=False, reason=release_reason)
        raise
    _unlink_marker(marker)

    if proc is not None and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1000:]
        await write_admin_audit_isolated(
            request,
            admin,
            event_type="admin.backup.create.fail",
            details={"returncode": proc.returncode},
        )
        await lock_service.release(lock, succeeded=False, reason=release_reason)
        return BackupNowOut(ok=False, stderr_tail=tail)

    if proc is not None and ts is None:
        output = f"{proc.stdout}\n{proc.stderr}"
        if _backup_script_was_skipped(output):
            _unlink_marker(marker)
            await write_admin_audit_isolated(
                request,
                admin,
                event_type="admin.backup.create.skipped",
                details={"reason": "backup_skipped"},
            )
            await lock_service.release(
                lock, succeeded=False, reason="backup_skipped"
            )
            raise _http(
                "backup_skipped",
                "backup was skipped because another maintenance operation is running",
                409,
            )
        ts = _timestamp_from_backup_stdout(output, started_at)
    if ts is None:
        ts = await _find_latest_paired_backup_after(started_at)
    if ts is None:
        await lock_service.release(
            lock, succeeded=False, reason="backup_timestamp_missing"
        )
        raise _http(
            "backup_timestamp_missing",
            "backup completed but timestamp was not found",
            500,
        )
    succeeded = True
    release_reason = "backup_complete"
    await write_admin_audit_isolated(
        request,
        admin,
        event_type="admin.backup.create",
        details={"timestamp": ts},
    )
    await lock_service.release(lock, succeeded=succeeded, reason=release_reason)
    return BackupNowOut(ok=True, timestamp=ts)


# ---- Restore ----

class RestoreOut(BaseModel):
    accepted: bool
    timestamp: str
    note: str


@router.post("/restore", response_model=RestoreOut, dependencies=[Depends(verify_csrf)])
async def restore_backup(
    body: RestoreIn, request: Request, admin: AdminUser
) -> RestoreOut:
    lock_service = SystemOperationLockService(
        fallback_busy=_maintenance_marker_busy,
    )
    try:
        lock = await lock_service.acquire(
            operation="restore", owner=str(admin.id), ttl_sec=300
        )
    except LockBusy:
        raise _http(
            "maintenance_busy",
            "another maintenance operation is running",
            409,
        )
    restore_script = _restore_script()
    if not restore_script.is_file():
        await lock_service.release(lock, succeeded=False, reason="script_missing")
        raise _http("script_missing", f"missing {restore_script}", 500)

    ts = body.timestamp.strip()
    if not _TIMESTAMP_RE.fullmatch(ts):
        await lock_service.release(lock, succeeded=False, reason="invalid_timestamp")
        raise _http("invalid_timestamp", "timestamp must match YYYYMMDD-HHMMSS", 400)

    backup_root = _backup_root().resolve()
    try:
        pair = _backup_pair_for_timestamp(backup_root, ts)
    except FileNotFoundError:
        await lock_service.release(lock, succeeded=False, reason="backup_not_found")
        raise _http("backup_not_found", f"no paired backup for {ts}", 404)
    except ValueError:
        await lock_service.release(lock, succeeded=False, reason="invalid_path")
        raise _http("invalid_path", "backup path escapes root or is not regular", 400)
    except OSError as exc:
        await lock_service.release(lock, succeeded=False, reason="backup_stat_failed")
        raise _http("backup_stat_failed", f"cannot stat backup files: {exc}", 500)

    skew = int(abs(pair.pg_stat.st_mtime - pair.redis_stat.st_mtime))
    if skew > _PAIR_MTIME_WINDOW_SEC:
        await lock_service.release(lock, succeeded=False, reason="backup_inconsistent")
        raise _http(
            "backup_inconsistent",
            f"PG/Redis mtime skew {skew}s exceeds {_PAIR_MTIME_WINDOW_SEC}s window",
            409,
        )

    marker = _maintenance_marker_path(_RESTORE_RUNNING_MARKER)
    started_at = datetime.now(timezone.utc)
    if _restore_trigger_only_mode():
        if not _try_write_pid_marker(
            marker,
            0,
            started_at,
            unit=_RESTORE_RUNNER_UNIT,
        ):
            await lock_service.release(
                lock, succeeded=False, reason="maintenance_busy"
            )
            raise _http(
                "maintenance_busy",
                "another maintenance operation is running",
                409,
            )
        log_path = _restore_log_path()
        try:
            initial_log_size = log_path.stat().st_size
        except OSError:
            initial_log_size = 0
        trigger_path = _restore_trigger_path()
        _write_restore_trigger(trigger_path, ts)
        if not await _wait_for_log_append(
            log_path,
            initial_size=initial_log_size,
            timeout_sec=_BACKUP_TRIGGER_START_TIMEOUT_SECONDS,
        ):
            _unlink_marker(trigger_path)
            _unlink_marker(marker)
            await lock_service.release(
                lock, succeeded=False, reason="restore_trigger_not_started"
            )
            raise _http(
                "restore_trigger_not_started",
                "restore trigger was written, but host restore service did not start",
                504,
            )
        await lock_service.release(lock, succeeded=True, reason="restore_launched")
        await write_admin_audit_isolated(
            request,
            admin,
            event_type="admin.backup.restore",
            details={"timestamp": ts, "unit": _RESTORE_RUNNER_UNIT},
        )
        return RestoreOut(
            accepted=True,
            timestamp=ts,
            note="恢复已由宿主机服务接管；完成前其他维护操作会被阻止",
        )

    if shutil.which("docker") is None:
        await lock_service.release(
            lock, succeeded=False, reason="restore_host_runner_required"
        )
        raise _http(
            "restore_host_runner_required",
            "restore requires the host path runner in containerized deployments",
            503,
        )

    # Non-container fallback: detach because restore stops API/worker itself.
    log_path = _restore_log_path()
    log_fh = _open_private_append(log_path)
    try:
        log_fh.write(
            f"\n=== restore trigger ts={ts} "
            f"at {datetime.now(timezone.utc).isoformat()} ===\n"
        )
        log_fh.flush()
        proc = subprocess.Popen(
            ["/usr/bin/env", "bash", str(restore_script), ts],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        _write_pid_marker(
            marker,
            proc.pid,
            started_at,
        )
    except Exception:
        await lock_service.release(lock, succeeded=False, reason="restore_launch_failed")
        raise
    finally:
        log_fh.close()
    await lock_service.release(lock, succeeded=True, reason="restore_launched")
    await write_admin_audit_isolated(
        request,
        admin,
        event_type="admin.backup.restore",
        details={"timestamp": ts},
    )
    return RestoreOut(
        accepted=True,
        timestamp=ts,
        note="恢复已触发；服务会短暂不可用，约 30-60 秒后重新登录验证",
    )
