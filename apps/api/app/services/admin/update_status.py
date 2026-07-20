"""Release, step-log and maintenance status services for admin update routes."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field


_TRIGGER_DELIMITER_RE = re.compile(
    r"^=== update (?:trigger|unit started) ", re.MULTILINE
)
_STEP_LINE_RE = re.compile(
    r"^::lumen-step::\s+phase=(?P<phase>[A-Za-z0-9_]+)\s+status=(?P<status>start|done|fail)"
    r"(?:\s+rc=(?P<rc>-?\d+))?"
    r"(?:\s+dur_ms=(?P<dur_ms>-?\d+))?"
    r"(?:\s+ts=(?P<ts>\S+))?\s*$"
)
_INFO_LINE_RE = re.compile(
    r"^::lumen-info::\s+phase=(?P<phase>[A-Za-z0-9_]+)\s+key=(?P<key>[A-Za-z0-9_]+)\s+value=(?P<value>.*)$"
)


class StepRecord(BaseModel):
    """One phase entry parsed from .update.log step lines.

    ``status`` is "running" until we see a ``status=done`` for the same phase.
    ``info`` collects every ``::lumen-info::`` key/value emitted under that phase.
    """

    phase: str
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    rc: int | None = None
    dur_ms: int | None = None
    info: dict[str, str] = Field(default_factory=dict)


def truncate_to_last_run(log_text: str) -> str:
    if not log_text:
        return log_text
    matches = list(_TRIGGER_DELIMITER_RE.finditer(log_text))
    return log_text[matches[-1].start() :] if matches else log_text


def parse_steps(log_text: str) -> list[StepRecord]:
    by_phase: dict[str, StepRecord] = {}
    order: list[str] = []
    for raw in truncate_to_last_run(log_text).splitlines():
        line = raw.strip()
        if not line:
            continue
        step = _STEP_LINE_RE.match(line)
        if step:
            _merge_step(by_phase, order, step.groupdict())
            continue
        info = _INFO_LINE_RE.match(line)
        if info:
            phase = info.group("phase")
            record = by_phase.setdefault(
                phase, StepRecord(phase=phase, status="running")
            )
            if phase not in order:
                order.append(phase)
            record.info[info.group("key")] = info.group("value").rstrip()
    return [by_phase[phase] for phase in order]


def _merge_step(
    by_phase: dict[str, StepRecord],
    order: list[str],
    fields: dict[str, str | None],
) -> None:
    phase = fields["phase"] or ""
    status = "done" if fields["status"] == "fail" else fields["status"] or "running"
    record = by_phase.get(phase)
    if record is None:
        record = StepRecord(
            phase=phase,
            status="running" if status == "start" else "done",
            started_at=fields["ts"] if status == "start" else None,
            ended_at=fields["ts"] if status == "done" else None,
            rc=_as_int(fields["rc"]) if status == "done" else None,
            dur_ms=_as_int(fields["dur_ms"]) if status == "done" else None,
        )
        by_phase[phase] = record
        order.append(phase)
        return
    if status == "start":
        record.status = "running"
        record.started_at = fields["ts"]
        record.ended_at = None
        record.rc = None
        record.dur_ms = None
        return
    record.status = "done"
    record.ended_at = fields["ts"]
    record.rc = _as_int(fields["rc"])
    record.dur_ms = _as_int(fields["dur_ms"])


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


class ReleaseInfo(BaseModel):
    id: str
    created_at: str | None = None
    sha: str | None = None
    branch: str | None = None
    alembic_head_expected: str | None = None
    alembic_head_applied: str | None = None
    is_current: bool = False
    is_previous: bool = False


def _readlink_target(link: Path) -> str | None:
    try:
        return os.readlink(link) if link.is_symlink() else None
    except OSError:
        return None


def _extract_release_id(link_target: str | None) -> str | None:
    return Path(link_target).name if link_target else None


def _read_release_metadata(release_dir: Path) -> dict[str, object]:
    try:
        data = json.loads(
            (release_dir / ".lumen_release.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _release_info_from_dir(release_dir: Path) -> ReleaseInfo | None:
    if not release_dir.is_dir():
        return None
    meta = _read_release_metadata(release_dir)
    return ReleaseInfo(
        id=str(meta.get("id") or release_dir.name),
        created_at=str(meta["created_at"]) if meta.get("created_at") else None,
        sha=str(meta["sha"]) if meta.get("sha") else None,
        branch=str(meta["branch"]) if meta.get("branch") else None,
        alembic_head_expected=(
            str(meta["alembic_head_expected"])
            if meta.get("alembic_head_expected")
            else None
        ),
        alembic_head_applied=(
            str(meta["alembic_head_applied"])
            if meta.get("alembic_head_applied")
            else None
        ),
    )


def list_releases(
    lumen_root: Path,
    *,
    limit: int | None = 10,
) -> list[ReleaseInfo]:
    releases_dir = lumen_root / "releases"
    if not releases_dir.is_dir():
        return []
    current_id = _extract_release_id(_readlink_target(lumen_root / "current"))
    previous_id = _extract_release_id(_readlink_target(lumen_root / "previous"))
    try:
        children = list(releases_dir.iterdir())
    except OSError:
        return []
    items: list[ReleaseInfo] = []
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        info = _release_info_from_dir(child)
        if info is None:
            continue
        info = info.model_copy(
            update={
                "is_current": bool(current_id and info.id == current_id),
                "is_previous": bool(previous_id and info.id == previous_id),
            }
        )
        items.append(info)
    typed = sorted(
        (item for item in items if item.created_at),
        key=lambda item: (item.created_at or "", item.id),
        reverse=True,
    )
    untyped = sorted(
        (item for item in items if not item.created_at),
        key=lambda item: item.id,
        reverse=True,
    )
    result = typed + untyped
    return result if limit is None else result[:limit]


def resolve_release(lumen_root: Path, release_id: str) -> Path | None:
    if (
        not release_id
        or "/" in release_id
        or ".." in release_id
        or release_id.startswith(".")
    ):
        return None
    target = lumen_root / "releases" / release_id
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to((lumen_root / "releases").resolve())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return resolved if resolved.is_dir() else None


class UpdateStatusOut(BaseModel):
    running: bool
    pid: int | None = None
    unit: str | None = None
    started_at: str | None = None
    log_tail: str
    phases: list[StepRecord] = Field(default_factory=list)
    current_release: ReleaseInfo | None = None
    previous_release: ReleaseInfo | None = None
    releases: list[ReleaseInfo] = Field(default_factory=list)


class SystemMaintenanceOut(BaseModel):
    running: bool
    phase: str | None = None
    started_at: str | None = None
    target_tag: str | None = None
    estimated_remaining_min: int = 0


@dataclass(frozen=True)
class StatusRuntime:
    read_marker: Callable[[], Any]
    read_log_full: Callable[[], str]
    read_log_tail: Callable[[], str]
    list_releases: Callable[[], list[ReleaseInfo]]
    parse_steps: Callable[[str], list[StepRecord]] = parse_steps


def build_status_snapshot(runtime: StatusRuntime) -> UpdateStatusOut:
    marker = runtime.read_marker()
    phases = runtime.parse_steps(runtime.read_log_full())
    releases = runtime.list_releases()
    current = next((item for item in releases if item.is_current), None)
    previous = next((item for item in releases if item.is_previous), None)
    return UpdateStatusOut(
        running=marker is not None,
        pid=marker.pid or None if marker else None,
        unit=marker.unit if marker else None,
        started_at=marker.started_at if marker else None,
        log_tail=runtime.read_log_tail(),
        phases=phases,
        current_release=current,
        previous_release=previous,
        releases=releases,
    )


def maintenance_snapshot(runtime: StatusRuntime) -> SystemMaintenanceOut:
    snapshot = build_status_snapshot(runtime)
    if not snapshot.running:
        return SystemMaintenanceOut(running=False)
    running_index = next(
        (idx for idx, item in enumerate(snapshot.phases) if item.status == "running"),
        max(0, len(snapshot.phases) - 1),
    )
    durations = [
        item.dur_ms for item in snapshot.phases if item.dur_ms and item.dur_ms > 0
    ]
    median_ms = sorted(durations)[len(durations) // 2] if durations else 60_000
    target_tag = next(
        (
            value
            for item in snapshot.phases
            for value in (item.info.get("tag"), item.info.get("target_tag"))
            if value
        ),
        None,
    )
    remaining = max(1, len(snapshot.phases) - running_index)
    return SystemMaintenanceOut(
        running=True,
        phase=next(
            (item.phase for item in snapshot.phases if item.status == "running"),
            "preparing",
        ),
        started_at=snapshot.started_at,
        target_tag=target_tag,
        estimated_remaining_min=max(1, int(median_ms * remaining / 60_000)),
    )
