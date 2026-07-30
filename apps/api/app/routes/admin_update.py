"""Admin one-click Lumen update route."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time  # noqa: F401
from datetime import datetime
from pathlib import Path
from typing import Annotated, AsyncIterator, TextIO

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
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
from .admin_proxies import load_proxies
from .admin_backups import (
    chmod_tolerate_eperm,
    discover_scripts_dir,
    maintenance_marker_busy,
    open_private_append,
)
from . import admin_update_environment as _update_environment
from . import admin_update_launcher as _update_launcher
from . import admin_update_marker as _update_marker
from . import admin_update_preferences as _update_preferences
from . import admin_update_runtime as _update_runtime
from . import admin_update_schemas as _update_schemas
from . import admin_update_streaming as _update_streaming
from . import admin_update_systemd as _update_systemd


_MARKER_CLEANUP_RUNTIME_STATE_KEY = _update_runtime.MARKER_CLEANUP_RUNTIME_STATE_KEY
_MarkerCleanupRuntime = _update_runtime.MarkerCleanupRuntime


def _marker_cleanup_runtime(request: Request) -> _MarkerCleanupRuntime:
    return _update_runtime.marker_cleanup_runtime(request)


_marker_cleanup_lifespan = _update_runtime.marker_cleanup_lifespan

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

_LUMEN_ROOT = os.environ.get("LUMEN_ROOT", "/opt/lumen")
_RELEASE_LIST_LIMIT = 10
_TRIGGER_DELIMITER_RE = re.compile(
    r"^=== update (?:trigger|unit started) ", re.MULTILINE
)

_SSE_HEARTBEAT_SEC = 15.0
_SSE_MAX_DURATION_SEC = 60 * 60  # 1h hard cap to prevent leaks
_SSE_LOG_POLL_SEC = 0.3  # tail-F poll interval
_SSE_LOG_BATCH_WINDOW_SEC = 0.2  # coalesce raw log lines into bursts
_TRIGGER_ONLY_RUNNER_START_TIMEOUT_SEC = 15.0
_SEMVER_UPDATE_TAG_RE = re.compile(
    r"^v(?P<version>[0-9]+(?:\.[0-9]+){2}(?:-[0-9A-Za-z.-]+)?)$"
)


UpdateMarker = _update_marker.UpdateMarker


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
    return discover_scripts_dir() / "update.sh"


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
    return _update_environment.read_dotenv_value(path, key)


def _shared_env_path(script: Path | None = None) -> Path:
    return _update_environment.shared_env_path(
        script,
        configured_path=os.environ.get("LUMEN_SHARED_ENV", "").strip(),
        lumen_root=_lumen_root,
    )


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
    return _update_marker.pid_is_running(pid)


def _marker_is_stale(started_at: str | None) -> bool:
    return _update_marker.marker_is_stale(
        started_at,
        stale_after_seconds=_PID_MARKER_STALE_AFTER_SECONDS,
    )


def _unit_is_running(unit: str) -> bool:
    return _update_marker.unit_is_running(unit)


def _parse_marker_text(raw: str) -> UpdateMarker:
    return _update_marker.parse_marker_text(raw)


def _marker_is_live(marker: UpdateMarker) -> bool:
    return _update_marker.marker_is_live(
        marker,
        trigger_only_mode=_runner_trigger_only_mode,
        marker_is_stale_fn=_marker_is_stale,
        unit_is_running_fn=_unit_is_running,
        pid_is_running_fn=_pid_is_running,
    )


def _read_marker() -> UpdateMarker | None:
    return _update_marker.read_marker(
        _update_marker_path(),
        parse_marker=_parse_marker_text,
        marker_is_live_fn=_marker_is_live,
    )


def _write_marker(pid: int, started_at: str, unit: str | None = None) -> None:
    _update_marker.write_marker(
        _update_marker_path(),
        pid=pid,
        started_at=started_at,
        unit=unit,
        chmod=chmod_tolerate_eperm,
    )


def _open_update_log() -> TextIO:
    return open_private_append(_update_log_path())


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
    _update_environment.clean_proxy_env(env)


def _apply_proxy_env(env: dict[str, str], proxy_url: str) -> None:
    _update_environment.apply_proxy_env(env, proxy_url)


def _proxy_url_from_env_file(path: Path) -> str | None:
    return _update_environment.proxy_url_from_env_file(path)


def _apply_dotenv_proxy_env(env: dict[str, str], env_file: Path) -> str | None:
    return _update_environment.apply_dotenv_proxy_env(env, env_file)


def _mask_proxy_url(proxy_url: str) -> str:
    return _update_environment.mask_proxy_url(proxy_url)


def _systemd_unit_name(started_at: datetime) -> str:
    return _update_systemd.systemd_unit_name(started_at)


def _current_service_identity_properties() -> list[str]:
    return _update_systemd.current_service_identity_properties()


def _write_update_env_file(env: dict[str, str], unit: str) -> Path:
    return _update_systemd.write_update_env_file(
        env,
        unit,
        marker_path=_update_marker_path(),
    )


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
    return _update_systemd.systemd_run_command(
        unit=unit,
        root=root,
        script=script,
        log_path=log_path,
        env_file=env_file,
        marker_path=marker_path,
        user_mode=user_mode,
    )


def _systemd_run_inline_command(
    *,
    unit: str,
    root: Path,
    log_path: Path,
    inline_script: str,
    user_mode: bool = False,
) -> list[str]:
    return _update_systemd.systemd_run_inline_command(
        unit=unit,
        root=root,
        log_path=log_path,
        inline_script=inline_script,
        user_mode=user_mode,
    )


def _systemd_run_available() -> bool:
    return _update_systemd.systemd_run_available()


def _run_systemd_command(
    command: list[str],
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return _update_systemd.run_systemd_command(command, env, cwd)


def _systemd_run_attempts(
    *,
    unit: str,
    root: Path,
    script: Path,
    log_path: Path,
    env_file: Path,
    marker_path: Path,
) -> list[tuple[str, list[str]]]:
    return _update_systemd.systemd_run_attempts(
        unit=unit,
        root=root,
        script=script,
        log_path=log_path,
        env_file=env_file,
        marker_path=marker_path,
    )


def _systemd_run_inline_attempts(
    *,
    unit: str,
    root: Path,
    log_path: Path,
    inline_script: str,
) -> list[tuple[str, list[str]]]:
    return _update_systemd.systemd_run_inline_attempts(
        unit=unit,
        root=root,
        log_path=log_path,
        inline_script=inline_script,
    )


def _log_attempt_failure(
    log_fh: TextIO,
    label: str,
    result: subprocess.CompletedProcess[str],
) -> None:
    _update_systemd.log_attempt_failure(log_fh, label, result)


def _start_update_via_path_unit(
    *,
    env: dict[str, str],
    log_fh: TextIO,
    started_at: datetime,
) -> tuple[int, str] | None:
    return _update_launcher.start_update_via_path_unit(
        runtime=_update_launcher.PathUnitLaunchRuntime(
            backup_root=Path(settings.backup_root).expanduser(),
            log_path=_update_log_path(),
            request_path=_update_runner_request_path(),
            trigger_path=_update_trigger_path(),
            marker_path=_update_marker_path(),
            unit=_UPDATE_RUNNER_UNIT,
            write_marker=_write_marker,
            request_payload=_runner_request_payload,
            chmod=chmod_tolerate_eperm,
            trigger_only_mode=_runner_trigger_only_mode,
            wait_for_log_append=_wait_for_log_append,
            trigger_timeout_sec=_TRIGGER_ONLY_RUNNER_START_TIMEOUT_SEC,
            unit_is_running=_unit_is_running,
        ),
        env=env,
        log_fh=log_fh,
        started_at=started_at,
    )


def _start_update_systemd_unit(
    *,
    script: Path,
    env: dict[str, str],
    log_fh: TextIO,
    started_at: datetime,
) -> tuple[int, str] | None:
    return _update_launcher.start_update_systemd_unit(
        script=script,
        env=env,
        log_fh=log_fh,
        started_at=started_at,
        systemd_unit_name=_systemd_unit_name,
        log_path=_update_log_path(),
        marker_path=_update_marker_path(),
        write_env_file=_write_update_env_file,
        write_marker=_write_marker,
        systemd_attempts=_systemd_run_attempts,
        run_systemd_command=_run_systemd_command,
        log_attempt_failure=_log_attempt_failure,
    )


async def _resolve_update_proxy(
    db: AsyncSession,
) -> tuple[ProviderProxyDefinition | None, str | None]:
    return await _update_preferences.resolve_update_proxy(
        db,
        get_spec=get_spec,
        get_setting=get_setting,
        load_proxies=load_proxies,
        resolve_proxy_url=resolve_provider_proxy_url,
        http_error=_http,
    )


StepRecord = _update_status.StepRecord
ReleaseInfo = _update_status.ReleaseInfo
UpdateStatusOut = _update_status.UpdateStatusOut
SystemMaintenanceOut = _update_status.SystemMaintenanceOut


UpdateTriggerOut = _update_schemas.UpdateTriggerOut
UpdateTriggerIn = _update_schemas.UpdateTriggerIn


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
    return await _update_preferences.update_channel(
        db,
        get_spec=get_spec,
        get_setting=get_setting,
    )


async def _update_check_ttl(db: AsyncSession) -> int:
    return await _update_preferences.update_check_ttl(
        db,
        get_spec=get_spec,
        get_setting=get_setting,
    )


async def _update_allow_prerelease(db: AsyncSession) -> bool:
    return await _update_preferences.update_allow_prerelease(
        db,
        get_spec=get_spec,
        get_setting=get_setting,
    )


def _sse_format(event: str, data: object) -> str:
    return _update_streaming.sse_format(event, data)


def _classify_log_line(line: str) -> tuple[str, dict[str, object]]:
    return _update_streaming.classify_log_line(line)


def _read_incremental(path: Path, last_pos: int) -> tuple[str, int]:
    return _update_streaming.read_incremental(path, last_pos)


def _wait_for_log_append(
    path: Path,
    *,
    initial_size: int,
    timeout_sec: float,
) -> bool:
    return _update_streaming.wait_for_log_append(
        path,
        initial_size=initial_size,
        timeout_sec=timeout_sec,
    )


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
    return StreamingResponse(
        _stream_update_events(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
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
        maintenance_marker_busy=maintenance_marker_busy,
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
        schedule_cleanup=lambda proc: _schedule_marker_cleanup_when_done(
            _marker_cleanup_runtime(request),
            proc,
        ),
    )
    return await _update_trigger.trigger_update(
        request,
        admin,
        db,
        body,
        runtime=runtime,
    )


def _schedule_marker_cleanup_when_done(
    runtime: _MarkerCleanupRuntime,
    proc: subprocess.Popen[bytes],
) -> asyncio.Task[None]:
    return runtime.schedule(proc, _cleanup_marker_when_done)


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

# Stable peer-route integration contract.
list_releases = _list_releases
log_attempt_failure = _log_attempt_failure
lumen_root = _lumen_root
open_update_log = _open_update_log
read_marker = _read_marker
resolve_release = _resolve_release
run_systemd_command = _run_systemd_command
systemd_run_available = _systemd_run_available
systemd_run_inline_attempts = _systemd_run_inline_attempts
update_log_path = _update_log_path
update_marker_path = _update_marker_path
write_marker = _write_marker
