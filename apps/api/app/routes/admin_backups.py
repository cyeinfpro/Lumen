"""Admin 备份与恢复路由。

端点：
- GET  /admin/backups           → 列所有配对备份点（PG + Redis 同时存在的 timestamp）
- POST /admin/backups/now       → 立即触发一次备份（同步，几秒）
- POST /admin/backups/restore   → 异步触发恢复脚本；API 自身随 worker 一起被重启。

恢复是破坏性操作，要求 admin 且带 CSRF。
"""

from __future__ import annotations

import asyncio
import os
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
from ..services.system_lock import LockBusy, SystemLock, SystemOperationLockService
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
from . import admin_backup_runtime as _backup_runtime

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
_RESTORE_ADOPTION_RECEIPT_NAME = ".restore.adoption.json"
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
    return _backup_runtime.try_write_pid_marker(
        path,
        pid,
        started_at,
        unit=unit,
        operation_id=operation_id,
        owner=owner,
        generation=generation,
        marker_names=MAINTENANCE_MARKER_NAMES,
        marker_is_live=_read_pid_marker_unlocked,
        marker_lock=maintenance_marker_lock,
    )


def _unlink_marker(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _unlink_owned_marker(path: Path, operation_id: str) -> None:
    with maintenance_marker_lock(path.parent):
        owner, generation, marker_operation_id = _marker_claim(path)
        if marker_operation_id == operation_id and owner == "api" and generation == 0:
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
_backup_pair_for_timestamp_async = _backup_catalog.backup_pair_for_timestamp_async
_backup_script_was_skipped = _backup_runtime.backup_script_was_skipped
_timestamp_from_backup_stdout = _backup_runtime.timestamp_from_backup_stdout
_wait_for_log_append = _backup_runtime.wait_for_log_append
_write_backup_trigger = _backup_runtime.write_backup_trigger
_write_restore_trigger = _backup_runtime.write_restore_trigger
_restore_adoption_receipt_matches = _backup_runtime.restore_adoption_receipt_matches
_BackupAttemptTimeout = _backup_runtime.BackupAttemptTimeout
_BackupTriggerNotStarted = _backup_runtime.BackupTriggerNotStarted
_BackupAttemptRuntime = _backup_runtime.BackupAttemptRuntime
_handle_backup_process = _backup_runtime.handle_backup_process
_run_backup_attempt = _backup_runtime.run_backup_attempt


@router.get("", response_model=BackupListOut)
async def list_backups(_admin: AdminUser) -> BackupListOut:
    return _backup_catalog.list_backup_items(_backup_root())


# ---- Trigger backup now ----


class BackupNowOut(BaseModel):
    timestamp: str | None = None
    ok: bool
    stderr_tail: str | None = None


async def _validated_backup_timestamp_for_operation(
    timestamp: str,
    operation_id: str,
    started_at: datetime,
) -> str | None:
    backup_root = _backup_root()
    try:
        completed_at = datetime.strptime(timestamp, "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    if completed_at < started_at.astimezone(timezone.utc).replace(microsecond=0):
        return None
    try:
        binding = await _backup_catalog.validate_backup_pair_async(
            backup_root,
            timestamp,
        )
    except (OSError, ValueError):
        return None
    if binding.operation_id != operation_id:
        return None
    return timestamp


async def _find_paired_backup_for_operation(
    operation_id: str,
    started_at: datetime,
) -> str | None:
    binding = _backup_catalog.find_backup_pair_metadata_for_operation(
        _backup_root(),
        operation_id,
        started_at,
    )
    if binding is None:
        return None
    return await _validated_backup_timestamp_for_operation(
        binding.timestamp,
        operation_id,
        started_at,
    )


async def _wait_for_paired_backup_operation(
    operation_id: str,
    started_at: datetime,
    *,
    timeout_sec: float,
) -> str | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    while loop.time() < deadline:
        ts = await _find_paired_backup_for_operation(operation_id, started_at)
        if ts is not None:
            return ts
        await asyncio.sleep(0.5)
    return await _find_paired_backup_for_operation(operation_id, started_at)


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
    release_reason = "backup_failed"
    started_at = datetime.now(timezone.utc)
    proc: _ScriptResult | None = None
    ts: str | None = None
    adopted = False
    trigger_mode = _backup_trigger_only_mode()
    try:
        proc, ts, adopted = await _run_backup_attempt(
            _BackupAttemptRuntime(
                backup_script=backup_script,
                backup_root=_backup_root(),
                marker=marker,
                operation_id=operation_id,
                started_at=started_at,
                trigger_path=_backup_trigger_path(),
                trigger_timeout_seconds=_BACKUP_TRIGGER_START_TIMEOUT_SECONDS,
                timeout_seconds=_BACKUP_TIMEOUT_SECONDS,
                write_trigger=_write_backup_trigger,
                marker_is_adopted=_marker_is_adopted,
                unlink_marker=_unlink_marker,
                unlink_owned_marker=_unlink_owned_marker,
                wait_for_pair=_wait_for_paired_backup_operation,
                run_script=_run_script,
            ),
            trigger_mode=trigger_mode,
        )
    except _BackupTriggerNotStarted:
        release_reason = "backup_trigger_not_started"
        await lock_service.release(lock, succeeded=False, reason=release_reason)
        raise _http(
            "backup_trigger_not_started",
            "backup trigger was written, but host backup service did not adopt it",
            504,
        )
    except (_BackupAttemptTimeout, TimeoutError):
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

    output = await _handle_backup_process(
        process=proc,
        request=request,
        admin=admin,
        lock_service=lock_service,
        lock=lock,
        audit=write_admin_audit_isolated,
        http_error=_http,
    )

    if proc is not None and ts is None:
        ts = _timestamp_from_backup_stdout(output, started_at, operation_id)
    if ts is not None:
        ts = await _validated_backup_timestamp_for_operation(
            ts,
            operation_id,
            started_at,
        )
    if ts is None:
        ts = await _find_paired_backup_for_operation(operation_id, started_at)
    if ts is None:
        await lock_service.release(
            lock, succeeded=False, reason="backup_timestamp_missing"
        )
        raise _http(
            "backup_timestamp_missing",
            "backup completed but timestamp was not found",
            500,
        )
    await write_admin_audit_isolated(
        request,
        admin,
        event_type="admin.backup.create",
        details={"timestamp": ts},
    )
    await lock_service.release(lock, succeeded=True, reason="backup_complete")
    return BackupNowOut(ok=True, timestamp=ts)


# ---- Restore ----


class RestoreOut(BaseModel):
    accepted: bool
    timestamp: str
    note: str


async def _wait_for_restore_adoption(
    marker: Path,
    receipt_path: Path,
    *,
    operation_id: str,
    timestamp: str,
) -> tuple[bool, bool]:
    deadline = asyncio.get_running_loop().time() + (
        _BACKUP_TRIGGER_START_TIMEOUT_SECONDS
    )
    marker_adopted = False
    while asyncio.get_running_loop().time() < deadline:
        if _marker_is_adopted(marker, operation_id):
            marker_adopted = True
            if _restore_adoption_receipt_matches(
                receipt_path,
                operation_id=operation_id,
                timestamp=timestamp,
            ):
                return True, True
        await asyncio.sleep(0.25)
    return False, marker_adopted


_restore_adoption_failure = _backup_runtime.restore_adoption_failure


async def _launch_restore_via_host_runner(
    lock_service: SystemOperationLockService,
    lock: SystemLock,
    request: Request,
    admin: AdminUser,
    *,
    timestamp: str,
    marker: Path,
    started_at: datetime,
    operation_id: str,
) -> RestoreOut:
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
    receipt_path = _backup_root() / _RESTORE_ADOPTION_RECEIPT_NAME
    _write_restore_trigger(trigger_path, timestamp, operation_id, started_at)
    adopted, marker_adopted = await _wait_for_restore_adoption(
        marker,
        receipt_path,
        operation_id=operation_id,
        timestamp=timestamp,
    )
    if not adopted:
        if not marker_adopted:
            _unlink_marker(trigger_path)
            _unlink_owned_marker(marker, operation_id)
        code, message = _restore_adoption_failure(marker_adopted)
        await lock_service.release(lock, succeeded=False, reason=code)
        raise _http(code, message, 504)

    await lock_service.release(lock, succeeded=True, reason="restore_launched")
    await write_admin_audit_isolated(
        request,
        admin,
        event_type="admin.backup.restore",
        details={
            "timestamp": timestamp,
            "unit": _RESTORE_RUNNER_UNIT,
            "operation_id": operation_id,
        },
    )
    return RestoreOut(
        accepted=True,
        timestamp=timestamp,
        note="恢复已由宿主机服务接管；完成前其他维护操作会被阻止",
    )


async def _launch_restore_direct(
    lock_service: SystemOperationLockService,
    lock: SystemLock,
    request: Request,
    admin: AdminUser,
    *,
    restore_script: Path,
    timestamp: str,
    marker: Path,
    started_at: datetime,
    operation_id: str,
) -> RestoreOut:
    if shutil.which("docker") is None:
        await lock_service.release(
            lock, succeeded=False, reason="restore_host_runner_required"
        )
        raise _http(
            "restore_host_runner_required",
            "restore requires the host path runner in containerized deployments",
            503,
        )

    log_fh = _open_private_append(_restore_log_path())
    try:
        log_fh.write(
            f"\n=== restore trigger ts={timestamp} "
            f"at {datetime.now(timezone.utc).isoformat()} ===\n"
        )
        log_fh.flush()
        proc = subprocess.Popen(
            ["/usr/bin/env", "bash", str(restore_script), timestamp],
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
        details={"timestamp": timestamp},
    )
    return RestoreOut(
        accepted=True,
        timestamp=timestamp,
        note="恢复已触发；服务会短暂不可用，约 30-60 秒后重新登录验证",
    )


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
        await _backup_pair_for_timestamp_async(backup_root, ts)
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
    if _restore_trigger_only_mode():
        return await _launch_restore_via_host_runner(
            lock_service,
            lock,
            request,
            admin,
            timestamp=ts,
            marker=marker,
            started_at=started_at,
            operation_id=operation_id,
        )

    return await _launch_restore_direct(
        lock_service,
        lock,
        request,
        admin,
        restore_script=restore_script,
        timestamp=ts,
        marker=marker,
        started_at=started_at,
        operation_id=operation_id,
    )


chmod_tolerate_eperm = _chmod_tolerate_eperm
discover_scripts_dir = _discover_scripts_dir
maintenance_marker_busy = _maintenance_marker_busy
open_private_append = _open_private_append
