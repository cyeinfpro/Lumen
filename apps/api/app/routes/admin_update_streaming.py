"""Log parsing and streaming primitives for admin updates."""

from __future__ import annotations

import json
from pathlib import Path
import re
import time


STEP_LINE_RE = re.compile(
    r"^::lumen-step::\s+phase=(?P<phase>[A-Za-z0-9_]+)\s+status=(?P<status>start|done|fail)"
    r"(?:\s+rc=(?P<rc>-?\d+))?"
    r"(?:\s+dur_ms=(?P<dur_ms>-?\d+))?"
    r"(?:\s+ts=(?P<ts>\S+))?"
    r"\s*$"
)
INFO_LINE_RE = re.compile(
    r"^::lumen-info::\s+phase=(?P<phase>[A-Za-z0-9_]+)\s+"
    r"key=(?P<key>[A-Za-z0-9_]+)\s+value=(?P<value>.*)$"
)


def sse_format(event: str, data: object) -> str:
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def classify_log_line(line: str) -> tuple[str, dict[str, object]]:
    stripped = line.rstrip("\n").rstrip("\r")
    step = STEP_LINE_RE.match(stripped.strip())
    if step:
        rc = step.group("rc")
        duration = step.group("dur_ms")
        return "step", {
            "phase": step.group("phase"),
            "status": (
                "done" if step.group("status") == "fail" else step.group("status")
            ),
            "ts": step.group("ts"),
            "rc": int(rc) if rc is not None else None,
            "dur_ms": int(duration) if duration is not None else None,
        }
    info = INFO_LINE_RE.match(stripped.strip())
    if info:
        return "info", {
            "phase": info.group("phase"),
            "key": info.group("key"),
            "value": info.group("value").rstrip(),
        }
    return "log", {"line": stripped}


def read_incremental(path: Path, last_pos: int) -> tuple[str, int]:
    try:
        size = path.stat().st_size
    except (FileNotFoundError, OSError):
        return "", last_pos
    if size < last_pos:
        last_pos = 0
    if size == last_pos:
        return "", last_pos
    try:
        with path.open("rb") as handle:
            handle.seek(last_pos)
            chunk = handle.read(size - last_pos)
    except OSError:
        return "", last_pos
    return chunk.decode("utf-8", errors="replace"), size


def wait_for_log_append(
    path: Path,
    *,
    initial_size: int,
    timeout_sec: float,
) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            if path.stat().st_size > initial_size:
                return True
        except OSError:
            pass
        time.sleep(0.25)
    return False
