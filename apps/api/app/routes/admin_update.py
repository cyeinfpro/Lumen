"""Admin one-click Lumen update route."""

from __future__ import annotations

import asyncio
import grp
import os
import pwd
import shlex
import shutil
import subprocess
from dataclasses import dataclass
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


@dataclass(frozen=True)
class UpdateMarker:
    pid: int
    started_at: str | None
    unit: str | None = None


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


def _unit_is_running(unit: str) -> bool:
    if not unit or shutil.which("systemctl") is None:
        return False
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _read_marker() -> UpdateMarker | None:
    marker = _update_marker_path()
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    pid = 0
    started_at: str | None = None
    unit: str | None = None
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
    if unit and _unit_is_running(unit):
        return UpdateMarker(pid=pid, started_at=started_at, unit=unit)
    if pid and _pid_is_running(pid):
        return UpdateMarker(pid=pid, started_at=started_at, unit=unit)
    try:
        marker.unlink()
    except OSError:
        pass
    return None


def _write_marker(pid: int, started_at: str, unit: str | None = None) -> None:
    marker = _update_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_suffix(f"{marker.suffix}.tmp")
    lines = [f"pid={pid}", f"started_at={started_at}"]
    if unit:
        lines.append(f"unit={unit}")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
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


def _systemd_unit_name(started_at: datetime) -> str:
    stamp = started_at.strftime("%Y%m%d%H%M%S")
    return f"lumen-update-{stamp}-{os.getpid()}.service"


def _current_service_identity_properties() -> list[str]:
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    return ["--property", f"User={user}", "--property", f"Group={group}"]


def _write_update_env_file(env: dict[str, str], unit: str) -> Path:
    path = _update_marker_path().with_name(f".update.{unit}.env")
    prefixes = (
        "LUMEN_UPDATE_",
        "LUMEN_API_HEALTH_",
        "LUMEN_WEB_HEALTH_",
        "LUMEN_HEALTH_",
    )
    keys = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
    for key in env:
        if key.startswith(prefixes):
            keys.add(key)

    lines = []
    for key in sorted(keys):
        value = env.get(key)
        if value is None:
            continue
        lines.append(f"export {key}={shlex.quote(value)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def _systemd_run_command(
    *,
    unit: str,
    root: Path,
    script: Path,
    log_path: Path,
    env_file: Path,
    marker_path: Path,
    user_mode: bool = False,
) -> list[str]:
    wrapper = r"""
set -euo pipefail
log_path="$1"
env_file="$2"
marker_path="$3"
script="$4"
cleanup() {
  rm -f "$env_file" "$marker_path"
}
trap cleanup EXIT
exec >>"$log_path" 2>&1
set -a
. "$env_file"
set +a
printf '=== update unit started at=%s unit=%s ===\n' "$(date -u +%FT%TZ)" "$LUMEN_UPDATE_SYSTEMD_UNIT"
/usr/bin/env bash "$script"
"""
    cmd: list[str] = ["systemd-run"]
    if user_mode:
        cmd.append("--user")
    cmd += [
        "--unit",
        unit,
        "--collect",
        "--property",
        f"WorkingDirectory={root}",
    ]
    if not user_mode:
        # User= and Group= are only valid in system mode; --user implicitly runs
        # under the invoking user's manager.
        cmd += _current_service_identity_properties()
    cmd += [
        "/usr/bin/env",
        "bash",
        "-lc",
        wrapper,
        "bash",
        str(log_path),
        str(env_file),
        str(marker_path),
        str(script),
    ]
    return cmd


def _systemd_run_available() -> bool:
    return shutil.which("systemd-run") is not None and shutil.which("systemctl") is not None


def _run_systemd_command(
    command: list[str],
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        close_fds=True,
        env=env,
        check=False,
    )


def _systemd_run_attempts(
    *,
    unit: str,
    root: Path,
    script: Path,
    log_path: Path,
    env_file: Path,
    marker_path: Path,
) -> list[tuple[str, list[str]]]:
    """Build the ordered list of systemd-run attempts.

    Cheapest first (system bus as current uid), then sudo (works with NOPASSWD
    or cached creds), then the user manager (works without root if linger is
    enabled for the runtime user).
    """
    system_cmd = _systemd_run_command(
        unit=unit,
        root=root,
        script=script,
        log_path=log_path,
        env_file=env_file,
        marker_path=marker_path,
    )
    user_cmd = _systemd_run_command(
        unit=unit,
        root=root,
        script=script,
        log_path=log_path,
        env_file=env_file,
        marker_path=marker_path,
        user_mode=True,
    )
    attempts: list[tuple[str, list[str]]] = [("systemd-run", system_cmd)]
    if shutil.which("sudo") is not None:
        attempts.append(("sudo -n systemd-run", ["sudo", "-n", *system_cmd]))
    attempts.append(("systemd-run --user", user_cmd))
    return attempts


def _log_attempt_failure(
    log_fh: TextIO,
    label: str,
    result: subprocess.CompletedProcess[str],
) -> None:
    log_fh.write(f"\n[{label}] failed (rc={result.returncode})\n")
    if result.stdout:
        log_fh.write(result.stdout)
        if not result.stdout.endswith("\n"):
            log_fh.write("\n")
    if result.stderr:
        log_fh.write(result.stderr)
        if not result.stderr.endswith("\n"):
            log_fh.write("\n")
    log_fh.flush()


def _start_update_systemd_unit(
    *,
    script: Path,
    env: dict[str, str],
    log_fh: TextIO,
    started_at: datetime,
) -> tuple[int, str] | None:
    """Try to launch update.sh in a transient systemd unit.

    Returns ``(0, unit_name)`` on the first successful attempt; returns ``None``
    when every attempt fails so the caller can fall back to a detached
    subprocess. Each attempt's stdout/stderr is appended to ``log_fh`` so the
    operator can see the real reason via the admin UI.
    """
    root = script.parent.parent
    unit = _systemd_unit_name(started_at)
    log_path = _update_log_path()
    marker_path = _update_marker_path()
    env = dict(env)
    env["LUMEN_UPDATE_SYSTEMD_UNIT"] = unit
    # `systemd-run --user` needs XDG_RUNTIME_DIR (and the dbus session it implies)
    # to reach the runtime user's manager. lumen-api inherits a system-service
    # environment without these vars, so inject them explicitly. Harmless for the
    # other attempts: system-mode systemd-run ignores XDG_RUNTIME_DIR, and `sudo`
    # strips it from the child env unless told otherwise.
    runtime_dir = f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    env_file = _write_update_env_file(env, unit)
    _write_marker(0, started_at.isoformat(), unit=unit)

    for label, command in _systemd_run_attempts(
        unit=unit,
        root=root,
        script=script,
        log_path=log_path,
        env_file=env_file,
        marker_path=marker_path,
    ):
        result = _run_systemd_command(command, env, root)
        if result.returncode == 0:
            return 0, unit
        _log_attempt_failure(log_fh, label, result)

    # Every attempt failed — clean up the staged files so the caller's fallback
    # path can rewrite the marker for its own pid.
    try:
        marker_path.unlink()
    except OSError:
        pass
    try:
        env_file.unlink()
    except OSError:
        pass
    return None


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
    pid: int | None = None
    unit: str | None = None
    started_at: datetime
    proxy_name: str | None = None
    log_path: str
    note: str


class UpdateStatusOut(BaseModel):
    running: bool
    pid: int | None = None
    unit: str | None = None
    started_at: str | None = None
    log_tail: str


@router.get("/status", response_model=UpdateStatusOut)
async def update_status(_admin: AdminUser) -> UpdateStatusOut:
    marker = _read_marker()
    if marker is None:
        return UpdateStatusOut(running=False, log_tail=_read_log_tail())
    return UpdateStatusOut(
        running=True,
        pid=marker.pid or None,
        unit=marker.unit,
        started_at=marker.started_at,
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
        if marker.unit:
            raise _http("update_running", f"Lumen update is already running ({marker.unit})", 409)
        raise _http("update_running", f"Lumen update is already running (pid {marker.pid})", 409)

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

        proc: subprocess.Popen[bytes] | None = None
        unit: str | None = None
        pid: int = 0
        if _systemd_run_available():
            outcome = _start_update_systemd_unit(
                script=script,
                env=env,
                log_fh=log_fh,
                started_at=started_at,
            )
            if outcome is not None:
                pid, unit = outcome
        if unit is None:
            # systemd-run is missing or every attempt failed (often because the
            # runtime user lacks NOPASSWD sudo and linger). Fall back to a
            # detached subprocess so the update can still proceed; update.sh
            # restarts lumen-api last to minimise the chance of self-killing.
            log_fh.write(
                "\n[fallback] launching update.sh as a detached subprocess; "
                "restart of lumen-api will be the last step. To use a transient "
                "systemd unit instead, grant 'sudo -n systemd-run' or run "
                "'loginctl enable-linger <runtime-user>'.\n"
            )
            log_fh.flush()
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
            pid = proc.pid
            _write_marker(pid, started_at.isoformat())
    finally:
        log_fh.close()

    await write_audit_isolated(
        event_type="admin.update.trigger",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"pid": pid or None, "unit": unit, "proxy_name": proxy.name if proxy else None},
    )

    if proc is not None:
        asyncio.create_task(_cleanup_marker_when_done(proc))
    return UpdateTriggerOut(
        accepted=True,
        pid=pid or None,
        unit=unit,
        started_at=started_at,
        proxy_name=proxy.name if proxy else None,
        log_path=str(_update_log_path()),
        note="更新已在后台启动；期间服务可能短暂不可用，脚本会在完成后重启运行进程并执行健康检查。",
    )


async def _cleanup_marker_when_done(proc: subprocess.Popen[bytes]) -> None:
    await asyncio.to_thread(proc.wait)
    pid = int(proc.pid)
    marker = _read_marker()
    if marker and marker.pid == pid:
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
    "_systemd_run_command",
    "_systemd_unit_name",
    "_update_script",
]
