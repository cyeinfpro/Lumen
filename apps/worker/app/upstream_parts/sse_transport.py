"""SSE parsing and curl subprocess resource lifecycle helpers."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any


def decode_sse_event(
    event_type: str | None,
    event_data: list[str],
) -> dict[str, Any] | None:
    data = "\n".join(event_data)
    if not data or data == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(event, dict):
        return None
    if event_type and "type" not in event:
        event["type"] = event_type
    return event


class CurlSSEEventParser:
    """Incrementally assemble JSON SSE events from decoded response lines."""

    def __init__(self) -> None:
        self._event_type: str | None = None
        self._event_data: list[str] = []

    def feed_line(self, raw: bytes) -> dict[str, Any] | None:
        line_text = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line_text == "":
            return self._flush()
        if line_text.startswith(":"):
            return None
        if line_text.startswith("event:"):
            self._event_type = line_text[6:].strip()
        elif line_text.startswith("data:"):
            self._event_data.append(line_text[5:].lstrip())
        return None

    def finish(self) -> dict[str, Any] | None:
        return self._flush()

    def _flush(self) -> dict[str, Any] | None:
        event = decode_sse_event(self._event_type, self._event_data)
        self._event_type = None
        self._event_data = []
        return event


StderrReader = Callable[[Any], Awaitable[bytes]]
ProcessTerminator = Callable[[asyncio.subprocess.Process | None], Awaitable[None]]


@dataclass
class CurlSSEProcess:
    """Own temporary request files, the curl process, and its stderr task."""

    body_fd: int | None
    body_path: str | None
    config_path: str | None = None
    proc: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[bytes] | None = None

    def close_body_fd(self) -> None:
        fd = self.body_fd
        if fd is None:
            return
        self.body_fd = None
        os.close(fd)

    def set_config_path(self, path: str) -> None:
        self.config_path = path

    async def start(
        self,
        command: Sequence[str],
        *,
        stderr_reader: StderrReader,
    ) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.proc = proc
        if proc.stderr is not None:
            self.stderr_task = asyncio.create_task(stderr_reader(proc.stderr))
        return proc

    async def wait(self) -> int:
        if self.proc is None:
            raise RuntimeError("curl sse process has not started")
        return await self.proc.wait()

    async def cleanup(self, terminate: ProcessTerminator) -> None:
        proc = self.proc
        self.proc = None
        try:
            await terminate(proc)
        finally:
            await self._cancel_stderr_task()
            with suppress(OSError):
                self.close_body_fd()
            self._unlink_paths()

    async def _cancel_stderr_task(self) -> None:
        task = self.stderr_task
        self.stderr_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _unlink_paths(self) -> None:
        for attribute in ("config_path", "body_path"):
            path = getattr(self, attribute)
            setattr(self, attribute, None)
            if path is not None:
                with suppress(OSError):
                    os.unlink(path)
