"""Update marker parsing, liveness, and persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from .admin_maintenance_marker_lock import (
    MAINTENANCE_MARKER_NAMES,
    maintenance_marker_lock,
)


_STALE_AFTER_SECONDS = 24 * 60 * 60
_MANUAL_STATES = frozenset({"failed_original_unhealthy", "manual_required"})


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while persisting update marker")
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


class UpdateMarkerBusy(RuntimeError):
    """Raised when a live update/rollback marker blocks a new launch claim.

    The Redis system lock is the cross-process guard in normal operation.
    When Redis is unavailable the service degrades to marker-file checks,
    so marker creation itself must be exclusive to close the local
    check/write race (same rationale as admin_backups._try_write_pid_marker).
    """


@dataclass(frozen=True)
class UpdateMarker:
    pid: int
    started_at: str | None
    unit: str | None = None
    operation_id: str | None = None
    request_sha256: str | None = None
    owner: str | None = None
    state: str | None = None
    generation: int = 0


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def marker_is_stale(started_at: str | None, *, stale_after_seconds: int) -> bool:
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - started.astimezone(timezone.utc)
    return age.total_seconds() > stale_after_seconds


def unit_is_running(unit: str) -> bool:
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


def parse_marker_text(raw: str) -> UpdateMarker:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value.strip()

    def int_value(key: str) -> int:
        try:
            return int(values.get(key, "0"))
        except ValueError:
            return 0

    def optional_value(key: str) -> str | None:
        return values.get(key) or None

    return UpdateMarker(
        pid=int_value("pid"),
        started_at=optional_value("started_at"),
        unit=optional_value("unit"),
        operation_id=optional_value("operation_id"),
        request_sha256=optional_value("request_sha256"),
        owner=optional_value("owner"),
        state=optional_value("state"),
        generation=int_value("generation"),
    )


def marker_is_live(
    marker: UpdateMarker,
    *,
    trigger_only_mode: Callable[[], bool],
    marker_is_stale_fn: Callable[[str | None], bool],
    unit_is_running_fn: Callable[[str], bool],
    pid_is_running_fn: Callable[[int], bool],
) -> bool:
    if marker.owner == "manual" and marker.state in _MANUAL_STATES:
        return True
    if marker.unit:
        if trigger_only_mode() and not marker_is_stale_fn(marker.started_at):
            return True
        if unit_is_running_fn(marker.unit):
            return True
    return bool(
        marker.pid
        and pid_is_running_fn(marker.pid)
        and not marker_is_stale_fn(marker.started_at)
    )


def read_marker(
    marker_path: Path,
    *,
    parse_marker: Callable[[str], UpdateMarker],
    marker_is_live_fn: Callable[[UpdateMarker], bool],
) -> UpdateMarker | None:
    with maintenance_marker_lock(marker_path.parent):
        try:
            marker = parse_marker(marker_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError):
            return None
        if marker_is_live_fn(marker):
            return marker
        try:
            marker_path.unlink()
        except OSError:
            pass
        return None


def _existing_marker_is_live(marker: Path) -> bool:
    """True iff an existing marker still guards a running operation.

    Mirrors admin_backups._read_pid_marker: a fresh unit line is
    authoritative, a pid line is live only while that process runs.

    An empty/unparsable marker (created with O_EXCL but payload not yet
    written) is treated as in-progress while its mtime is fresh: otherwise
    a contender could read the marker inside the winner's open→write window,
    judge it corrupt, unlink it, and both processes would proceed.
    """
    try:
        raw = marker.read_text(encoding="utf-8")
        parsed = parse_marker_text(raw)
    except (FileNotFoundError, OSError):
        return False
    if parsed.owner == "manual" and parsed.state in _MANUAL_STATES:
        return True
    if parsed.unit and not marker_is_stale(
        parsed.started_at, stale_after_seconds=_STALE_AFTER_SECONDS
    ):
        return True
    if (
        parsed.pid
        and pid_is_running(parsed.pid)
        and not marker_is_stale(
            parsed.started_at, stale_after_seconds=_STALE_AFTER_SECONDS
        )
    ):
        return True
    if not raw.strip():
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            return False
        return age < _STALE_AFTER_SECONDS
    return False


def write_marker(
    marker: Path,
    *,
    pid: int,
    started_at: str,
    unit: str | None,
    operation_id: str | None = None,
    request_sha256: str | None = None,
    owner: str = "api",
    generation: int = 0,
) -> bool:
    """Atomically claim the update/rollback marker.

    Returns False when a live marker already exists; the caller must treat
    that as "another update or rollback is running" instead of launching.
    Creation uses O_CREAT|O_EXCL so two processes racing to claim the
    marker (e.g. while Redis is unavailable) cannot both succeed; a stale
    or corrupt marker is removed and the claim retried once.
    """
    marker.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"pid={pid}", f"started_at={started_at}"]
    if unit:
        lines.append(f"unit={unit}")
    if operation_id is None:
        operation_id = f"update-{started_at.replace(':', '').replace('-', '')}"
    lines.extend(
        [
            f"operation_id={operation_id}",
            *(
                [f"request_sha256={request_sha256}"]
                if request_sha256 is not None
                else []
            ),
            f"owner={owner}",
            f"generation={generation}",
        ]
    )
    payload = ("\n".join(lines) + "\n").encode()
    with maintenance_marker_lock(marker.parent):
        for name in MAINTENANCE_MARKER_NAMES:
            candidate = marker.parent / name
            if _existing_marker_is_live(candidate):
                return False
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        descriptor, temporary_raw = tempfile.mkstemp(
            prefix=f".{marker.name}.",
            suffix=".tmp",
            dir=marker.parent,
        )
        temporary = Path(temporary_raw)
        reserved = False
        replaced = False
        try:
            try:
                os.fchmod(descriptor, 0o660)
            except PermissionError:
                # Squashed CIFS mounts pin the mode; O_CREAT already set
                # 0o660 and non-owner fchmod there returns EPERM.
                pass
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                reservation = os.open(
                    marker,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o660,
                )
            except FileExistsError:
                return False
            os.close(reservation)
            reserved = True
            os.replace(temporary, marker)
            replaced = True
            _fsync_directory(marker.parent)
            return True
        except Exception:
            if reserved and not replaced:
                try:
                    marker.unlink()
                    _fsync_directory(marker.parent)
                except OSError:
                    pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
