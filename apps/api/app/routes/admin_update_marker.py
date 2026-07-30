"""Update marker parsing, liveness, and persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True)
class UpdateMarker:
    pid: int
    started_at: str | None
    unit: str | None = None


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


def marker_is_live(
    marker: UpdateMarker,
    *,
    trigger_only_mode: Callable[[], bool],
    marker_is_stale_fn: Callable[[str | None], bool],
    unit_is_running_fn: Callable[[str], bool],
    pid_is_running_fn: Callable[[int], bool],
) -> bool:
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


def write_marker(
    marker: Path,
    *,
    pid: int,
    started_at: str,
    unit: str | None,
    chmod: Callable[[Path, int], None],
) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_suffix(f"{marker.suffix}.tmp")
    lines = [f"pid={pid}", f"started_at={started_at}"]
    if unit:
        lines.append(f"unit={unit}")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    chmod(tmp, 0o600)
    tmp.replace(marker)
