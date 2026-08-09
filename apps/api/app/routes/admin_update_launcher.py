"""Privileged update launcher implementations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from typing import TextIO
import uuid

from .admin_update_marker import UpdateMarkerBusy
from .admin_maintenance_marker_lock import maintenance_marker_lock


_MAX_ADOPTION_RECEIPT_BYTES = 16 * 1024


def request_payload(
    env: dict[str, str],
    started_at: datetime,
    operation_id: str,
) -> dict[str, object]:
    """Build the narrow, non-executable request consumed by the host runner."""
    return {
        "schema": 2,
        "operation_id": operation_id,
        "target_tag": env["LUMEN_UPDATE_RESOLVED_TAG"],
        "channel": env["LUMEN_UPDATE_CHANNEL"],
        "force_redeploy": env.get("LUMEN_UPDATE_FORCE_REDEPLOY") == "1",
        "idempotency_key": env["LUMEN_UPDATE_IDEMPOTENCY_KEY"],
        "proxy_url": env.get("LUMEN_UPDATE_PROXY_URL"),
        "issued_at": started_at.isoformat(),
    }


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while persisting update launch state")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                raise
    finally:
        os.close(descriptor)


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    chmod: Callable[[Path, int], None],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_raw)
    try:
        try:
            os.fchmod(descriptor, mode)
        except PermissionError:
            pass
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _unlink_all(paths: tuple[Path, ...]) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def _marker_is_host_adopted(
    path: Path,
    *,
    operation_id: str,
    request_sha256: str,
) -> bool:
    with maintenance_marker_lock(path.parent):
        try:
            values: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    values[key] = value.strip()
            return (
                values.get("operation_id") == operation_id
                and values.get("request_sha256") == request_sha256
                and values.get("owner") == "host"
                and int(values.get("generation", "0")) >= 1
            )
        except (FileNotFoundError, OSError, ValueError):
            return False


def _unlink_api_marker(
    path: Path,
    *,
    operation_id: str,
    request_sha256: str,
) -> None:
    with maintenance_marker_lock(path.parent):
        try:
            values: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    values[key] = value.strip()
            if (
                values.get("operation_id") == operation_id
                and values.get("request_sha256") == request_sha256
                and values.get("owner") == "api"
                and int(values.get("generation", "0")) == 0
            ):
                path.unlink()
                _fsync_directory(path.parent)
        except (FileNotFoundError, OSError, ValueError):
            pass


def adoption_receipt_matches(
    path: Path,
    *,
    operation_id: str,
    request_sha256: str,
) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > _MAX_ADOPTION_RECEIPT_BYTES
        ):
            return False
        raw = os.read(descriptor, _MAX_ADOPTION_RECEIPT_BYTES + 1)
        if len(raw) != info.st_size:
            return False
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    generation = payload.get("generation") if isinstance(payload, dict) else None
    pid = payload.get("pid") if isinstance(payload, dict) else None
    return bool(
        isinstance(payload, dict)
        and payload.get("schema") == 1
        and payload.get("operation_id") == operation_id
        and payload.get("request_sha256") == request_sha256
        and payload.get("owner") == "host"
        and isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation >= 1
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and isinstance(payload.get("accepted_at"), str)
        and payload.get("status") == "accepted"
    )


@dataclass(frozen=True)
class PathUnitLaunchRuntime:
    backup_root: Path
    request_path: Path
    trigger_path: Path
    marker_path: Path
    receipt_path: Path
    unit: str
    write_marker: Callable[..., bool]
    request_payload: Callable[[dict[str, str], datetime, str], dict[str, object]]
    chmod: Callable[[Path, int], None]
    adoption_receipt_matches: Callable[..., bool]
    trigger_timeout_sec: float


def start_update_via_path_unit(
    *,
    runtime: PathUnitLaunchRuntime,
    env: dict[str, str],
    log_fh: TextIO,
    started_at: datetime,
) -> tuple[int, str] | None:
    backup_root = runtime.backup_root
    request_path = runtime.request_path
    trigger_path = runtime.trigger_path
    marker_path = runtime.marker_path
    receipt_path = runtime.receipt_path
    unit = runtime.unit
    write_marker = runtime.write_marker
    request_payload = runtime.request_payload
    chmod = runtime.chmod
    receipt_matches = runtime.adoption_receipt_matches
    trigger_timeout_sec = runtime.trigger_timeout_sec
    backup_root.mkdir(parents=True, exist_ok=True)

    operation_id = f"update-{uuid.uuid4().hex}"
    request_document = request_payload(env, started_at, operation_id)
    request_bytes = _canonical_json_bytes(request_document)
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    if not write_marker(
        0,
        started_at.isoformat(),
        unit,
        operation_id=operation_id,
        request_sha256=request_sha256,
    ):
        raise UpdateMarkerBusy("another update or rollback is already running")
    try:
        _atomic_write_bytes(
            request_path,
            request_bytes,
            mode=0o600,
            chmod=chmod,
        )
        _atomic_write_bytes(
            trigger_path,
            _canonical_json_bytes(
                {
                    "issued_at": started_at.isoformat(),
                    "operation_id": operation_id,
                    "request_sha256": request_sha256,
                    "schema": 1,
                }
            ),
            mode=0o600,
            chmod=chmod,
        )
    except Exception:
        _unlink_all((trigger_path, request_path))
        _unlink_api_marker(
            marker_path,
            operation_id=operation_id,
            request_sha256=request_sha256,
        )
        raise

    deadline = time.monotonic() + trigger_timeout_sec
    while time.monotonic() < deadline:
        if receipt_matches(
            receipt_path,
            operation_id=operation_id,
            request_sha256=request_sha256,
        ):
            return 0, unit
        time.sleep(0.25)
    log_fh.write(
        f"\n[{unit}] trigger file was written, but the host runner did not "
        f"publish a matching adoption receipt within {int(trigger_timeout_sec)}s. "
        "Check that lumen-update.path is installed, enabled, and watching "
        "the same backup directory mounted into lumen-api.\n"
    )
    log_fh.flush()
    if not _marker_is_host_adopted(
        marker_path,
        operation_id=operation_id,
        request_sha256=request_sha256,
    ):
        _unlink_all((trigger_path, request_path))
        _unlink_api_marker(
            marker_path,
            operation_id=operation_id,
            request_sha256=request_sha256,
        )
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
