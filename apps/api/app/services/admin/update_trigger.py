"""Update trigger orchestration.

The route supplies a runtime object so existing monkeypatch and deployment
integration points remain at the route boundary without making this service
import a route module.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession


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
    write_marker: Callable[..., None]
    runner_unit_available: Callable[[], bool]
    runner_trigger_only_mode: Callable[[], bool]
    start_update_via_path_unit: Callable[..., tuple[int, str] | None]
    systemd_run_available: Callable[[], bool]
    start_update_systemd_unit: Callable[..., tuple[int, str] | None]
    write_audit: Callable[..., Awaitable[None]]
    schedule_cleanup: Callable[[subprocess.Popen[bytes]], Any]


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

    started_at = datetime.now(timezone.utc)
    log_fh = runtime.open_update_log()
    proc: subprocess.Popen[bytes] | None = None
    unit: str | None = None
    pid = 0
    launched = False
    release_reason = "launch_failed"
    script = runtime.update_script()
    try:
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
        if unit is None:
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
            runtime.write_marker(pid, started_at.isoformat())
        if unit is not None or pid:
            launched = True
            release_reason = "launched"
        return pid, unit, proc, started_at
    finally:
        log_fh.close()
        await lock_service.release(lock, succeeded=launched, reason=release_reason)


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
