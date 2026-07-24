from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from collections.abc import AsyncIterator
from datetime import datetime, timezone
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
    spec = importlib.util.spec_from_file_location(
        "image_job_write_line_under_test", path
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.ALLOW_LEGACY_BEARER_AUTH = True
    return module


def _png_bytes(color: tuple[int, int, int] = (128, 128, 128)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (2, 2), color=color).save(buf, format="PNG")
    return buf.getvalue()


class _JsonRequest:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        content_length: str | None = None,
    ) -> None:
        self.raw = json.dumps(payload).encode("utf-8")
        self.headers = {"authorization": "Bearer sk-test"}
        if content_length is not None:
            self.headers["content-length"] = content_length
        self.stream_started = False

    async def stream(self) -> AsyncIterator[bytes]:
        self.stream_started = True
        midpoint = len(self.raw) // 2
        yield self.raw[:midpoint]
        yield self.raw[midpoint:]


class _StreamResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = chunks or []
        self.yielded = 0
        self.closed = False
        self.read_called = False

    async def __aenter__(self) -> _StreamResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    async def aiter_bytes(
        self,
        chunk_size: int | None = None,
    ) -> AsyncIterator[bytes]:
        _ = chunk_size
        for chunk in self.chunks:
            self.yielded += 1
            await asyncio.sleep(0)
            yield chunk

    async def aread(self) -> bytes:
        self.read_called = True
        return b"".join(self.chunks)


def _configure_db(app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "image_jobs.sqlite3")
    monkeypatch.setattr(app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app, "REFS_DIR", tmp_path / "refs")
    app.init_storage_sync()


def _job_row_payload(
    app: Any, *, endpoint: str = "/v1/images/generations"
) -> dict[str, Any]:
    return {
        "payload_json": app.json_dump(
            {
                "endpoint": endpoint,
                "body": {"prompt": "cat"},
                "request_type": "generations",
                "retention_days": 1,
            }
        ),
        "auth_header": "Bearer sk-test",
        "job_id": "job-persistent-1",
        "created_at": "2026-07-11T00:00:00+00:00",
        "retention_days": 1,
    }


def test_create_rejects_declared_oversize_before_reading_stream(monkeypatch) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_IMAGE_JOB_REQUEST_BYTES", 10)
    request = _JsonRequest(
        {
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
        },
        content_length="11",
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.create_image_job(request))

    assert exc.value.status_code == 413
    assert request.stream_started is False


@pytest.mark.parametrize(
    ("limit_name", "limit", "value", "expected"),
    [
        ("MAX_JSON_DEPTH", 2, {"a": {"b": {"c": 1}}}, "depth"),
        ("MAX_JSON_ARRAY_ITEMS", 2, [1, 2, 3], "array"),
        ("MAX_JSON_KEY_CHARS", 3, {"long": 1}, "key"),
        ("MAX_JSON_STRING_CHARS", 3, "long", "string"),
    ],
)
def test_json_shape_limits(
    monkeypatch,
    limit_name: str,
    limit: int,
    value: object,
    expected: str,
) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, limit_name, limit)

    with pytest.raises(HTTPException) as exc:
        app.validate_json_shape(value)

    assert expected in str(exc.value.detail).lower()


def test_json_shape_accepts_exact_container_depth(monkeypatch) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_JSON_DEPTH", 2)

    app.validate_json_shape({"a": {"b": 1}})


@pytest.mark.parametrize(
    "constant",
    ["NaN", "Infinity", "-Infinity", "1e999"],
)
def test_image_job_json_rejects_non_finite_constants(constant: str) -> None:
    app = load_app_module()
    raw = (
        f'{{"endpoint":"/v1/images/generations","body":{{"value":{constant}}}}}'
    ).encode()

    with pytest.raises(HTTPException) as exc:
        app.load_image_job_json(raw)

    assert exc.value.status_code == 400
    assert app.parse_json_bytes(raw) is None


def test_validate_payload_and_json_dump_reject_non_finite_numbers() -> None:
    app = load_app_module()
    payload = {
        "endpoint": "/v1/images/generations",
        "body": {"value": float("nan")},
    }

    with pytest.raises(HTTPException):
        app.validate_payload(payload)
    with pytest.raises(ValueError):
        app.json_dump(payload)
    assert app._try_parse_sse_data('{"value":Infinity}') is None


def test_queue_full_rejects_without_persisting_failure_row(
    monkeypatch,
    tmp_path,
) -> None:
    app = load_app_module()
    _configure_db(app, monkeypatch, tmp_path)
    app._queue = asyncio.Queue(maxsize=1)
    app._queued_ids = set()
    app._inflight = set()

    request = _JsonRequest(
        {
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
        }
    )

    async def run() -> int:
        app._queue.put_nowait("already-queued")
        app._queued_ids.add("already-queued")
        with pytest.raises(HTTPException) as exc:
            await app.create_image_job(request)
        assert exc.value.status_code == 503
        rows = await app.db_all("SELECT job_id FROM jobs")
        return len(rows)

    assert asyncio.run(run()) == 0


def test_upstream_idempotency_key_is_stable_across_safe_retry(monkeypatch) -> None:
    app = load_app_module()
    app._http_client = SimpleNamespace()
    monkeypatch.setattr(app, "RETRY_NETWORK_MAX", 1)
    monkeypatch.setattr(app, "RETRY_RESPONSES_STREAM_MAX", 0)
    monkeypatch.setattr(app, "RETRY_UPSTREAM_5XX_MAX", 0)
    monkeypatch.setattr(app, "RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(app, "UPSTREAM_IDEMPOTENCY_GUARANTEED", False)
    seen_keys: list[str] = []

    async def fake_once(*_args: object, **kwargs: Any):
        seen_keys.append(kwargs["headers"]["Idempotency-Key"])
        if len(seen_keys) == 1:
            raise httpx.ConnectError("connect failed before request dispatch")
        return 200, [{"url": "saved"}]

    monkeypatch.setattr(app, "_call_upstream_once", fake_once)

    status, images = asyncio.run(app.call_upstream(_job_row_payload(app)))

    expected = app.upstream_idempotency_key("job-persistent-1")
    assert status == 200
    assert images == [{"url": "saved"}]
    assert seen_keys == [expected, expected]


def test_ambiguous_failure_is_not_retried_without_upstream_guarantee(
    monkeypatch,
) -> None:
    app = load_app_module()
    app._http_client = SimpleNamespace()
    monkeypatch.setattr(app, "RETRY_NETWORK_MAX", 1)
    monkeypatch.setattr(app, "RETRY_RESPONSES_STREAM_MAX", 1)
    monkeypatch.setattr(app, "RETRY_UPSTREAM_5XX_MAX", 1)
    monkeypatch.setattr(app, "UPSTREAM_IDEMPOTENCY_GUARANTEED", False)
    attempts = 0

    async def fake_once(*_args: object, **_kwargs: object):
        nonlocal attempts
        attempts += 1
        raise app.JobFailure(
            "read failed after upstream may have accepted the request",
            retryable=True,
            retry_requires_idempotency=True,
            outcome_uncertain=True,
            error_class=app.ERROR_CLASS_NETWORK,
        )

    monkeypatch.setattr(app, "_call_upstream_once", fake_once)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(app.call_upstream(_job_row_payload(app)))

    assert attempts == 1
    assert exc.value.outcome_uncertain is True
    assert exc.value.retry_suppressed is True


def test_ambiguous_failure_retries_when_upstream_guarantees_idempotency(
    monkeypatch,
) -> None:
    app = load_app_module()
    app._http_client = SimpleNamespace()
    monkeypatch.setattr(app, "RETRY_NETWORK_MAX", 1)
    monkeypatch.setattr(app, "RETRY_RESPONSES_STREAM_MAX", 1)
    monkeypatch.setattr(app, "RETRY_UPSTREAM_5XX_MAX", 0)
    monkeypatch.setattr(app, "RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(app, "UPSTREAM_IDEMPOTENCY_GUARANTEED", True)
    attempts = 0

    async def fake_once(*_args: object, **_kwargs: object):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise app.JobFailure(
                "read failed after upstream may have accepted the request",
                retryable=True,
                retry_requires_idempotency=True,
                outcome_uncertain=True,
                error_class=app.ERROR_CLASS_NETWORK,
            )
        return 200, [{"url": "saved"}]

    monkeypatch.setattr(app, "_call_upstream_once", fake_once)

    status, images = asyncio.run(app.call_upstream(_job_row_payload(app)))

    assert status == 200
    assert images == [{"url": "saved"}]
    assert attempts == 2


@pytest.mark.parametrize(
    ("error_class_name", "upstream_status"),
    [
        ("ERROR_CLASS_NETWORK", None),
        ("ERROR_CLASS_UPSTREAM_5XX", 502),
    ],
)
def test_download_failure_after_successful_post_is_uncertain_without_replay(
    monkeypatch,
    error_class_name: str,
    upstream_status: int | None,
) -> None:
    app = load_app_module()
    response_content = b'{"data":[{"url":"https://cdn.example/result.png"}]}'
    post_attempts = 0

    class _PostClient:
        def stream(self, *_args: object, **_kwargs: object) -> _StreamResponse:
            nonlocal post_attempts
            post_attempts += 1
            return _StreamResponse(
                headers={
                    "content-type": "application/json",
                    "content-length": str(len(response_content)),
                },
                chunks=[response_content],
            )

    async def failed_download(
        *_args: object,
        **_kwargs: object,
    ) -> None:
        raise app.JobFailure(
            "result URL fetch failed after POST succeeded",
            upstream_status=upstream_status,
            retryable=True,
            retry_requires_idempotency=True,
            error_class=getattr(app, error_class_name),
        )

    app._http_client = _PostClient()
    monkeypatch.setattr(app, "download_image_url", failed_download)
    monkeypatch.setattr(app, "RETRY_NETWORK_MAX", 1)
    monkeypatch.setattr(app, "RETRY_UPSTREAM_5XX_MAX", 1)
    monkeypatch.setattr(app, "RETRY_RESPONSES_STREAM_MAX", 1)
    monkeypatch.setattr(app, "UPSTREAM_IDEMPOTENCY_GUARANTEED", False)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(app.call_upstream(_job_row_payload(app)))

    assert post_attempts == 1
    assert exc.value.outcome_uncertain is True
    assert exc.value.retry_suppressed is True


def test_process_job_persists_uncertain_terminal_state(
    monkeypatch,
    tmp_path,
) -> None:
    app = load_app_module()
    _configure_db(app, monkeypatch, tmp_path)

    async def fake_call_upstream(_row: object):
        failure = app.JobFailure(
            "upstream result unresolved",
            retryable=True,
            retry_requires_idempotency=True,
            outcome_uncertain=True,
            error_class=app.ERROR_CLASS_NETWORK,
        )
        failure.retry_suppressed = True
        raise failure

    monkeypatch.setattr(app, "call_upstream", fake_call_upstream)

    async def run() -> dict[str, Any]:
        await app.insert_job(
            "job-uncertain",
            {
                "request_type": "generations",
                "endpoint": "/v1/images/generations",
                "body": {"prompt": "cat"},
                "retention_days": 1,
            },
            "Bearer sk-test",
        )
        await app.process_job("job-uncertain")
        row = await app.db_one(
            "SELECT * FROM jobs WHERE job_id = ?",
            ("job-uncertain",),
        )
        assert row is not None
        return app.row_to_response(row)

    response = asyncio.run(run())

    assert response["status"] == "uncertain"
    assert response["retryable"] is True
    assert response["retry_suppressed"] is True
    assert response["outcome_uncertain"] is True
    assert "idempotency" in response["retry_policy"]


def test_restart_marks_running_job_uncertain_without_upstream_guarantee(
    monkeypatch,
    tmp_path,
) -> None:
    app = load_app_module()
    _configure_db(app, monkeypatch, tmp_path)
    monkeypatch.setattr(app, "UPSTREAM_IDEMPOTENCY_GUARANTEED", False)

    async def run() -> dict[str, Any]:
        await app.insert_job(
            "job-interrupted",
            {
                "request_type": "generations",
                "endpoint": "/v1/images/generations",
                "body": {"prompt": "cat"},
                "retention_days": 1,
            },
            "Bearer sk-test",
        )
        assert await app.mark_running("job-interrupted")
        await app.fail_interrupted_running_jobs()
        row = await app.db_one(
            "SELECT * FROM jobs WHERE job_id = ?",
            ("job-interrupted",),
        )
        assert row is not None
        return app.row_to_response(row)

    response = asyncio.run(run())

    assert response["status"] == "uncertain"
    assert response["retry_suppressed"] is True
    assert response["outcome_uncertain"] is True


def test_non_stream_response_content_length_preflight_skips_body(
    monkeypatch,
) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_UPSTREAM_RESPONSE_BYTES", 10)
    response = _StreamResponse(
        headers={
            "content-type": "application/json",
            "content-length": "11",
        },
        chunks=[b'{"ok":true}'],
    )

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app._extract_non_stream_response_images(
                response,
                SimpleNamespace(),
            )
        )

    assert exc.value.outcome_uncertain is True
    assert response.yielded == 0


def test_direct_image_content_length_uses_single_image_preflight(
    monkeypatch,
) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_IMAGE_BYTES", 8)
    monkeypatch.setattr(app, "MAX_UPSTREAM_RESPONSE_BYTES", 100)
    response = _StreamResponse(
        headers={
            "content-type": "image/png",
            "content-length": "9",
        },
        chunks=[b"not-read"],
    )

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app._extract_non_stream_response_images(
                response,
                SimpleNamespace(),
            )
        )

    assert "单图" in exc.value.error
    assert response.yielded == 0


def test_non_stream_response_aborts_on_streamed_overflow(monkeypatch) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_UPSTREAM_RESPONSE_BYTES", 10)
    response = _StreamResponse(
        headers={"content-type": "application/json"},
        chunks=[b"123456", b"789012", b"not-read"],
    )

    with pytest.raises(app.JobFailure):
        asyncio.run(
            app._extract_non_stream_response_images(
                response,
                SimpleNamespace(),
            )
        )

    assert response.yielded == 2


def test_upstream_error_body_content_length_preflight_is_bounded(
    monkeypatch,
) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_UPSTREAM_ERROR_BODY_BYTES", 4)
    response = _StreamResponse(
        status_code=502,
        headers={"content-length": "100"},
        chunks=[b"not-read"],
    )

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(app._raise_upstream_http_error(response))

    assert exc.value.upstream_body["truncated"] is True
    assert exc.value.outcome_uncertain is True
    assert response.yielded == 0


def test_upstream_error_body_stops_at_streamed_limit_without_reading_full_body(
    monkeypatch,
) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "MAX_UPSTREAM_ERROR_BODY_BYTES", 4)
    response = _StreamResponse(
        status_code=502,
        headers={"content-type": "text/plain"},
        chunks=[b"1234", b"5678", b"unread"],
    )

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(app._raise_upstream_http_error(response))

    assert exc.value.upstream_body == {
        "preview": 1234,
        "truncated": True,
    }
    assert response.yielded == 2
    assert response.read_called is False


def test_image_candidate_count_limit(monkeypatch) -> None:
    app = load_app_module()
    raw = _png_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    monkeypatch.setattr(app, "MAX_IMAGE_CANDIDATES", 2)
    monkeypatch.setattr(app, "MAX_IMAGE_BYTES", len(raw) + 1)
    monkeypatch.setattr(app, "MAX_TOTAL_IMAGE_BYTES", len(raw) * 4)
    response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps(
            {
                "data": [
                    {"b64_json": encoded},
                    {"b64_json": encoded},
                    {"b64_json": encoded},
                ]
            }
        ).encode("utf-8"),
    )

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(app.extract_response_images(response, SimpleNamespace()))

    assert "候选数" in exc.value.error


def test_image_candidate_total_byte_limit(monkeypatch) -> None:
    app = load_app_module()
    raw = _png_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    monkeypatch.setattr(app, "MAX_IMAGE_CANDIDATES", 4)
    monkeypatch.setattr(app, "MAX_IMAGE_BYTES", len(raw) + 1)
    monkeypatch.setattr(app, "MAX_TOTAL_IMAGE_BYTES", len(raw) + 1)
    response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps(
            {"data": [{"b64_json": encoded}, {"b64_json": encoded}]}
        ).encode("utf-8"),
    )

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(app.extract_response_images(response, SimpleNamespace()))

    assert "总字节" in exc.value.error


def test_direct_image_response_enforces_single_image_limit(monkeypatch) -> None:
    app = load_app_module()
    raw = _png_bytes(color=(0, 0, 0))
    monkeypatch.setattr(app, "MAX_IMAGE_BYTES", len(raw) - 1)
    monkeypatch.setattr(app, "MAX_TOTAL_IMAGE_BYTES", len(raw) * 2)
    response = httpx.Response(
        200,
        headers={"content-type": "image/png"},
        content=raw,
    )

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(app.extract_response_images(response, SimpleNamespace()))

    assert "单图" in exc.value.error


def test_row_to_response_sanitizes_legacy_non_finite_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = load_app_module()
    _configure_db(app, monkeypatch, tmp_path)

    async def run() -> dict[str, Any]:
        await app.insert_job(
            "job-legacy-nan",
            {
                "request_type": "generations",
                "endpoint": "/v1/images/generations",
                "body": {"prompt": "cat"},
                "retention_days": 1,
            },
            "Bearer sk-test",
        )
        await app.db_exec(
            """
            UPDATE jobs
            SET status = 'failed', finished_at = ?, error = ?,
                upstream_body = ?
            WHERE job_id = ?
            """,
            (
                "2026-07-11T00:00:00+00:00",
                "legacy error",
                '{"value":NaN}',
                "job-legacy-nan",
            ),
        )
        row = await app.db_one(
            "SELECT * FROM jobs WHERE job_id = ?",
            ("job-legacy-nan",),
        )
        assert row is not None
        return app.row_to_response(row)

    response = asyncio.run(run())

    assert response["upstream_body"] == '{"value":NaN}'
    json.dumps(response, allow_nan=False)


def test_retention_pass_uses_each_job_expiry_not_file_mtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = load_app_module()
    _configure_db(app, monkeypatch, tmp_path)
    monkeypatch.setattr(app, "MAX_RETENTION_DAYS", 30)
    monkeypatch.setattr(app, "JOB_TTL_DAYS", 30)
    monkeypatch.setattr(
        app,
        "utc_now",
        lambda: datetime(2026, 7, 11, tzinfo=timezone.utc),
    )

    rows = [
        (
            "expired-explicit",
            20,
            app.json_dump([{"expires_at": "2026-07-02T00:00:00+00:00"}]),
        ),
        ("expired-retention", 2, "[]"),
        (
            "live-job",
            20,
            app.json_dump([{"expires_at": "2026-07-21T00:00:00+00:00"}]),
        ),
    ]
    for job_id, retention_days, images_json in rows:
        app._db_exec_sync(
            """
            INSERT INTO jobs (
                job_id, auth_hash, auth_header, request_type, endpoint,
                payload_json, status, relay_url, retention_days,
                created_at, updated_at, finished_at, images_json
            ) VALUES (?, ?, NULL, 'generations', '/v1/images/generations',
                      '{}', 'succeeded', 'http://upstream', ?,
                      ?, ?, ?, ?)
            """,
            (
                job_id,
                "auth",
                retention_days,
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T01:00:00+00:00",
                images_json,
            ),
        )
        image_dir, _ = app.job_image_dir(
            job_id,
            "2026-07-01T00:00:00+00:00",
        )
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / "image-1.png"
        image_path.write_bytes(_png_bytes())
        if job_id == "live-job":
            os.utime(image_path, (1, 1))

    asyncio.run(app._run_retention_pass())

    remaining = {
        row["job_id"] for row in app._db_all_sync("SELECT job_id FROM jobs", ())
    }
    expired_explicit_dir, _ = app.job_image_dir(
        "expired-explicit",
        "2026-07-01T00:00:00+00:00",
    )
    expired_retention_dir, _ = app.job_image_dir(
        "expired-retention",
        "2026-07-01T00:00:00+00:00",
    )
    live_dir, _ = app.job_image_dir(
        "live-job",
        "2026-07-01T00:00:00+00:00",
    )

    assert remaining == {"live-job"}
    assert not expired_explicit_dir.exists()
    assert not expired_retention_dir.exists()
    assert (live_dir / "image-1.png").is_file()


def test_lifespan_second_start_reinitializes_runtime_state(monkeypatch) -> None:
    app = load_app_module()
    clients: list[Any] = []

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            self.closed = False
            clients.append(self)

        async def aclose(self) -> None:
            self.closed = True

    async def no_jobs(*_args: object, **_kwargs: object) -> list[Any]:
        return []

    async def no_op() -> None:
        return None

    async def idle(*_args: object) -> None:
        await app._shutdown.wait()

    monkeypatch.setattr(app, "init_storage_sync", lambda: None)
    monkeypatch.setattr(app, "fail_interrupted_running_jobs", no_op)
    monkeypatch.setattr(app, "db_all", no_jobs)
    monkeypatch.setattr(app, "worker_loop", idle)
    monkeypatch.setattr(app, "retention_sweeper", idle)
    monkeypatch.setattr(app, "stuck_reconciler", idle)
    monkeypatch.setattr(app.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(app, "CONCURRENCY", 1)
    monkeypatch.setattr(app, "GRACEFUL_SHUTDOWN_S", 0)

    async def run() -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        states: list[tuple[int, int, int]] = []
        for index in range(2):
            async with app.lifespan(app.app):
                assert app._shutdown.is_set() is False
                assert app._queue.qsize() == 0
                states.append(
                    (
                        id(app._queue),
                        id(app._queue_state_lock),
                        id(app._shutdown),
                    )
                )
                if index == 0:
                    assert await app.enqueue_job("transient") == "enqueued"
            assert app._shutdown.is_set() is True
        return states[0], states[1]

    first_state, second_state = asyncio.run(run())

    assert first_state != second_state
    assert len(clients) == 2
    assert all(client.closed for client in clients)
