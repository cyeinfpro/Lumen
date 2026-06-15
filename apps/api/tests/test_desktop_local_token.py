from __future__ import annotations

import json

import pytest
from starlette.responses import PlainTextResponse

from app import main as api_main


async def _inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
    response = PlainTextResponse("ok")
    await response(scope, receive, send)


async def _collect_response(path: str = "/api/me", headers=None) -> tuple[int, dict]:
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    app = api_main._DesktopLocalTokenMiddleware(_inner_app)
    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": headers or [],
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body.decode() or "{}") if body != b"ok" else {"ok": True}


@pytest.mark.asyncio
async def test_desktop_local_token_missing_secret_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main.settings, "lumen_runtime", "desktop")
    monkeypatch.setattr(api_main.settings, "lumen_local_token", "")

    status, body = await _collect_response()

    assert status == 503
    assert body["error"]["code"] == "desktop_token_misconfigured"


@pytest.mark.asyncio
async def test_desktop_public_readiness_path_bypasses_local_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main.settings, "lumen_runtime", "desktop")
    monkeypatch.setattr(api_main.settings, "lumen_local_token", "")

    status, body = await _collect_response(path="/healthz")

    assert status == 200
    assert body == {"ok": True}
