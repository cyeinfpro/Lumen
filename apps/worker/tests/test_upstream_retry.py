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
        return httpx.Response(
            200,
            json={"data": [{"b64_json": "ZmFrZQ==", "revised_prompt": "ok"}]},
        )

    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    monkeypatch.setattr(upstream, "_post_with_retry", fake_post_with_retry)

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

    assert result == ("ZmFrZQ==", "ok")
    headers = seen["headers"]
    expected_key = upstream._image_idempotency_key(
        trace_id="gen-fixed",
        endpoint="images/generations",
        body=seen["json_body"],
    )
    assert headers["x-trace-id"] == "gen-fixed"
    assert headers["Idempotency-Key"] == expected_key


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
