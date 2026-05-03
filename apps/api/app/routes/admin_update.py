"""Admin one-click Lumen update route."""

from __future__ import annotations

import asyncio
import grp
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, AsyncIterator, TextIO

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.providers import ProviderProxyDefinition, resolve_provider_proxy_url
from lumen_core.runtime_settings import get_spec

from ..config import settings
from ..deps import AdminUser, verify_csrf
from ..db import get_db
from ..runtime_settings import get_setting
from ._admin_common import (
    admin_http as _http,
    cleanup_marker_when_done,
    write_admin_audit_isolated,
)
from .admin_proxies import _load_proxies
from .admin_backups import _discover_scripts_dir, _open_private_append


router = APIRouter(prefix="/admin/update", tags=["admin"])

_UPDATE_LOG_NAME = ".update.log"
_UPDATE_RUNNING_MARKER = ".update.running"
_UPDATE_TRIGGER_NAME = ".update.trigger"
_UPDATE_RUNNER_ENV_NAME = ".update.env"
_UPDATE_RUNNER_UNIT = "lumen-update-runner.service"
_LOG_TAIL_CHARS = 6000

# Release directory layout — overridable via env so unit tests / non-prod
# installs can point at a sandbox without touching config.py schema.
_LUMEN_ROOT = os.environ.get("LUMEN_ROOT", "/opt/lumen")

_RELEASE_LIST_LIMIT = 10

# Step protocol regexes — see update.sh contract.
_STEP_LINE_RE = re.compile(
    r"^::lumen-step::\s+phase=(?P<phase>[A-Za-z0-9_]+)\s+status=(?P<status>start|done|fail)"
    r"(?:\s+rc=(?P<rc>-?\d+))?"
    r"(?:\s+dur_ms=(?P<dur_ms>-?\d+))?"
    r"(?:\s+ts=(?P<ts>\S+))?"
    r"\s*$"
)
_INFO_LINE_RE = re.compile(
    r"^::lumen-info::\s+phase=(?P<phase>[A-Za-z0-9_]+)\s+key=(?P<key>[A-Za-z0-9_]+)\s+value=(?P<value>.*)$"
)
_TRIGGER_DELIMITER_RE = re.compile(r"^=== update (?:trigger|unit started) ", re.MULTILINE)

# SSE knobs — keep in sync with nginx idle / proxy_read_timeout.
_SSE_HEARTBEAT_SEC = 15.0
_SSE_MAX_DURATION_SEC = 60 * 60  # 1h hard cap to prevent leaks
_SSE_LOG_POLL_SEC = 0.3  # tail-F poll interval
_SSE_LOG_BATCH_WINDOW_SEC = 0.2  # coalesce raw log lines into bursts


@dataclass(frozen=True)
class UpdateMarker:
    pid: int
    started_at: str | None
    unit: str | None = None


def _update_script() -> Path:
    return _discover_scripts_dir() / "update.sh"


def _update_log_path() -> Path:
    return Path(settings.backup_root).expanduser() / _UPDATE_LOG_NAME


def _update_marker_path() -> Path:
    return Path(settings.backup_root).expanduser() / _UPDATE_RUNNING_MARKER


def _update_trigger_path() -> Path:
    return Path(settings.backup_root).expanduser() / _UPDATE_TRIGGER_NAME


def _update_runner_env_path() -> Path:
    return Path(settings.backup_root).expanduser() / _UPDATE_RUNNER_ENV_NAME


def _lumen_root() -> Path:
    """Return the Lumen install root (releases/, current, previous live here).

    Resolved per-call so tests can override LUMEN_ROOT mid-process.
    """
    return Path(os.environ.get("LUMEN_ROOT", _LUMEN_ROOT)).expanduser()


def _read_dotenv_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return None
    prefix = f"{key}="
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value or None
    return None


def _shared_env_path(script: Path | None = None) -> Path:
    configured = os.environ.get("LUMEN_SHARED_ENV", "").strip()
    if configured:
        return Path(configured).expanduser()
    root = _lumen_root()
    candidate = root / "shared" / ".env"
    if candidate.is_file():
        return candidate
    if script is not None:
        release_env = script.parent.parent / ".env"
        try:
            if release_env.is_file():
                return release_env.resolve()
        except OSError:
            pass
    return candidate


def _runner_unit_available() -> bool:
    """True iff the system has lumen-update-runner.service installed.

    When present we let PID 1 start the update via a path-watched trigger
    file. This sidesteps lumen-api's NoNewPrivileges/ProtectSystem sandbox
    entirely — no dbus, no sudo, no polkit needed.
    """
    if shutil.which("systemctl") is None:
        return False
    result = subprocess.run(
        ["systemctl", "list-unit-files", _UPDATE_RUNNER_UNIT, "--no-legend"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    return _UPDATE_RUNNER_UNIT in result.stdout


def _runner_env_lines(env: dict[str, str]) -> list[str]:
    """Subset of env vars worth forwarding to the runner via EnvironmentFile."""
    keys = (
        "LUMEN_UPDATE_NONINTERACTIVE",
        "LUMEN_UPDATE_GIT_PULL",
        "LUMEN_UPDATE_BUILD",
        "LUMEN_UPDATE_SYSTEMD_UNIT",
        "LUMEN_UPDATE_CHANNEL",
        "LUMEN_IMAGE_TAG",
        "LUMEN_UPDATE_PROXY_URL",
        "LUMEN_UPDATE_ROOT",
        "LUMEN_REPO_DIR",
        "LUMEN_SOURCE_ROOT",
        "LUMEN_HTTP_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    )
    lines: list[str] = []
    for key in keys:
        value = env.get(key)
        if value is None:
            continue
        # systemd EnvironmentFile uses simple KEY=VALUE; values must not be
        # quoted (systemd parses them literally including any quotes).
        if "\n" in value or "\r" in value:
            continue
        lines.append(f"{key}={value}")
    return lines


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


def _read_log_full() -> str:
    """Read the entire .update.log. Used by status/SSE for step parsing.

    We only ever scan the segment after the *last* ``=== update ... ===`` header
    so cross-update phase repetitions don't pollute the current view.
    """
    path = _update_log_path()
    try:
        return path.read_text(encoding="utf-8", errors="replace")
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
    env["LUMEN_UPDATE_PROXY_URL"] = proxy_url
    env["LUMEN_HTTP_PROXY"] = proxy_url
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["ALL_PROXY"] = proxy_url
    env["http_proxy"] = proxy_url
    env["https_proxy"] = proxy_url
    env["all_proxy"] = proxy_url


def _proxy_url_from_env_file(path: Path) -> str | None:
    for key in (
        "LUMEN_UPDATE_PROXY_URL",
        "LUMEN_HTTP_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        value = _read_dotenv_value(path, key)
        if value:
            return value
    return None


def _apply_dotenv_proxy_env(env: dict[str, str], env_file: Path) -> str | None:
    proxy_url = _proxy_url_from_env_file(env_file)
    if not proxy_url:
        return None
    _apply_proxy_env(env, proxy_url)
    no_proxy = (
        _read_dotenv_value(env_file, "NO_PROXY")
        or _read_dotenv_value(env_file, "no_proxy")
        or "127.0.0.1,localhost,::1"
    )
    env.setdefault("NO_PROXY", no_proxy)
    env.setdefault("no_proxy", no_proxy)
    return proxy_url


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
        "LUMEN_ROLLBACK_",
    )
    keys = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LUMEN_REPO_DIR",
        "LUMEN_SOURCE_ROOT",
        "LUMEN_HTTP_PROXY",
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


def _systemd_run_inline_command(
    *,
    unit: str,
    root: Path,
    log_path: Path,
    inline_script: str,
    user_mode: bool = False,
) -> list[str]:
    """Build a systemd-run command that executes an inline shell snippet.

    Used by the rollback endpoint — there is no wrapper EnvironmentFile, the
    script is passed verbatim. stdout/stderr are appended to ``log_path`` so
    the existing SSE stream surfaces the rollback progress with no extra wiring.
    """
    wrapper = r"""
set -euo pipefail
log_path="$1"
inline="$2"
exec >>"$log_path" 2>&1
printf '=== rollback unit started at=%s unit=%s ===\n' "$(date -u +%FT%TZ)" "${LUMEN_UPDATE_SYSTEMD_UNIT:-unknown}"
/usr/bin/env bash -c "$inline"
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
        "--setenv",
        f"LUMEN_UPDATE_SYSTEMD_UNIT={unit}",
    ]
    if not user_mode:
        cmd += _current_service_identity_properties()
    cmd += [
        "/usr/bin/env",
        "bash",
        "-lc",
        wrapper,
        "bash",
        str(log_path),
        inline_script,
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


def _systemd_run_inline_attempts(
    *,
    unit: str,
    root: Path,
    log_path: Path,
    inline_script: str,
) -> list[tuple[str, list[str]]]:
    """Same fallback chain as ``_systemd_run_attempts`` but for inline shell.

    Used by the rollback endpoint to reuse the path-unit / sudo / user-mode
    cascade without having to involve update.sh.
    """
    system_cmd = _systemd_run_inline_command(
        unit=unit,
        root=root,
        log_path=log_path,
        inline_script=inline_script,
    )
    user_cmd = _systemd_run_inline_command(
        unit=unit,
        root=root,
        log_path=log_path,
        inline_script=inline_script,
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


def _start_update_via_path_unit(
    *,
    env: dict[str, str],
    log_fh: TextIO,
    started_at: datetime,
) -> tuple[int, str] | None:
    """Trigger lumen-update-runner.service via the path-watched trigger file.

    Writes ``.update.env`` (runner reads via EnvironmentFile) and ``.update.trigger``.
    PID 1 — not lumen-api — launches the runner, so all of lumen-api's sandbox
    constraints (NoNewPrivileges, ProtectSystem=strict) and the polkit/sudo
    plumbing become irrelevant. Synchronously waits up to ~10s for the runner
    to become active so callers get a deterministic success/fail signal.
    """
    backup_root = Path(settings.backup_root).expanduser()
    backup_root.mkdir(parents=True, exist_ok=True)
    env_path = _update_runner_env_path()
    trigger_path = _update_trigger_path()
    unit = _UPDATE_RUNNER_UNIT

    # 1) Marker first so concurrent triggers see the lock immediately.
    _write_marker(0, started_at.isoformat(), unit=unit)

    # 2) Env file for the runner. Tmp+rename keeps systemd from racing with
    #    a half-written file when PathChanged fires.
    env_text = "\n".join(_runner_env_lines(env)) + "\n"
    env_tmp = env_path.with_suffix(f"{env_path.suffix}.tmp")
    env_tmp.write_text(env_text, encoding="utf-8")
    os.chmod(env_tmp, 0o600)
    env_tmp.replace(env_path)

    # 3) Trigger file. Content is the ISO timestamp; PathChanged on the path
    #    unit fires on close-after-write and starts the runner unit.
    trigger_tmp = trigger_path.with_suffix(f"{trigger_path.suffix}.tmp")
    trigger_tmp.write_text(started_at.isoformat() + "\n", encoding="utf-8")
    os.chmod(trigger_tmp, 0o600)
    trigger_tmp.replace(trigger_path)

    # 4) Wait for the runner to come up. The path-watcher latency is normally
    #    < 1s; allow generous slack so a busy host doesn't return a misleading
    #    failure. Exit early as soon as we observe the unit active.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _unit_is_running(unit):
            return 0, unit
        time.sleep(0.3)

    # Runner did not pick up the trigger. Clean staged files so the caller can
    # try the next attempt and we don't leave a stale lock.
    log_fh.write(
        f"\n[{unit}] path-unit trigger did not activate within 15s; falling through.\n"
    )
    log_fh.flush()
    for path in (trigger_path, env_path, _update_marker_path()):
        try:
            path.unlink()
        except OSError:
            pass
    return None


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


# ---------------------------------------------------------------------------
# Step protocol parsing
# ---------------------------------------------------------------------------


class StepRecord(BaseModel):
    """One phase entry parsed from .update.log step lines.

    ``status`` is "running" until we see a ``status=done`` for the same phase.
    ``info`` collects every ``::lumen-info::`` key/value emitted under that phase.
    """

    phase: str
    status: str  # "running" | "done"
    started_at: str | None = None
    ended_at: str | None = None
    rc: int | None = None
    dur_ms: int | None = None
    info: dict[str, str] = Field(default_factory=dict)


def _truncate_to_last_run(log_text: str) -> str:
    """Return log content from the last ``=== update [trigger|unit started] ... ===`` onward.

    Why: phases like ``switch`` recur on every update; we must not mix
    yesterday's done with today's running. The trigger header is always written
    by trigger_update / the rollback path so its presence is reliable.
    """
    if not log_text:
        return log_text
    matches = list(_TRIGGER_DELIMITER_RE.finditer(log_text))
    if not matches:
        return log_text
    return log_text[matches[-1].start():]


def _parse_steps(log_text: str) -> list[StepRecord]:
    """Single-pass scan of step / info lines into per-phase StepRecords.

    Phases keep their *first* start (so ``started_at`` reflects when this
    update began the phase) but accept the *last* done — guarding against the
    rare case where a phase logs done twice. ``info`` is append-merged.
    """
    text = _truncate_to_last_run(log_text)
    if not text:
        return []
    by_phase: dict[str, StepRecord] = {}
    order: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _STEP_LINE_RE.match(line)
        if m:
            phase = m.group("phase")
            raw_status = m.group("status")
            status = "done" if raw_status == "fail" else raw_status
            ts = m.group("ts")
            rc = m.group("rc")
            dur = m.group("dur_ms")
            existing = by_phase.get(phase)
            if existing is None:
                existing = StepRecord(
                    phase=phase,
                    status="running" if status == "start" else "done",
                    started_at=ts if status == "start" else None,
                    ended_at=ts if status == "done" else None,
                    rc=int(rc) if rc is not None and status == "done" else None,
                    dur_ms=int(dur) if dur is not None and status == "done" else None,
                )
                by_phase[phase] = existing
                order.append(phase)
                continue
            if status == "start":
                # Keep the latest start so a phase that re-runs reflects the
                # most recent attempt; ended_at gets cleared because we're
                # back to running.
                existing.started_at = ts
                existing.status = "running"
                existing.ended_at = None
                existing.rc = None
                existing.dur_ms = None
            else:
                existing.status = "done"
                existing.ended_at = ts
                if rc is not None:
                    try:
                        existing.rc = int(rc)
                    except ValueError:
                        existing.rc = None
                if dur is not None:
                    try:
                        existing.dur_ms = int(dur)
                    except ValueError:
                        existing.dur_ms = None
            continue
        m = _INFO_LINE_RE.match(line)
        if m:
            phase = m.group("phase")
            key = m.group("key")
            value = m.group("value").rstrip()
            existing = by_phase.get(phase)
            if existing is None:
                existing = StepRecord(phase=phase, status="running")
                by_phase[phase] = existing
                order.append(phase)
            # info dict is mutated in-place; pydantic keeps the reference
            existing.info[key] = value
    return [by_phase[p] for p in order]


# ---------------------------------------------------------------------------
# Release listing
# ---------------------------------------------------------------------------


class ReleaseInfo(BaseModel):
    id: str
    created_at: str | None = None
    sha: str | None = None
    branch: str | None = None
    alembic_head_expected: str | None = None
    alembic_head_applied: str | None = None
    is_current: bool = False
    is_previous: bool = False


def _readlink_target(link: Path) -> str | None:
    """Resolve a symlink to its (relative or absolute) target name.

    We only care about the final basename — releases live as ``releases/<id>``,
    so a relative symlink ``releases/2025...`` and an absolute one both end up
    matching by ``Path.name``.
    """
    try:
        if not link.is_symlink():
            return None
        return os.readlink(link)
    except OSError:
        return None


def _extract_release_id(link_target: str | None) -> str | None:
    if not link_target:
        return None
    return Path(link_target).name or None


def _read_release_metadata(release_dir: Path) -> dict[str, object]:
    meta_path = release_dir / ".lumen_release.json"
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _release_info_from_dir(release_dir: Path) -> ReleaseInfo | None:
    if not release_dir.is_dir():
        return None
    meta = _read_release_metadata(release_dir)
    rid = str(meta.get("id") or release_dir.name)
    return ReleaseInfo(
        id=rid,
        created_at=str(meta["created_at"]) if meta.get("created_at") else None,
        sha=str(meta["sha"]) if meta.get("sha") else None,
        branch=str(meta["branch"]) if meta.get("branch") else None,
        alembic_head_expected=(
            str(meta["alembic_head_expected"])
            if meta.get("alembic_head_expected")
            else None
        ),
        alembic_head_applied=(
            str(meta["alembic_head_applied"])
            if meta.get("alembic_head_applied")
            else None
        ),
    )


def _list_releases(lumen_root: Path | None = None) -> list[ReleaseInfo]:
    """Scan ``<root>/releases/<id>/`` and return ReleaseInfo for each, newest first.

    ``current`` and ``previous`` are flagged via readlink. Releases without a
    ``.lumen_release.json`` still get listed (id = directory name) but with
    most fields ``None`` — better to surface the directory than to drop it.
    """
    root = lumen_root or _lumen_root()
    releases_dir = root / "releases"
    if not releases_dir.is_dir():
        return []

    current_id = _extract_release_id(_readlink_target(root / "current"))
    previous_id = _extract_release_id(_readlink_target(root / "previous"))

    items: list[ReleaseInfo] = []
    try:
        children = list(releases_dir.iterdir())
    except OSError:
        return []
    for child in children:
        # Hidden / non-dir entries (e.g. tarballs left by the build) are skipped.
        if not child.is_dir() or child.name.startswith("."):
            continue
        info = _release_info_from_dir(child)
        if info is None:
            continue
        if current_id and info.id == current_id:
            info = info.model_copy(update={"is_current": True})
        if previous_id and info.id == previous_id:
            info = info.model_copy(update={"is_previous": True})
        items.append(info)

    # Newest first by created_at when available; fall back to id (which is
    # typically a sortable timestamp). Releases lacking created_at sort *last*
    # — we deliberately split the sort into two passes so the "missing field
    # last" rule survives the reverse=True flip used to put newest on top.
    typed_items = [r for r in items if r.created_at]
    typed_items.sort(key=lambda r: (r.created_at or "", r.id), reverse=True)
    untyped_items = [r for r in items if not r.created_at]
    untyped_items.sort(key=lambda r: r.id, reverse=True)
    return (typed_items + untyped_items)[:_RELEASE_LIST_LIMIT]


def _resolve_release(lumen_root: Path, release_id: str) -> Path | None:
    """Validate that ``release_id`` is a real subdirectory of ``releases/``.

    Returns ``None`` for missing or path-traversal attempts (any id containing
    ``..`` / ``/`` / leading dot is rejected).
    """
    if not release_id or "/" in release_id or ".." in release_id or release_id.startswith("."):
        return None
    target = lumen_root / "releases" / release_id
    try:
        # Resolve and confirm we did not escape releases/.
        resolved = target.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    releases_root = (lumen_root / "releases").resolve()
    try:
        resolved.relative_to(releases_root)
    except ValueError:
        return None
    if not resolved.is_dir():
        return None
    return resolved


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


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
    phases: list[StepRecord] = Field(default_factory=list)
    current_release: ReleaseInfo | None = None
    previous_release: ReleaseInfo | None = None
    releases: list[ReleaseInfo] = Field(default_factory=list)


def _build_status_snapshot() -> UpdateStatusOut:
    """Single source of truth for status assembly. Used by both the polling
    endpoint and the SSE initial state event so the two never drift.
    """
    marker = _read_marker()
    log_text = _read_log_full()
    phases = _parse_steps(log_text)
    releases = _list_releases()
    current = next((r for r in releases if r.is_current), None)
    previous = next((r for r in releases if r.is_previous), None)
    if marker is None:
        return UpdateStatusOut(
            running=False,
            log_tail=_read_log_tail(),
            phases=phases,
            current_release=current,
            previous_release=previous,
            releases=releases,
        )
    return UpdateStatusOut(
        running=True,
        pid=marker.pid or None,
        unit=marker.unit,
        started_at=marker.started_at,
        log_tail=_read_log_tail(),
        phases=phases,
        current_release=current,
        previous_release=previous,
        releases=releases,
    )


@router.get("/status", response_model=UpdateStatusOut)
async def update_status(_admin: AdminUser) -> UpdateStatusOut:
    return await asyncio.to_thread(_build_status_snapshot)


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


def _sse_format(event: str, data: object) -> str:
    """Wire-encode one SSE event. Always JSON-serialised data.

    Keeps newlines escaped — a multi-line value would otherwise break the
    ``event:\ndata:\n\n`` framing and confuse browsers.
    """
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _classify_log_line(line: str) -> tuple[str, dict[str, object]]:
    """Map a raw log line to an SSE (event_name, payload) pair.

    Step / info lines get parsed into structured deltas; everything else falls
    through to a generic ``log`` event so the operator sees stdout in real time.
    """
    stripped = line.rstrip("\n").rstrip("\r")
    m = _STEP_LINE_RE.match(stripped.strip())
    if m:
        rc = m.group("rc")
        dur = m.group("dur_ms")
        return "step", {
            "phase": m.group("phase"),
            "status": "done" if m.group("status") == "fail" else m.group("status"),
            "ts": m.group("ts"),
            "rc": int(rc) if rc is not None else None,
            "dur_ms": int(dur) if dur is not None else None,
        }
    m = _INFO_LINE_RE.match(stripped.strip())
    if m:
        return "info", {
            "phase": m.group("phase"),
            "key": m.group("key"),
            "value": m.group("value").rstrip(),
        }
    return "log", {"line": stripped}


def _read_incremental(path: Path, last_pos: int) -> tuple[str, int]:
    """Read everything appended after ``last_pos``; return (text, new_pos).

    If the file shrunk (rotation / truncation / reboot wiped the marker
    directory) we reset to 0 so we don't read garbage.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "", last_pos
    except OSError:
        return "", last_pos
    if size < last_pos:
        last_pos = 0
    if size == last_pos:
        return "", last_pos
    try:
        with path.open("rb") as fh:
            fh.seek(last_pos)
            chunk = fh.read(size - last_pos)
    except OSError:
        return "", last_pos
    return chunk.decode("utf-8", errors="replace"), size


async def _stream_update_events(request: Request) -> AsyncIterator[str]:
    """Async generator powering ``GET /admin/update/stream``.

    Order of operations:
    1. Emit an initial ``state`` snapshot so a fresh client lands on a known view.
    2. Poll ``.update.log`` every 300ms; classify each new line and emit
       ``step`` / ``info`` / ``log`` events. Raw ``log`` events are coalesced
       within a 200ms window to keep the wire small under noisy phases.
    3. Heartbeat every 15s (no payload, just keeps reverse proxies open).
    4. End when the marker disappears (update finished) or when 1h cap hits.
    5. Detect client disconnect on every iteration and exit cleanly.
    """
    log_path = _update_log_path()
    deadline = time.monotonic() + _SSE_MAX_DURATION_SEC

    # 1) Initial snapshot. Run sync IO in a thread to keep the loop responsive.
    snapshot = await asyncio.to_thread(_build_status_snapshot)
    yield _sse_format("state", snapshot.model_dump(mode="json"))

    # Track pre-existing log size so we only stream lines appended after this
    # connection opened. New connections do NOT re-emit the entire log; the
    # initial snapshot already includes phases/log_tail.
    try:
        last_pos = log_path.stat().st_size
    except (FileNotFoundError, OSError):
        last_pos = 0

    last_heartbeat = time.monotonic()
    line_buffer: list[str] = []
    last_flush = time.monotonic()
    marker_gone_at: float | None = None

    try:
        while True:
            # Hard deadline guard. Breaks the connection cleanly so the client
            # can reconnect with a fresh budget.
            if time.monotonic() >= deadline:
                yield _sse_format("done", {"reason": "max_duration"})
                return

            # Client closed the EventSource (browser tab closed / nginx hung up).
            if await request.is_disconnected():
                return

            # Pull whatever has been appended since the last poll.
            chunk, last_pos = await asyncio.to_thread(
                _read_incremental, log_path, last_pos
            )
            if chunk:
                # ``splitlines(keepends=False)`` plus a manual trailing-newline
                # check: incomplete lines stay in the buffer until the next
                # write completes them. Phase / info lines are emitted
                # immediately; raw log lines accumulate.
                lines = chunk.splitlines()
                # If the chunk did not end with a newline the last "line" may
                # be a partial write — still emit it as a log event because
                # update.sh writes with line-buffered redirection so partials
                # are rare. Erring on the side of low latency.
                for line in lines:
                    event_name, payload = _classify_log_line(line)
                    if event_name in ("step", "info"):
                        # Flush any pending raw log batch first to preserve
                        # ordering relative to step transitions.
                        if line_buffer:
                            yield _sse_format("log", {"lines": line_buffer})
                            line_buffer = []
                            last_flush = time.monotonic()
                        yield _sse_format(event_name, payload)
                    else:
                        line_buffer.append(payload["line"])  # type: ignore[arg-type]

            now = time.monotonic()
            # Flush coalesced raw lines on a short window so a chatty phase
            # doesn't spam the client one-event-per-line.
            if line_buffer and now - last_flush >= _SSE_LOG_BATCH_WINDOW_SEC:
                yield _sse_format("log", {"lines": line_buffer})
                line_buffer = []
                last_flush = now

            # Heartbeat keeps idle proxies (nginx 60s default) from killing us.
            if now - last_heartbeat >= _SSE_HEARTBEAT_SEC:
                yield _sse_format("ping", {})
                last_heartbeat = now

            # Marker disappearance signals end-of-update. Wait one extra tick
            # so the final ``::lumen-step:: phase=cleanup status=done`` line
            # has time to flush from the runner's stdout buffer before we
            # close the stream — without this delay clients sometimes miss
            # the terminal phase event.
            marker = await asyncio.to_thread(_read_marker)
            if marker is None:
                if marker_gone_at is None:
                    marker_gone_at = now
                elif now - marker_gone_at >= 1.0:
                    # Drain any final tail before saying goodbye.
                    chunk, last_pos = await asyncio.to_thread(
                        _read_incremental, log_path, last_pos
                    )
                    if chunk:
                        for line in chunk.splitlines():
                            event_name, payload = _classify_log_line(line)
                            if event_name in ("step", "info"):
                                if line_buffer:
                                    yield _sse_format("log", {"lines": line_buffer})
                                    line_buffer = []
                                yield _sse_format(event_name, payload)
                            else:
                                line_buffer.append(payload["line"])  # type: ignore[arg-type]
                        if line_buffer:
                            yield _sse_format("log", {"lines": line_buffer})
                            line_buffer = []
                    final = await asyncio.to_thread(_build_status_snapshot)
                    yield _sse_format(
                        "done",
                        {
                            "final_status": {
                                "running": final.running,
                                "phases": [p.model_dump(mode="json") for p in final.phases],
                                "current_release": (
                                    final.current_release.model_dump(mode="json")
                                    if final.current_release
                                    else None
                                ),
                            }
                        },
                    )
                    return
            else:
                marker_gone_at = None

            await asyncio.sleep(_SSE_LOG_POLL_SEC)
    except asyncio.CancelledError:
        # FastAPI cancels generators on disconnect; re-raise so upstream
        # cleanup runs. Buffered raw lines are simply dropped — the client
        # has already gone away.
        raise


@router.get("/stream")
async def update_stream(request: Request, _admin: AdminUser) -> StreamingResponse:
    """SSE feed of update progress.

    Browsers consume via ``new EventSource('/api/admin/update/stream')``.
    Events:
      - ``state``  full UpdateStatusOut snapshot, sent once on connect
      - ``step``   StepRecord delta (phase transitioned)
      - ``info``   k/v from ::lumen-info:: lines
      - ``log``    raw log lines (batched) for free-form output
      - ``ping``   heartbeat every 15s
      - ``done``   final state + reason; client should close
    """
    return StreamingResponse(
        _stream_update_events(request),
        media_type="text/event-stream",
        headers={
            # ``no-cache`` + ``X-Accel-Buffering: no`` are belt-and-suspenders
            # against intermediaries (nginx/cloudflare) buffering the response.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            # Force Connection: keep-alive even if the proxy strips upstream
            # signalling — SSE is fundamentally a long-lived GET.
            "Connection": "keep-alive",
        },
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
        env_file_proxy_url = None
        if proxy_url:
            _apply_proxy_env(env, proxy_url)
        else:
            env_file_proxy_url = _apply_dotenv_proxy_env(env, _shared_env_path(script))
            if env_file_proxy_url:
                log_fh.write(f"proxy_url={_mask_proxy_url(env_file_proxy_url)}\n")
                log_fh.flush()
        # Local-loopback exemptions so update.sh's healthz curl doesn't route
        # 127.0.0.1 through the upstream socks5 proxy and timeout forever.
        env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
        env.setdefault("no_proxy", "127.0.0.1,localhost,::1")
        env["LUMEN_UPDATE_NONINTERACTIVE"] = "1"
        env.setdefault("LUMEN_UPDATE_GIT_PULL", "1")
        env.setdefault("LUMEN_UPDATE_BUILD", "0")

        proc: subprocess.Popen[bytes] | None = None
        unit: str | None = None
        pid: int = 0
        # Preferred path: a system-installed lumen-update-runner.service watched
        # by lumen-update.path. Trigger via a file write — PID 1 starts the
        # runner, so we sidestep lumen-api's sandbox completely.
        if _runner_unit_available():
            outcome = _start_update_via_path_unit(
                env=env,
                log_fh=log_fh,
                started_at=started_at,
            )
            if outcome is not None:
                pid, unit = outcome
        if unit is None and _systemd_run_available():
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

    await write_admin_audit_isolated(
        request,
        admin,
        event_type="admin.update.trigger",
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
    await cleanup_marker_when_done(
        proc,
        read_marker_fn=_read_marker,
        marker_path_fn=_update_marker_path,
    )


__all__ = [
    "router",
    "ReleaseInfo",
    "StepRecord",
    "UpdateStatusOut",
    "_apply_proxy_env",
    "_build_status_snapshot",
    "_clean_proxy_env",
    "_list_releases",
    "_lumen_root",
    "_open_update_log",
    "_parse_steps",
    "_pid_is_running",
    "_read_marker",
    "_resolve_release",
    "_resolve_update_proxy",
    "_systemd_run_available",
    "_systemd_run_command",
    "_systemd_run_inline_attempts",
    "_systemd_run_inline_command",
    "_systemd_unit_name",
    "_unit_is_running",
    "_update_log_path",
    "_update_marker_path",
    "_update_script",
    "_write_marker",
]
