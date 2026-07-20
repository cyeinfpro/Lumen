from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from app.upstream_parts import sse_transport


def test_sse_event_parser_handles_comments_multiline_data_and_eof() -> None:
    parser = sse_transport.CurlSSEEventParser()

    assert parser.feed_line(b": keep-alive\n") is None
    assert parser.feed_line(b"event: response.completed\r\n") is None
    assert parser.feed_line(b'data: {"response":\n') is None
    assert parser.feed_line(b'data: {"id":"response-1"}}\n') is None
    assert parser.finish() == {
        "type": "response.completed",
        "response": {"id": "response-1"},
    }


def test_sse_event_parser_resets_on_blank_and_ignores_invalid_events() -> None:
    parser = sse_transport.CurlSSEEventParser()

    assert parser.feed_line(b"event: ignored\n") is None
    assert parser.feed_line(b"\n") is None
    assert parser.feed_line(b"data: [DONE]\n") is None
    assert parser.feed_line(b"\n") is None
    assert parser.feed_line(b"data: not-json\n") is None
    assert parser.feed_line(b"\n") is None
    assert parser.finish() is None


@pytest.mark.asyncio
async def test_curl_sse_process_owns_spawn_stderr_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body_fd, body_path = (
        os.open(
            tmp_path / "body.json",
            os.O_CREAT | os.O_RDWR,
            0o600,
        ),
        tmp_path / "body.json",
    )
    config_path = tmp_path / "curl.conf"
    config_path.write_text("header = test\n", encoding="utf-8")
    spawn_kwargs: dict[str, Any] = {}
    stderr_started = asyncio.Event()
    stderr_finished = asyncio.Event()
    never_release = asyncio.Event()
    terminated: list[object | None] = []

    class FakeProc:
        stderr = object()

        async def wait(self) -> int:
            return 0

    proc = FakeProc()

    async def fake_create_subprocess_exec(
        *_args: Any,
        **kwargs: Any,
    ) -> FakeProc:
        spawn_kwargs.update(kwargs)
        return proc

    async def read_stderr(_stream: Any) -> bytes:
        stderr_started.set()
        try:
            await never_release.wait()
        finally:
            stderr_finished.set()
        return b""

    async def terminate(received: object | None) -> None:
        terminated.append(received)

    monkeypatch.setattr(
        sse_transport.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    process = sse_transport.CurlSSEProcess(
        body_fd=body_fd,
        body_path=str(body_path),
    )
    process.set_config_path(str(config_path))

    assert (
        await process.start(["curl", "https://example.test"], stderr_reader=read_stderr)
        is proc
    )
    await asyncio.wait_for(stderr_started.wait(), timeout=1.0)
    await process.cleanup(terminate)

    assert spawn_kwargs == {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "start_new_session": True,
    }
    assert terminated == [proc]
    assert stderr_finished.is_set()
    assert not body_path.exists()
    assert not config_path.exists()
    with pytest.raises(OSError):
        os.fstat(body_fd)
