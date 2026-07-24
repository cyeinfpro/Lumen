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
import hashlib
import importlib.util
import json
import stat
import sqlite3
import sys
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image


def load_app_module():
    asyncio.set_event_loop(asyncio.new_event_loop())
    path = Path(__file__).resolve().parents[1] / "app.py"
    module_dir = str(path.parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    spec = importlib.util.spec_from_file_location("image_job_app_under_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.ALLOW_LEGACY_BEARER_AUTH = True
    return module


def _tiny_png_b64() -> str:
    buf = BytesIO()
    Image.new("RGB", (2, 2), color=(128, 128, 128)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _png_bytes(size: tuple[int, int] = (2, 2)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=(128, 128, 128)).save(buf, format="PNG")
    return buf.getvalue()


class _ChunkedRequest:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        content_length: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.headers = dict(headers or {})
        if content_length is not None:
            self.headers["content-length"] = content_length
        self._chunks = chunks
        self.stream_started = False

    async def stream(self) -> AsyncIterator[bytes]:
        self.stream_started = True
        for chunk in self._chunks:
            yield chunk


class _JsonRequest:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        headers: dict[str, str],
    ) -> None:
        self.payload = payload
        self.headers = headers

    async def json(self) -> dict[str, object]:
        return self.payload

    async def stream(self) -> AsyncIterator[bytes]:
        yield json.dumps(self.payload).encode("utf-8")


def test_reference_request_body_rejects_declared_and_streamed_overflow() -> None:
    app = load_app_module()
    declared = _ChunkedRequest([b"not-read"], content_length="11")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(app._read_request_body_bounded(declared, max_bytes=10))

    assert exc.value.status_code == 413
    assert declared.stream_started is False

    chunked = _ChunkedRequest([b"12345", b"67890", b"x"])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app._read_request_body_bounded(chunked, max_bytes=10))

    assert exc.value.status_code == 413


def test_reference_request_body_accepts_exact_limit() -> None:
    app = load_app_module()
    request = _ChunkedRequest([b"12345", b"67890"])

    assert (
        asyncio.run(app._read_request_body_bounded(request, max_bytes=10))
        == b"1234567890"
    )


def test_init_storage_migrates_legacy_jobs_before_idempotency_index(
    monkeypatch,
    tmp_path,
) -> None:
    app = load_app_module()

    db_path = tmp_path / "image_jobs.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                auth_hash TEXT NOT NULL,
                auth_header TEXT,
                request_type TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                relay_url TEXT NOT NULL,
                retention_days INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                elapsed_ms INTEGER,
                upstream_status INTEGER,
                image_count INTEGER NOT NULL DEFAULT 0,
                images_json TEXT,
                error TEXT,
                upstream_body TEXT
            );
            INSERT INTO jobs (
                job_id, auth_hash, auth_header, request_type, endpoint,
                payload_json, status, relay_url, retention_days,
                created_at, updated_at
            ) VALUES (
                'job-migrate', 'owner-hash', 'Bearer sk-migrate',
                'generations', '/v1/images/generations', '{}', 'queued',
                'https://relay.invalid', 1,
                '2026-07-01T00:00:00+00:00',
                '2026-07-01T00:00:00+00:00'
            );
            """
        )
    finally:
        conn.close()

    app.init_storage_sync()

    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(jobs)")}
        index_columns = [
            row[2]
            for row in conn.execute(
                "PRAGMA index_info(jobs_auth_idempotency_idx)"
            )
        ]
        migrated_upstream_hash = conn.execute(
            "SELECT upstream_auth_hash FROM jobs WHERE job_id = 'job-migrate'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert "idempotency_key" in cols
    assert "request_hash" in cols
    assert "upstream_auth_hash" in cols
    assert "jobs_auth_idempotency_idx" in indexes
    assert index_columns == [
        "auth_hash",
        "upstream_auth_hash",
        "idempotency_key",
    ]
    assert migrated_upstream_hash == app.auth_hash("Bearer sk-migrate")
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_refs_migration_rolls_back_and_retries_after_interruption(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = load_app_module()
    persistence = app._job_persistence_module
    db_path = tmp_path / "refs.sqlite3"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE refs (
            sha256 TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO refs VALUES (
            'legacy-sha', 'legacy-token', 'png', 10,
            '2026-07-01T00:00:00+00:00'
        );
        """
    )

    original_copy = persistence._copy_refs_rows

    def interrupted_copy(*args: Any, **kwargs: Any) -> None:
        original_copy(*args, **kwargs)
        raise RuntimeError("simulated migration interruption")

    monkeypatch.setattr(persistence, "_copy_refs_rows", interrupted_copy)
    with pytest.raises(RuntimeError):
        persistence._ensure_refs_auth_schema(conn)

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(refs)")}
    assert "refs" in tables
    assert "refs_auth_migration_new" not in tables
    assert "auth_hash" not in columns

    monkeypatch.setattr(persistence, "_copy_refs_rows", original_copy)
    persistence._ensure_refs_auth_schema(conn)

    migrated = conn.execute("SELECT auth_hash, sha256, token FROM refs").fetchall()
    conn.close()
    assert [tuple(row) for row in migrated] == [
        ("legacy:legacy-sha", "legacy-sha", "legacy-token")
    ]


def test_refs_migration_rejects_invalid_sqlite_identifiers() -> None:
    app = load_app_module()
    persistence = app._job_persistence_module

    assert persistence._sqlite_identifier("refs_auth_migration_new") == (
        '"refs_auth_migration_new"'
    )
    with pytest.raises(ValueError, match="invalid SQLite identifier"):
        persistence._sqlite_identifier('refs"; DROP TABLE jobs; --')


def test_refs_migration_recovers_old_partial_state_without_data_loss(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = load_app_module()
    db_path = tmp_path / "image_jobs.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE refs (
            auth_hash TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            token TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(auth_hash, sha256)
        );
        INSERT INTO refs VALUES (
            'auth-current', 'current-sha', 'current-token', 'png', 10,
            '2026-07-02T00:00:00+00:00'
        );
        CREATE TABLE refs_legacy_auth_migration (
            sha256 TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO refs_legacy_auth_migration VALUES (
            'legacy-sha', 'legacy-token', 'jpg', 20,
            '2026-07-01T00:00:00+00:00'
        );
        CREATE TABLE refs_auth_migration_new (
            auth_hash TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            token TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(auth_hash, sha256)
        );
        INSERT INTO refs_auth_migration_new VALUES (
            'auth-staged', 'staged-sha', 'staged-token', 'webp', 30,
            '2026-07-03T00:00:00+00:00'
        );
        """
    )
    conn.close()

    app.init_storage_sync()

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT auth_hash, sha256, token FROM refs ORDER BY sha256"
    ).fetchall()
    legacy_table = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'refs_legacy_auth_migration'"
    ).fetchone()
    conn.close()

    assert rows == [
        ("auth-current", "current-sha", "current-token"),
        ("legacy:legacy-sha", "legacy-sha", "legacy-token"),
        ("auth-staged", "staged-sha", "staged-token"),
    ]
    assert legacy_table is None


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
    detail = app._first_stream_error([{"type": "response.incomplete", "response": {}}])
    assert isinstance(detail, dict)
    assert detail["code"] == "response_incomplete"


# --- 2) SSE flow -----------------------------------------------------------


class _ScriptedSseResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines: list[str], delay_s: float = 0.0) -> None:
        self._lines = lines
        self._delay_s = delay_s

    async def aiter_bytes(self, chunk_size: int | None = None):
        _ = chunk_size
        for line in self._lines:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            else:
                await asyncio.sleep(0)
            yield line.encode("utf-8") + b"\n"


def test_responses_stream_response_failed_raises_validation_for_moderation(
    monkeypatch,
) -> None:
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
    assert (
        "moderation_blocked" in exc.value.error.lower() or "safety" in exc.value.error
    )


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

        async def aiter_bytes(self, chunk_size: int | None = None):
            _ = chunk_size
            await asyncio.sleep(1.0)  # 模拟上游 stall
            yield b"data: {}\n"

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.extract_responses_stream_images(
                _StallResponse(), SimpleNamespace(), job_id="job_idle"
            )
        )
    assert exc.value.error_class == app.ERROR_CLASS_NETWORK
    assert exc.value.retryable is True
    assert "idle" in exc.value.error.lower()


def test_responses_stream_rejects_overlong_unterminated_line_before_decoder_buffers(
    monkeypatch,
) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "RESPONSES_STREAM_MAX_BYTES", 32)

    class _RawOnlyResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def __init__(self) -> None:
            self.chunks_requested = 0
            self.line_decoder_called = False

        async def aiter_lines(self):
            self.line_decoder_called = True
            raise AssertionError("SSE must not use httpx line decoding")
            yield ""

        async def aiter_bytes(self, chunk_size: int | None = None):
            _ = chunk_size
            self.chunks_requested += 1
            yield b"data: " + (b"x" * 64)
            raise AssertionError("reader requested bytes after the limit was hit")

    response = _RawOnlyResponse()
    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.extract_responses_stream_images(
                response,
                SimpleNamespace(),
                job_id="job_long_line",
            )
        )

    assert exc.value.error == (
        "Responses stream exceeded sidecar byte budget before final image"
    )
    assert exc.value.error_class == app.ERROR_CLASS_NETWORK
    assert exc.value.retryable is True
    assert response.chunks_requested == 1
    assert response.line_decoder_called is False


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
        self.yielded = 0
        self.closed = False
        self.read_called = False

    async def __aenter__(self) -> "_StreamGetResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    async def aiter_raw(self, chunk_size: int | None = None):
        _ = chunk_size
        for c in self._chunks:
            self.yielded += 1
            await asyncio.sleep(0)
            yield c

    async def aiter_bytes(self, chunk_size: int | None = None):
        _ = chunk_size
        async for chunk in self.aiter_raw():
            yield chunk

    async def aread(self) -> bytes:
        self.read_called = True
        return b"".join(self._chunks)


class _FakeStreamClient:
    def __init__(self, response: _StreamGetResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_FakeStreamClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: Any) -> _StreamGetResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._response


class _SequenceStreamClient:
    def __init__(self, responses: list[_StreamGetResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_SequenceStreamClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: Any) -> _StreamGetResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)


def _patch_pinned_client(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
    client: Any,
) -> None:
    monkeypatch.setattr(
        app,
        "_new_pinned_image_download_client",
        lambda _target: client,
    )


def test_download_image_url_rejects_via_content_length(monkeypatch) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_IMAGE_BYTES", 1024)

    resp = _StreamGetResponse(
        status_code=200,
        headers={"content-length": "10485760", "content-type": "image/png"},
        chunks=[b""],
    )
    client = _FakeStreamClient(resp)
    _patch_pinned_client(app, monkeypatch, client)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                client,  # type: ignore[arg-type]
                "https://93.184.216.34/big.png",
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
    _patch_pinned_client(app, monkeypatch, client)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                client,  # type: ignore[arg-type]
                "https://93.184.216.34/medium.png",
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
    _patch_pinned_client(app, monkeypatch, client)

    candidate = asyncio.run(
        app.download_image_url(
            client,  # type: ignore[arg-type]
            "https://93.184.216.34/ok.png",
            cache={},
        )
    )
    assert candidate is not None
    assert candidate.data == payload
    assert candidate.mime_type == "image/png"
    assert client.calls[0]["follow_redirects"] is False


def test_download_image_url_rejects_private_network_target() -> None:
    app = load_app_module()
    resp = _StreamGetResponse(
        status_code=200,
        headers={"content-type": "image/png"},
        chunks=[_png_bytes()],
    )
    client = _FakeStreamClient(resp)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                client,  # type: ignore[arg-type]
                "http://169.254.169.254/latest/meta-data",
                cache={},
            )
        )

    assert exc.value.error_class == app.ERROR_CLASS_VALIDATION
    assert not client.calls


def test_download_image_url_rejects_redirect_to_private_network(monkeypatch) -> None:
    app = load_app_module()
    client = _SequenceStreamClient(
        [
            _StreamGetResponse(
                status_code=302,
                headers={"location": "http://127.0.0.1/private.png"},
                chunks=[],
            ),
            _StreamGetResponse(
                status_code=200,
                headers={"content-type": "image/png"},
                chunks=[_png_bytes()],
            ),
        ]
    )
    _patch_pinned_client(app, monkeypatch, client)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                client,  # type: ignore[arg-type]
                "https://93.184.216.34/redirect.png",
                cache={},
            )
        )

    assert exc.value.error_class == app.ERROR_CLASS_VALIDATION
    assert len(client.calls) == 1


def test_download_image_url_retries_on_http_error(monkeypatch) -> None:
    app = load_app_module()

    class _RaisingStreamClient:
        async def __aenter__(self) -> "_RaisingStreamClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def stream(self, *_args: Any, **_kw: Any) -> Any:
            raise httpx.ConnectError("boom")

    client = _RaisingStreamClient()
    _patch_pinned_client(app, monkeypatch, client)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                client,  # type: ignore[arg-type]
                "https://93.184.216.34/err.png",
                cache={},
            )
        )
    assert exc.value.retryable is True
    assert exc.value.error_class == app.ERROR_CLASS_NETWORK
    assert exc.value.outcome_uncertain is True


def test_download_image_url_before_post_is_not_outcome_uncertain(
    monkeypatch,
) -> None:
    app = load_app_module()

    class _RaisingStreamClient:
        async def __aenter__(self) -> "_RaisingStreamClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def stream(self, *_args: Any, **_kw: Any) -> Any:
            raise httpx.ConnectError("boom")

    client = _RaisingStreamClient()
    _patch_pinned_client(app, monkeypatch, client)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                client,  # type: ignore[arg-type]
                "https://93.184.216.34/err.png",
                cache={},
                retry_requires_idempotency=False,
            )
        )

    assert exc.value.retryable is True
    assert exc.value.outcome_uncertain is False


def test_download_image_url_dns_failure_is_uncertain_after_post(
    monkeypatch,
) -> None:
    app = load_app_module()

    async def failed_resolution(_url: str) -> None:
        raise app.ImageDownloadResolutionError("image URL host cannot be resolved")

    monkeypatch.setattr(
        app,
        "resolve_public_image_download_target",
        failed_resolution,
    )

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                SimpleNamespace(),
                "https://cdn.example/result.png",
                cache={},
            )
        )

    assert exc.value.error_class == app.ERROR_CLASS_NETWORK
    assert exc.value.retryable is True
    assert exc.value.outcome_uncertain is True


def test_download_image_url_caps_error_body(monkeypatch) -> None:
    app = load_app_module()
    response = _StreamGetResponse(
        status_code=502,
        headers={"content-type": "text/plain"},
        chunks=[
            b"A" * (32 * 1024),
            b"B" * (32 * 1024),
            b"C",
            b"D" * (32 * 1024),
        ],
    )
    client = _FakeStreamClient(response)
    _patch_pinned_client(app, monkeypatch, client)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.download_image_url(
                client,  # type: ignore[arg-type]
                "https://93.184.216.34/error.png",
                cache={},
            )
        )

    assert exc.value.error_class == app.ERROR_CLASS_UPSTREAM_5XX
    assert exc.value.upstream_body["truncated"] is True
    assert exc.value.outcome_uncertain is True
    assert response.yielded == 3
    assert response.closed is True


def test_download_image_url_pins_first_validated_resolution(monkeypatch) -> None:
    app = load_app_module()
    resolution_calls = 0
    captured_targets: list[Any] = []

    def fake_getaddrinfo(
        _host: str,
        port: int,
        *_args: Any,
        **_kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        nonlocal resolution_calls
        resolution_calls += 1
        ip = "93.184.216.34" if resolution_calls == 1 else "127.0.0.1"
        return [(app.socket.AF_INET, app.socket.SOCK_STREAM, 6, "", (ip, port))]

    response = _StreamGetResponse(
        status_code=200,
        headers={"content-type": "image/png"},
        chunks=[_png_bytes()],
    )
    client = _FakeStreamClient(response)

    def fake_client_factory(target: Any) -> _FakeStreamClient:
        captured_targets.append(target)
        return client

    monkeypatch.setattr(app.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(app, "_new_pinned_image_download_client", fake_client_factory)

    candidate = asyncio.run(
        app.download_image_url(
            client,  # type: ignore[arg-type]
            "https://rebind.example/image.png",
            cache={},
        )
    )

    assert candidate is not None
    assert resolution_calls == 1
    assert captured_targets[0].resolved_ips == ("93.184.216.34",)


def test_pinned_image_transport_preserves_host_sni_and_validated_ip() -> None:
    app = load_app_module()
    target = app.PublicImageDownloadTarget(
        "https://cdn.example/image.png",
        ("93.184.216.34",),
    )
    response_bytes = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
    )

    class ScriptedStream:
        def __init__(self) -> None:
            self.reads = 0
            self.writes: list[bytes] = []
            self.sni_hosts: list[str] = []

        async def read(self, _max_bytes: int, timeout: float | None = None) -> bytes:
            _ = timeout
            self.reads += 1
            return response_bytes if self.reads == 1 else b""

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            _ = timeout
            self.writes.append(buffer)

        async def aclose(self) -> None:
            return None

        async def start_tls(
            self,
            ssl_context: Any,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> "ScriptedStream":
            _ = ssl_context, timeout
            self.sni_hosts.append(server_hostname or "")
            return self

        def get_extra_info(self, _info: str) -> Any:
            return None

    stream = ScriptedStream()

    class ScriptedBackend:
        def __init__(self) -> None:
            self.connected_hosts: list[str] = []

        async def connect_tcp(self, host: str, *_args: Any, **_kwargs: Any) -> Any:
            self.connected_hosts.append(host)
            return stream

        async def connect_unix_socket(
            self, *_args: Any, **_kwargs: Any
        ) -> Any:  # pragma: no cover - defensive interface parity
            raise AssertionError("unexpected unix socket")

        async def sleep(self, _seconds: float) -> None:
            return None

    transport = app.pinned_async_http_transport(target)
    network_backend = transport._pool._network_backend  # noqa: SLF001
    scripted_backend = ScriptedBackend()
    network_backend._backend = scripted_backend  # noqa: SLF001

    async def run_request() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            return await client.get(target.url)

    response = asyncio.run(run_request())

    assert response.content == b"OK"
    assert scripted_backend.connected_hosts == ["93.184.216.34"]
    assert stream.sni_hosts == ["cdn.example"]
    assert b"host: cdn.example\r\n" in b"".join(stream.writes).lower()


# --- 4) restart recovery ----------------------------------------------------


def test_fail_interrupted_running_jobs_requeues_when_auth_present(
    monkeypatch, tmp_path
) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "UPSTREAM_IDEMPOTENCY_GUARANTEED", True)

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


def test_image_job_create_is_idempotent_per_auth_and_key(monkeypatch, tmp_path) -> None:
    app = load_app_module()

    db_path = tmp_path / "image_jobs.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()

    async def fake_enqueue(_job_id: str) -> str:
        return "queued"

    monkeypatch.setattr(app, "enqueue_job", fake_enqueue)

    def request(authorization: str) -> _JsonRequest:
        return _JsonRequest(
            {
                "endpoint": "/v1/images/generations",
                "body": {"prompt": "cat"},
            },
            headers={
                "authorization": authorization,
                "Idempotency-Key": "stable-job",
            },
        )

    async def _run() -> tuple[dict[str, object], dict[str, object], int]:
        first = await app.create_image_job(request("Bearer sk-test"))
        second = await app.create_image_job(request("bEaReR   sk-test"))
        rows = await app.db_all("SELECT * FROM jobs", ())
        return first, second, len(rows)

    first, second, row_count = asyncio.run(_run())

    assert first["job_id"] == second["job_id"]
    assert row_count == 1


def test_image_job_service_auth_is_separate_from_upstream_bearer(
    monkeypatch,
    tmp_path,
) -> None:
    app = load_app_module()
    sidecar_token = "sidecar-token-" + "s" * 32
    upstream_key = "sk-upstream-secret"
    monkeypatch.setattr(app, "SIDECAR_TOKEN", sidecar_token)
    monkeypatch.setattr(app, "ALLOW_LEGACY_BEARER_AUTH", False)
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "image_jobs.sqlite3")
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()

    async def fake_enqueue(_job_id: str) -> str:
        return "queued"

    monkeypatch.setattr(app, "enqueue_job", fake_enqueue)
    request = _JsonRequest(
        {
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
        },
        headers={
            "authorization": f"Bearer {sidecar_token}",
            "x-lumen-upstream-authorization": f"Bearer {upstream_key}",
        },
    )

    async def _run() -> object:
        response = await app.create_image_job(request)
        return await app.db_one(
            "SELECT * FROM jobs WHERE job_id = ?",
            (response["job_id"],),
        )

    row = asyncio.run(_run())

    assert row is not None
    assert row["auth_hash"] == app.auth_hash(f"Bearer {sidecar_token}")
    assert row["upstream_auth_hash"] == app.auth_hash(f"Bearer {upstream_key}")
    assert row["auth_header"] == f"Bearer {upstream_key}"
    assert upstream_key not in row["payload_json"]
    assert row["request_hash"] == app.scoped_request_hash(
        app.validate_payload(request.payload),
        f"Bearer {upstream_key}",
        legacy_auth=False,
    )


def test_image_job_idempotency_is_scoped_to_upstream_provider(
    monkeypatch,
    tmp_path,
) -> None:
    app = load_app_module()
    sidecar_token = "sidecar-token-" + "s" * 32
    monkeypatch.setattr(app, "SIDECAR_TOKEN", sidecar_token)
    monkeypatch.setattr(app, "ALLOW_LEGACY_BEARER_AUTH", False)
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "image_jobs.sqlite3")
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()

    async def fake_enqueue(_job_id: str) -> str:
        return "queued"

    monkeypatch.setattr(app, "enqueue_job", fake_enqueue)

    def request(upstream_key: str) -> _JsonRequest:
        return _JsonRequest(
            {
                "endpoint": "/v1/images/generations",
                "body": {"prompt": "cat"},
            },
            headers={
                "authorization": f"Bearer {sidecar_token}",
                "x-lumen-upstream-authorization": f"Bearer {upstream_key}",
                "Idempotency-Key": "generation-stable-job",
            },
        )

    async def _run() -> tuple[dict[str, object], dict[str, object], list[Any]]:
        first = await app.create_image_job(request("sk-provider-a"))
        second = await app.create_image_job(request("sk-provider-b"))
        rows = await app.db_all(
            """
            SELECT job_id, upstream_auth_hash
            FROM jobs
            ORDER BY job_id
            """,
            (),
        )
        return first, second, rows

    first, second, rows = asyncio.run(_run())

    assert first["job_id"] != second["job_id"]
    assert len(rows) == 2
    assert {row["upstream_auth_hash"] for row in rows} == {
        app.auth_hash("Bearer sk-provider-a"),
        app.auth_hash("Bearer sk-provider-b"),
    }


def test_image_job_service_auth_uses_compare_digest(monkeypatch) -> None:
    app = load_app_module()
    sidecar_token = "sidecar-token-" + "s" * 32
    calls: list[tuple[bytes, bytes]] = []

    def fake_compare_digest(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(
        app._payload_helpers.hmac,
        "compare_digest",
        fake_compare_digest,
    )
    monkeypatch.setattr(app, "SIDECAR_TOKEN", sidecar_token)
    monkeypatch.setattr(app, "ALLOW_LEGACY_BEARER_AUTH", False)

    owner, legacy = app.authenticate_caller(
        SimpleNamespace(headers={"authorization": f"Bearer {sidecar_token}"})
    )

    assert owner == f"Bearer {sidecar_token}"
    assert legacy is False
    assert calls == [(sidecar_token.encode(), sidecar_token.encode())]


def test_image_job_service_auth_fails_closed_without_config(monkeypatch) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "SIDECAR_TOKEN", "")
    monkeypatch.setattr(app, "ALLOW_LEGACY_BEARER_AUTH", False)

    with pytest.raises(HTTPException) as exc:
        app.authenticate_caller(
            SimpleNamespace(headers={"authorization": "Bearer sk-upstream"})
        )

    assert exc.value.status_code == 503
    assert "sk-upstream" not in str(exc.value.detail)


def test_image_job_startup_auth_config_is_fail_closed() -> None:
    app = load_app_module()

    with pytest.raises(RuntimeError, match="IMAGE_JOB_SIDECAR_TOKEN"):
        app.validate_sidecar_auth_config(
            "",
            allow_legacy=False,
            min_token_chars=32,
        )

    app.validate_sidecar_auth_config(
        "",
        allow_legacy=True,
        min_token_chars=32,
    )


def test_image_job_legacy_auth_requires_explicit_opt_in(monkeypatch) -> None:
    app = load_app_module()
    sidecar_token = "sidecar-token-" + "s" * 32
    request = SimpleNamespace(
        headers={"authorization": "Bearer sk-legacy-upstream"}
    )
    monkeypatch.setattr(app, "SIDECAR_TOKEN", sidecar_token)
    monkeypatch.setattr(app, "ALLOW_LEGACY_BEARER_AUTH", False)

    with pytest.raises(HTTPException) as exc:
        app.authenticate_caller(request)
    assert exc.value.status_code == 401

    monkeypatch.setattr(app, "ALLOW_LEGACY_BEARER_AUTH", True)
    owner, legacy = app.authenticate_caller(request)
    assert owner == "Bearer sk-legacy-upstream"
    assert legacy is True


def test_new_service_auth_can_poll_legacy_job(monkeypatch, tmp_path) -> None:
    app = load_app_module()
    sidecar_token = "sidecar-token-" + "s" * 32
    upstream_auth = "Bearer sk-legacy-upstream"
    monkeypatch.setattr(app, "SIDECAR_TOKEN", sidecar_token)
    monkeypatch.setattr(app, "ALLOW_LEGACY_BEARER_AUTH", False)
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "image_jobs.sqlite3")
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()
    payload = app.validate_payload(
        {
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
        }
    )

    async def _run() -> dict[str, object]:
        await app.insert_job("job-legacy", payload, upstream_auth)
        return await app.get_image_job(
            "job-legacy",
            SimpleNamespace(
                headers={
                    "authorization": f"Bearer {sidecar_token}",
                    "x-lumen-upstream-authorization": upstream_auth,
                }
            ),
        )

    response = asyncio.run(_run())
    assert response["job_id"] == "job-legacy"
    assert response["status"] == "queued"


def test_image_job_service_auth_rejects_missing_upstream_header(monkeypatch) -> None:
    app = load_app_module()
    sidecar_token = "sidecar-token-" + "s" * 32
    monkeypatch.setattr(app, "SIDECAR_TOKEN", sidecar_token)
    monkeypatch.setattr(app, "ALLOW_LEGACY_BEARER_AUTH", False)
    request = _JsonRequest(
        {
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
        },
        headers={"authorization": f"Bearer {sidecar_token}"},
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.create_image_job(request))

    assert exc.value.status_code == 400
    assert sidecar_token not in str(exc.value.detail)


def test_image_job_payload_idempotency_key_is_persisted_and_deduped(
    monkeypatch,
    tmp_path,
) -> None:
    app = load_app_module()

    db_path = tmp_path / "image_jobs.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()

    async def fake_enqueue(_job_id: str) -> str:
        return "queued"

    monkeypatch.setattr(app, "enqueue_job", fake_enqueue)

    def request() -> _JsonRequest:
        return _JsonRequest(
            {
                "endpoint": "/v1/images/generations",
                "idempotency_key": "payload-stable-job",
                "body": {"prompt": "cat"},
            },
            headers={"authorization": "Bearer sk-test"},
        )

    async def _run() -> tuple[dict[str, object], dict[str, object], int, str | None]:
        first = await app.create_image_job(request())
        second = await app.create_image_job(request())
        rows = await app.db_all("SELECT * FROM jobs", ())
        return first, second, len(rows), rows[0]["idempotency_key"]

    first, second, row_count, stored_key = asyncio.run(_run())

    assert first["job_id"] == second["job_id"]
    assert row_count == 1
    assert stored_key == hashlib.sha256(b"payload-stable-job").hexdigest()


def test_idempotent_duplicate_reschedules_existing_queued_job(
    monkeypatch,
    tmp_path,
) -> None:
    app = load_app_module()

    db_path = tmp_path / "image_jobs.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()

    def request() -> _JsonRequest:
        return _JsonRequest(
            {
                "endpoint": "/v1/images/generations",
                "body": {"prompt": "cat"},
            },
            headers={
                "authorization": "Bearer sk-test",
                "Idempotency-Key": "stable-job",
            },
        )

    async def _run() -> tuple[dict[str, object], int, bool]:
        raw_payload = await request().json()
        payload = app.validate_payload(raw_payload)
        await app.insert_job(
            "job-existing",
            payload,
            "Bearer sk-test",
            idempotency_key=hashlib.sha256(b"stable-job").hexdigest(),
            payload_hash=app.request_hash(payload),
        )

        response = await app.create_image_job(request())
        rows = await app.db_all("SELECT * FROM jobs", ())
        return response, len(rows), "job-existing" in app._queued_ids

    response, row_count, queued = asyncio.run(_run())

    assert response["job_id"] == "job-existing"
    assert row_count == 1
    assert queued is True


def test_image_job_idempotency_key_conflict_rejects_different_payload(
    monkeypatch,
    tmp_path,
) -> None:
    app = load_app_module()

    db_path = tmp_path / "image_jobs.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()

    async def fake_enqueue(_job_id: str) -> str:
        return "queued"

    monkeypatch.setattr(app, "enqueue_job", fake_enqueue)

    def request(prompt: str) -> _JsonRequest:
        return _JsonRequest(
            {
                "endpoint": "/v1/images/generations",
                "body": {"prompt": prompt},
            },
            headers={
                "authorization": "Bearer sk-test",
                "Idempotency-Key": "stable-job",
            },
        )

    async def _run() -> None:
        await app.create_image_job(request("cat"))
        await app.create_image_job(request("dog"))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_run())

    assert exc.value.status_code == 409


def test_refs_dedupe_is_scoped_by_auth_hash(monkeypatch, tmp_path) -> None:
    app = load_app_module()

    db_path = tmp_path / "image_jobs.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()

    raw = _png_bytes()
    sha = hashlib.sha256(raw).hexdigest()

    app._write_ref_sync("auth-a", sha, "token-a", "png", raw)  # noqa: SLF001
    assert app._existing_ref_sync("auth-a", sha) == ("token-a", "png")  # noqa: SLF001
    assert app._existing_ref_sync("auth-b", sha) is None  # noqa: SLF001

    app._write_ref_sync("auth-b", sha, "token-b", "png", raw)  # noqa: SLF001
    assert app._existing_ref_sync("auth-a", sha) == ("token-a", "png")  # noqa: SLF001
    assert app._existing_ref_sync("auth-b", sha) == ("token-b", "png")  # noqa: SLF001


def test_reference_upload_uses_pillow_format_for_mismatched_content_type(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = load_app_module()
    db_path = tmp_path / "image_jobs.sqlite3"
    refs_dir = tmp_path / "refs"
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", refs_dir)
    app.init_storage_sync()

    raw = _png_bytes()
    request = _ChunkedRequest(
        [raw],
        headers={
            "authorization": "Bearer sk-test",
            "content-type": "image/jpeg",
        },
    )

    result = asyncio.run(app.upload_reference(request))

    assert result["url"].endswith(".png")
    files = list(refs_dir.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".png"
    assert files[0].read_bytes() == raw
    assert app._candidate_filename(
        "ref-0",
        app.ImageCandidate(raw, "image/jpeg"),
    ) == ("ref-0.png", "image/png")


def test_image_metadata_rejects_images_above_pixel_limit(monkeypatch) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_IMAGE_PIXELS", 1)

    with pytest.raises(app.JobFailure) as exc:
        app.image_metadata(_png_bytes((2, 2)), "image/png")

    assert exc.value.error_class == app.ERROR_CLASS_IMAGE_SAVE


def test_save_images_rejects_signature_only_fake_before_writing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    fake_png = b"\x89PNG\r\n\x1a\n" + b"not-a-real-png"

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.save_images(
                "job-fake-image",
                "2026-07-11T00:00:00+00:00",
                1,
                [app.ImageCandidate(fake_png, "image/png")],
            )
        )

    assert exc.value.error_class == app.ERROR_CLASS_IMAGE_SAVE
    assert "Pillow" in exc.value.error
    assert not (tmp_path / "data" / "images").exists()


def test_pillow_decompression_limit_tracks_config(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_JOB_MAX_IMAGE_PIXELS", "12345")
    app = load_app_module()

    assert app.MAX_IMAGE_PIXELS == 12345
    assert app.Image.MAX_IMAGE_PIXELS == 12345


def test_process_job_claims_queued_row_once(monkeypatch, tmp_path) -> None:
    app = load_app_module()

    db_path = tmp_path / "image_jobs.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()

    calls: list[str] = []

    async def fake_call_upstream(row) -> tuple[int, list[dict[str, object]]]:
        calls.append(row["job_id"])
        await asyncio.sleep(0)
        return (
            200,
            [
                {
                    "url": "https://example.com/image.png",
                    "width": 2,
                    "height": 2,
                    "bytes": 10,
                    "format": "png",
                    "expires_at": "2026-05-05T00:00:00+00:00",
                }
            ],
        )

    monkeypatch.setattr(app, "call_upstream", fake_call_upstream)

    async def _run() -> tuple[list[str], str, int]:
        await app.insert_job(
            "job-claim",
            {
                "request_type": "generations",
                "endpoint": "/v1/images/generations",
                "body": {"prompt": "cat"},
                "retention_days": 1,
            },
            "Bearer sk-test",
        )
        await asyncio.gather(app.process_job("job-claim"), app.process_job("job-claim"))
        row = await app.db_one(
            "SELECT status, attempts FROM jobs WHERE job_id = ?",
            ("job-claim",),
        )
        assert row is not None
        return calls, row["status"], row["attempts"]

    seen_calls, status, attempts = asyncio.run(_run())

    assert seen_calls == ["job-claim"]
    assert status == "succeeded"
    assert attempts == 1
