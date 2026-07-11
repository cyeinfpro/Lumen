from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from app import http_retry


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, _url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(kwargs)
        return httpx.Response(200)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_json", [True, False])
async def test_post_with_retry_omits_unset_timeout(use_json: bool) -> None:
    recorder = RecordingClient()

    await http_retry.post_with_retry(
        client=cast(httpx.AsyncClient, recorder),
        url="https://example.invalid/v1/generate",
        headers={"Authorization": "Bearer test"},
        json_body={"prompt": "test"} if use_json else None,
        data=None if use_json else {"prompt": "test"},
        files=None
        if use_json
        else [("image", ("input.png", b"png-bytes", "image/png"))],
        timeout=None,
    )

    assert "timeout" not in recorder.calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("use_json", [True, False])
async def test_post_with_retry_forwards_explicit_timeout(use_json: bool) -> None:
    recorder = RecordingClient()
    timeout = httpx.Timeout(12.0)

    await http_retry.post_with_retry(
        client=cast(httpx.AsyncClient, recorder),
        url="https://example.invalid/v1/generate",
        headers={"Authorization": "Bearer test"},
        json_body={"prompt": "test"} if use_json else None,
        data=None if use_json else {"prompt": "test"},
        files=None
        if use_json
        else [("image", ("input.png", b"png-bytes", "image/png"))],
        timeout=timeout,
    )

    assert recorder.calls[0]["timeout"] is timeout
