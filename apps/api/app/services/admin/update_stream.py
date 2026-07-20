"""SSE stream orchestration for the admin update progress endpoint."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import Request


@dataclass(frozen=True)
class UpdateStreamRuntime:
    log_path: Callable[[], Path]
    build_snapshot: Callable[[], Any]
    read_incremental: Callable[[Path, int], tuple[str, int]]
    read_marker: Callable[[], Any]
    classify_log_line: Callable[[str], tuple[str, dict[str, object]]]
    format_event: Callable[[str, object], str]
    max_duration_sec: float
    heartbeat_sec: float
    poll_sec: float
    batch_window_sec: float


async def stream_update_events(
    request: Request,
    *,
    runtime: UpdateStreamRuntime,
) -> AsyncIterator[str]:
    log_path = runtime.log_path()
    deadline = time.monotonic() + runtime.max_duration_sec
    snapshot = await asyncio.to_thread(runtime.build_snapshot)
    yield runtime.format_event("state", snapshot.model_dump(mode="json"))
    try:
        last_pos = log_path.stat().st_size
    except (FileNotFoundError, OSError):
        last_pos = 0

    last_heartbeat = time.monotonic()
    last_flush = last_heartbeat
    buffered: list[str] = []
    marker_gone_at: float | None = None
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                yield runtime.format_event("done", {"reason": "max_duration"})
                return
            if await request.is_disconnected():
                return

            chunk_events, last_pos, buffered = await _read_chunk_events(
                runtime,
                log_path,
                last_pos,
                buffered,
            )
            for event in chunk_events:
                yield event

            now = time.monotonic()
            periodic, buffered, last_flush, last_heartbeat = _periodic_events(
                runtime,
                buffered=buffered,
                last_flush=last_flush,
                last_heartbeat=last_heartbeat,
                now=now,
            )
            for event in periodic:
                yield event

            marker = await asyncio.to_thread(runtime.read_marker)
            marker_gone_at, finished = _marker_state(
                marker, marker_gone_at=marker_gone_at, now=now
            )
            if finished:
                async for final_event in _finish_stream(
                    runtime, log_path, last_pos, buffered
                ):
                    yield final_event
                return
            await asyncio.sleep(runtime.poll_sec)
    except asyncio.CancelledError:
        raise


def _periodic_events(
    runtime: UpdateStreamRuntime,
    *,
    buffered: list[str],
    last_flush: float,
    last_heartbeat: float,
    now: float,
) -> tuple[list[str], list[str], float, float]:
    events: list[str] = []
    if buffered and now - last_flush >= runtime.batch_window_sec:
        events.append(runtime.format_event("log", {"lines": buffered}))
        buffered = []
        last_flush = now
    if now - last_heartbeat >= runtime.heartbeat_sec:
        events.append(runtime.format_event("ping", {}))
        last_heartbeat = now
    return events, buffered, last_flush, last_heartbeat


def _marker_state(
    marker: Any,
    *,
    marker_gone_at: float | None,
    now: float,
) -> tuple[float | None, bool]:
    if marker is not None:
        return None, False
    marker_gone_at = marker_gone_at or now
    return marker_gone_at, now - marker_gone_at >= 1.0


async def _read_chunk_events(
    runtime: UpdateStreamRuntime,
    log_path: Path,
    last_pos: int,
    buffered: list[str],
) -> tuple[list[str], int, list[str]]:
    chunk, last_pos = await asyncio.to_thread(
        runtime.read_incremental, log_path, last_pos
    )
    events: list[str] = []
    for line in chunk.splitlines():
        event, payload = runtime.classify_log_line(line)
        if event in {"step", "info"}:
            if buffered:
                events.append(runtime.format_event("log", {"lines": buffered}))
                buffered = []
            events.append(runtime.format_event(event, payload))
        else:
            buffered.append(str(payload.get("line", "")))
    return events, last_pos, buffered


async def _finish_stream(
    runtime: UpdateStreamRuntime,
    log_path: Path,
    last_pos: int,
    buffered: list[str],
) -> AsyncIterator[str]:
    for event in _final_events(runtime, log_path, last_pos, buffered):
        yield event
    final = await asyncio.to_thread(runtime.build_snapshot)
    yield runtime.format_event(
        "done",
        {
            "final_status": {
                "running": final.running,
                "phases": [phase.model_dump(mode="json") for phase in final.phases],
                "current_release": (
                    final.current_release.model_dump(mode="json")
                    if final.current_release
                    else None
                ),
            }
        },
    )


def _final_events(
    runtime: UpdateStreamRuntime,
    log_path: Path,
    last_pos: int,
    buffered: list[str],
) -> Any:
    chunk, _ = runtime.read_incremental(log_path, last_pos)
    if chunk:
        for line in chunk.splitlines():
            event, payload = runtime.classify_log_line(line)
            if event in {"step", "info"}:
                if buffered:
                    yield runtime.format_event("log", {"lines": buffered})
                    buffered.clear()
                yield runtime.format_event(event, payload)
            else:
                buffered.append(str(payload.get("line", "")))
    if buffered:
        yield runtime.format_event("log", {"lines": buffered})
