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


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _ResponseContext:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def __aenter__(self) -> httpx.Response:
        return self.response

    async def __aexit__(self, *_args: Any) -> None:
        await self.response.aclose()


class _StreamingClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def stream(self, *_args: Any, **_kwargs: Any) -> _ResponseContext:
        return _ResponseContext(self.response)


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


@pytest.mark.asyncio
async def test_post_with_retry_limits_success_body_while_streaming() -> None:
    stream = _ChunkStream([b"1234", b"56", b"must-not-be-read"])
    response = httpx.Response(200, stream=stream)

    with pytest.raises(http_retry.ResponseBodyTooLarge):
        await http_retry.post_with_retry(
            client=cast(httpx.AsyncClient, _StreamingClient(response)),
            url="https://example.invalid/v1/generate",
            headers={},
            json_body={"prompt": "test"},
            max_attempts=1,
            max_response_bytes=5,
        )

    assert stream.yielded == 2
    assert stream.closed is True


@pytest.mark.asyncio
async def test_post_with_retry_truncates_error_body_while_streaming() -> None:
    stream = _ChunkStream([b"1234", b"5678", b"must-not-be-read"])
    response = httpx.Response(400, stream=stream)

    result = await http_retry.post_with_retry(
        client=cast(httpx.AsyncClient, _StreamingClient(response)),
        url="https://example.invalid/v1/generate",
        headers={},
        json_body={"prompt": "test"},
        max_attempts=1,
        max_error_response_bytes=5,
    )

    assert result.content == b"12345"
    assert result.extensions["lumen_body_truncated"] is True
    assert stream.yielded == 2
    assert stream.closed is True
