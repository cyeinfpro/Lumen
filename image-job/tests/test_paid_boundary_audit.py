from __future__ import annotations

import asyncio
import base64
import socket
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from image_job.candidates import ImageCandidate
from image_job.config import ImageJobSettings, ImageJobTimeouts, SecretText
from image_job.contracts import (
    ERROR_CLASS_UPSTREAM_4XX,
    JobFailure,
    JobProcessOutcome,
    UpstreamDispatchReceipt,
)
from image_job.runtime import create_runtime


def _settings(
    tmp_path: Path,
    *,
    upstream_base_url: str = "http://127.0.0.1:8081",
) -> ImageJobSettings:
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    return ImageJobSettings(
        data_dir=data_dir,
        refs_dir=data_dir / "refs",
        state_dir=state_dir,
        db_path=state_dir / "jobs.sqlite3",
        queue_max=4,
        concurrency=1,
        sidecar_token=SecretText("s" * 32),
        upstream_base_url=upstream_base_url,
        public_base_url="https://images.example.test",
        timeouts=ImageJobTimeouts(
            upstream_s=1,
            connect_s=0.2,
            graceful_shutdown_s=0,
        ),
        credential_active_key_id="test-v1",
        credential_master_secret=SecretText("test-master-secret-" + "x" * 32),
        retry_network_max=0,
        retry_responses_stream_max=0,
        retry_upstream_5xx_max=0,
        retry_backoff_s=0,
        stuck_reconcile_interval_s=60,
        retention_sweep_interval_s=60,
    )


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(20, 40, 60)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _seed(runtime: Any, job_id: str, payload: dict[str, Any]) -> None:
    await runtime.repository.initialize()
    await runtime.jobs.persistence.insert_job(
        job_id,
        payload,
        "Bearer sk-test",
    )


class _NoPostClient:
    def __init__(self) -> None:
        self.post_count = 0

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        self.post_count += 1
        raise AssertionError("transport must not start for local preparation failure")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_point",
    ["materialize_url", "download_edit_file", "build_multipart"],
)
async def test_local_preparation_failure_is_proven_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    runtime = create_runtime(_settings(tmp_path))
    encoded = base64.b64encode(_png_bytes()).decode("ascii")
    payload: dict[str, Any] = {
        "request_type": "edits",
        "endpoint": "/v1/images/edits",
        "body": {
            "prompt": "edit",
            "images": [{"b64_json": encoded}],
        },
        "retention_days": 1,
    }
    if failure_point != "materialize_url":
        payload["image_edit_input_transport"] = "file"
    if failure_point == "download_edit_file":
        payload["body"]["images"] = [
            {"image_url": "https://inputs.example.test/input.png"}
        ]
    await _seed(runtime, f"job-{failure_point}", payload)
    client = _NoPostClient()
    runtime.upstream.client = client  # type: ignore[assignment]

    async def fail_local(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    if failure_point == "materialize_url":
        monkeypatch.setattr(runtime.upstream.processing, "save_input_image", fail_local)
    elif failure_point == "download_edit_file":
        monkeypatch.setattr(runtime.upstream.processing, "download_image_url", fail_local)
    else:
        monkeypatch.setattr(httpx.Request, "aread", fail_local)

    outcome = await runtime.jobs.process(f"job-{failure_point}")
    row = await runtime.repository.one(
        "SELECT status, outcome_uncertain FROM jobs WHERE job_id = ?",
        (f"job-{failure_point}",),
    )

    assert outcome is JobProcessOutcome.FAILED
    assert row is not None
    assert row["status"] == "failed"
    assert bool(row["outcome_uncertain"]) is False
    assert client.post_count == 0
    assert runtime.jobs.dispatch_outcomes[("not_started", "failed")] == 1


async def _read_request(reader: asyncio.StreamReader) -> None:
    headers = await reader.readuntil(b"\r\n\r\n")
    content_length = 0
    for line in headers.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
            break
    if content_length:
        await reader.readexactly(content_length)


def _unused_loopback_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_connect_refused_has_no_dispatch_receipt_and_fails(
    tmp_path: Path,
) -> None:
    port = _unused_loopback_port()
    runtime = create_runtime(
        _settings(tmp_path, upstream_base_url=f"http://127.0.0.1:{port}")
    )
    await _seed(
        runtime,
        "job-connect-refused",
        {
            "request_type": "generations",
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
            "retention_days": 1,
        },
    )
    runtime.upstream.client = httpx.AsyncClient(
        timeout=httpx.Timeout(1, connect=0.2),
        trust_env=False,
    )
    try:
        outcome = await runtime.jobs.process("job-connect-refused")
    finally:
        await runtime.upstream.client.aclose()

    row = await runtime.repository.one(
        "SELECT status, outcome_uncertain FROM jobs WHERE job_id = ?",
        ("job-connect-refused",),
    )
    assert outcome is JobProcessOutcome.FAILED
    assert row is not None
    assert row["status"] == "failed"
    assert bool(row["outcome_uncertain"]) is False


@pytest.mark.asyncio
async def test_header_write_receipt_makes_disconnect_uncertain(
    tmp_path: Path,
) -> None:
    post_count = 0

    async def drop_after_request(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal post_count
        await _read_request(reader)
        post_count += 1
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(drop_after_request, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    runtime = create_runtime(
        _settings(tmp_path, upstream_base_url=f"http://127.0.0.1:{port}")
    )
    await _seed(
        runtime,
        "job-disconnected",
        {
            "request_type": "generations",
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
            "retention_days": 1,
        },
    )
    runtime.upstream.client = httpx.AsyncClient(
        timeout=httpx.Timeout(1, connect=0.2),
        trust_env=False,
    )
    try:
        async with server:
            outcome = await runtime.jobs.process("job-disconnected")
    finally:
        await runtime.upstream.client.aclose()

    row = await runtime.repository.one(
        "SELECT status, outcome_uncertain FROM jobs WHERE job_id = ?",
        ("job-disconnected",),
    )
    assert post_count == 1
    assert outcome is JobProcessOutcome.UNCERTAIN
    assert row is not None
    assert row["status"] == "uncertain"
    assert bool(row["outcome_uncertain"]) is True
    assert runtime.jobs.dispatch_outcomes[("started", "uncertain")] == 1


@pytest.mark.asyncio
async def test_explicit_no_cost_rejection_stays_failed_after_dispatch(
    tmp_path: Path,
) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await _seed(
        runtime,
        "job-rejected",
        {
            "request_type": "generations",
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
            "retention_days": 1,
        },
    )

    async def reject(
        _row: Any,
        *,
        dispatch: UpstreamDispatchReceipt,
        **_kwargs: Any,
    ) -> Any:
        dispatch.mark_started("test.send_request_headers.started")
        raise JobFailure(
            "upstream rejected without billing",
            upstream_status=400,
            cost_proven_absent=True,
            error_class=ERROR_CLASS_UPSTREAM_4XX,
        )

    runtime.upstream.call = reject
    outcome = await runtime.jobs.process("job-rejected")
    row = await runtime.repository.one(
        "SELECT status, outcome_uncertain FROM jobs WHERE job_id = ?",
        ("job-rejected",),
    )

    assert outcome is JobProcessOutcome.FAILED
    assert row is not None
    assert row["status"] == "failed"
    assert bool(row["outcome_uncertain"]) is False
    assert runtime.jobs.dispatch_outcomes[("started", "failed")] == 1


@pytest.mark.asyncio
async def test_real_transport_receipt_allows_verified_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_count = 0

    async def successful_upstream(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal post_count
        await _read_request(reader)
        post_count += 1
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n\r\n{}"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(successful_upstream, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    runtime = create_runtime(
        _settings(tmp_path, upstream_base_url=f"http://127.0.0.1:{port}")
    )
    await _seed(
        runtime,
        "job-success",
        {
            "request_type": "generations",
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
            "retention_days": 1,
        },
    )

    async def candidate(*_args: Any, **_kwargs: Any) -> list[ImageCandidate]:
        return [ImageCandidate(_png_bytes(), "image/png")]

    monkeypatch.setattr(runtime.upstream.processing, "extract_response_images", candidate)
    runtime.upstream.client = httpx.AsyncClient(
        timeout=httpx.Timeout(1, connect=0.2),
        trust_env=False,
    )
    try:
        async with server:
            outcome = await runtime.jobs.process("job-success")
    finally:
        await runtime.upstream.client.aclose()

    row = await runtime.repository.one(
        """
        SELECT status, artifact_schema, images_json
        FROM jobs WHERE job_id = ?
        """,
        ("job-success",),
    )
    assert post_count == 1
    assert outcome is JobProcessOutcome.SUCCEEDED
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["artifact_schema"] == 2
    assert '"sha256":"' in row["images_json"]
    assert runtime.jobs.dispatch_outcomes[("started", "succeeded")] == 1
