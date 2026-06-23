from __future__ import annotations

from typing import Any

import httpx
import pytest

from app import upstream


@pytest.mark.asyncio
async def test_responses_image_retry_keeps_progress_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks_seen: list[bool] = []

    async def fake_stream(
        *,
        prompt: str,
        size: str,
        action: str,
        images: list[bytes] | None,
        quality: str,
        model: str | None = None,
        progress_callback: Any = None,
        use_httpx: bool = False,
        base_url_override: str | None = None,
        api_key_override: str | None = None,
    ) -> tuple[str, str | None]:
        _ = (
            prompt,
            size,
            action,
            images,
            quality,
            model,
            use_httpx,
            base_url_override,
            api_key_override,
        )
        callbacks_seen.append(progress_callback is not None)
        if len(callbacks_seen) == 1:
            raise upstream.UpstreamError(
                "temporary failure",
                status_code=503,
                error_code="server_error",
            )
        return "ZmFrZS1wbmc=", None

    monkeypatch.setattr(upstream, "_responses_image_stream", fake_stream)
    monkeypatch.setattr(upstream.asyncio, "sleep", lambda _delay: _done())

    async def progress(_event: dict[str, Any]) -> None:
        return None

    result = await upstream._responses_image_stream_with_retry(
        prompt="test",
        size="1024x1024",
        action="generate",
        images=None,
        quality="high",
        progress_callback=progress,
        use_httpx=False,
    )

    assert result == ("ZmFrZS1wbmc=", None)
    assert callbacks_seen == [True, True]


def test_bare_httpx_timeout_exception_is_retryable() -> None:
    assert upstream._is_retryable_fallback_exception(
        httpx.TimeoutException("curl guard timeout")
    )


def test_fallback_retry_backoff_clamps_at_four_seconds() -> None:
    assert upstream._fallback_retry_backoff_seconds(1) == 1.0
    assert upstream._fallback_retry_backoff_seconds(2) == 2.0
    assert upstream._fallback_retry_backoff_seconds(3) == 4.0
    assert upstream._fallback_retry_backoff_seconds(4) == 4.0
    assert upstream._fallback_retry_backoff_seconds(6) == 4.0


def test_max_attempts_for_5xx_is_three() -> None:
    exc = upstream.UpstreamError(
        "temporary upstream error",
        status_code=503,
        error_code="server_error",
    )
    assert upstream._max_attempts_for_exception(exc) == 3


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

    monkeypatch.setattr(upstream.asyncio, "sleep", fake_sleep)

    resp = await upstream._post_with_retry(
        client=_Client(),  # type: ignore[arg-type]
        url="https://example.invalid/v1/images/generations",
        headers={},
        json_body={"prompt": "test"},
    )

    assert resp.status_code == 200
    assert sleeps == [2.5]


@pytest.mark.asyncio
async def test_post_with_retry_can_disable_httpx_exception_retries() -> None:
    class _Client:
        calls = 0

        async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            self.calls += 1
            raise httpx.ReadTimeout("image still rendering")

    client = _Client()

    with pytest.raises(httpx.ReadTimeout):
        await upstream._post_with_retry(
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
        upstream.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(httpx.TimeoutException):
        await upstream._curl_post_multipart_using_paths(
            url="https://example.invalid/v1/images/edits",
            data={"prompt": "test"},
            staged_files=[],
            headers={},
            timeout_s=180,
        )


def test_image_idempotency_key_uses_stable_file_fingerprints() -> None:
    files = [
        ("image[]", ("ref.png", b"secret-image-bytes", "image/png")),
        ("mask", ("mask.png", b"mask-bytes", "image/png")),
    ]
    key_a = upstream._image_idempotency_key(
        trace_id="gen-fixed",
        endpoint="images/edits",
        body={"size": "1024x1024", "prompt": "edit"},
        files=files,
    )
    key_b = upstream._image_idempotency_key(
        trace_id="gen-fixed",
        endpoint="images/edits",
        body={"prompt": "edit", "size": "1024x1024"},
        files=files,
    )
    fingerprints = upstream._image_file_fingerprints(files)
    serialized = upstream._json_dumps_stable({"files": fingerprints})

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
        return httpx.Response(
            200,
            json={"data": [{"b64_json": "ZmFrZQ==", "revised_prompt": "ok"}]},
        )

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        return upstream._TimeoutConfig(connect=10.0, read=20.0, write=30.0)

    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    monkeypatch.setattr(upstream, "_post_with_retry", fake_post_with_retry)
    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)

    token = upstream.push_image_trace_id("gen-fixed")
    try:
        result = await upstream._direct_generate_image_once(
            prompt="test",
            size="1024x1024",
            n=1,
            quality="high",
            output_format="png",
            output_compression=None,
            background="auto",
            moderation="auto",
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
        )
    finally:
        upstream.pop_image_trace_id(token)

    assert result == [("ZmFrZQ==", "ok")]
    headers = seen["headers"]
    expected_key = upstream._image_idempotency_key(
        trace_id="gen-fixed",
        endpoint="images/generations",
        body=seen["json_body"],
    )
    assert headers["x-trace-id"] == "gen-fixed"
    assert headers["Idempotency-Key"] == expected_key
    assert seen["timeout"].read == upstream._IMAGE_READ_TIMEOUT_MIN_S
    assert seen["retry_httpx_exceptions"] is False


@pytest.mark.asyncio
async def test_direct_generate_timeout_is_result_unknown_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_images_client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_post_with_retry(**_kwargs: Any) -> httpx.Response:
        raise httpx.ReadTimeout("client gave up")

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        return upstream._TimeoutConfig(connect=10.0, read=20.0, write=30.0)

    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    monkeypatch.setattr(upstream, "_post_with_retry", fake_post_with_retry)
    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._direct_generate_image_once(
            prompt="test",
            size="1024x1024",
            n=1,
            quality="high",
            output_format="png",
            output_compression=None,
            background="auto",
            moderation="auto",
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
        )

    exc = exc_info.value
    assert exc.error_code == upstream.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
    assert exc.payload["timeout_s"] == upstream._IMAGE_READ_TIMEOUT_MIN_S
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
    assert not upstream._should_continue_image_provider_failover(
        exc,
        retriable=False,
    )


@pytest.mark.asyncio
async def test_direct_edit_timeout_is_result_unknown_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_curl_post_multipart(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        seen["timeout_s"] = kwargs["timeout_s"]
        raise httpx.TimeoutException("curl image edit timed out")

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        return upstream._TimeoutConfig(connect=10.0, read=20.0, write=30.0)

    monkeypatch.setattr(upstream, "_curl_post_multipart", fake_curl_post_multipart)
    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._direct_edit_image_once(
            prompt="test edit",
            size="1024x1024",
            images=[b"\x89PNG\r\n\x1a\n" + b"\x00" * 32],
            mask=None,
            n=1,
            quality="high",
            output_format="png",
            output_compression=None,
            background="auto",
            moderation="auto",
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
        )

    exc = exc_info.value
    assert seen["timeout_s"] == upstream._IMAGE_READ_TIMEOUT_MIN_S
    assert exc.error_code == upstream.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
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
    assert not upstream._should_continue_image_provider_failover(
        exc,
        retriable=False,
    )


@pytest.mark.asyncio
async def test_responses_image_retry_honors_429_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def fake_stream(**_kwargs: Any) -> tuple[str, str | None]:
        nonlocal calls
        calls += 1
        raise upstream.UpstreamError(
            "rate limited",
            status_code=429,
            error_code="rate_limit_error",
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(upstream, "_responses_image_stream", fake_stream)
    monkeypatch.setattr(upstream.asyncio, "sleep", fake_sleep)

    with pytest.raises(upstream.UpstreamError):
        await upstream._responses_image_stream_with_retry(
            prompt="test",
            size="1024x1024",
            action="generate",
            images=None,
            quality="high",
            progress_callback=None,
            use_httpx=False,
        )

    assert calls == 5
    assert sleeps == [10.0, 10.0, 10.0, 10.0]


async def _done() -> None:
    return None
