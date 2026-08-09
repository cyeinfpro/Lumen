"""Stateless helpers for admin backup trigger and process output handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import stat
import tempfile
from typing import Any

from . import admin_backup_catalog as _backup_catalog
from .admin_backup_fs import chmod_tolerate_eperm as _chmod_tolerate_eperm


class BackupTriggerNotStarted(RuntimeError):
    """Raised when the host runner does not adopt a trigger in time."""


class BackupAttemptTimeout(TimeoutError):
    """Raised when a trigger is adopted but no committed pair appears."""


_MAX_ADOPTION_RECEIPT_BYTES = 16 * 1024


@dataclass(frozen=True)
class BackupAttemptRuntime:
    backup_script: Path
    backup_root: Path
    marker: Path
    operation_id: str
    started_at: datetime
    trigger_path: Path
    trigger_timeout_seconds: int
    timeout_seconds: int
    write_trigger: Any
    marker_is_adopted: Any
    unlink_marker: Any
    unlink_owned_marker: Any
    wait_for_pair: Any
    run_script: Any


async def wait_for_log_append(
    path: Path,
    *,
    initial_size: int,
    timeout_sec: float,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    while loop.time() < deadline:
        try:
            if path.stat().st_size > initial_size:
                return True
        except OSError:
            pass
        await asyncio.sleep(0.25)
    return False


def timestamp_from_backup_stdout(
    stdout: str,
    started_at: datetime,
    operation_id: str | None,
    *,
    allow_legacy: bool = False,
) -> str | None:
    started_second = started_at.astimezone(timezone.utc).replace(microsecond=0)
    lines = (stdout or "").splitlines()
    timestamp = _structured_timestamp(lines, started_second, operation_id)
    if timestamp is not None:
        return timestamp

    # Legacy text has no operation identity and is therefore available only to
    # explicitly opted-in callers that have no operation_id to bind.
    if not allow_legacy or operation_id is not None:
        return None
    return _legacy_timestamp(lines, started_second)


def _structured_timestamp(
    lines: list[str],
    started_second: datetime,
    operation_id: str | None,
) -> str | None:
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if (
            operation_id is None
            or not isinstance(payload, dict)
            or payload.get("operation_id") != operation_id
        ):
            continue
        ts = payload.get("timestamp")
        if not isinstance(ts, str) or not _backup_catalog.TIMESTAMP_RE.fullmatch(ts):
            continue
        completed_at = datetime.strptime(ts, "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc
        )
        if completed_at >= started_second:
            return ts
    return None


def _legacy_timestamp(lines: list[str], started_second: datetime) -> str | None:
    for line in reversed(lines):
        timestamp = _legacy_timestamp_from_line(line)
        if timestamp is not None and _timestamp_not_before(timestamp, started_second):
            return timestamp
    return None


def _legacy_timestamp_from_line(line: str) -> str | None:
    if "complete" not in line.lower():
        return None
    if "backup " in line:
        parts = line.split()
        for index, token in enumerate(parts[:-1]):
            timestamp = parts[index + 1].rstrip(":")
            if token == "backup" and _backup_catalog.TIMESTAMP_RE.fullmatch(timestamp):
                return timestamp
    match = re.search(r"\b([0-9]{8}-[0-9]{6})\b", line)
    return match.group(1) if match else None


def _timestamp_not_before(timestamp: str, started_second: datetime) -> bool:
    completed_at = datetime.strptime(timestamp, "%Y%m%d-%H%M%S").replace(
        tzinfo=timezone.utc
    )
    return completed_at >= started_second


def backup_script_was_skipped(output: str) -> bool:
    lowered = (output or "").lower()
    return ("skipped:" in lowered or "deferred:" in lowered) and (
        "maintenance lock" in lowered or "already running" in lowered
    )


def write_backup_trigger(
    path: Path,
    started_at: datetime,
    operation_id: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(
        json.dumps(
            {
                "operation_id": operation_id,
                "owner": "api",
                "generation": 0,
                "started_at": started_at.isoformat(),
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    _chmod_tolerate_eperm(tmp, 0o600)
    tmp.replace(path)


def restore_request_sha256(operation_id: str, timestamp: str) -> str:
    encoded = (
        json.dumps(
            {
                "operation_id": operation_id,
                "schema": 2,
                "timestamp": timestamp,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while persisting backup runtime state")
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


def _atomic_write_bytes(path: Path, payload: bytes, *, mode: int) -> None:
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
        _chmod_tolerate_eperm(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def try_write_pid_marker(
    path: Path,
    pid: int,
    started_at: datetime,
    *,
    unit: str | None,
    operation_id: str | None,
    owner: str,
    generation: int,
    marker_names: tuple[str, ...],
    marker_is_live: Any,
    marker_lock: Any,
) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"pid={pid}", f"started_at={started_at.isoformat()}"]
    if unit:
        lines.append(f"unit={unit}")
    if operation_id:
        lines.extend(
            (
                f"operation_id={operation_id}",
                f"owner={owner}",
                f"generation={generation}",
            )
        )
    payload = ("\n".join(lines) + "\n").encode()
    with marker_lock(path.parent):
        if any(marker_is_live(path.parent / name) for name in marker_names):
            return False
        descriptor, temporary_raw = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_raw)
        try:
            try:
                os.fchmod(descriptor, 0o660)
            except PermissionError:
                pass
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            _chmod_tolerate_eperm(temporary, 0o660)
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
            _fsync_directory(path.parent)
            return True
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


def write_restore_trigger(
    path: Path,
    timestamp: str,
    operation_id: str,
    issued_at: datetime,
) -> None:
    payload = {
        "schema": 2,
        "operation_id": operation_id,
        "timestamp": timestamp,
        "issued_at": issued_at.astimezone(timezone.utc).isoformat(),
        "request_sha256": restore_request_sha256(operation_id, timestamp),
    }
    _atomic_write_bytes(
        path,
        (
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        mode=0o600,
    )


def restore_adoption_receipt_matches(
    path: Path,
    *,
    operation_id: str,
    timestamp: str,
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
        and payload.get("request_sha256")
        == restore_request_sha256(operation_id, timestamp)
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


def restore_adoption_failure(marker_adopted: bool) -> tuple[str, str]:
    if marker_adopted:
        return (
            "restore_adoption_unconfirmed",
            "host restore marker was adopted, but the matching durable "
            "receipt was not confirmed",
        )
    return (
        "restore_trigger_not_started",
        "restore trigger was written, but host restore service did not adopt it",
    )


async def run_backup_attempt(
    runtime: BackupAttemptRuntime,
    *,
    trigger_mode: bool,
) -> tuple[Any, str | None, bool]:
    if trigger_mode:
        runtime.backup_root.mkdir(parents=True, exist_ok=True)
        runtime.write_trigger(
            runtime.trigger_path,
            runtime.started_at,
            runtime.operation_id,
        )
        deadline = asyncio.get_running_loop().time() + runtime.trigger_timeout_seconds
        adopted = False
        while asyncio.get_running_loop().time() < deadline:
            if runtime.marker_is_adopted(runtime.marker, runtime.operation_id):
                adopted = True
                break
            await asyncio.sleep(0.25)
        if not adopted:
            runtime.unlink_marker(runtime.trigger_path)
            runtime.unlink_owned_marker(runtime.marker, runtime.operation_id)
            raise BackupTriggerNotStarted
        timestamp = await runtime.wait_for_pair(
            runtime.operation_id,
            runtime.started_at,
            timeout_sec=max(
                1,
                runtime.timeout_seconds - runtime.trigger_timeout_seconds,
            ),
        )
        if timestamp is None:
            raise BackupAttemptTimeout
        return None, timestamp, True

    process = await runtime.run_script(
        runtime.backup_script,
        timeout=runtime.timeout_seconds,
        env={
            "BACKUP_ROOT": str(runtime.backup_root),
            "LUMEN_BACKUP_ROOT": str(runtime.backup_root),
            "LUMEN_BACKUP_OPERATION_ID": runtime.operation_id,
        },
    )
    return process, None, False


async def handle_backup_process(
    *,
    process: Any,
    request: Any,
    admin: Any,
    lock_service: Any,
    lock: Any,
    audit: Any,
    http_error: Any,
) -> str:
    if process is None:
        return ""
    output = f"{process.stdout}\n{process.stderr}"
    if backup_script_was_skipped(output):
        await audit(
            request,
            admin,
            event_type="admin.backup.create.skipped",
            details={"reason": "backup_skipped"},
        )
        await lock_service.release(lock, succeeded=False, reason="backup_skipped")
        raise http_error(
            "backup_skipped",
            "backup was skipped because another maintenance operation is running",
            409,
        )
    if process.returncode != 0:
        tail = (process.stderr or process.stdout or "")[-1000:]
        await audit(
            request,
            admin,
            event_type="admin.backup.create.fail",
            details={"returncode": process.returncode, "stderr_tail": tail},
        )
        await lock_service.release(
            lock,
            succeeded=False,
            reason="backup_script_failed",
        )
        raise http_error(
            "backup_script_failed",
            "backup process exited unsuccessfully",
            502,
            details={"returncode": process.returncode, "stderr_tail": tail},
        )
    return output
