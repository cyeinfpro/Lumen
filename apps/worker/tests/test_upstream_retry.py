from __future__ import annotations

from typing import Any

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


async def _done() -> None:
    return None
