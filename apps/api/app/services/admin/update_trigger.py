"""Update trigger orchestration.

The route supplies a runtime object so existing monkeypatch and deployment
integration points remain at the route boundary without making this service
import a route module.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession


_UPDATE_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,240}$")
_UPDATE_JOURNAL_MAX_BYTES = 2 * 1024 * 1024
_UPDATE_RESUME_MAX_BYTES = 4096
_ACTIVE_UPDATE_JOURNAL_STATUSES = frozenset({"running", "failed"})
_CLEAN_UPDATE_JOURNAL_STATUSES = frozenset({"complete", "rolled_back"})
_UNRESOLVED_UPDATE_JOURNAL_STATUSES = frozenset(
    {"manual_required", "failed_recovered_original"}
)
_VALID_UPDATE_JOURNAL_STATUSES = (
    _ACTIVE_UPDATE_JOURNAL_STATUSES
    | _CLEAN_UPDATE_JOURNAL_STATUSES
    | _UNRESOLVED_UPDATE_JOURNAL_STATUSES
)


@dataclass(frozen=True)
class TriggerRuntime:
    http_error: Callable[[str, str, int], Exception]
    response_model: Any
    response_factory: Callable[..., Any]
    update_script: Callable[[], Path]
    read_marker: Callable[[], Any]
    ensure_not_running: Callable[[Any], None]
    maintenance_marker_busy: Callable[[], bool]
    update_channel: Callable[[AsyncSession], Awaitable[str]]
    update_allow_prerelease: Callable[[AsyncSession], Awaitable[bool]]
    update_check_ttl: Callable[[AsyncSession], Awaitable[int]]
    resolve_update_proxy: Callable[[AsyncSession], Awaitable[tuple[Any, str | None]]]
    lumen_root: Callable[[], Path]
    update_check_service: Any
    validate_update_tag: Callable[[str], str]
    derive_idempotency_key: Callable[..., str]
    get_cached_json: Callable[[str, str], Awaitable[dict[str, Any] | None]]
    cache_json: Callable[[str, str, Any, int], Awaitable[None]]
    lock_service_factory: Callable[..., Any]
    update_log_path: Callable[[], Path]
    open_update_log: Callable[[], Any]
    clean_proxy_env: Callable[[dict[str, str]], None]
    apply_proxy_env: Callable[[dict[str, str], str], None]
    apply_dotenv_proxy_env: Callable[[dict[str, str], Path], str | None]
    shared_env_path: Callable[[Path | None], Path]
    mask_proxy_url: Callable[[str], str]
    version_from_update_tag: Callable[[str], str | None]
    write_marker: Callable[..., bool]
    runner_unit_available: Callable[[], bool]
    runner_trigger_only_mode: Callable[[], bool]
    start_update_via_path_unit: Callable[..., tuple[int, str] | None]
    systemd_run_available: Callable[[], bool]
    start_update_systemd_unit: Callable[..., tuple[int, str] | None]
    write_audit: Callable[..., Awaitable[None]]
    schedule_cleanup: Callable[[subprocess.Popen[bytes]], Any]


@dataclass(frozen=True)
class _UpdateStateProblem:
    code: str
    message: str
    http_status: int


class _UpdateStateReadError(RuntimeError):
    pass


def _read_bounded_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _UpdateStateReadError(f"cannot inspect {label}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise _UpdateStateReadError(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise _UpdateStateReadError(f"{label} changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise _UpdateStateReadError(f"{label} changed while being read")
        try:
            current = path.lstat()
        except OSError as exc:
            raise _UpdateStateReadError(f"cannot revalidate {label}") from exc
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            raise _UpdateStateReadError(f"{label} path changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_recovery_operation_id(path: Path) -> str | None:
    raw = _read_bounded_regular_file(
        path,
        label="update recovery marker",
        max_bytes=_UPDATE_RESUME_MAX_BYTES,
    )
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _UpdateStateReadError("update recovery marker is not UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != 1 or not _UPDATE_OPERATION_ID_RE.fullmatch(lines[0]):
        raise _UpdateStateReadError("update recovery marker is invalid")
    return lines[0]


def _read_update_journal(path: Path) -> dict[str, Any] | None:
    raw = _read_bounded_regular_file(
        path,
        label="update journal",
        max_bytes=_UPDATE_JOURNAL_MAX_BYTES,
    )
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _UpdateStateReadError("update journal is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 2:
        raise _UpdateStateReadError("update journal schema is invalid")
    operation_id = payload.get("operation_id")
    status_value = payload.get("status")
    if (
        not isinstance(operation_id, str)
        or not _UPDATE_OPERATION_ID_RE.fullmatch(operation_id)
        or not isinstance(status_value, str)
        or status_value not in _VALID_UPDATE_JOURNAL_STATUSES
    ):
        raise _UpdateStateReadError("update journal identity or status is invalid")
    return payload


def _path_exists(path: Path, *, label: str) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _UpdateStateReadError(f"cannot inspect {label}") from exc
    return True


def _default_update_state_problem(runtime: TriggerRuntime) -> _UpdateStateProblem | None:
    shared_root = runtime.lumen_root() / "shared"
    journal_path = shared_root / ".update-journal.json"
    resume_path = shared_root / ".update-resume"
    backup_root = runtime.update_log_path().parent
    request_path = backup_root / ".update.request.json"
    trigger_path = backup_root / ".update.trigger"
    try:
        resume_operation_id = _read_recovery_operation_id(resume_path)
        journal = _read_update_journal(journal_path)
        request_exists = _path_exists(request_path, label="update request")
        trigger_exists = _path_exists(trigger_path, label="update trigger")
    except _UpdateStateReadError:
        return _UpdateStateProblem(
            code="update_recovery_state_unreadable",
            message=(
                "cannot safely inspect existing update recovery state; "
                "inspect the default update journal and resume marker"
            ),
            http_status=503,
        )

    journal_status = str(journal["status"]) if journal is not None else None
    journal_operation_id = (
        str(journal["operation_id"]) if journal is not None else None
    )
    if resume_operation_id is not None:
        if (
            journal_status not in _ACTIVE_UPDATE_JOURNAL_STATUSES
            or journal_operation_id != resume_operation_id
        ):
            message = (
                "update recovery marker and journal are inconsistent; "
                "recover or archive them before starting another update"
            )
        else:
            message = (
                "an unfinished update recovery state exists; "
                "resume it before starting another update"
            )
        return _UpdateStateProblem(
            code="update_recovery_pending",
            message=message,
            http_status=409,
        )
    if journal_status in (
        _ACTIVE_UPDATE_JOURNAL_STATUSES
        | _UNRESOLVED_UPDATE_JOURNAL_STATUSES
    ):
        return _UpdateStateProblem(
            code="update_recovery_pending",
            message=(
                "an unfinished update journal exists without a consumable recovery "
                "state; recover or archive it before starting another update"
            ),
            http_status=409,
        )
    if request_exists or trigger_exists:
        return _UpdateStateProblem(
            code="update_recovery_pending",
            message=(
                "a pending update request or trigger already exists; "
                "let the host runner consume or recover it before starting another update"
            ),
            http_status=409,
        )
    return None


async def _resolve_target(
    db: AsyncSession,
    body: Any,
    *,
    runtime: TriggerRuntime,
) -> tuple[str, Any, str | None, str]:
    channel = (body.channel or await runtime.update_channel(db)).strip().lower()
    if channel not in {"stable", "main", "pinned", "minor", "major"}:
        raise runtime.http_error("invalid_channel", "invalid update channel", 422)
    allow_prerelease = await runtime.update_allow_prerelease(db)
    ttl_sec = await runtime.update_check_ttl(db)
    proxy, proxy_url = await runtime.resolve_update_proxy(db)
    target_tag = (body.target_tag or "").strip()
    if target_tag:
        try:
            target_tag = runtime.validate_update_tag(target_tag)
        except ValueError as exc:
            raise runtime.http_error(
                "invalid_target_tag", "invalid update target tag", 422
            ) from exc
    else:
        service = runtime.update_check_service(
            root=runtime.lumen_root(), ttl_sec=ttl_sec
        )
        result = await service.check(
            channel=channel,
            allow_prerelease=allow_prerelease,
            force=body.force_redeploy,
            proxy_url=proxy_url,
        )
        target_tag = result.resolved_image_tag
    if target_tag == "latest":
        raise runtime.http_error(
            "invalid_target_tag",
            "mutable latest is not accepted; use a release channel or concrete tag",
            422,
        )
    return channel, proxy, proxy_url, target_tag


async def _require_confirmation(
    request: Any,
    admin: Any,
    body: Any,
    *,
    channel: str,
    target_tag: str,
    runtime: TriggerRuntime,
) -> None:
    confirmed = (body.confirmed_target_tag or "").strip()
    if body.confirm_update and confirmed == target_tag:
        return
    await runtime.write_audit(
        request,
        admin,
        event_type="admin.update.confirmation_required",
        details={
            "target_tag": target_tag,
            "confirmed_target_tag": confirmed or None,
            "force_redeploy": body.force_redeploy,
            "channel": channel,
        },
    )
    raise runtime.http_error(
        "update_confirmation_required",
        "confirm_update=true with matching confirmed_target_tag is required to start an update",
        403,
    )


def _idempotency_key(
    request: Any, admin: Any, body: Any, target_tag: str, *, runtime: TriggerRuntime
) -> str:
    explicit = request.headers.get("Idempotency-Key")
    if explicit:
        return explicit
    payload = json.dumps(
        {
            "target_tag": target_tag,
            "confirmed_target_tag": (body.confirmed_target_tag or "").strip(),
            "force_redeploy": body.force_redeploy,
            "channel": (body.channel or "").strip().lower(),
            "confirm_update": body.confirm_update,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return runtime.derive_idempotency_key(
        admin.id,
        request.url.path,
        payload,
        int(time.time() // 30),
    )


def _kill_launched_script(proc: subprocess.Popen[bytes]) -> None:
    """Abort a freshly-spawned update.sh after losing the marker claim.

    The script runs in its own session (start_new_session=True), so
    SIGTERM to the group reaches update.sh as well as its bash wrapper;
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


async def _start_update_execution(
    *,
    script: Path,
    env: dict[str, str],
    log_fh: Any,
    started_at: datetime,
    runtime: TriggerRuntime,
) -> tuple[int, str | None, subprocess.Popen[bytes] | None]:
    pid = 0
    unit: str | None = None
    proc: subprocess.Popen[bytes] | None = None
    if await asyncio.to_thread(runtime.runner_unit_available):
        outcome = await asyncio.to_thread(
            runtime.start_update_via_path_unit,
            env=env,
            log_fh=log_fh,
            started_at=started_at,
        )
        if outcome is not None:
            pid, unit = outcome
        elif runtime.runner_trigger_only_mode():
            raise runtime.http_error(
                "update_runner_not_started",
                "已写入一键更新触发文件，但宿主机 lumen-update-runner.service 未开始执行；"
                "请确认 lumen-update.path 已安装并启用，且监听的数据目录与当前 LUMEN_DATA_ROOT 一致。",
                503,
            )
    if unit is None and runtime.systemd_run_available():
        outcome = runtime.start_update_systemd_unit(
            script=script,
            env=env,
            log_fh=log_fh,
            started_at=started_at,
        )
        if outcome is not None:
            pid, unit = outcome
    if unit is not None:
        return pid, unit, proc

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
    if runtime.write_marker(pid, started_at.isoformat()):
        return pid, unit, proc
    _kill_launched_script(proc)
    raise runtime.http_error(
        "update_running",
        "Lumen update is already running; wait for it to finish first",
        409,
    )


async def _launch(
    request: Any,
    admin: Any,
    body: Any,
    db: AsyncSession,
    *,
    channel: str,
    proxy: Any,
    proxy_url: str | None,
    target_tag: str,
    idempotency_key: str,
    runtime: TriggerRuntime,
) -> tuple[int, str | None, subprocess.Popen[bytes] | None, datetime]:
    lock_service = runtime.lock_service_factory(
        fallback_busy=lambda: (
            runtime.read_marker() is not None or runtime.maintenance_marker_busy()
        )
    )
    try:
        lock = await lock_service.acquire(
            operation="update", owner=str(admin.id), ttl_sec=1800
        )
    except Exception as exc:
        if exc.__class__.__name__ != "LockBusy":
            raise
        raise runtime.http_error(
            "update_running",
            "Lumen update is already running; wait for it to finish first",
            409,
        ) from exc

    log_fh: Any | None = None
    launched = False
    release_reason = "launch_failed"
    try:
        problem = await asyncio.to_thread(_default_update_state_problem, runtime)
        if problem is not None:
            release_reason = problem.code
            raise runtime.http_error(
                problem.code,
                problem.message,
                problem.http_status,
            )
        script = runtime.update_script()
        started_at = datetime.now(timezone.utc)
        try:
            log_fh = runtime.open_update_log()
            _write_trigger_log(
                log_fh,
                started_at=started_at,
                admin_id=admin.id,
                proxy=proxy,
                target_tag=target_tag,
                body=body,
                idempotency_key=idempotency_key,
                proxy_url=proxy_url,
                runtime=runtime,
            )
            env = _build_update_env(
                body,
                channel=channel,
                target_tag=target_tag,
                idempotency_key=idempotency_key,
                proxy_url=proxy_url,
                script=script,
                log_fh=log_fh,
                runtime=runtime,
            )
        except OSError as exc:
            release_reason = "update_log_unwritable"
            raise runtime.http_error(
                "update_log_unwritable",
                "the update log is not writable; repair its directory permissions",
                503,
            ) from exc
        try:
            pid, unit, proc = await _start_update_execution(
                script=script,
                env=env,
                log_fh=log_fh,
                started_at=started_at,
                runtime=runtime,
            )
            launched = True
            release_reason = "launched"
            return pid, unit, proc, started_at
        except Exception as exc:
            if exc.__class__.__name__ != "UpdateMarkerBusy":
                raise
            raise runtime.http_error(
                "update_running",
                "Lumen update is already running; wait for it to finish first",
                409,
            ) from exc
    finally:
        try:
            if log_fh is not None:
                log_fh.close()
        finally:
            await lock_service.release(
                lock,
                succeeded=launched,
                reason=release_reason,
            )


def _write_trigger_log(
    log_fh: Any,
    *,
    started_at: datetime,
    admin_id: Any,
    proxy: Any,
    target_tag: str,
    body: Any,
    idempotency_key: str,
    proxy_url: str | None,
    runtime: TriggerRuntime,
) -> None:
    log_fh.write(
        f"\n=== update trigger at={started_at.isoformat()} user={admin_id} "
        f"proxy={proxy.name if proxy else 'none'} ===\n"
    )
    log_fh.write(
        f"::lumen-info:: phase=check key=idempotency_key value={idempotency_key}\n"
    )
    log_fh.write(
        "::lumen-info:: phase=check key=resolved_tag_source value="
        f"{body.target_tag and 'override' or 'resolved'}\n"
    )
    log_fh.write(f"::lumen-info:: phase=check key=resolved_tag value={target_tag}\n")
    if proxy_url:
        log_fh.write(f"proxy_url={runtime.mask_proxy_url(proxy_url)}\n")
    log_fh.flush()


def _build_update_env(
    body: Any,
    *,
    channel: str,
    target_tag: str,
    idempotency_key: str,
    proxy_url: str | None,
    script: Path,
    log_fh: Any,
    runtime: TriggerRuntime,
) -> dict[str, str]:
    env = os.environ.copy()
    runtime.clean_proxy_env(env)
    if proxy_url:
        runtime.apply_proxy_env(env, proxy_url)
    else:
        dotenv_proxy = runtime.apply_dotenv_proxy_env(
            env, runtime.shared_env_path(script)
        )
        if dotenv_proxy:
            log_fh.write(f"proxy_url={runtime.mask_proxy_url(dotenv_proxy)}\n")
            log_fh.flush()
    env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
    env.setdefault("no_proxy", "127.0.0.1,localhost,::1")
    env.update(
        {
            "LUMEN_UPDATE_NONINTERACTIVE": "1",
            "LUMEN_UPDATE_MODE": env.get("LUMEN_UPDATE_MODE", "fast"),
            "LUMEN_UPDATE_GIT_PULL": env.get("LUMEN_UPDATE_GIT_PULL", "1"),
            "LUMEN_UPDATE_BUILD": env.get("LUMEN_UPDATE_BUILD", "0"),
            "LUMEN_UPDATE_CHANNEL": channel,
            "LUMEN_UPDATE_RESOLVED_TAG": target_tag,
            "LUMEN_UPDATE_IDEMPOTENCY_KEY": idempotency_key,
            "LUMEN_IMAGE_TAG": target_tag,
        }
    )
    version = runtime.version_from_update_tag(target_tag)
    if version:
        env["LUMEN_VERSION"] = version
    if body.force_redeploy:
        env["LUMEN_UPDATE_FORCE_REDEPLOY"] = "1"
    return env


async def trigger_update(
    request: Any,
    admin: Any,
    db: AsyncSession,
    body: Any,
    *,
    runtime: TriggerRuntime,
) -> Any:
    script = runtime.update_script()
    if not script.is_file():
        raise runtime.http_error("script_missing", f"missing {script}", 500)
    marker = await asyncio.to_thread(runtime.read_marker)
    runtime.ensure_not_running(marker)
    if await asyncio.to_thread(runtime.maintenance_marker_busy):
        raise runtime.http_error(
            "maintenance_busy",
            "another maintenance operation is running",
            409,
        )

    channel, proxy, proxy_url, target_tag = await _resolve_target(
        db, body, runtime=runtime
    )
    await _require_confirmation(
        request,
        admin,
        body,
        channel=channel,
        target_tag=target_tag,
        runtime=runtime,
    )
    idempotency_key = _idempotency_key(
        request, admin, body, target_tag, runtime=runtime
    )
    cached = await runtime.get_cached_json("lumen:update:idempotency", idempotency_key)
    if cached is not None:
        replayed = runtime.response_model.model_validate({**cached, "replayed": True})
        await runtime.write_audit(
            request,
            admin,
            event_type="admin.update.trigger",
            details={
                "pid": replayed.pid,
                "unit": replayed.unit,
                "proxy_name": replayed.proxy_name,
                "target_tag": replayed.target_tag,
                "idempotency_key": idempotency_key,
                "cache_hit": True,
                "confirmed": True,
            },
        )
        return replayed

    pid, unit, proc, started_at = await _launch(
        request,
        admin,
        body,
        db,
        channel=channel,
        proxy=proxy,
        proxy_url=proxy_url,
        target_tag=target_tag,
        idempotency_key=idempotency_key,
        runtime=runtime,
    )
    response = runtime.response_factory(
        accepted=True,
        pid=pid or None,
        unit=unit,
        started_at=started_at,
        proxy_name=proxy.name if proxy else None,
        log_path=str(runtime.update_log_path()),
        note="更新已在后台启动；期间服务可能短暂不可用，脚本会在完成后重启运行进程并执行健康检查。",
        target_tag=target_tag,
        idempotency_key=idempotency_key,
        replayed=False,
    )
    await runtime.cache_json(
        "lumen:update:idempotency", idempotency_key, response, 86400
    )
    await runtime.write_audit(
        request,
        admin,
        event_type="admin.update.trigger",
        details={
            "pid": pid or None,
            "unit": unit,
            "proxy_name": proxy.name if proxy else None,
            "target_tag": target_tag,
            "idempotency_key": idempotency_key,
            "cache_hit": False,
            "confirmed": True,
        },
    )
    if proc is not None:
        runtime.schedule_cleanup(proc)
    return response
