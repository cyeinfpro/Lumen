"""Admin one-click Lumen update route."""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, TextIO

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.providers import ProviderProxyDefinition, resolve_provider_proxy_url
from lumen_core.runtime_settings import get_spec

from ..audit import hash_email, request_ip_hash, write_audit_isolated
from ..config import settings
from ..deps import AdminUser, verify_csrf
from ..db import get_db
from ..runtime_settings import get_setting
from .admin_proxies import _load_proxies
from .admin_backups import _discover_scripts_dir, _open_private_append


router = APIRouter(prefix="/admin/update", tags=["admin"])

_UPDATE_LOG_NAME = ".update.log"
_UPDATE_RUNNING_MARKER = ".update.running"
_LOG_TAIL_CHARS = 6000


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(status_code=http, detail={"error": {"code": code, "message": msg}})


def _update_script() -> Path:
    return _discover_scripts_dir() / "update.sh"


def _update_log_path() -> Path:
    return Path(settings.backup_root).expanduser() / _UPDATE_LOG_NAME


def _update_marker_path() -> Path:
    return Path(settings.backup_root).expanduser() / _UPDATE_RUNNING_MARKER


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


def _read_marker() -> tuple[int, str | None] | None:
    marker = _update_marker_path()
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    pid = 0
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
    if pid and _pid_is_running(pid):
        return pid, started_at
    try:
        marker.unlink()
    except OSError:
        pass
    return None


def _write_marker(pid: int, started_at: str) -> None:
    marker = _update_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_suffix(f"{marker.suffix}.tmp")
    tmp.write_text(f"pid={pid}\nstarted_at={started_at}\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(marker)


def _open_update_log() -> TextIO:
    return _open_private_append(_update_log_path())


def _read_log_tail() -> str:
    path = _update_log_path()
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - _LOG_TAIL_CHARS))
            return fh.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def _clean_proxy_env(env: dict[str, str]) -> None:
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)


def _apply_proxy_env(env: dict[str, str], proxy_url: str) -> None:
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["ALL_PROXY"] = proxy_url
    env["http_proxy"] = proxy_url
    env["https_proxy"] = proxy_url
    env["all_proxy"] = proxy_url


def _mask_proxy_url(proxy_url: str) -> str:
    if "@" not in proxy_url:
        return proxy_url
    scheme, rest = proxy_url.split("://", 1) if "://" in proxy_url else ("", proxy_url)
    _auth, host = rest.rsplit("@", 1)
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


async def _resolve_update_proxy(
    db: AsyncSession,
) -> tuple[ProviderProxyDefinition | None, str | None]:
    use_spec = get_spec("update.use_proxy_pool")
    name_spec = get_spec("update.proxy_name")
    use_raw = await get_setting(db, use_spec) if use_spec is not None else None
    if str(use_raw or "0").strip() != "1":
        return None, None

    proxies = [proxy for proxy in await _load_proxies(db) if proxy.enabled]
    if not proxies:
        raise _http("proxy_unavailable", "update proxy pool is enabled but has no enabled proxies", 409)

    name_raw = await get_setting(db, name_spec) if name_spec is not None else None
    target_name = str(name_raw or "").strip()
    if target_name:
        proxy = next((p for p in proxies if p.name == target_name), None)
        if proxy is None:
            raise _http("proxy_not_found", f"update proxy '{target_name}' not found or disabled", 409)
    else:
        proxy = proxies[0]

    proxy_url = await resolve_provider_proxy_url(proxy)
    if not proxy_url:
        raise _http("proxy_resolve_failed", f"update proxy '{proxy.name}' could not be resolved", 409)
    return proxy, proxy_url


class UpdateTriggerOut(BaseModel):
    accepted: bool
    pid: int
    started_at: datetime
    proxy_name: str | None = None
    log_path: str
    note: str


class UpdateStatusOut(BaseModel):
    running: bool
    pid: int | None = None
    started_at: str | None = None
    log_tail: str


@router.get("/status", response_model=UpdateStatusOut)
async def update_status(_admin: AdminUser) -> UpdateStatusOut:
    marker = _read_marker()
    if marker is None:
        return UpdateStatusOut(running=False, log_tail=_read_log_tail())
    pid, started_at = marker
    return UpdateStatusOut(
        running=True,
        pid=pid,
        started_at=started_at,
        log_tail=_read_log_tail(),
    )


@router.post("", response_model=UpdateTriggerOut, dependencies=[Depends(verify_csrf)])
async def trigger_update(
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UpdateTriggerOut:
    script = _update_script()
    if not script.is_file():
        raise _http("script_missing", f"missing {script}", 500)
    marker = _read_marker()
    if marker is not None:
        pid, _started_at = marker
        raise _http("update_running", f"Lumen update is already running (pid {pid})", 409)

    proxy, proxy_url = await _resolve_update_proxy(db)
    started_at = datetime.now(timezone.utc)
    log_fh = _open_update_log()
    try:
        log_fh.write(
            "\n=== update trigger "
            f"at={started_at.isoformat()} user={admin.id} proxy={proxy.name if proxy else 'none'} ===\n"
        )
        if proxy_url:
            log_fh.write(f"proxy_url={_mask_proxy_url(proxy_url)}\n")
        log_fh.flush()

        env = os.environ.copy()
        _clean_proxy_env(env)
        if proxy_url:
            _apply_proxy_env(env, proxy_url)
        env["LUMEN_UPDATE_NONINTERACTIVE"] = "1"
        env.setdefault("LUMEN_UPDATE_GIT_PULL", "1")
        env.setdefault("LUMEN_UPDATE_BUILD", "1")

        proc = subprocess.Popen(
            ["/usr/bin/env", "bash", str(script)],
            cwd=str(script.parent.parent),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        _write_marker(proc.pid, started_at.isoformat())
    finally:
        log_fh.close()

    await write_audit_isolated(
        event_type="admin.update.trigger",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"pid": proc.pid, "proxy_name": proxy.name if proxy else None},
    )

    asyncio.create_task(_cleanup_marker_when_done(proc))
    return UpdateTriggerOut(
        accepted=True,
        pid=proc.pid,
        started_at=started_at,
        proxy_name=proxy.name if proxy else None,
        log_path=str(_update_log_path()),
        note="更新已在后台启动；期间服务可能短暂不可用，完成后按日志提示重启运行进程。",
    )


async def _cleanup_marker_when_done(proc: subprocess.Popen[bytes]) -> None:
    await asyncio.to_thread(proc.wait)
    pid = int(proc.pid)
    marker = _read_marker()
    if marker and marker[0] == pid:
        try:
            _update_marker_path().unlink()
        except OSError:
            pass


__all__ = [
    "router",
    "_apply_proxy_env",
    "_clean_proxy_env",
    "_pid_is_running",
    "_read_marker",
    "_resolve_update_proxy",
    "_update_script",
]
