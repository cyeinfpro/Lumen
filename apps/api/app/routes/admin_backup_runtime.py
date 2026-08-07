"""Stateless helpers for admin backup trigger and process output handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import admin_backup_catalog as _backup_catalog
from .admin_backup_fs import chmod_tolerate_eperm as _chmod_tolerate_eperm


class BackupTriggerNotStarted(RuntimeError):
    """Raised when the host runner does not adopt a trigger in time."""


class BackupAttemptTimeout(TimeoutError):
    """Raised when a trigger is adopted but no committed pair appears."""


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
) -> str | None:
    del started_at
    for line in reversed((stdout or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                ts = payload.get("timestamp")
                if isinstance(ts, str) and _backup_catalog.TIMESTAMP_RE.fullmatch(ts):
                    return ts
        if "complete" in line and "backup " in line:
            parts = line.split()
            for i, token in enumerate(parts):
                if token == "backup" and i + 1 < len(parts):
                    ts = parts[i + 1].rstrip(":")
                    if _backup_catalog.TIMESTAMP_RE.fullmatch(ts):
                        return ts
        if "complete" in line.lower():
            match = re.search(r"\b([0-9]{8}-[0-9]{6})\b", line)
            if match:
                return match.group(1)
    return None


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


def write_restore_trigger(path: Path, timestamp: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(timestamp + "\n", encoding="utf-8")
    _chmod_tolerate_eperm(tmp, 0o600)
    tmp.replace(path)


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
        deadline = (
            asyncio.get_running_loop().time()
            + runtime.trigger_timeout_seconds
        )
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
