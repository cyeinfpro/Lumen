"""Systemd command construction for the admin update route."""

from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import shlex
import shutil
import subprocess
from typing import TextIO

from .admin_backups import chmod_tolerate_eperm


def systemd_unit_name(started_at) -> str:
    stamp = started_at.strftime("%Y%m%d%H%M%S")
    return f"lumen-update-{stamp}-{os.getpid()}.service"


def current_service_identity_properties() -> list[str]:
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    return ["--property", f"User={user}", "--property", f"Group={group}"]


def write_update_env_file(
    env: dict[str, str],
    unit: str,
    *,
    marker_path: Path,
) -> Path:
    path = marker_path.with_name(f".update.{unit}.env")
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
    lines = [
        f"export {key}={shlex.quote(value)}"
        for key in sorted(keys)
        if (value := env.get(key)) is not None
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    chmod_tolerate_eperm(tmp, 0o600)
    tmp.replace(path)
    return path


def systemd_run_command(
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
        cmd += current_service_identity_properties()
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


def systemd_run_inline_command(
    *,
    unit: str,
    root: Path,
    log_path: Path,
    inline_script: str,
    user_mode: bool = False,
) -> list[str]:
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
        cmd += current_service_identity_properties()
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


def systemd_run_available() -> bool:
    return (
        shutil.which("systemd-run") is not None
        and shutil.which("systemctl") is not None
    )


def run_systemd_command(
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


def systemd_run_attempts(
    *,
    unit: str,
    root: Path,
    script: Path,
    log_path: Path,
    env_file: Path,
    marker_path: Path,
) -> list[tuple[str, list[str]]]:
    system_cmd = systemd_run_command(
        unit=unit,
        root=root,
        script=script,
        log_path=log_path,
        env_file=env_file,
        marker_path=marker_path,
    )
    user_cmd = systemd_run_command(
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


def systemd_run_inline_attempts(
    *,
    unit: str,
    root: Path,
    log_path: Path,
    inline_script: str,
) -> list[tuple[str, list[str]]]:
    system_cmd = systemd_run_inline_command(
        unit=unit,
        root=root,
        log_path=log_path,
        inline_script=inline_script,
    )
    user_cmd = systemd_run_inline_command(
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


def log_attempt_failure(
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
