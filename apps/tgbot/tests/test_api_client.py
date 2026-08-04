from __future__ import annotations

import json
import sys
from collections import namedtuple
from pathlib import Path
from typing import Any

import httpx
import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app import api_client  # noqa: E402
from app.api_client import ApiError, LumenApi  # noqa: E402


DiskUsage = namedtuple("DiskUsage", "total used free")


def _api_with_transport(transport: httpx.MockTransport) -> LumenApi:
    api = object.__new__(LumenApi)
    api._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://lumen.test",
        transport=transport,
        headers={"X-Bot-Token": "secret"},
    )
    return api


def test_headers_include_telegram_user_id() -> None:
    api = object.__new__(LumenApi)

    assert api._hdr(123) == {  # noqa: SLF001
        "X-Telegram-Chat-Id": "123",
        "X-Telegram-User-Id": "123",
    }
    assert api._hdr(123, tg_user_id=456)["X-Telegram-User-Id"] == "456"  # noqa: SLF001


@pytest.mark.asyncio
async def test_bind_sends_tg_user_id_in_header_and_body() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["json"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"user_id": "u1", "email": "u@example.com", "display_name": "User"},
        )

    api = _api_with_transport(httpx.MockTransport(handler))
    try:
        await api.bind(100, "code-1", "alice", tg_user_id=200)
    finally:
        await api.aclose()

    assert captured["headers"]["x-telegram-chat-id"] == "100"
    assert captured["headers"]["x-telegram-user-id"] == "200"
    assert captured["json"]["chat_id"] == "100"
    assert captured["json"]["tg_user_id"] == "200"


@pytest.mark.asyncio
async def test_generation_and_enhance_keep_group_chat_and_actor_ids_distinct() -> None:
    captured: list[tuple[str, str, str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.url.path,
                request.headers["x-telegram-chat-id"],
                request.headers["x-telegram-user-id"],
                request.headers.get("idempotency-key", ""),
            )
        )
        if request.url.path.endswith("/prompts/enhance"):
            return httpx.Response(200, json={"enhanced": "enhanced cat"})
        return httpx.Response(
            200,
            json={
                "user_id": "user-1",
                "conversation_id": "conversation-1",
                "message_id": "message-1",
                "generation_ids": ["generation-1"],
            },
        )

    api = _api_with_transport(httpx.MockTransport(handler))
    try:
        await api.create_generation(
            -100123,
            {
                "idempotency_key": "tg:group-key",
                "prompt": "cat",
            },
            tg_user_id=42,
        )
        await api.enhance_prompt(
            -100123,
            "cat",
            idempotency_key="tg:enhance-group-key",
            tg_user_id=42,
        )
    finally:
        await api.aclose()

    assert captured == [
        ("/telegram/generations", "-100123", "42", ""),
        (
            "/telegram/prompts/enhance",
            "-100123",
            "42",
            "tg:enhance-group-key",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connection", "5xx", "malformed_success"])
async def test_enhance_ambiguous_retry_reuses_caller_idempotency_key(
    failure: str,
) -> None:
    keys: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["idempotency-key"])
        if failure == "connection":
            raise httpx.ReadError("response lost", request=request)
        if failure == "5xx":
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": "temporary_failure",
                        "message": "temporary failure",
                    }
                },
            )
        return httpx.Response(200, text="<html>truncated</html>")

    api = _api_with_transport(httpx.MockTransport(handler))
    try:
        for _attempt in range(2):
            with pytest.raises(ApiError) as excinfo:
                await api.enhance_prompt(
                    100,
                    "cat",
                    idempotency_key="tg:stable-enhance-key",
                    tg_user_id=100,
                )
            assert excinfo.value.outcome_unknown is True
    finally:
        await api.aclose()

    assert keys == ["tg:stable-enhance-key", "tg:stable-enhance-key"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_key",
    ["", " tg:key", "tg:key ", "tg:bad key", "请求", "x" * 97],
)
async def test_enhance_rejects_invalid_stable_key_before_http(
    raw_key: str,
) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"enhanced": "enhanced cat"})

    api = _api_with_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError, match="stable printable ASCII"):
            await api.enhance_prompt(
                100,
                "cat",
                idempotency_key=raw_key,
                tg_user_id=100,
            )
    finally:
        await api.aclose()

    assert requests == 0


@pytest.mark.asyncio
async def test_generation_connection_loss_keeps_caller_idempotency_key() -> None:
    payloads: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        raise httpx.ConnectError("connection lost after write", request=request)

    api = _api_with_transport(httpx.MockTransport(handler))
    payload = {
        "idempotency_key": "tg:stable-generation-key",
        "prompt": "cat",
    }
    try:
        for _attempt in range(2):
            with pytest.raises(ApiError) as excinfo:
                await api.create_generation(100, payload, tg_user_id=100)
            assert excinfo.value.outcome_unknown is True
    finally:
        await api.aclose()

    assert [item["idempotency_key"] for item in payloads] == [
        "tg:stable-generation-key",
        "tg:stable-generation-key",
    ]


@pytest.mark.asyncio
async def test_generation_rejects_missing_stable_idempotency_key_before_http() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={})

    api = _api_with_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError, match="stable printable ASCII"):
            await api.create_generation(
                100,
                {"prompt": "cat"},
                tg_user_id=100,
            )
    finally:
        await api.aclose()

    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_key",
    [" tg:key", "tg:key ", "tg:bad key", "请求", "x" * 65],
)
async def test_generation_rejects_invalid_stable_key_before_http(
    raw_key: str,
) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={})

    api = _api_with_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError, match="stable printable ASCII"):
            await api.create_generation(
                100,
                {
                    "idempotency_key": raw_key,
                    "prompt": "cat",
                },
                tg_user_id=100,
            )
    finally:
        await api.aclose()

    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>ok</html>"),
        httpx.Response(200, json={"generation_ids": []}),
    ],
)
async def test_generation_malformed_success_is_ambiguous(
    response: httpx.Response,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    api = _api_with_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiError) as excinfo:
            await api.create_generation(
                100,
                {
                    "idempotency_key": "tg:stable-generation-key",
                    "prompt": "cat",
                },
                tg_user_id=100,
            )
    finally:
        await api.aclose()

    assert excinfo.value.code == "ambiguous_response"
    assert excinfo.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_download_rejects_content_length_that_would_exhaust_tmp_space(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": "20"},
            content=b"",
        )

    monkeypatch.setattr(api_client.settings, "download_tmp_dir", str(tmp_path))
    monkeypatch.setattr(
        api_client.shutil,
        "disk_usage",
        lambda _path: DiskUsage(
            1024,
            0,
            api_client._MIN_FREE_DISK_BYTES + 10,  # noqa: SLF001
        ),
    )
    api = _api_with_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiError) as excinfo:
            await api.download_image_to_file(100, "image-1", tg_user_id=100)
    finally:
        await api.aclose()

    assert excinfo.value.code == "disk_full"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_download_cleans_partial_file_when_stream_space_check_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": ""},
            content=b"abcdef",
        )

    free_values = [
        api_client._MIN_FREE_DISK_BYTES + 100,  # noqa: SLF001
        api_client._MIN_FREE_DISK_BYTES + 5,  # noqa: SLF001
    ]

    def disk_usage(_path: str) -> DiskUsage:
        free = (
            free_values.pop(0) if free_values else api_client._MIN_FREE_DISK_BYTES + 5
        )  # noqa: SLF001
        return DiskUsage(1024, 0, free)

    monkeypatch.setattr(api_client.settings, "download_tmp_dir", str(tmp_path))
    monkeypatch.setattr(api_client, "_DOWNLOAD_DISK_CHECK_INTERVAL_BYTES", 4)
    monkeypatch.setattr(api_client.shutil, "disk_usage", disk_usage)
    api = _api_with_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiError) as excinfo:
            await api.download_image_to_file(100, "image-1", tg_user_id=100)
    finally:
        await api.aclose()

    assert excinfo.value.code == "disk_full"
    assert list(tmp_path.iterdir()) == []
