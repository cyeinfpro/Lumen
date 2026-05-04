"""image-stability-hardening §image-job 新增能力的单元测试。

覆盖：
- _is_responses_success_terminal / _is_responses_error_terminal
- _first_stream_error 识别 response.incomplete
- extract_responses_stream_images：response.failed 抛 validation 类
- extract_responses_stream_images：response.incomplete 抛 upstream 类
- extract_responses_stream_images：idle timeout 触发 retryable network 错
- download_image_url：Content-Length 预检拒巨型 body
- download_image_url：streaming 累计超阈值时立即中断
- fail_interrupted_running_jobs：有 auth → 重排为 queued；无 auth → 标 failed
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image


def load_app_module():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    path = Path(__file__).resolve().parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("image_job_app_under_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tiny_png_b64() -> str:
    buf = BytesIO()
    Image.new("RGB", (2, 2), color=(128, 128, 128)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# --- 1) terminal helpers ----------------------------------------------------


def test_is_responses_success_terminal_accepts_completed_and_done() -> None:
    app = load_app_module()
    assert app._is_responses_success_terminal({"type": "response.completed"})
    assert app._is_responses_success_terminal({"type": "response.done"})
    assert not app._is_responses_success_terminal({"type": "response.failed"})
    assert not app._is_responses_success_terminal({"type": "other"})
    assert not app._is_responses_success_terminal(None)


def test_is_responses_error_terminal_accepts_failed_incomplete_error() -> None:
    app = load_app_module()
    assert app._is_responses_error_terminal({"type": "response.failed"})
    assert app._is_responses_error_terminal({"type": "response.incomplete"})
    assert app._is_responses_error_terminal({"type": "error"})
    assert not app._is_responses_error_terminal({"type": "response.completed"})


def test_first_stream_error_picks_up_response_incomplete() -> None:
    app = load_app_module()
    detail = app._first_stream_error(
        [
            {"type": "response.in_progress"},
            {
                "type": "response.incomplete",
                "response": {
                    "incomplete_details": {
                        "reason": "max_output_tokens",
                        "message": "limit reached",
                    }
                },
            },
        ]
    )
    assert isinstance(detail, dict)
    assert detail.get("reason") == "max_output_tokens"


def test_first_stream_error_falls_back_when_incomplete_has_no_details() -> None:
    app = load_app_module()
    detail = app._first_stream_error(
        [{"type": "response.incomplete", "response": {}}]
    )
    assert isinstance(detail, dict)
    assert detail["code"] == "response_incomplete"


# --- 2) SSE flow -----------------------------------------------------------


class _ScriptedSseResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines: list[str], delay_s: float = 0.0) -> None:
        self._lines = lines
        self._delay_s = delay_s

    async def aiter_lines(self):
        for line in self._lines:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            else:
                await asyncio.sleep(0)
            yield line


def test_responses_stream_response_failed_raises_validation_for_moderation(monkeypatch) -> None:
    app = load_app_module()

    async def fake_touch(_job_id: str) -> None:
        return None

    monkeypatch.setattr(app, "touch_running", fake_touch)
    monkeypatch.setattr(app, "JOB_HEARTBEAT_INTERVAL_S", 0)

    resp = _ScriptedSseResponse(
        [
            'data: {"type":"response.failed","response":{"status":"failed",'
            '"error":{"code":"moderation_blocked","message":"safety"}}}',
            "",
        ]
    )

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.extract_responses_stream_images(
                resp, SimpleNamespace(), job_id="job_modblock"
            )
        )
    assert exc.value.error_class == app.ERROR_CLASS_VALIDATION
    assert "moderation_blocked" in exc.value.error.lower() or "safety" in exc.value.error


def test_responses_stream_response_incomplete_does_not_become_network_retry(
    monkeypatch,
) -> None:
    """response.incomplete + reason=max_output_tokens：必须按 upstream 错误抛，
    不能像旧实现一样错判为 retryable network 流中断。"""
    app = load_app_module()

    async def fake_touch(_job_id: str) -> None:
        return None

    monkeypatch.setattr(app, "touch_running", fake_touch)
    monkeypatch.setattr(app, "JOB_HEARTBEAT_INTERVAL_S", 0)

    resp = _ScriptedSseResponse(
        [
            'data: {"type":"response.incomplete","response":{'
            '"incomplete_details":{"reason":"max_output_tokens"}}}',
            "",
        ]
    )

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.extract_responses_stream_images(
                resp, SimpleNamespace(), job_id="job_inc"
            )
        )
    # incomplete 不属于 ERROR_CLASS_NETWORK，不应自动 retry
    assert exc.value.error_class != app.ERROR_CLASS_NETWORK


def test_responses_stream_idle_timeout_raises_retryable_network(monkeypatch) -> None:
    app = load_app_module()

    async def fake_touch(_job_id: str) -> None:
        return None

    monkeypatch.setattr(app, "touch_running", fake_touch)
    monkeypatch.setattr(app, "JOB_HEARTBEAT_INTERVAL_S", 0)
    # 把 idle timeout 调到极小值
    monkeypatch.setattr(app, "RESPONSES_STREAM_IDLE_TIMEOUT_S", 0.05)

    class _StallResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            await asyncio.sleep(1.0)  # 模拟上游 stall
            yield "data: {}"

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.extract_responses_stream_images(
                _StallResponse(), SimpleNamespace(), job_id="job_idle"
            )
        )
    assert exc.value.error_class == app.ERROR_CLASS_NETWORK
    assert exc.value.retryable is True
    assert "idle" in exc.value.error.lower()


# --- 3) download_image_url streaming 预检 ----------------------------------


class _StreamGetResponse:
    def __init__(
        self,
        *,
        status_code: int,
        headers: dict[str, str],
        chunks: list[bytes],
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._chunks = chunks
        self.is_success = 200 <= status_code < 400

    async def __aenter__(self) -> "_StreamGetResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def aiter_bytes(self):
        for c in self._chunks:
            await asyncio.sleep(0)
            yield c

    async def aread(self) -> bytes:
        return b"".join(self._chunks)


class _FakeStreamClient:
    def __init__(self, response: _StreamGetResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> _StreamGetResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._response


def test_download_image_url_rejects_via_content_length(monkeypatch) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_IMAGE_BYTES", 1024)

    resp = _StreamGetResponse(
        status_code=200,
        headers={"content-length": "10485760", "content-type": "image/png"},
        chunks=[b""],
    )
    client = _FakeStreamClient(resp)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                client,  # type: ignore[arg-type]
                "https://cdn.example/big.png",
                cache={},
            )
        )
    assert exc.value.error_class == app.ERROR_CLASS_IMAGE_SAVE
    assert "Content-Length" in exc.value.error


def test_download_image_url_aborts_when_streaming_exceeds_limit(monkeypatch) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_IMAGE_BYTES", 16)

    # 不给 Content-Length，让 streaming 累加触发限制
    resp = _StreamGetResponse(
        status_code=200,
        headers={"content-type": "image/png"},
        chunks=[b"A" * 8, b"B" * 8, b"C" * 8],  # 累计 24 > 16
    )
    client = _FakeStreamClient(resp)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                client,  # type: ignore[arg-type]
                "https://cdn.example/medium.png",
                cache={},
            )
        )
    assert exc.value.error_class == app.ERROR_CLASS_IMAGE_SAVE


def test_download_image_url_succeeds_under_limit(monkeypatch) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_IMAGE_BYTES", 1024 * 1024)

    payload = b"\x89PNG\r\n\x1a\n" + b"A" * 100
    resp = _StreamGetResponse(
        status_code=200,
        headers={"content-type": "image/png"},
        chunks=[payload[:32], payload[32:]],
    )
    client = _FakeStreamClient(resp)

    candidate = asyncio.run(
        app.download_image_url(
            client,  # type: ignore[arg-type]
            "https://cdn.example/ok.png",
            cache={},
        )
    )
    assert candidate is not None
    assert candidate.data == payload
    assert candidate.mime_type == "image/png"


def test_download_image_url_retries_on_http_error(monkeypatch) -> None:
    app = load_app_module()

    class _RaisingStreamClient:
        def stream(self, *_args: Any, **_kw: Any) -> Any:
            raise httpx.ConnectError("boom")

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                _RaisingStreamClient(),  # type: ignore[arg-type]
                "https://cdn.example/err.png",
                cache={},
            )
        )
    assert exc.value.retryable is True
    assert exc.value.error_class == app.ERROR_CLASS_NETWORK


# --- 4) restart recovery ----------------------------------------------------


def test_fail_interrupted_running_jobs_requeues_when_auth_present(
    monkeypatch, tmp_path
) -> None:
    app = load_app_module()

    db_path = tmp_path / "image_jobs.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()

    async def _setup_and_run() -> tuple[str, str | None, int | None]:
        # 1) 插入两条 running：一条有 auth，一条无 auth
        await app.db_exec(
            """
            INSERT INTO jobs (
                job_id, auth_hash, auth_header, request_type, endpoint,
                payload_json, status, relay_url, retention_days,
                created_at, updated_at, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-with-auth",
                "h1",
                "Bearer sk-keep",
                "responses",
                "/v1/responses",
                "{}",
                "running",
                "http://upstream",
                1,
                "2026-05-04T00:00:00+00:00",
                "2026-05-04T00:00:00+00:00",
                "2026-05-04T00:00:00+00:00",
            ),
        )
        await app.db_exec(
            """
            INSERT INTO jobs (
                job_id, auth_hash, auth_header, request_type, endpoint,
                payload_json, status, relay_url, retention_days,
                created_at, updated_at, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-no-auth",
                "h2",
                None,
                "responses",
                "/v1/responses",
                "{}",
                "running",
                "http://upstream",
                1,
                "2026-05-04T00:00:00+00:00",
                "2026-05-04T00:00:00+00:00",
                "2026-05-04T00:00:00+00:00",
            ),
        )
        await app.fail_interrupted_running_jobs()
        rows = await app.db_all(
            "SELECT job_id, status, started_at, attempts FROM jobs ORDER BY job_id"
        )
        return rows  # type: ignore[return-value]

    rows = asyncio.run(_setup_and_run())
    by_id = {r["job_id"]: r for r in rows}

    # 有 auth 的被重排为 queued；started_at 清空；attempts +1
    assert by_id["job-with-auth"]["status"] == "queued"
    assert by_id["job-with-auth"]["started_at"] is None
    assert by_id["job-with-auth"]["attempts"] == 1

    # 无 auth 的被标 failed（保持原行为）
    assert by_id["job-no-auth"]["status"] == "failed"
