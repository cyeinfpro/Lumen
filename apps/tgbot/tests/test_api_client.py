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
            await api.download_image_to_file(100, "image-1")
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
        free = free_values.pop(0) if free_values else api_client._MIN_FREE_DISK_BYTES + 5  # noqa: SLF001
        return DiskUsage(1024, 0, free)

    monkeypatch.setattr(api_client.settings, "download_tmp_dir", str(tmp_path))
    monkeypatch.setattr(api_client, "_DOWNLOAD_DISK_CHECK_INTERVAL_BYTES", 4)
    monkeypatch.setattr(api_client.shutil, "disk_usage", disk_usage)
    api = _api_with_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiError) as excinfo:
            await api.download_image_to_file(100, "image-1")
    finally:
        await api.aclose()

    assert excinfo.value.code == "disk_full"
    assert list(tmp_path.iterdir()) == []
