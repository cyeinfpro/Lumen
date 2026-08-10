from __future__ import annotations

import asyncio
import email.utils
import inspect
import os
import stat
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.upstream_parts import direct_requests
from app.upstream_parts import entrypoints as upstream
from app.upstream_parts.image_execution import (
    ImageExecutionRequest,
    ImageRequestContext,
)
from app.upstream_parts.image_jobs import image_job_idempotency_key
from app.upstream_parts.generated_payload import InlineImageBytes
from app.upstream_parts.upstream_impl import build_image_upstream_runtime
from lumen_core.upstream_billing import (
    UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
    UPSTREAM_RESPONSE_HTTP_ATTEMPTS,
    UPSTREAM_RESPONSE_REQUEST_ID,
    UPSTREAM_RESPONSE_STATUS_CODE,
    UPSTREAM_RESPONSE_TRACE_ID,
)
from lumen_core.url_security import PublicHttpTarget


TEST_UPSTREAM_RUNTIME = build_image_upstream_runtime()
TEST_UPSTREAM_SERVICES = TEST_UPSTREAM_RUNTIME.services


def _image_request(**overrides: Any) -> ImageExecutionRequest:
    request_context = ImageRequestContext.create(
        upstream_runtime=TEST_UPSTREAM_RUNTIME,
    )
    values: dict[str, Any] = {
        "action": "generate",
        "prompt": "test",
        "size": "1024x1024",
        "images": None,
        "mask": None,
        "n": 1,
        "quality": "high",
        "output_format": None,
        "output_compression": None,
        "background": None,
        "moderation": None,
        "model": None,
        "progress_callback": None,
        "provider_override": None,
        "user_id": None,
        "request_context": request_context,
        "upstream_runtime": TEST_UPSTREAM_RUNTIME,
    }
    values.update(overrides)
    return ImageExecutionRequest(**values)


@pytest.mark.asyncio
async def test_responses_image_retry_keeps_progress_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks_seen: list[bool] = []
    retry_attempts_seen: list[int] = []
    trace_ids_seen: list[str] = []

    async def fake_stream(
        request: ImageExecutionRequest,
        *,
        use_httpx: bool = False,
        base_url_override: str | None = None,
        api_key_override: str | None = None,
    ) -> tuple[str, str | None]:
        _ = (
            use_httpx,
            base_url_override,
            api_key_override,
        )
        callbacks_seen.append(request.progress_callback is not None)
        retry_attempts_seen.append(request.request_context.retry_attempt)
        trace_ids_seen.append(request.request_context.trace_id)
        if len(callbacks_seen) == 1:
            error = upstream.UpstreamError(
                "request was not delivered",
                status_code=0,
                error_code="direct_image_request_failed",
                payload={},
            )
            error.upstream_receipt_reason = UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
            raise error
        return "ZmFrZS1wbmc=", None

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.responses, "responses_image_stream", fake_stream
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio, "sleep", lambda _delay: _done()
    )

    async def progress(_event: dict[str, Any]) -> None:
        return None

    result = await TEST_UPSTREAM_SERVICES.retry.responses_image_stream_with_retry(
        _image_request(progress_callback=progress),
        use_httpx=False,
    )

    assert result == ("ZmFrZS1wbmc=", None)
    assert callbacks_seen == [True, True]
    assert retry_attempts_seen == [1, 2]
    assert len(set(trace_ids_seen)) == 1


def test_direct_responses_interfaces_use_typed_request_carrier() -> None:
    expected_parameters = {
        TEST_UPSTREAM_SERVICES.direct.direct_generate_image_once: (
            "request",
            "base_url_override",
            "api_key_override",
            "proxy_override",
            "pinned_target_override",
            "before_attempt",
            "streaming_override",
        ),
        TEST_UPSTREAM_SERVICES.direct.direct_edit_image_once: (
            "request",
            "base_url_override",
            "api_key_override",
            "proxy_override",
            "pinned_target_override",
        ),
        TEST_UPSTREAM_SERVICES.direct.direct_generate_image_with_failover: ("request",),
        TEST_UPSTREAM_SERVICES.direct.direct_edit_image_with_failover: ("request",),
        TEST_UPSTREAM_SERVICES.direct.responses_image_stream_with_failover: (
            "request",
            "use_httpx",
        ),
        TEST_UPSTREAM_SERVICES.retry.responses_image_stream_with_retry: (
            "request",
            "use_httpx",
            "base_url_override",
            "api_key_override",
            "proxy_override",
            "pinned_target_override",
            "before_attempt",
        ),
        TEST_UPSTREAM_SERVICES.responses.responses_image_stream: (
            "request",
            "use_httpx",
            "base_url_override",
            "api_key_override",
            "proxy_override",
            "pinned_target_override",
        ),
    }

    for function, expected in expected_parameters.items():
        assert tuple(inspect.signature(function).parameters) == expected


def test_bare_httpx_timeout_exception_is_retryable() -> None:
    assert TEST_UPSTREAM_SERVICES.retry.is_retryable_fallback_exception(
        httpx.TimeoutException("curl guard timeout")
    )


def test_fallback_retry_backoff_clamps_at_four_seconds() -> None:
    assert TEST_UPSTREAM_SERVICES.retry.fallback_retry_backoff_seconds(1) == 1.0
    assert TEST_UPSTREAM_SERVICES.retry.fallback_retry_backoff_seconds(2) == 2.0
    assert TEST_UPSTREAM_SERVICES.retry.fallback_retry_backoff_seconds(3) == 4.0
    assert TEST_UPSTREAM_SERVICES.retry.fallback_retry_backoff_seconds(4) == 4.0
    assert TEST_UPSTREAM_SERVICES.retry.fallback_retry_backoff_seconds(6) == 4.0


def test_max_attempts_for_5xx_is_three() -> None:
    exc = upstream.UpstreamError(
        "temporary upstream error",
        status_code=503,
        error_code="server_error",
    )
    assert TEST_UPSTREAM_SERVICES.retry.max_attempts_for_exception(exc) == 3


def test_parse_retry_after_accepts_http_date() -> None:
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    parsed = TEST_UPSTREAM_SERVICES.core.parse_retry_after_seconds(
        email.utils.format_datetime(retry_at)
    )

    assert parsed == 15.0


@pytest.mark.asyncio
async def test_post_with_retry_honors_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    class _Client:
        calls = 0

        async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            self.calls += 1
            if self.calls == 1:
                return httpx.Response(503, headers={"retry-after": "2.5"})
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio, "sleep", fake_sleep
    )

    resp = await TEST_UPSTREAM_SERVICES.core.post_with_retry(
        client=_Client(),  # type: ignore[arg-type]
        url="https://example.invalid/v1/images/generations",
        headers={},
        json_body={"prompt": "test"},
    )

    assert resp.status_code == 200
    assert sleeps == [2.5]


@pytest.mark.asyncio
async def test_post_with_retry_claims_quota_for_every_physical_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    class _Client:
        calls = 0

        async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            self.calls += 1
            return httpx.Response(503 if self.calls == 1 else 200)

    async def before_attempt(attempt: int) -> None:
        attempts.append(attempt)

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio, "sleep", lambda _delay: _done()
    )

    response = await TEST_UPSTREAM_SERVICES.core.post_with_retry(
        client=_Client(),  # type: ignore[arg-type]
        url="https://example.invalid/v1/images/generations",
        headers={},
        json_body={"prompt": "test"},
        before_attempt=before_attempt,
    )

    assert response.status_code == 200
    assert attempts == [1, 2]


@pytest.mark.asyncio
async def test_post_with_retry_can_disable_ambiguous_status_replay() -> None:
    attempts: list[int] = []

    class _Client:
        calls = 0

        async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            self.calls += 1
            return httpx.Response(503)

    client = _Client()

    async def before_attempt(attempt: int) -> None:
        attempts.append(attempt)

    response = await TEST_UPSTREAM_SERVICES.core.post_with_retry(
        client=client,  # type: ignore[arg-type]
        url="https://example.invalid/v1/images/generations",
        headers={},
        json_body={"prompt": "test"},
        retry_status_codes=False,
        before_attempt=before_attempt,
    )

    assert response.status_code == 503
    assert client.calls == 1
    assert attempts == [1]


@pytest.mark.asyncio
async def test_direct_generate_replays_503_once_with_same_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts: list[dict[str, Any]] = []
    progress_events: list[dict[str, Any]] = []

    class _Client:
        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            posts.append(
                {
                    "url": url,
                    "headers": dict(kwargs["headers"]),
                    "json": dict(kwargs["json"]),
                }
            )
            if len(posts) == 1:
                return httpx.Response(
                    503,
                    headers={"x-request-id": "req-first"},
                    json={"error": {"message": "gateway failed"}},
                )
            return httpx.Response(
                200,
                headers={
                    "x-request-id": "req-final",
                    "traceparent": "00-final-trace-01",
                },
                json={"data": [{"b64_json": "ZmFrZQ==", "revised_prompt": "ok"}]},
            )

    client = _Client()

    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> _Client:
        return client

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle,
        "get_images_client",
        fake_get_images_client,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio,
        "sleep",
        lambda _delay: _done(),
    )

    result = await TEST_UPSTREAM_SERVICES.direct.direct_generate_image_once(
        _image_request(
            progress_callback=progress_events.append,
            request_context=ImageRequestContext.create(trace_id="gen-replay"),
        ),
        base_url_override="https://example.invalid/v1",
        api_key_override="sk-test",
    )

    assert result == [(InlineImageBytes(b"fake"), "ok")]
    assert len(posts) == 2
    assert posts[0]["url"] == posts[1]["url"]
    assert posts[0]["json"] == posts[1]["json"]
    assert (
        posts[0]["headers"]["Idempotency-Key"] == posts[1]["headers"]["Idempotency-Key"]
    )
    assert [event["type"] for event in progress_events] == [
        "dispatch_ready",
        "response_ready",
    ]
    response_event = progress_events[-1]
    assert response_event[UPSTREAM_RESPONSE_STATUS_CODE] == 200
    assert response_event[UPSTREAM_RESPONSE_REQUEST_ID] == "req-final"
    assert response_event[UPSTREAM_RESPONSE_TRACE_ID] == "00-final-trace-01"
    assert response_event[UPSTREAM_RESPONSE_HTTP_ATTEMPTS] == 2


@pytest.mark.asyncio
async def test_direct_generate_repeated_503_is_terminal_with_response_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts: list[dict[str, Any]] = []
    progress_events: list[dict[str, Any]] = []

    class _Client:
        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            posts.append(
                {
                    "url": url,
                    "headers": dict(kwargs["headers"]),
                    "json": dict(kwargs["json"]),
                }
            )
            return httpx.Response(
                503,
                headers={
                    "x-request-id": f"req-{len(posts)}",
                    "x-trace-id": f"trace-{len(posts)}",
                    "authorization": "Bearer must-not-persist",
                    "set-cookie": "session=must-not-persist",
                },
                json={"error": {"message": "gateway failed"}},
            )

    client = _Client()

    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> _Client:
        return client

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle,
        "get_images_client",
        fake_get_images_client,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio,
        "sleep",
        lambda _delay: _done(),
    )

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await TEST_UPSTREAM_SERVICES.direct.direct_generate_image_once(
            _image_request(
                progress_callback=progress_events.append,
                request_context=ImageRequestContext.create(trace_id="gen-unknown"),
            ),
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
        )

    error = exc_info.value
    assert len(posts) == 2
    assert posts[0]["json"] == posts[1]["json"]
    assert (
        posts[0]["headers"]["Idempotency-Key"] == posts[1]["headers"]["Idempotency-Key"]
    )
    assert error.error_code == "direct_image_result_unknown"
    assert error.status_code == 503
    assert error.payload["upstream_result_unknown"] is True
    assert error.payload[UPSTREAM_RESPONSE_STATUS_CODE] == 503
    assert error.payload[UPSTREAM_RESPONSE_REQUEST_ID] == "req-2"
    assert error.payload[UPSTREAM_RESPONSE_TRACE_ID] == "trace-2"
    assert error.payload[UPSTREAM_RESPONSE_HTTP_ATTEMPTS] == 2
    assert "must-not-persist" not in repr(error.payload)
    assert [event["type"] for event in progress_events] == [
        "dispatch_ready",
        "response_received",
    ]
    assert not TEST_UPSTREAM_SERVICES.retry.should_continue_image_provider_failover(
        error,
        retriable=False,
    )


@pytest.mark.asyncio
async def test_response_received_receipt_failure_keeps_safe_metadata() -> None:
    async def fail_receipt(_event: dict[str, Any]) -> None:
        raise RuntimeError("database unavailable")

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await TEST_UPSTREAM_SERVICES.transport.emit_image_progress(
            fail_receipt,
            "response_received",
            **{
                UPSTREAM_RESPONSE_STATUS_CODE: 503,
                UPSTREAM_RESPONSE_REQUEST_ID: "req-final",
                UPSTREAM_RESPONSE_TRACE_ID: "trace-final",
                UPSTREAM_RESPONSE_HTTP_ATTEMPTS: 2,
            },
        )

    error = exc_info.value
    assert error.error_code == "direct_image_result_unknown"
    assert error.status_code == 503
    assert error.payload["receipt_persist_failed"] is True
    assert error.payload[UPSTREAM_RESPONSE_REQUEST_ID] == "req-final"
    assert error.payload[UPSTREAM_RESPONSE_TRACE_ID] == "trace-final"


@pytest.mark.asyncio
async def test_reference_url_live_resolves_public_target_before_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_resolve(url: str, *, allow_http: bool):
        seen["resolved"] = (url, allow_http)
        return SimpleNamespace(url="https://resolved.example/ref.png")

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            seen["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def head(self, url: str) -> httpx.Response:
            seen["head_url"] = url
            return httpx.Response(204)

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure,
        "resolve_public_http_target",
        fake_resolve,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.httpx, "AsyncClient", _Client
    )

    assert await TEST_UPSTREAM_SERVICES.references.reference_url_is_live(
        "https://user.example/ref.png"
    )
    assert seen["resolved"] == ("https://user.example/ref.png", True)
    assert seen["head_url"] == "https://resolved.example/ref.png"
    assert seen["client_kwargs"]["trust_env"] is False


@pytest.mark.asyncio
async def test_reference_url_live_rejects_redirect_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_resolve(url: str, *, allow_http: bool):
        seen["resolved"] = (url, allow_http)
        return SimpleNamespace(url="https://resolved.example/ref.png")

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            seen["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def head(self, url: str) -> httpx.Response:
            seen["head_url"] = url
            return httpx.Response(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure,
        "resolve_public_http_target",
        fake_resolve,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.httpx, "AsyncClient", _Client
    )

    assert not await TEST_UPSTREAM_SERVICES.references.reference_url_is_live(
        " https://user.example/ref.png "
    )
    assert seen["resolved"] == ("https://user.example/ref.png", True)
    assert seen["head_url"] == "https://resolved.example/ref.png"
    assert seen["client_kwargs"]["follow_redirects"] is False
    assert seen["client_kwargs"]["trust_env"] is False


@pytest.mark.asyncio
async def test_post_with_retry_can_disable_httpx_exception_retries() -> None:
    class _Client:
        calls = 0

        async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            self.calls += 1
            raise httpx.ReadTimeout("image still rendering")

    client = _Client()

    with pytest.raises(httpx.ReadTimeout):
        await TEST_UPSTREAM_SERVICES.core.post_with_retry(
            client=client,  # type: ignore[arg-type]
            url="https://example.invalid/v1/images/generations",
            headers={},
            json_body={"prompt": "test"},
            retry_httpx_exceptions=False,
        )

    assert client.calls == 1


@pytest.mark.asyncio
async def test_curl_multipart_rc28_is_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Proc:
        returncode = 28
        pid = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"Operation timed out after 180001 milliseconds"

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _Proc:
        return _Proc()

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(httpx.TimeoutException):
        await TEST_UPSTREAM_SERVICES.transport.curl_post_multipart_using_paths(
            url="https://example.invalid/v1/images/edits",
            data={"prompt": "test"},
            staged_files=[],
            headers={},
            timeout_s=180,
        )


@pytest.mark.asyncio
async def test_curl_multipart_keeps_secrets_out_of_argv_and_uses_form_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Reader:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = list(chunks)

        async def read(self, _size: int) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

    class _Proc:
        def __init__(self) -> None:
            self.returncode = 0
            self.pid = 0
            self.stdout = _Reader([b'{"ok":true}\n__HTTP_STATUS__:200'])
            self.stderr = _Reader([])

        async def wait(self) -> int:
            return self.returncode

    async def fake_create_subprocess_exec(
        *args: str,
        **_kwargs: Any,
    ) -> _Proc:
        captured.setdefault("argv", []).append(args)
        config_path = args[args.index("--config") + 1]
        captured.setdefault("config_path", []).append(config_path)
        captured.setdefault("config_mode", []).append(
            stat.S_IMODE(os.stat(config_path).st_mode)
        )
        captured.setdefault("config", []).append(
            open(  # noqa: SIM115
                config_path,
                encoding="utf-8",
            ).read()
        )
        return _Proc()

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    target = PublicHttpTarget(
        "https://example.invalid/v1",
        ("203.0.113.20",),
    )
    (
        status,
        payload,
    ) = await TEST_UPSTREAM_SERVICES.transport.curl_post_multipart_using_paths(
        url="https://example.invalid/v1/images/edits",
        data={"prompt": "@/etc/passwd", "note": "<~/.ssh/id_rsa"},
        staged_files=[],
        headers={"Authorization": "Bearer sk-secret"},
        timeout_s=30,
        proxy_url="http://proxy-user:proxy-pass@proxy.example:8080",
        pinned_target=target,
    )
    (
        direct_status,
        _,
    ) = await TEST_UPSTREAM_SERVICES.transport.curl_post_multipart_using_paths(
        url="https://example.invalid/v1/images/edits",
        data={"prompt": "direct"},
        staged_files=[],
        headers={"Authorization": "Bearer sk-secret"},
        timeout_s=30,
        pinned_target=target,
    )

    argv = tuple(str(arg) for arg in captured["argv"][0])
    argv_text = "\0".join(argv)
    assert status == 200
    assert direct_status == 200
    assert payload == {"ok": True}
    assert "--form-string" in argv
    assert "prompt=@/etc/passwd" in argv
    assert "note=<~/.ssh/id_rsa" in argv
    assert "sk-secret" not in argv_text
    assert "proxy-pass" not in argv_text
    assert captured["config_mode"] == [0o600, 0o600]
    assert "Bearer sk-secret" in captured["config"][0]
    assert "proxy-pass" in captured["config"][0]
    assert "resolve =" not in captured["config"][0]
    assert 'resolve = "example.invalid:443:203.0.113.20"' in captured["config"][1]
    assert all(not os.path.exists(path) for path in captured["config_path"])


def test_image_idempotency_key_uses_stable_file_fingerprints() -> None:
    files = [
        ("image[]", ("ref.png", b"secret-image-bytes", "image/png")),
        ("mask", ("mask.png", b"mask-bytes", "image/png")),
    ]
    key_a = TEST_UPSTREAM_SERVICES.core.image_idempotency_key(
        trace_id="gen-fixed",
        endpoint="images/edits",
        body={"size": "1024x1024", "prompt": "edit"},
        files=files,
    )
    key_b = TEST_UPSTREAM_SERVICES.core.image_idempotency_key(
        trace_id="gen-fixed",
        endpoint="images/edits",
        body={"prompt": "edit", "size": "1024x1024"},
        files=files,
    )
    fingerprints = TEST_UPSTREAM_SERVICES.core.image_file_fingerprints(files)
    serialized = TEST_UPSTREAM_SERVICES.core.json_dumps_stable({"files": fingerprints})

    assert key_a == key_b
    assert "secret-image-bytes" not in serialized
    assert fingerprints[0]["size"] == len(b"secret-image-bytes")
    assert len(fingerprints[0]["sha256"]) == 64


@pytest.mark.asyncio
async def test_direct_generate_image_once_sends_bound_trace_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_post_with_retry(**kwargs: Any) -> httpx.Response:
        seen["headers"] = dict(kwargs["headers"])
        seen["json_body"] = dict(kwargs["json_body"])
        seen["timeout"] = kwargs.get("timeout")
        seen["retry_httpx_exceptions"] = kwargs.get("retry_httpx_exceptions")
        seen["retry_status_codes"] = kwargs.get("retry_status_codes")
        seen["max_attempts"] = kwargs.get("max_attempts")
        return httpx.Response(
            200,
            json={"data": [{"b64_json": "ZmFrZQ==", "revised_prompt": "ok"}]},
        )

    async def fake_timeout_config() -> TEST_UPSTREAM_SERVICES.lifecycle.TimeoutConfig:
        return TEST_UPSTREAM_SERVICES.lifecycle.TimeoutConfig(
            connect=10.0, read=20.0, write=30.0
        )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle, "get_images_client", fake_get_images_client
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.core, "post_with_retry", fake_post_with_retry
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle, "resolve_timeout_config", fake_timeout_config
    )

    result = await TEST_UPSTREAM_SERVICES.direct.direct_generate_image_once(
        _image_request(
            output_format="png",
            background="auto",
            moderation="auto",
            request_context=ImageRequestContext.create(trace_id="gen-fixed"),
        ),
        base_url_override="https://example.invalid/v1",
        api_key_override="sk-test",
    )

    assert result == [(InlineImageBytes(b"fake"), "ok")]
    headers = seen["headers"]
    expected_key = TEST_UPSTREAM_SERVICES.core.image_idempotency_key(
        trace_id="gen-fixed",
        endpoint="images/generations",
        body=seen["json_body"],
    )
    assert headers["x-trace-id"] == "gen-fixed"
    assert headers["Idempotency-Key"] == expected_key
    assert seen["timeout"].read == TEST_UPSTREAM_SERVICES.core.IMAGE_READ_TIMEOUT_MIN_S
    assert seen["retry_httpx_exceptions"] is False
    assert seen["retry_status_codes"] is True
    assert seen["max_attempts"] == 2
    assert "stream" not in seen["json_body"]
    assert "partial_images" not in seen["json_body"]


@pytest.mark.asyncio
async def test_direct_generate_stream_returns_on_final_image_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    progress_events: list[dict[str, Any]] = []

    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_iter_sse_curl(**kwargs: Any) -> Any:
        seen["json_body"] = dict(kwargs["json_body"])
        seen["headers"] = dict(kwargs["headers"])
        await kwargs["on_dispatch_ready"]()
        await kwargs["on_response_head"](
            200,
            {
                "content-type": "text/event-stream",
                "x-request-id": "req-stream-final",
            },
        )
        yield {
            "type": "image_generation.partial_image",
            "partial_image_index": 7,
            "b64_json": "cGFydGlhbA==",
        }
        yield {
            "type": "image_generation.completed",
            "b64_json": "ZmluYWw=",
            "revised_prompt": "final prompt",
        }
        raise AssertionError("stream must close after the final image event")

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle,
        "get_images_client",
        fake_get_images_client,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.transport,
        "iter_sse_curl",
        fake_iter_sse_curl,
    )

    result = await TEST_UPSTREAM_SERVICES.direct.direct_generate_image_once(
        _image_request(
            progress_callback=progress_events.append,
            request_context=ImageRequestContext.create(trace_id="gen-stream"),
        ),
        base_url_override="https://example.invalid/v1",
        api_key_override="sk-test",
        streaming_override=True,
    )

    assert result == [(InlineImageBytes(b"final"), "final prompt")]
    assert seen["json_body"]["stream"] is True
    assert seen["json_body"]["partial_images"] == 0
    assert seen["json_body"]["response_format"] == "b64_json"
    assert seen["headers"]["x-trace-id"] == "gen-stream"
    assert [event["type"] for event in progress_events] == [
        "dispatch_ready",
        "response_ready",
        "partial_image",
    ]
    assert progress_events[1][UPSTREAM_RESPONSE_REQUEST_ID] == "req-stream-final"
    assert progress_events[2]["index"] == 0
    assert progress_events[2]["count"] == 1


@pytest.mark.asyncio
async def test_direct_generate_stream_replays_503_once_with_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[dict[str, Any]] = []

    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_iter_sse_curl(**kwargs: Any) -> Any:
        attempts.append(
            {
                "body": dict(kwargs["json_body"]),
                "headers": dict(kwargs["headers"]),
            }
        )
        await kwargs["on_dispatch_ready"]()
        if len(attempts) == 1:
            await kwargs["on_response_head"](503, {"x-request-id": "req-first"})
            raise upstream.UpstreamError(
                "gateway failed",
                status_code=503,
                error_code="server_error",
                payload={"response_received": True},
            )
        await kwargs["on_response_head"](200, {"x-request-id": "req-final"})
        yield {
            "type": "image_generation.completed",
            "b64_json": "ZmluYWw=",
        }

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle,
        "get_images_client",
        fake_get_images_client,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.transport,
        "iter_sse_curl",
        fake_iter_sse_curl,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio,
        "sleep",
        lambda _delay: _done(),
    )

    result = await TEST_UPSTREAM_SERVICES.direct.direct_generate_image_once(
        _image_request(request_context=ImageRequestContext.create(trace_id="retry")),
        base_url_override="https://example.invalid/v1",
        api_key_override="sk-test",
        streaming_override=True,
    )

    assert result == [(InlineImageBytes(b"final"), None)]
    assert len(attempts) == 2
    assert attempts[0]["body"] == attempts[1]["body"]
    assert (
        attempts[0]["headers"]["Idempotency-Key"]
        == attempts[1]["headers"]["Idempotency-Key"]
    )


@pytest.mark.asyncio
async def test_direct_generate_stream_preserves_explicit_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_iter_sse_curl(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        await kwargs["on_dispatch_ready"]()
        await kwargs["on_response_head"](200, {})
        yield {
            "type": "error",
            "error": {
                "type": "server_error",
                "code": "server_error",
                "message": "image service failed",
            },
        }

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle,
        "get_images_client",
        fake_get_images_client,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.transport,
        "iter_sse_curl",
        fake_iter_sse_curl,
    )

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await TEST_UPSTREAM_SERVICES.direct.direct_generate_image_once(
            _image_request(),
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
            streaming_override=True,
        )

    assert calls == 1
    assert exc_info.value.error_code == "server_error"
    assert exc_info.value.payload["upstream_error"]["message"] == (
        "image service failed"
    )
    assert exc_info.value.payload.get("upstream_result_unknown") is not True


@pytest.mark.asyncio
async def test_direct_generate_stream_interruption_is_result_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_iter_sse_curl(**kwargs: Any) -> Any:
        await kwargs["on_dispatch_ready"]()
        await kwargs["on_response_head"](200, {"x-request-id": "req-cut"})
        if False:
            yield {}
        raise upstream.UpstreamError(
            "stream disconnected",
            status_code=200,
            error_code="sse_curl_failed",
            payload={"response_received": True},
        )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle,
        "get_images_client",
        fake_get_images_client,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.transport,
        "iter_sse_curl",
        fake_iter_sse_curl,
    )

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await TEST_UPSTREAM_SERVICES.direct.direct_generate_image_once(
            _image_request(),
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
            streaming_override=True,
        )

    error = exc_info.value
    assert error.error_code == "direct_image_result_unknown"
    assert error.payload["upstream_result_unknown"] is True
    assert error.payload[UPSTREAM_RESPONSE_REQUEST_ID] == "req-cut"
    assert error.payload[UPSTREAM_RESPONSE_HTTP_ATTEMPTS] == 1


@pytest.mark.asyncio
async def test_image_job_submit_uses_attempt_scoped_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_post_with_retry(**kwargs: Any) -> httpx.Response:
        seen["headers"] = dict(kwargs["headers"])
        seen["json_body"] = dict(kwargs["json_body"])
        return httpx.Response(
            409,
            json={"error": {"message": "conflict", "code": "conflict"}},
        )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle, "get_images_client", fake_get_images_client
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.core, "post_with_retry", fake_post_with_retry
    )
    with pytest.raises(upstream.UpstreamError):
        await TEST_UPSTREAM_SERVICES.image_jobs.submit_and_wait_image_job(
            payload={
                "endpoint": "/v1/images/generations",
                "request_type": "generations",
                "retention_days": 1,
                "idempotency_key": "generation:stable",
            },
            base_url="https://jobs.example",
            api_key="sk-test",
            proxy=None,
            progress_callback=None,
            request_context=ImageRequestContext.create(trace_id="trace-not-stable"),
        )

    expected = image_job_idempotency_key(
        context=ImageRequestContext.create(trace_id="trace-not-stable"),
        provider_id="unknown",
        endpoint="/v1/images/generations",
    )
    assert seen["headers"]["Idempotency-Key"] == expected
    assert seen["headers"]["x-trace-id"] == "trace-not-stable"
    assert (
        seen["headers"]["authorization"]
        == "Bearer test-image-job-sidecar-token-0123456789"
    )
    assert seen["headers"]["X-Lumen-Upstream-Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_image_job_submit_does_not_fall_back_to_upstream_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.settings, "image_job_sidecar_token", ""
    )

    with pytest.raises(upstream.UpstreamError) as exc:
        await TEST_UPSTREAM_SERVICES.image_jobs.submit_and_wait_image_job(
            payload={
                "endpoint": "/v1/images/generations",
                "request_type": "generations",
                "retention_days": 1,
            },
            base_url="https://jobs.example",
            api_key="sk-must-not-become-sidecar-token",
            proxy=None,
            progress_callback=None,
        )

    assert exc.value.status_code == 503
    assert "sk-must-not-become-sidecar-token" not in str(exc.value)
    assert exc.value.payload["configuration"] == "sidecar_auth"


@pytest.mark.asyncio
async def test_direct_generate_timeout_is_result_unknown_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_events: list[dict[str, Any]] = []

    class _Client:
        calls = 0

        async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            self.calls += 1
            raise httpx.ReadTimeout("client gave up")

    client = _Client()

    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> _Client:
        return client

    async def fake_timeout_config() -> TEST_UPSTREAM_SERVICES.lifecycle.TimeoutConfig:
        return TEST_UPSTREAM_SERVICES.lifecycle.TimeoutConfig(
            connect=10.0, read=20.0, write=30.0
        )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle, "get_images_client", fake_get_images_client
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle, "resolve_timeout_config", fake_timeout_config
    )

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await TEST_UPSTREAM_SERVICES.direct.direct_generate_image_once(
            _image_request(
                output_format="png",
                background="auto",
                moderation="auto",
                progress_callback=progress_events.append,
            ),
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
        )

    exc = exc_info.value
    assert (
        exc.error_code
        == TEST_UPSTREAM_SERVICES.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
    )
    assert (
        exc.payload["timeout_s"] == TEST_UPSTREAM_SERVICES.core.IMAGE_READ_TIMEOUT_MIN_S
    )
    assert exc.payload["upstream_result_unknown"] is True
    assert client.calls == 1
    assert [event["type"] for event in progress_events] == ["dispatch_ready"]
    from app.retry import is_retriable

    assert (
        is_retriable(
            exc.error_code,
            exc.status_code,
            error_message=str(exc),
        ).retriable
        is False
    )
    assert not TEST_UPSTREAM_SERVICES.retry.should_continue_image_provider_failover(
        exc,
        retriable=False,
    )


@pytest.mark.asyncio
async def test_direct_generate_has_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_post_with_retry(**_kwargs: Any) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def fake_image_request_timeout(
        _size: str,
        *,
        runtime: object | None = None,
    ) -> tuple[httpx.Timeout, float]:
        _ = runtime
        return httpx.Timeout(1.0), 0.01

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle, "get_images_client", fake_get_images_client
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.core, "post_with_retry", fake_post_with_retry
    )
    monkeypatch.setattr(
        direct_requests,
        "_image_request_timeout",
        fake_image_request_timeout,
    )

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await TEST_UPSTREAM_SERVICES.direct.direct_generate_image_once(
            _image_request(),
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
        )

    exc = exc_info.value
    assert (
        exc.error_code
        == TEST_UPSTREAM_SERVICES.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
    )
    assert exc.payload["timeout_s"] == 0.01
    assert exc.payload["upstream_result_unknown"] is True
    assert exc.payload["exception"] == "TimeoutError"


@pytest.mark.asyncio
async def test_direct_edit_timeout_is_result_unknown_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_curl_post_multipart(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        seen["timeout_s"] = kwargs["timeout_s"]
        raise httpx.TimeoutException("curl image edit timed out")

    async def fake_timeout_config() -> TEST_UPSTREAM_SERVICES.lifecycle.TimeoutConfig:
        return TEST_UPSTREAM_SERVICES.lifecycle.TimeoutConfig(
            connect=10.0, read=20.0, write=30.0
        )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.transport,
        "curl_post_multipart",
        fake_curl_post_multipart,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle, "resolve_timeout_config", fake_timeout_config
    )

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await TEST_UPSTREAM_SERVICES.direct.direct_edit_image_once(
            _image_request(
                action="edit",
                prompt="test edit",
                images=[b"\x89PNG\r\n\x1a\n" + b"\x00" * 32],
                output_format="png",
                background="auto",
                moderation="auto",
            ),
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
        )

    exc = exc_info.value
    assert seen["timeout_s"] == TEST_UPSTREAM_SERVICES.core.IMAGE_READ_TIMEOUT_MIN_S
    assert (
        exc.error_code
        == TEST_UPSTREAM_SERVICES.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
    )
    assert exc.payload["path"] == "images/edits"
    assert exc.payload["upstream_result_unknown"] is True
    from app.retry import is_retriable

    assert (
        is_retriable(
            exc.error_code,
            exc.status_code,
            error_message=str(exc),
        ).retriable
        is False
    )
    assert not TEST_UPSTREAM_SERVICES.retry.should_continue_image_provider_failover(
        exc,
        retriable=False,
    )


@pytest.mark.asyncio
async def test_direct_edit_2xx_invalid_json_is_result_unknown_after_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    progress_events: list[dict[str, Any]] = []

    async def fake_curl_post_multipart(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        nonlocal calls
        calls += 1
        await kwargs["on_dispatch_ready"]()
        await kwargs["on_response_ready"]()
        return 200, {"raw": "not-json"}

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.transport,
        "curl_post_multipart",
        fake_curl_post_multipart,
    )

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await TEST_UPSTREAM_SERVICES.direct.direct_edit_image_once(
            _image_request(
                action="edit",
                images=[b"\x89PNG\r\n\x1a\n" + b"\x00" * 32],
                progress_callback=progress_events.append,
            ),
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
        )

    assert exc_info.value.error_code == "direct_image_result_unknown"
    assert exc_info.value.payload["response_received"] is True
    assert calls == 1
    assert [event["type"] for event in progress_events] == [
        "dispatch_ready",
        "response_ready",
    ]


@pytest.mark.asyncio
async def test_responses_image_retry_honors_429_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def fake_stream(
        _request: ImageExecutionRequest,
        **_kwargs: Any,
    ) -> tuple[str, str | None]:
        nonlocal calls
        calls += 1
        raise upstream.UpstreamError(
            "rate limited",
            status_code=429,
            error_code="rate_limit_error",
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.responses, "responses_image_stream", fake_stream
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio, "sleep", fake_sleep
    )

    with pytest.raises(upstream.UpstreamError):
        await TEST_UPSTREAM_SERVICES.retry.responses_image_stream_with_retry(
            _image_request(),
            use_httpx=False,
        )

    assert calls == 5
    assert sleeps == [10.0, 10.0, 10.0, 10.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_kind",
    [
        "5xx",
        "5xx_with_forged_undelivered_receipt",
        "read_timeout",
        "unknown_transport",
    ],
)
async def test_responses_image_post_ambiguous_failure_is_not_replayed_or_failed_over(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    calls = 0
    claims: list[int] = []
    sleeps: list[float] = []

    async def fake_stream(
        _request: ImageExecutionRequest,
        **_kwargs: Any,
    ) -> tuple[str, str | None]:
        nonlocal calls
        calls += 1
        if failure_kind.startswith("5xx"):
            payload = {"path": "responses", "method": "POST"}
            if failure_kind.endswith("forged_undelivered_receipt"):
                payload["receipt_reason"] = UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
            raise upstream.UpstreamError(
                "gateway failed after accepting the request",
                status_code=503,
                error_code="server_error",
                payload=payload,
            )
        if failure_kind == "read_timeout":
            raise httpx.ReadTimeout("response body timed out")
        raise upstream.UpstreamError(
            "transport outcome is unknown",
            status_code=None,
            error_code="upstream_error",
            payload={"path": "responses", "method": "POST"},
        )

    async def before_attempt(attempt: int) -> None:
        claims.append(attempt)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.responses,
        "responses_image_stream",
        fake_stream,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio,
        "sleep",
        fake_sleep,
    )

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await TEST_UPSTREAM_SERVICES.retry.responses_image_stream_with_retry(
            _image_request(),
            use_httpx=False,
            before_attempt=before_attempt,
        )

    error = exc_info.value
    assert error.error_code == "direct_image_result_unknown"
    assert error.payload["upstream_result_unknown"] is True
    assert error.payload["path"] == "responses"
    assert error.payload["method"] == "POST"
    assert calls == 1
    assert claims == [1]
    assert sleeps == []

    from app.retry import is_retriable

    decision = is_retriable(
        error.error_code,
        error.status_code,
        error_message=str(error),
    )
    assert decision.retriable is False
    assert not TEST_UPSTREAM_SERVICES.retry.should_continue_image_provider_failover(
        error,
        retriable=decision.retriable,
    )


@pytest.mark.asyncio
async def test_responses_image_retry_claims_each_physical_stream_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims: list[int] = []
    calls = 0

    async def fake_stream(
        _request: ImageExecutionRequest,
        **_kwargs: Any,
    ) -> tuple[str, str | None]:
        nonlocal calls
        calls += 1
        if calls == 1:
            error = upstream.UpstreamError(
                "request was not delivered",
                status_code=0,
                error_code="direct_image_request_failed",
                payload={},
            )
            error.upstream_receipt_reason = UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
            raise error
        return "ZmFrZS1wbmc=", None

    async def before_attempt(attempt: int) -> None:
        claims.append(attempt)

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.responses, "responses_image_stream", fake_stream
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio, "sleep", lambda _delay: _done()
    )

    result = await TEST_UPSTREAM_SERVICES.retry.responses_image_stream_with_retry(
        _image_request(),
        use_httpx=False,
        before_attempt=before_attempt,
    )

    assert result == ("ZmFrZS1wbmc=", None)
    assert claims == [1, 2]


async def _done() -> None:
    return None
