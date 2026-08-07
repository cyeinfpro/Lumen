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
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..config import settings
from ..deps import AdminUser, verify_csrf
from ..services.system_lock import LockBusy, SystemOperationLockService
from ._admin_common import admin_http as _http, write_admin_audit_isolated
from . import admin_backup_catalog as _backup_catalog
from .admin_backup_fs import (
    ScriptResult as _ScriptResult,
    chmod_tolerate_eperm as _chmod_tolerate_eperm,
    open_private_append as _open_private_append,
)
from .admin_maintenance_marker_lock import (
    MAINTENANCE_MARKER_NAMES,
    maintenance_marker_lock,
)

router = APIRouter(prefix="/admin/backups", tags=["admin"])

# YYYYMMDD-HHMMSS 严格格式：8 位日期 + 短横线 + 6 位时间。
_TIMESTAMP_RE = _backup_catalog.TIMESTAMP_RE
# 备份点配对一致性窗口：PG 和 Redis 文件 mtime 偏差应 ≤ 该秒数。
_PAIR_MTIME_WINDOW_SEC = _backup_catalog.PAIR_MTIME_WINDOW_SEC
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

_BackupPair = _backup_catalog.BackupPair


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


def _kill_launched_script(proc: subprocess.Popen[bytes]) -> None:
    """Abort a freshly-spawned restore script after losing the marker claim.

    The script runs in its own session (start_new_session=True), so
    SIGTERM to the group reaches the script as well as its bash wrapper;
    escalate to SIGKILL if it does not exit within 5s.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


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


def _read_pid_marker_unlocked(path: Path) -> bool:
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


def _marker_claim(path: Path) -> tuple[str | None, int | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None, None, None
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value.strip()
    try:
        generation = int(values.get("generation", "0"))
    except ValueError:
        generation = None
    return values.get("owner"), generation, values.get("operation_id")


def _marker_is_adopted(path: Path, operation_id: str) -> bool:
    with maintenance_marker_lock(path.parent):
        owner, generation, marker_operation_id = _marker_claim(path)
    return (
        marker_operation_id == operation_id
        and owner == "host"
        and generation is not None
        and generation >= 1
    )


def _read_pid_marker(path: Path) -> bool:
    with maintenance_marker_lock(path.parent):
        return _read_pid_marker_unlocked(path)


def _try_write_pid_marker(
    path: Path,
    pid: int,
    started_at: datetime,
    *,
    unit: str | None = None,
    operation_id: str | None = None,
    owner: str = "api",
    generation: int = 0,
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
    if operation_id:
        lines.append(f"operation_id={operation_id}")
        lines.append(f"owner={owner}")
        lines.append(f"generation={generation}")
    payload = ("\n".join(lines) + "\n").encode()
    with maintenance_marker_lock(path.parent):
        for name in MAINTENANCE_MARKER_NAMES:
            if _read_pid_marker_unlocked(path.parent / name):
                return False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
        except FileExistsError:
            return False
        try:
            try:
                os.fchmod(fd, 0o660)
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


def _unlink_marker(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _unlink_owned_marker(path: Path, operation_id: str) -> None:
    with maintenance_marker_lock(path.parent):
        owner, generation, marker_operation_id = _marker_claim(path)
        if (
            marker_operation_id == operation_id
            and owner == "api"
            and generation == 0
        ):
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


_ALLOWED_SCRIPT_NAMES = frozenset({"backup.sh", "restore.sh"})


def _resolved_script(name: str) -> Path:
    """Resolve an allowlisted script under the configured scripts dir.

    ``lumen_scripts_dir`` comes from settings rather than a request, but a
    misconfigured or symlinked value would otherwise let ``bash`` run whatever
    sits at that path. Pin the basename to the allowlist and require the
    resolved file to stay inside the resolved scripts dir.
    """
    if name not in _ALLOWED_SCRIPT_NAMES:
        raise _http("script_not_allowed", f"script {name} is not allowlisted", 500)
    base = _discover_scripts_dir().resolve()
    candidate = (base / name).resolve()
    if candidate.parent != base:
        raise _http("script_outside_root", f"{name} escapes {base}", 500)
    return candidate


def _backup_script() -> Path:
    return _resolved_script("backup.sh")


def _restore_script() -> Path:
    return _resolved_script("restore.sh")


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


BackupItem = _backup_catalog.BackupItem
BackupListOut = _backup_catalog.BackupListOut
RestoreIn = _backup_catalog.RestoreIn
_parse_ts = _backup_catalog.parse_ts
_resolved_backup_dir = _backup_catalog.resolved_backup_dir
_regular_file_lstat = _backup_catalog.regular_file_lstat
_backup_pair_for_timestamp = _backup_catalog.backup_pair_for_timestamp


@router.get("", response_model=BackupListOut)
async def list_backups(_admin: AdminUser) -> BackupListOut:
    return _backup_catalog.list_backup_items(_backup_root())


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
        try:
            binding = _backup_catalog.validate_backup_pair(backup_root, ts)
            pg_stat = binding.pg_path.stat()
            redis_stat = binding.redis_path.stat()
        except (OSError, ValueError):
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
    return ("skipped:" in lowered or "deferred:" in lowered) and (
        "maintenance lock" in lowered or "already running" in lowered
    )


def _write_backup_trigger(
    path: Path,
    started_at: datetime,
    operation_id: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(
        json.dumps(
            {
                "operation_id": operation_id,
                "owner": "api",
                "generation": 0,
                "started_at": started_at.isoformat(),
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
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
            operation="backup",
            owner=str(admin.id),
            ttl_sec=_BACKUP_TIMEOUT_SECONDS + 30,
        )
    except LockBusy:
        raise _http(
            "maintenance_busy",
            "another maintenance operation is running",
            409,
        )
    marker = _maintenance_marker_path(_BACKUP_RUNNING_MARKER)
    operation_id = f"backup-{uuid.uuid4().hex}"
    if not _try_write_pid_marker(
        marker,
        os.getpid(),
        datetime.now(timezone.utc),
        operation_id=operation_id,
    ):
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
    adopted = False
    try:
        trigger_mode = _backup_trigger_only_mode()
        if _backup_trigger_only_mode():
            backup_root = _backup_root()
            backup_root.mkdir(parents=True, exist_ok=True)
            trigger_path = _backup_trigger_path()
            _write_backup_trigger(trigger_path, started_at, operation_id)
            deadline = asyncio.get_running_loop().time() + (
                _BACKUP_TRIGGER_START_TIMEOUT_SECONDS
            )
            while asyncio.get_running_loop().time() < deadline:
                if _marker_is_adopted(marker, operation_id):
                    adopted = True
                    break
                await asyncio.sleep(0.25)
            if not adopted:
                release_reason = "backup_trigger_not_started"
                _unlink_marker(trigger_path)
                _unlink_owned_marker(marker, operation_id)
                raise _http(
                    "backup_trigger_not_started",
                    "backup trigger was written, but host backup service did not adopt it",
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
                    "LUMEN_BACKUP_OPERATION_ID": operation_id,
                },
            )
    except TimeoutError:
        release_reason = "backup_timeout"
        if not adopted:
            _unlink_owned_marker(marker, operation_id)
        await lock_service.release(lock, succeeded=False, reason=release_reason)
        raise _http(
            "backup_timeout",
            f"backup exceeded {_BACKUP_TIMEOUT_SECONDS}s",
            504,
        )
    except Exception:
        if not adopted:
            _unlink_owned_marker(marker, operation_id)
        await lock_service.release(lock, succeeded=False, reason=release_reason)
        raise
    if not trigger_mode:
        _unlink_marker(marker)

    output = f"{proc.stdout}\n{proc.stderr}" if proc is not None else ""
    if proc is not None and _backup_script_was_skipped(output):
        await write_admin_audit_isolated(
            request,
            admin,
            event_type="admin.backup.create.skipped",
            details={"reason": "backup_skipped"},
        )
        await lock_service.release(lock, succeeded=False, reason="backup_skipped")
        raise _http(
            "backup_skipped",
            "backup was skipped because another maintenance operation is running",
            409,
        )

    if proc is not None and proc.returncode != 0:
        release_reason = "backup_script_failed"
        tail = (proc.stderr or proc.stdout or "")[-1000:]
        await write_admin_audit_isolated(
            request,
            admin,
            event_type="admin.backup.create.fail",
            details={"returncode": proc.returncode, "stderr_tail": tail},
        )
        await lock_service.release(lock, succeeded=False, reason=release_reason)
        raise _http(
            "backup_script_failed",
            "backup process exited unsuccessfully",
            502,
            details={"returncode": proc.returncode, "stderr_tail": tail},
        )

    if proc is not None and ts is None:
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
        _backup_pair_for_timestamp(backup_root, ts)
    except FileNotFoundError:
        await lock_service.release(lock, succeeded=False, reason="backup_not_found")
        raise _http("backup_not_found", f"no paired backup for {ts}", 404)
    except ValueError:
        await lock_service.release(lock, succeeded=False, reason="invalid_path")
        raise _http("invalid_path", "backup path escapes root or is not regular", 400)
    except OSError as exc:
        await lock_service.release(lock, succeeded=False, reason="backup_stat_failed")
        raise _http("backup_stat_failed", f"cannot stat backup files: {exc}", 500)

    marker = _maintenance_marker_path(_RESTORE_RUNNING_MARKER)
    started_at = datetime.now(timezone.utc)
    operation_id = f"restore-{uuid.uuid4().hex}"
    adopted = False
    if _restore_trigger_only_mode():
        if not _try_write_pid_marker(
            marker,
            0,
            started_at,
            unit=_RESTORE_RUNNER_UNIT,
            operation_id=operation_id,
        ):
            await lock_service.release(lock, succeeded=False, reason="maintenance_busy")
            raise _http(
                "maintenance_busy",
                "another maintenance operation is running",
                409,
            )
        trigger_path = _restore_trigger_path()
        _write_restore_trigger(trigger_path, ts)
        deadline = asyncio.get_running_loop().time() + (
            _BACKUP_TRIGGER_START_TIMEOUT_SECONDS
        )
        while asyncio.get_running_loop().time() < deadline:
            if _marker_is_adopted(marker, operation_id):
                adopted = True
                break
            await asyncio.sleep(0.25)
        if not adopted:
            _unlink_marker(trigger_path)
            _unlink_owned_marker(marker, operation_id)
            await lock_service.release(
                lock, succeeded=False, reason="restore_trigger_not_started"
            )
            raise _http(
                "restore_trigger_not_started",
                "restore trigger was written, but host restore service did not adopt it",
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
        if not _try_write_pid_marker(
            marker,
            proc.pid,
            started_at,
            operation_id=operation_id,
        ):
            # Another maintenance op won the marker claim while the Redis
            # lock was degraded; never let a second restore.sh run.
            _kill_launched_script(proc)
            raise _http(
                "maintenance_busy",
                "another maintenance operation is running",
                409,
            )
    except Exception:
        await lock_service.release(
            lock, succeeded=False, reason="restore_launch_failed"
        )
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


chmod_tolerate_eperm = _chmod_tolerate_eperm
discover_scripts_dir = _discover_scripts_dir
maintenance_marker_busy = _maintenance_marker_busy
open_private_append = _open_private_append
