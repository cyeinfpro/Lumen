"""Privileged update launcher implementations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time
from typing import TextIO

from .admin_update_marker import UpdateMarkerBusy


def _unlink_all(paths: tuple[Path, ...]) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


@dataclass(frozen=True)
class PathUnitLaunchRuntime:
    backup_root: Path
    log_path: Path
    request_path: Path
    trigger_path: Path
    marker_path: Path
    unit: str
    write_marker: Callable[[int, str, str | None], bool]
    request_payload: Callable[[dict[str, str], datetime], dict[str, object]]
    chmod: Callable[[Path, int], None]
    trigger_only_mode: Callable[[], bool]
    wait_for_log_append: Callable[..., bool]
    trigger_timeout_sec: float
    unit_is_running: Callable[[str], bool]


def start_update_via_path_unit(
    *,
    runtime: PathUnitLaunchRuntime,
    env: dict[str, str],
    log_fh: TextIO,
    started_at: datetime,
) -> tuple[int, str] | None:
    backup_root = runtime.backup_root
    log_path = runtime.log_path
    request_path = runtime.request_path
    trigger_path = runtime.trigger_path
    marker_path = runtime.marker_path
    unit = runtime.unit
    write_marker = runtime.write_marker
    request_payload = runtime.request_payload
    chmod = runtime.chmod
    trigger_only_mode = runtime.trigger_only_mode
    wait_for_log_append = runtime.wait_for_log_append
    trigger_timeout_sec = runtime.trigger_timeout_sec
    unit_is_running = runtime.unit_is_running
    backup_root.mkdir(parents=True, exist_ok=True)
    try:
        initial_log_size = log_path.stat().st_size
    except OSError:
        initial_log_size = 0

    if not write_marker(0, started_at.isoformat(), unit):
        raise UpdateMarkerBusy("another update or rollback is already running")
    request_text = json.dumps(
        request_payload(env, started_at),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    request_tmp = request_path.with_suffix(f"{request_path.suffix}.tmp")
    request_tmp.write_text(request_text + "\n", encoding="utf-8")
    chmod(request_tmp, 0o600)
    request_tmp.replace(request_path)

    trigger_tmp = trigger_path.with_suffix(f"{trigger_path.suffix}.tmp")
    trigger_tmp.write_text(started_at.isoformat() + "\n", encoding="utf-8")
    chmod(trigger_tmp, 0o600)
    trigger_tmp.replace(trigger_path)

    staged_paths = (trigger_path, request_path, marker_path)
    if trigger_only_mode():
        if wait_for_log_append(
            log_path,
            initial_size=initial_log_size,
            timeout_sec=trigger_timeout_sec,
        ):
            return 0, unit
        log_fh.write(
            f"\n[{unit}] trigger file was written, but the host runner did not "
            f"append output within {int(trigger_timeout_sec)}s. "
            "Check that lumen-update.path is installed, enabled, and watching "
            "the same backup directory mounted into lumen-api.\n"
        )
        log_fh.flush()
        _unlink_all(staged_paths)
        return None

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if unit_is_running(unit):
            return 0, unit
        time.sleep(0.3)
    log_fh.write(
        f"\n[{unit}] path-unit trigger did not activate within 15s; falling through.\n"
    )
    log_fh.flush()
    _unlink_all(staged_paths)
    return None


def start_update_systemd_unit(
    *,
    script: Path,
    env: dict[str, str],
    log_fh: TextIO,
    started_at: datetime,
    systemd_unit_name: Callable[[datetime], str],
    log_path: Path,
    marker_path: Path,
    write_env_file: Callable[[dict[str, str], str], Path],
    write_marker: Callable[[int, str, str | None], None],
    systemd_attempts: Callable[..., list[tuple[str, list[str]]]],
    run_systemd_command: Callable[
        [list[str], dict[str, str], Path],
        subprocess.CompletedProcess[str],
    ],
    log_attempt_failure: Callable[
        [TextIO, str, subprocess.CompletedProcess[str]],
        None,
    ],
) -> tuple[int, str] | None:
    root = script.parent.parent
    unit = systemd_unit_name(started_at)
    env = dict(env)
    env["LUMEN_UPDATE_SYSTEMD_UNIT"] = unit
    runtime_dir = f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    env_file = write_env_file(env, unit)
    if not write_marker(0, started_at.isoformat(), unit):
        _unlink_all((env_file,))
        raise UpdateMarkerBusy("another update or rollback is already running")

    for label, command in systemd_attempts(
        unit=unit,
        root=root,
        script=script,
        log_path=log_path,
        env_file=env_file,
        marker_path=marker_path,
    ):
        result = run_systemd_command(command, env, root)
        if result.returncode == 0:
            return 0, unit
        log_attempt_failure(log_fh, label, result)

    _unlink_all((marker_path, env_file))
    return None
