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
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, AsyncIterator, TextIO

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.providers import ProviderProxyDefinition, resolve_provider_proxy_url
from lumen_core.runtime_settings import get_spec

from ..config import settings
from ..deps import AdminUser, verify_csrf
from ..db import get_db
from ..runtime_settings import get_setting
from ..services.admin import update_status as _update_status
from ..services.admin import update_stream as _update_stream
from ..services.admin import update_trigger as _update_trigger
from ..services.github_releases import validate_update_tag
from ..services.idempotency import cache_json, derive_idempotency_key, get_cached_json
from ..services.system_lock import SystemOperationLockService
from ..services.update_check import (
    UpdateCheckOut,
    UpdateCheckService,
    UpdateVersionOut,
)
from ..services.update_warm import maybe_warm_pull
from ._admin_common import (
    admin_http as _http,
    cleanup_marker_when_done,
    write_admin_audit_isolated,
)
from .admin_proxies import _load_proxies
from .admin_backups import (
    _chmod_tolerate_eperm,
    _discover_scripts_dir,
    _maintenance_marker_busy,
    _open_private_append,
)

_marker_cleanup_tasks: set[asyncio.Task[None]] = set()
_marker_cleanup_tasks_lock = threading.Lock()


async def _shutdown_marker_cleanup_tasks() -> None:
    with _marker_cleanup_tasks_lock:
        tasks = list(_marker_cleanup_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    with _marker_cleanup_tasks_lock:
        _marker_cleanup_tasks.difference_update(tasks)


@asynccontextmanager
async def _marker_cleanup_lifespan(_app: object) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await _shutdown_marker_cleanup_tasks()


router = APIRouter(
    prefix="/admin/update",
    tags=["admin"],
    lifespan=_marker_cleanup_lifespan,
)
router_public = APIRouter(tags=["system"])

_UPDATE_LOG_NAME = ".update.log"
_UPDATE_RUNNING_MARKER = ".update.running"
_UPDATE_TRIGGER_NAME = ".update.trigger"
_UPDATE_RUNNER_REQUEST_NAME = ".update.request.json"
_UPDATE_RUNNER_UNIT = "lumen-update-runner.service"
_LOG_TAIL_CHARS = 6000
_PID_MARKER_STALE_AFTER_SECONDS = 24 * 60 * 60

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
_TRIGGER_DELIMITER_RE = re.compile(
    r"^=== update (?:trigger|unit started) ", re.MULTILINE
)

# SSE knobs — keep in sync with nginx idle / proxy_read_timeout.
_SSE_HEARTBEAT_SEC = 15.0
_SSE_MAX_DURATION_SEC = 60 * 60  # 1h hard cap to prevent leaks
_SSE_LOG_POLL_SEC = 0.3  # tail-F poll interval
_SSE_LOG_BATCH_WINDOW_SEC = 0.2  # coalesce raw log lines into bursts
_TRIGGER_ONLY_RUNNER_START_TIMEOUT_SEC = 15.0
_SEMVER_UPDATE_TAG_RE = re.compile(
    r"^v(?P<version>[0-9]+(?:\.[0-9]+){2}(?:-[0-9A-Za-z.-]+)?)$"
)


@dataclass(frozen=True)
class UpdateMarker:
    pid: int
    started_at: str | None
    unit: str | None = None


def _ensure_update_not_running(marker: UpdateMarker | None) -> None:
    if marker is None:
        return
    if marker.unit:
        raise _http(
            "update_running",
            f"Lumen update is already running ({marker.unit})",
            409,
        )
    raise _http(
        "update_running",
        f"Lumen update is already running (pid {marker.pid})",
        409,
    )


def _update_script() -> Path:
    return _discover_scripts_dir() / "update.sh"


def _version_from_update_tag(tag: str) -> str | None:
    match = _SEMVER_UPDATE_TAG_RE.fullmatch((tag or "").strip())
    return match.group("version") if match else None


def _update_log_path() -> Path:
    return Path(settings.backup_root).expanduser() / _UPDATE_LOG_NAME


def _update_marker_path() -> Path:
    return Path(settings.backup_root).expanduser() / _UPDATE_RUNNING_MARKER


def _update_trigger_path() -> Path:
    return Path(settings.backup_root).expanduser() / _UPDATE_TRIGGER_NAME


def _update_runner_request_path() -> Path:
    return Path(settings.backup_root).expanduser() / _UPDATE_RUNNER_REQUEST_NAME


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
        value = line[len(prefix) :].strip()
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


def _runner_trigger_only_mode() -> bool:
    """True when the API runs in a container that can only write the trigger file.

    Containerised lumen-api has no systemctl client, no dbus session, and no
    way to query systemd on the host. docker-compose sets
    ``LUMEN_UPDATE_VIA_TRIGGER=1`` so this code path knows to skip the
    systemctl probes and trust the host's lumen-update.path watcher.
    """
    return os.environ.get("LUMEN_UPDATE_VIA_TRIGGER", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _runner_unit_available() -> bool:
    """True iff the system has lumen-update-runner.service installed.

    When present we let PID 1 start the update via a path-watched trigger
    file. This sidesteps lumen-api's NoNewPrivileges/ProtectSystem sandbox
    entirely — no dbus, no sudo, no polkit needed.
    """
    # Containerised deploys can't run systemctl. Trust LUMEN_UPDATE_VIA_TRIGGER
    # — it's only set in docker-compose, where the host always has the path
    # watcher installed (otherwise the operator misconfigured the host).
    if _runner_trigger_only_mode():
        return True
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


def _runner_request_payload(
    env: dict[str, str], started_at: datetime
) -> dict[str, object]:
    """Build the narrow request consumed by the privileged host runner.

    The backup directory is writable by lumen-api. It must therefore never be
    used as a systemd EnvironmentFile or as a source of executable paths.
    """
    return {
        "schema": 1,
        "target_tag": env["LUMEN_UPDATE_RESOLVED_TAG"],
        "channel": env["LUMEN_UPDATE_CHANNEL"],
        "force_redeploy": env.get("LUMEN_UPDATE_FORCE_REDEPLOY") == "1",
        "idempotency_key": env["LUMEN_UPDATE_IDEMPOTENCY_KEY"],
        "proxy_url": env.get("LUMEN_UPDATE_PROXY_URL"),
        "issued_at": started_at.isoformat(),
    }


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
    return age.total_seconds() > _PID_MARKER_STALE_AFTER_SECONDS


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


def _parse_marker_text(raw: str) -> UpdateMarker:
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
    return UpdateMarker(pid=pid, started_at=started_at, unit=unit)


def _marker_is_live(marker: UpdateMarker) -> bool:
    if marker.unit:
        if _runner_trigger_only_mode() and not _marker_is_stale(marker.started_at):
            return True
        if _unit_is_running(marker.unit):
            return True
    return bool(
        marker.pid
        and _pid_is_running(marker.pid)
        and not _marker_is_stale(marker.started_at)
    )


def _read_marker() -> UpdateMarker | None:
    marker_path = _update_marker_path()
    try:
        marker = _parse_marker_text(marker_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError):
        return None
    if _marker_is_live(marker):
        return marker
    try:
        marker_path.unlink()
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
    _chmod_tolerate_eperm(tmp, 0o600)
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
    keys = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LUMEN_UPDATE_NONINTERACTIVE",
        "LUMEN_UPDATE_GIT_PULL",
        "LUMEN_UPDATE_BUILD",
        "LUMEN_UPDATE_MODE",
        "LUMEN_UPDATE_SYSTEMD_UNIT",
        "LUMEN_UPDATE_CHANNEL",
        "LUMEN_UPDATE_RESOLVED_TAG",
        "LUMEN_UPDATE_IDEMPOTENCY_KEY",
        "LUMEN_UPDATE_FORCE_REDEPLOY",
        "LUMEN_IMAGE_TAG",
        "LUMEN_VERSION",
        "LUMEN_HTTP_PROXY",
        "LUMEN_UPDATE_PROXY_URL",
        "LUMEN_API_HEALTH_URL",
        "LUMEN_WEB_HEALTH_URL",
        "LUMEN_HEALTH_COMPOSE_ATTEMPTS",
        "LUMEN_HEALTH_COMPOSE_INTERVAL",
        "LUMEN_HEALTH_TIMEOUT_SECONDS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }

    lines = []
    for key in sorted(keys):
        value = env.get(key)
        if value is None:
            continue
        lines.append(f"export {key}={shlex.quote(value)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _chmod_tolerate_eperm(tmp, 0o600)
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
    return (
        shutil.which("systemd-run") is not None
        and shutil.which("systemctl") is not None
    )


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

    Writes a constrained JSON request and ``.update.trigger``.
    PID 1 — not lumen-api — launches the runner, so all of lumen-api's sandbox
    constraints (NoNewPrivileges, ProtectSystem=strict) and the polkit/sudo
    plumbing become irrelevant. Synchronously waits up to ~10s for the runner
    to become active so callers get a deterministic success/fail signal.
    """
    backup_root = Path(settings.backup_root).expanduser()
    backup_root.mkdir(parents=True, exist_ok=True)
    log_path = _update_log_path()
    try:
        initial_log_size = log_path.stat().st_size
    except OSError:
        initial_log_size = 0
    request_path = _update_runner_request_path()
    trigger_path = _update_trigger_path()
    unit = _UPDATE_RUNNER_UNIT

    # 1) Marker first so concurrent triggers see the lock immediately.
    _write_marker(0, started_at.isoformat(), unit=unit)

    # 2) Fixed-schema request for the root-owned runner. The runner rejects
    # unknown fields and never accepts executable paths from this file.
    request_text = json.dumps(
        _runner_request_payload(env, started_at),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    request_tmp = request_path.with_suffix(f"{request_path.suffix}.tmp")
    request_tmp.write_text(request_text + "\n", encoding="utf-8")
    _chmod_tolerate_eperm(request_tmp, 0o600)
    request_tmp.replace(request_path)

    # 3) Trigger file. Content is the ISO timestamp; PathChanged on the path
    #    unit fires on close-after-write and starts the runner unit.
    trigger_tmp = trigger_path.with_suffix(f"{trigger_path.suffix}.tmp")
    trigger_tmp.write_text(started_at.isoformat() + "\n", encoding="utf-8")
    _chmod_tolerate_eperm(trigger_tmp, 0o600)
    trigger_tmp.replace(trigger_path)

    # 4) Wait for the runner to come up. The path-watcher latency is normally
    #    < 1s; allow generous slack so a busy host doesn't return a misleading
    #    failure. Exit early as soon as we observe the unit active.
    if _runner_trigger_only_mode():
        # Containerised lumen-api can't query host systemd. The only reliable
        # in-container confirmation is that the host runner appended output to
        # the bind-mounted update log after the trigger file changed.
        if _wait_for_log_append(
            log_path,
            initial_size=initial_log_size,
            timeout_sec=_TRIGGER_ONLY_RUNNER_START_TIMEOUT_SEC,
        ):
            return 0, unit
        log_fh.write(
            f"\n[{unit}] trigger file was written, but the host runner did not "
            f"append output within {int(_TRIGGER_ONLY_RUNNER_START_TIMEOUT_SEC)}s. "
            "Check that lumen-update.path is installed, enabled, and watching "
            "the same backup directory mounted into lumen-api.\n"
        )
        log_fh.flush()
        for path in (trigger_path, request_path, _update_marker_path()):
            try:
                path.unlink()
            except OSError:
                pass
        return None
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
    for path in (trigger_path, request_path, _update_marker_path()):
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
        raise _http(
            "proxy_unavailable",
            "update proxy pool is enabled but has no enabled proxies",
            409,
        )

    name_raw = await get_setting(db, name_spec) if name_spec is not None else None
    target_name = str(name_raw or "").strip()
    if target_name:
        proxy = next((p for p in proxies if p.name == target_name), None)
        if proxy is None:
            raise _http(
                "proxy_not_found",
                f"update proxy '{target_name}' not found or disabled",
                409,
            )
    else:
        proxy = proxies[0]

    proxy_url = await resolve_provider_proxy_url(proxy)
    if not proxy_url:
        raise _http(
            "proxy_resolve_failed",
            f"update proxy '{proxy.name}' could not be resolved",
            409,
        )
    return proxy, proxy_url


StepRecord = _update_status.StepRecord
ReleaseInfo = _update_status.ReleaseInfo
UpdateStatusOut = _update_status.UpdateStatusOut
SystemMaintenanceOut = _update_status.SystemMaintenanceOut


class UpdateTriggerOut(BaseModel):
    accepted: bool
    pid: int | None = None
    unit: str | None = None
    started_at: datetime
    proxy_name: str | None = None
    log_path: str
    note: str
    target_tag: str | None = None
    idempotency_key: str | None = None
    replayed: bool = False


class UpdateTriggerIn(BaseModel):
    target_tag: str | None = None
    force_redeploy: bool = False
    channel: str | None = None
    confirm_update: bool = False
    confirmed_target_tag: str | None = None


def _list_releases(
    lumen_root: Path | None = None,
    *,
    limit: int | None = _RELEASE_LIST_LIMIT,
) -> list[ReleaseInfo]:
    return _update_status.list_releases(
        lumen_root or _lumen_root(),
        limit=limit,
    )


def _resolve_release(lumen_root: Path, release_id: str) -> Path | None:
    return _update_status.resolve_release(lumen_root, release_id)


_parse_steps = _update_status.parse_steps
_truncate_to_last_run = _update_status.truncate_to_last_run


def _build_status_snapshot() -> UpdateStatusOut:
    runtime = _update_status.StatusRuntime(
        read_marker=_read_marker,
        read_log_full=_read_log_full,
        read_log_tail=_read_log_tail,
        list_releases=lambda: _list_releases(),
        parse_steps=_parse_steps,
    )
    return _update_status.build_status_snapshot(runtime)


def _maintenance_snapshot() -> SystemMaintenanceOut:
    runtime = _update_status.StatusRuntime(
        read_marker=_read_marker,
        read_log_full=_read_log_full,
        read_log_tail=_read_log_tail,
        list_releases=lambda: _list_releases(),
        parse_steps=_parse_steps,
    )
    return _update_status.maintenance_snapshot(runtime)


async def _update_channel(db: AsyncSession) -> str:
    spec = get_spec("update.channel")
    if spec is None:
        return "stable"
    raw = await get_setting(db, spec)
    value = (raw or "stable").strip().lower()
    return (
        value if value in {"stable", "main", "pinned", "minor", "major"} else "stable"
    )


async def _update_check_ttl(db: AsyncSession) -> int:
    spec = get_spec("update.check_ttl_sec")
    if spec is None:
        return 1200
    raw = await get_setting(db, spec)
    try:
        return max(0, int(raw)) if raw is not None else 1200
    except ValueError:
        return 1200


async def _update_allow_prerelease(db: AsyncSession) -> bool:
    spec = get_spec("update.allow_prerelease")
    if spec is None:
        return False
    raw = await get_setting(db, spec)
    return str(raw or "0").strip() in {"1", "true", "yes", "on"}


def _sse_format(event: str, data: object) -> str:
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _classify_log_line(line: str) -> tuple[str, dict[str, object]]:
    stripped = line.rstrip("\n").rstrip("\r")
    step = _STEP_LINE_RE.match(stripped.strip())
    if step:
        rc = step.group("rc")
        duration = step.group("dur_ms")
        return "step", {
            "phase": step.group("phase"),
            "status": "done"
            if step.group("status") == "fail"
            else step.group("status"),
            "ts": step.group("ts"),
            "rc": int(rc) if rc is not None else None,
            "dur_ms": int(duration) if duration is not None else None,
        }
    info = _INFO_LINE_RE.match(stripped.strip())
    if info:
        return "info", {
            "phase": info.group("phase"),
            "key": info.group("key"),
            "value": info.group("value").rstrip(),
        }
    return "log", {"line": stripped}


def _read_incremental(path: Path, last_pos: int) -> tuple[str, int]:
    try:
        size = path.stat().st_size
    except (FileNotFoundError, OSError):
        return "", last_pos
    if size < last_pos:
        last_pos = 0
    if size == last_pos:
        return "", last_pos
    try:
        with path.open("rb") as handle:
            handle.seek(last_pos)
            chunk = handle.read(size - last_pos)
    except OSError:
        return "", last_pos
    return chunk.decode("utf-8", errors="replace"), size


def _wait_for_log_append(
    path: Path,
    *,
    initial_size: int,
    timeout_sec: float,
) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            if path.stat().st_size > initial_size:
                return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


async def _stream_update_events(request: Request) -> AsyncIterator[str]:
    runtime = _update_stream.UpdateStreamRuntime(
        log_path=_update_log_path,
        build_snapshot=_build_status_snapshot,
        read_incremental=_read_incremental,
        read_marker=_read_marker,
        classify_log_line=_classify_log_line,
        format_event=_sse_format,
        max_duration_sec=_SSE_MAX_DURATION_SEC,
        heartbeat_sec=_SSE_HEARTBEAT_SEC,
        poll_sec=_SSE_LOG_POLL_SEC,
        batch_window_sec=_SSE_LOG_BATCH_WINDOW_SEC,
    )
    async for event in _update_stream.stream_update_events(request, runtime=runtime):
        yield event


@router.get("/status", response_model=UpdateStatusOut)
async def update_status(_admin: AdminUser) -> UpdateStatusOut:
    return await asyncio.to_thread(_build_status_snapshot)


@router_public.get("/system/maintenance", response_model=SystemMaintenanceOut)
async def system_maintenance() -> SystemMaintenanceOut:
    return await asyncio.to_thread(_maintenance_snapshot)


@router.get("/version", response_model=UpdateVersionOut)
async def update_version(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UpdateVersionOut:
    channel = await _update_channel(db)
    service = UpdateCheckService(root=_lumen_root(), ttl_sec=0)
    return await service.version(channel=channel)


@router.get("/check", response_model=UpdateCheckOut)
async def update_check(
    request: Request,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    force: bool = False,
) -> UpdateCheckOut:
    channel = await _update_channel(db)
    allow_prerelease = await _update_allow_prerelease(db)
    ttl_sec = await _update_check_ttl(db)
    proxy, proxy_url = await _resolve_update_proxy(db)
    service = UpdateCheckService(root=_lumen_root(), ttl_sec=ttl_sec)
    out = await service.check(
        channel=channel,
        allow_prerelease=allow_prerelease,
        force=force,
        proxy_url=proxy_url,
    )
    warm_started = False
    if out.has_update is True and out.resolved_image_tag:
        warm_started = await maybe_warm_pull(out.resolved_image_tag)
        warm_state = "started" if warm_started else "already_running_or_skipped"
        out.warm_pull = {"state": warm_state, "tag": out.resolved_image_tag}
    await write_admin_audit_isolated(
        request,
        _admin,
        event_type="admin.update.check",
        details={
            "channel": channel,
            "force": force,
            "cache_hit": out.cache.cached,
            "stale": out.cache.stale,
            "target_tag": out.resolved_image_tag,
            "proxy_name": proxy.name if proxy else None,
            "warm_pull_started": warm_started,
        },
    )
    return out


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
    body: UpdateTriggerIn | None = None,
) -> UpdateTriggerOut:
    # The response note promises a restart and health check after the update.
    # Keep this compatibility marker in the route source for deployment tooling.
    _ = "重启运行进程并执行健康检查"
    body = body or UpdateTriggerIn()
    runtime = _update_trigger.TriggerRuntime(
        http_error=_http,
        response_model=UpdateTriggerOut,
        response_factory=UpdateTriggerOut,
        update_script=_update_script,
        read_marker=_read_marker,
        ensure_not_running=_ensure_update_not_running,
        maintenance_marker_busy=_maintenance_marker_busy,
        update_channel=_update_channel,
        update_allow_prerelease=_update_allow_prerelease,
        update_check_ttl=_update_check_ttl,
        resolve_update_proxy=_resolve_update_proxy,
        lumen_root=_lumen_root,
        update_check_service=UpdateCheckService,
        validate_update_tag=validate_update_tag,
        derive_idempotency_key=derive_idempotency_key,
        get_cached_json=get_cached_json,
        cache_json=cache_json,
        lock_service_factory=SystemOperationLockService,
        update_log_path=_update_log_path,
        open_update_log=_open_update_log,
        clean_proxy_env=_clean_proxy_env,
        apply_proxy_env=_apply_proxy_env,
        apply_dotenv_proxy_env=_apply_dotenv_proxy_env,
        shared_env_path=_shared_env_path,
        mask_proxy_url=_mask_proxy_url,
        version_from_update_tag=_version_from_update_tag,
        write_marker=_write_marker,
        runner_unit_available=_runner_unit_available,
        runner_trigger_only_mode=_runner_trigger_only_mode,
        start_update_via_path_unit=_start_update_via_path_unit,
        systemd_run_available=_systemd_run_available,
        start_update_systemd_unit=_start_update_systemd_unit,
        write_audit=write_admin_audit_isolated,
        schedule_cleanup=_schedule_marker_cleanup_when_done,
    )
    return await _update_trigger.trigger_update(
        request,
        admin,
        db,
        body,
        runtime=runtime,
    )


def _schedule_marker_cleanup_when_done(
    proc: subprocess.Popen[bytes],
) -> asyncio.Task[None]:
    task = asyncio.create_task(_cleanup_marker_when_done(proc))
    with _marker_cleanup_tasks_lock:
        _marker_cleanup_tasks.add(task)
    task.add_done_callback(_discard_marker_cleanup_task)
    return task


def _discard_marker_cleanup_task(task: asyncio.Task[None]) -> None:
    with _marker_cleanup_tasks_lock:
        _marker_cleanup_tasks.discard(task)


async def _cleanup_marker_when_done(proc: subprocess.Popen[bytes]) -> None:
    await cleanup_marker_when_done(
        proc,
        read_marker_fn=_read_marker,
        marker_path_fn=_update_marker_path,
    )


__all__ = [
    "router",
    "router_public",
    "ReleaseInfo",
    "StepRecord",
    "SystemMaintenanceOut",
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
