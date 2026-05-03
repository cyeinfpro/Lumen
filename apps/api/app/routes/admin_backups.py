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
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, TextIO

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..config import settings
from ..deps import AdminUser, verify_csrf
from ._admin_common import admin_http as _http, write_admin_audit_isolated


router = APIRouter(prefix="/admin/backups", tags=["admin"])


# YYYYMMDD-HHMMSS 严格格式：8 位日期 + 短横线 + 6 位时间。
_TIMESTAMP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")
# 备份点配对一致性窗口：PG 和 Redis 文件 mtime 偏差应 ≤ 该秒数。
_PAIR_MTIME_WINDOW_SEC = 600
_BACKUP_TIMEOUT_SECONDS = 180


class _ScriptResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


def _open_private_append(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def _backup_root() -> Path:
    return Path(settings.backup_root).expanduser()


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


async def _run_script(script: Path, *args: str, timeout: int) -> _ScriptResult:
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/env",
        "bash",
        str(script),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
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


@router.get("", response_model=BackupListOut)
async def list_backups(_admin: AdminUser) -> BackupListOut:
    backup_root = _backup_root()
    pg_dir = backup_root / "pg"
    redis_dir = backup_root / "redis"
    if not pg_dir.is_dir() or not redis_dir.is_dir():
        return BackupListOut(items=[], total=0)

    pg_map: dict[str, tuple[int, float]] = {}
    for p in pg_dir.iterdir():
        if not p.is_file():
            continue
        ts = _parse_ts(p.name, ".pg.dump.gz")
        if ts is None:
            continue
        try:
            st = p.stat()
            pg_map[ts] = (st.st_size, st.st_mtime)
        except OSError:
            continue

    redis_map: dict[str, tuple[int, float]] = {}
    for p in redis_dir.iterdir():
        if not p.is_file():
            continue
        ts = _parse_ts(p.name, ".redis.tgz")
        if ts is None:
            continue
        try:
            st = p.stat()
            redis_map[ts] = (st.st_size, st.st_mtime)
        except OSError:
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


@router.post("/now", response_model=BackupNowOut, dependencies=[Depends(verify_csrf)])
async def backup_now(request: Request, admin: AdminUser) -> BackupNowOut:
    backup_script = _backup_script()
    if not backup_script.is_file():
        raise _http("script_missing", f"missing {backup_script}", 500)
    try:
        proc = await _run_script(
            backup_script,
            timeout=_BACKUP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise _http(
            "backup_timeout",
            f"backup exceeded {_BACKUP_TIMEOUT_SECONDS}s",
            504,
        )

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1000:]
        await write_admin_audit_isolated(
            request,
            admin,
            event_type="admin.backup.create.fail",
            details={"returncode": proc.returncode},
        )
        return BackupNowOut(ok=False, stderr_tail=tail)

    # 从 stdout 找最后一条 "backup <ts> complete"
    ts = None
    for line in (proc.stdout or "").splitlines()[::-1]:
        if "complete" in line and "backup " in line:
            parts = line.split()
            # 形如 "[backup ...] backup 20260424-123000 complete"
            for i, t in enumerate(parts):
                if t == "backup" and i + 1 < len(parts) and parts[i + 1] != "":
                    if parts[i + 1] != "complete":
                        ts = parts[i + 1].rstrip(":")
                        break
            if ts:
                break
    await write_admin_audit_isolated(
        request,
        admin,
        event_type="admin.backup.create",
        details={"timestamp": ts},
    )
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
    restore_script = _restore_script()
    if not restore_script.is_file():
        raise _http("script_missing", f"missing {restore_script}", 500)

    ts = body.timestamp.strip()
    if not _TIMESTAMP_RE.fullmatch(ts):
        raise _http("invalid_timestamp", "timestamp must match YYYYMMDD-HHMMSS", 400)

    backup_root = _backup_root().resolve()
    pg = backup_root / "pg" / f"{ts}.pg.dump.gz"
    rd = backup_root / "redis" / f"{ts}.redis.tgz"
    # Why: ``ts`` is regex-validated to ``YYYYMMDD-HHMMSS`` so it cannot
    # contain ``/`` or ``..``, but we still pin the resolved paths back
    # under ``backup_root`` as a belt-and-braces guard against future regex
    # widening or symlink shenanigans inside the backup tree.
    try:
        pg.resolve().relative_to(backup_root)
        rd.resolve().relative_to(backup_root)
    except (ValueError, OSError):
        raise _http("invalid_path", "backup path escapes root", 400)
    if not pg.is_file() or not rd.is_file():
        raise _http("backup_not_found", f"no paired backup for {ts}", 404)

    try:
        skew = int(abs(pg.stat().st_mtime - rd.stat().st_mtime))
    except OSError as exc:
        raise _http("backup_stat_failed", f"cannot stat backup files: {exc}", 500)
    if skew > _PAIR_MTIME_WINDOW_SEC:
        raise _http(
            "backup_inconsistent",
            f"PG/Redis mtime skew {skew}s exceeds {_PAIR_MTIME_WINDOW_SEC}s window",
            409,
        )

    # Fire-and-forget：脚本里会 `systemctl stop lumen-api`，这个请求的进程会被杀掉，
    # 所以必须脱离进程组（start_new_session=True），让 systemd 的 stop 不牵连子进程。
    log_path = backup_root / ".restore.log"
    log_fh = _open_private_append(log_path)
    try:
        log_fh.write(f"\n=== restore trigger ts={ts} at {datetime.now(timezone.utc).isoformat()} ===\n")
        log_fh.flush()
        subprocess.Popen(
            ["/usr/bin/env", "bash", str(restore_script), ts],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_fh.close()
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
