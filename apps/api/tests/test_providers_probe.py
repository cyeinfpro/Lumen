from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import Request

from lumen_core.schemas import ProvidersUpdateIn


class _StubResponse:
    def __init__(self, status_code: int, payload: object, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _StubAsyncClient:
    def __init__(self, response: _StubResponse) -> None:
        self.response = response
        self.posts: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_StubAsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _StubResponse:
        self.posts.append({"url": url, **kwargs})
        return self.response


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _FakeProvidersDb:
    def __init__(self, raw: str) -> None:
        self.setting = SimpleNamespace(value=raw)
        self.execute_count = 0
        self.committed = False

    async def execute(self, _stmt: object) -> _ScalarResult:
        self.execute_count += 1
        if self.execute_count == 1:
            return _ScalarResult(self.setting.value)
        return _ScalarResult(self.setting)

    async def commit(self) -> None:
        self.committed = True


def _admin_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/admin/providers",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_provider_probe_normalizes_responses_url() -> None:
    from app.routes import providers

    assert providers._responses_url("https://upstream.example") == (
        "https://upstream.example/v1/responses"
    )
    assert providers._responses_url("https://upstream.example/v1") == (
        "https://upstream.example/v1/responses"
    )


@pytest.mark.asyncio
async def test_manual_provider_probe_calls_responses_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_StubResponse(200, {"output_text": "9801"}))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    ok, _latency, err = await providers._probe_one(
        "https://upstream.example", "sk-test"
    )

    assert ok is True
    assert err is None
    assert client.posts[0]["url"] == "https://upstream.example/v1/responses"
    assert client.posts[0]["json"]["model"] == "gpt-5.4-mini"
    assert client.posts[0]["json"]["instructions"]
    assert "99 times 99" in client.posts[0]["json"]["input"][0]["content"][0]["text"]
    assert client.posts[0]["json"]["stream"] is False


@pytest.mark.asyncio
async def test_manual_provider_probe_uses_configured_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers
    from lumen_core.providers import ProviderProxyDefinition

    captured: dict[str, Any] = {}
    client = _StubAsyncClient(_StubResponse(200, {"output_text": "9801"}))

    def fake_client(**kwargs: Any) -> _StubAsyncClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(providers.httpx, "AsyncClient", fake_client)

    ok, _latency, err = await providers._probe_one(
        "https://upstream.example",
        "sk-test",
        proxy=ProviderProxyDefinition(
            name="egress",
            protocol="socks5",
            host="127.0.0.1",
            port=1080,
        ),
    )

    assert ok is True
    assert err is None
    assert captured["proxy"] == "socks5h://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_manual_provider_probe_skips_generation_locked_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_StubResponse(200, {"output_text": "9801"}))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    out = await providers.probe_providers(
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        _FakeProvidersDb(
            json.dumps(
                [
                    {
                        "name": "image2-only",
                        "base_url": "https://upstream.example",
                        "api_key": "sk-test",
                        "enabled": True,
                        "image_jobs_endpoint": "generations",
                        "image_jobs_endpoint_lock": True,
                    }
                ]
            )
        ),  # type: ignore[arg-type]
        None,
    )

    assert out.items[0].name == "image2-only"
    assert out.items[0].ok is False
    assert out.items[0].status == "skipped"
    assert out.items[0].error == "endpoint_locked_to_generations"
    assert client.posts == []


@pytest.mark.asyncio
async def test_update_providers_preserves_existing_ssh_proxy_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    old_raw = json.dumps(
        {
            "proxies": [
                {
                    "name": "ssh-cn",
                    "type": "ssh",
                    "host": "203.0.113.10",
                    "port": 22,
                    "username": "root",
                    "password": "old-secret",
                    "enabled": True,
                }
            ],
            "providers": [
                {
                    "name": "primary",
                    "base_url": "https://upstream.example",
                    "api_key": "sk-old",
                    "proxy": "ssh-cn",
                }
            ],
        }
    )
    db = _FakeProvidersDb(old_raw)

    async def fake_write_audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(providers, "write_audit", fake_write_audit)

    out = await providers.update_providers(
        ProvidersUpdateIn(
            proxies=[
                {
                    "name": "ssh-cn",
                    "type": "ssh",
                    "host": "203.0.113.10",
                    "port": 22,
                    "username": "root",
                    "password": "",
                    "enabled": True,
                }
            ],
            items=[
                {
                    "name": "primary",
                    "base_url": "https://upstream.example",
                    "api_key": "",
                    "priority": 0,
                    "weight": 1,
                    "enabled": True,
                    "proxy": "ssh-cn",
                    "image_jobs_enabled": True,
                }
            ],
        ),
        _admin_request(),
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        db,  # type: ignore[arg-type]
    )

    saved = json.loads(db.setting.value)
    assert db.committed is True
    assert saved["proxies"][0]["password"] == "old-secret"
    assert saved["providers"][0]["api_key"] == "sk-old"
    assert saved["providers"][0]["image_jobs_enabled"] is True
    assert out.items[0].image_jobs_enabled is True
    assert out.proxies[0].password_hint == "****cret"


@pytest.mark.asyncio
async def test_manual_provider_probe_rejects_200_wrong_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_StubResponse(200, {"output_text": "9802"}))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    ok, _latency, err = await providers._probe_one(
        "https://upstream.example/v1", "sk-test"
    )

    assert ok is False
    assert err == "wrong_answer"
    assert client.posts[0]["url"] == "https://upstream.example/v1/responses"


@pytest.mark.asyncio
async def test_manual_provider_probe_rejects_auth_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_StubResponse(401, {"error": "unauthorized"}))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    ok, _latency, err = await providers._probe_one(
        "https://upstream.example", "sk-test"
    )

    assert ok is False
    assert err == "HTTP 401"


@pytest.mark.asyncio
async def test_manual_provider_probe_extracts_sse_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    raw = (
        'event: response.output_text.delta\n'
        'data: {"type":"response.output_text.delta","delta":"9801"}\n\n'
        'event: response.completed\n'
        'data: {"type":"response.completed"}\n\n'
    )
    client = _StubAsyncClient(_StubResponse(200, ValueError("not json"), raw))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    ok, _latency, err = await providers._probe_one(
        "https://upstream.example", "sk-test"
    )

    assert ok is True
    assert err is None


@pytest.mark.asyncio
async def test_manual_provider_probe_reports_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    class _TimeoutClient:
        async def __aenter__(self) -> "_TimeoutClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> object:
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: _TimeoutClient())

    ok, _latency, err = await providers._probe_one(
        "https://upstream.example", "sk-test"
    )

    assert ok is False
    assert err == "timeout"
