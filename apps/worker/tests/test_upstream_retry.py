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
