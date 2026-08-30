from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import Request

from lumen_core.schemas import ProviderItemIn, ProvidersUpdateIn
from app.routes.provider_parts.presentation import (
    provider_agent_update_fields,
    provider_out,
)
from app.services.admin_model_cache import AdminModelCache


class _StubResponse:
    def __init__(self, status_code: int, payload: object, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


def _responses_sse(
    text: str = "9801",
    *,
    terminal: bool = True,
    include_usage: bool = True,
) -> _StubResponse:
    raw = (
        "event: response.output_text.delta\n"
        f'data: {{"type":"response.output_text.delta","delta":"{text}"}}\n\n'
    )
    if terminal:
        response = {"status": "completed"}
        if include_usage:
            response["usage"] = {}
        raw += (
            "event: response.completed\n"
            f'data: {{"type":"response.completed","response":{json.dumps(response)}}}\n\n'
        )
    return _StubResponse(200, ValueError("streaming response"), raw)


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


def test_agent_thinking_level_map_survives_provider_round_trip() -> None:
    stored = {
        "name": "provider",
        "base_url": "https://provider.example/v1",
        "agent_thinking_level_map": {"xhigh": "high", "max": "high"},
    }
    output = provider_out(stored, 0)
    assert output.agent_thinking_level_map == {"xhigh": "high", "max": "high"}

    partial = ProviderItemIn(name="provider", base_url=stored["base_url"])
    updated = provider_agent_update_fields(partial, stored)
    assert updated["agent_thinking_level_map"] == {
        "xhigh": "high",
        "max": "high",
    }


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
        compile_statement = getattr(_stmt, "compile", None)
        params = compile_statement().params if callable(compile_statement) else {}
        if "upstream.default_model" in params.values():
            return _ScalarResult("gpt-5.4-mini")
        self.execute_count += 1
        if self.execute_count == 1:
            return _ScalarResult(self.setting.value)
        return _ScalarResult(self.setting)

    async def commit(self) -> None:
        self.committed = True


def _admin_request() -> Request:
    cache = AdminModelCache()
    runtime = SimpleNamespace(admin_models=lambda: cache)
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/admin/providers",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "app": SimpleNamespace(state=SimpleNamespace(runtime=runtime)),
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


def test_provider_admin_output_parses_string_booleans_without_truthy_coercion() -> None:
    from app.routes import providers

    item = providers._to_out(
        {
            "name": "manual",
            "base_url": "https://upstream.example",
            "api_key": "sk-test",
            "enabled": "false",
            "image_jobs_enabled": "0",
            "image_streaming_enabled": "true",
            "image_jobs_endpoint": "generations",
            "image_jobs_endpoint_lock": "false",
        },
        0,
    )
    proxy = providers._to_proxy_out(
        {
            "name": "egress",
            "type": "socks5",
            "host": "127.0.0.1",
            "enabled": "false",
        },
        0,
    )

    assert item.enabled is False
    assert item.image_jobs_enabled is False
    assert item.image_streaming_enabled is True
    assert item.image_jobs_endpoint_lock is False
    assert proxy.enabled is False


def test_provider_admin_output_does_not_mask_missing_key() -> None:
    from app.routes import providers

    item = providers._to_out(
        {
            "name": "missing-key",
            "base_url": "https://upstream.example",
            "api_key": "",
            "enabled": True,
        },
        0,
    )

    assert item.api_key_hint == ""


@pytest.mark.asyncio
async def test_manual_provider_probe_calls_responses_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    captured: dict[str, Any] = {}
    client = _StubAsyncClient(_responses_sse())

    def fake_client(**kwargs: Any) -> _StubAsyncClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(providers.httpx, "AsyncClient", fake_client)

    ok, _latency, err = await providers._probe_one(
        "https://upstream.example", "sk-test"
    )

    assert ok is True
    assert err is None
    assert client.posts[0]["url"] == "https://upstream.example/v1/responses"
    assert client.posts[0]["json"]["model"] == "gpt-5.4-mini"
    assert client.posts[0]["json"]["instructions"]
    assert "99 * 99" in client.posts[0]["json"]["input"]
    assert client.posts[0]["json"]["stream"] is True
    assert captured["follow_redirects"] is False
    assert captured["trust_env"] is False


@pytest.mark.asyncio
async def test_manual_provider_probe_rejects_terminal_without_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_responses_sse(include_usage=False))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    outcome = await providers._probe_one(
        "https://upstream.example",
        "sk-test",
        model="configured-model",
    )

    assert outcome.ok is False
    assert outcome.error == "usage_missing"


@pytest.mark.asyncio
async def test_manual_provider_probe_uses_configured_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers
    from lumen_core.providers import ProviderProxyDefinition

    captured: dict[str, Any] = {}
    client = _StubAsyncClient(_responses_sse())

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
async def test_manual_agent_probe_ignores_image_generation_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_responses_sse())
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
    assert out.items[0].ok is True
    assert out.items[0].status == "healthy"
    assert out.items[0].error is None
    assert client.posts[0]["url"] == "https://upstream.example/v1/responses"


@pytest.mark.asyncio
async def test_manual_provider_probe_treats_string_false_enabled_as_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_responses_sse())
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    out = await providers.probe_providers(
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        _FakeProvidersDb(
            json.dumps(
                [
                    {
                        "name": "disabled",
                        "base_url": "https://upstream.example",
                        "api_key": "sk-test",
                        "enabled": "false",
                    }
                ]
            )
        ),  # type: ignore[arg-type]
        None,
    )

    assert out.items[0].name == "disabled"
    assert out.items[0].ok is False
    assert out.items[0].status == "disabled"
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
                    "image_streaming_enabled": True,
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
    assert saved["providers"][0]["image_streaming_enabled"] is True
    assert out.items[0].image_jobs_enabled is True
    assert out.items[0].image_streaming_enabled is True
    assert out.proxies[0].password_hint == "****cret"


@pytest.mark.asyncio
async def test_legacy_admin_save_preserves_agent_and_vision_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    old_raw = json.dumps(
        {
            "providers": [
                {
                    "name": "agent-provider",
                    "base_url": "https://upstream.example",
                    "api_key": "sk-old",
                    "vision_supported": True,
                    "responses_supported": True,
                    "agent_api": "anthropic-messages",
                    "agent_models": ["claude-sonnet-4", "claude-haiku-4"],
                    "agent_context_window": 200000,
                    "agent_max_output_tokens": 8192,
                    "agent_reasoning_supported": False,
                }
            ],
            "proxies": [],
        }
    )
    db = _FakeProvidersDb(old_raw)

    async def fake_write_audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(providers, "write_audit", fake_write_audit)
    await providers.update_providers(
        ProvidersUpdateIn(
            items=[
                {
                    "name": "agent-provider",
                    "base_url": "https://upstream.example",
                    "api_key": "",
                    "enabled": True,
                }
            ]
        ),
        _admin_request(),
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        db,  # type: ignore[arg-type]
    )

    saved = json.loads(db.setting.value)["providers"][0]
    assert saved["vision_supported"] is True
    assert saved["responses_supported"] is True
    assert saved["agent_api"] == "anthropic-messages"
    assert saved["agent_models"] == ["claude-sonnet-4", "claude-haiku-4"]
    assert saved["agent_context_window"] == 200000
    assert saved["agent_max_output_tokens"] == 8192
    assert saved["agent_reasoning_supported"] is False


@pytest.mark.asyncio
async def test_update_providers_persists_default_model_with_provider_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    db = _FakeProvidersDb(json.dumps({"providers": [], "proxies": []}))
    settings: dict[str, str] = {}

    async def fake_write_audit(*_args: object, **_kwargs: object) -> None:
        return None

    async def fake_upsert(_db: object, key: str, value: str) -> None:
        settings[key] = value

    monkeypatch.setattr(providers, "write_audit", fake_write_audit)
    monkeypatch.setattr(providers, "_upsert_setting_value", fake_upsert)

    out = await providers.update_providers(
        ProvidersUpdateIn(
            default_model="gpt-5.6-sol",
            items=[
                {
                    "name": "agent-provider",
                    "base_url": "https://upstream.example/v1",
                    "api_key": "sk-test",
                    "purposes": ["chat"],
                    "agent_models": ["gpt-5.6-sol", "gpt-5.6-mini"],
                }
            ],
        ),
        _admin_request(),
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        db,  # type: ignore[arg-type]
    )

    saved = json.loads(db.setting.value)["providers"][0]
    assert saved["agent_models"] == ["gpt-5.6-sol", "gpt-5.6-mini"]
    assert out.items[0].agent_models == ["gpt-5.6-sol", "gpt-5.6-mini"]
    assert settings == {"upstream.default_model": "gpt-5.6-sol"}
    assert db.committed is True


@pytest.mark.asyncio
async def test_update_providers_allows_disabled_provider_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    db = _FakeProvidersDb(json.dumps({"providers": [], "proxies": []}))

    async def fake_write_audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(providers, "write_audit", fake_write_audit)

    out = await providers.update_providers(
        ProvidersUpdateIn(
            items=[
                {
                    "name": "disabled-placeholder",
                    "base_url": "https://upstream.example",
                    "api_key": "",
                    "priority": 0,
                    "weight": 1,
                    "enabled": False,
                }
            ],
        ),
        _admin_request(),
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        db,  # type: ignore[arg-type]
    )

    saved = json.loads(db.setting.value)
    assert db.committed is True
    assert saved["providers"][0]["api_key"] == ""
    assert saved["providers"][0]["enabled"] is False
    assert out.items[0].enabled is False


@pytest.mark.asyncio
async def test_update_providers_requires_new_key_when_base_url_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    db = _FakeProvidersDb(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "primary",
                        "base_url": "https://old.example/v1",
                        "api_key": "sk-old",
                    }
                ],
                "proxies": [],
            }
        )
    )

    async def fake_write_audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(providers, "write_audit", fake_write_audit)

    with pytest.raises(Exception) as excinfo:
        await providers.update_providers(
            ProvidersUpdateIn(
                items=[
                    {
                        "name": "primary",
                        "base_url": "https://new.example/v1",
                        "api_key": "",
                    }
                ]
            ),
            _admin_request(),
            SimpleNamespace(id="admin-1", email="admin@example.com"),
            db,  # type: ignore[arg-type]
        )

    assert "修改 base_url 后必须重新填写 api_key" in str(excinfo.value.detail)
    assert db.committed is False


@pytest.mark.asyncio
async def test_update_providers_rejects_enabled_provider_with_disabled_proxy() -> None:
    from app.routes import providers

    db = _FakeProvidersDb(json.dumps({"providers": [], "proxies": []}))

    with pytest.raises(Exception) as excinfo:
        await providers.update_providers(
            ProvidersUpdateIn(
                proxies=[
                    {
                        "name": "ssh-cn",
                        "type": "ssh",
                        "host": "203.0.113.10",
                        "port": 22,
                        "enabled": False,
                    }
                ],
                items=[
                    {
                        "name": "primary",
                        "base_url": "https://upstream.example",
                        "api_key": "sk-test",
                        "enabled": True,
                        "proxy": "ssh-cn",
                    }
                ],
            ),
            _admin_request(),
            SimpleNamespace(id="admin-1", email="admin@example.com"),
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert "disabled proxy" in excinfo.value.detail["error"]["message"]
    assert db.committed is False


def test_video_provider_proxy_validation_rejects_disabled_shared_proxy() -> None:
    from app.routes import providers

    shared_raw = json.dumps(
        {
            "proxies": [
                {
                    "name": "ssh-cn",
                    "type": "ssh",
                    "host": "203.0.113.10",
                    "port": 22,
                    "enabled": False,
                }
            ],
            "providers": [
                {
                    "name": "disabled-placeholder",
                    "base_url": "https://upstream.example",
                    "api_key": "",
                    "enabled": False,
                }
            ],
        }
    )
    video_raw = json.dumps(
        {
            "providers": [
                {
                    "name": "video",
                    "kind": "fake",
                    "base_url": "https://video.example",
                    "api_key": "",
                    "enabled": True,
                    "proxy": "ssh-cn",
                    "models": {"seedance": "fake-model"},
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="is disabled"):
        providers.ensure_enabled_video_provider_proxies(
            video_raw,
            shared_provider_raw=shared_raw,
        )


@pytest.mark.asyncio
async def test_manual_provider_probe_rejects_200_wrong_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_responses_sse("9802"))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    ok, _latency, err = await providers._probe_one(
        "https://upstream.example/v1", "sk-test"
    )

    assert ok is False
    assert err is not None and err.startswith("wrong_answer:")
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
    assert err == "HTTP 401: unauthorized"


@pytest.mark.asyncio
async def test_manual_provider_probe_reports_upstream_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(
        _StubResponse(400, {"error": {"message": "model gpt-x is unavailable"}})
    )
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    outcome = await providers._probe_one("https://upstream.example", "sk-test")

    assert outcome.ok is False
    assert outcome.error == "HTTP 400: model gpt-x is unavailable"


@pytest.mark.asyncio
async def test_manual_provider_probe_extracts_sse_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    raw = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"9801"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n\n'
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


# ---------------------------------------------------------------------------
# image-stability-hardening §P2: capability_signal 输出
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_outcome_404_signals_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_StubResponse(404, {"error": "not found"}))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    outcome = await providers._probe_one("https://upstream.example", "sk-test")

    assert outcome.ok is False
    assert outcome.http_status == 404
    assert outcome.capability_signal == "unsupported"


@pytest.mark.asyncio
async def test_probe_outcome_405_signals_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_StubResponse(405, {"error": "method not allowed"}))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    outcome = await providers._probe_one("https://upstream.example", "sk-test")

    assert outcome.capability_signal == "unsupported"


@pytest.mark.asyncio
async def test_probe_outcome_401_signals_auth_not_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401/403 不能据此判定 capability=False，仅是鉴权问题。"""
    from app.routes import providers

    client = _StubAsyncClient(_StubResponse(401, {"error": "unauthorized"}))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    outcome = await providers._probe_one("https://upstream.example", "sk-test")

    assert outcome.capability_signal == "auth"
    assert outcome.http_status == 401


@pytest.mark.asyncio
async def test_probe_outcome_500_signals_transient_not_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx 是临时不健康，capability_signal=transient，不会写死 unsupported。"""
    from app.routes import providers

    client = _StubAsyncClient(_StubResponse(503, {"error": "service unavailable"}))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    outcome = await providers._probe_one("https://upstream.example", "sk-test")

    assert outcome.capability_signal == "transient"


@pytest.mark.asyncio
async def test_probe_outcome_429_signals_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_StubResponse(429, {"error": "rate limited"}))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    outcome = await providers._probe_one("https://upstream.example", "sk-test")

    assert outcome.capability_signal == "transient"


@pytest.mark.asyncio
async def test_probe_outcome_200_correct_signals_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    client = _StubAsyncClient(_responses_sse())
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **_kw: client)

    outcome = await providers._probe_one("https://upstream.example", "sk-test")

    assert outcome.ok is True
    assert outcome.capability_signal == "supported"


@pytest.mark.asyncio
async def test_probe_outcome_timeout_signals_transient(
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

    outcome = await providers._probe_one("https://upstream.example", "sk-test")

    assert outcome.capability_signal == "transient"


# ---------------------------------------------------------------------------
# image-stability-hardening §P2: PUT /providers 持久化 capability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_providers_persists_capability_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """capability=False 通过 PUT /providers 写回 system_settings 并能从 GET 读出。"""
    from app.routes import providers

    written: dict[str, str] = {}

    class _Db:
        def __init__(self) -> None:
            self.execute_count = 0

        async def execute(self, _stmt: object) -> _ScalarResult:
            self.execute_count += 1
            if self.execute_count == 1:
                # _read_providers (老配置 None)
                return _ScalarResult(None)
            if self.execute_count == 2:
                # SELECT existing SystemSetting → 没有
                return _ScalarResult(None)
            return _ScalarResult(None)

        def add(self, obj: Any) -> None:
            written["raw"] = obj.value

        async def commit(self) -> None:
            return None

    async def fake_audit(*_args: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(providers, "write_audit", fake_audit)
    monkeypatch.setattr(providers, "validate_providers", lambda raw: None)

    body = ProvidersUpdateIn(
        items=[
            {
                "name": "p-with-cap",
                "base_url": "https://up.example",
                "api_key": "sk-cap",
                "responses_supported": True,
                "vision_supported": True,
                "agent_api": "anthropic-messages",
                "agent_context_window": 200000,
                "agent_max_output_tokens": 8192,
                "agent_reasoning_supported": False,
                "image_generations_supported": False,
                "image_responses_supported": True,
                "image_edit_input_transport": "file",
            },
            {
                "name": "p-without-cap",
                "base_url": "https://up2.example",
                "api_key": "sk-nocap",
            },
        ]
    )

    out = await providers.update_providers(
        body,
        _admin_request(),
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        _Db(),  # type: ignore[arg-type]
    )

    # 序列化里 capability=None 的 provider 不写字段（保持配置最小）
    persisted = json.loads(written["raw"])
    items = persisted["providers"]
    p_with = next(it for it in items if it["name"] == "p-with-cap")
    assert p_with["responses_supported"] is True
    assert p_with["vision_supported"] is True
    assert p_with["agent_api"] == "anthropic-messages"
    assert p_with["agent_context_window"] == 200000
    assert p_with["agent_max_output_tokens"] == 8192
    assert p_with["agent_reasoning_supported"] is False
    assert p_with["image_generations_supported"] is False
    assert p_with["image_responses_supported"] is True
    assert p_with["image_edit_input_transport"] == "file"
    p_without = next(it for it in items if it["name"] == "p-without-cap")
    assert "responses_supported" not in p_without
    assert "image_generations_supported" not in p_without

    # API 返回值里 capability 也透出
    out_with_cap = next(it for it in out.items if it.name == "p-with-cap")
    assert out_with_cap.responses_supported is True
    assert out_with_cap.vision_supported is True
    assert out_with_cap.agent_api == "anthropic-messages"
    assert out_with_cap.agent_context_window == 200000
    assert out_with_cap.agent_reasoning_supported is False
    assert out_with_cap.image_generations_supported is False
    assert out_with_cap.image_edit_input_transport == "file"
    out_without_cap = next(it for it in out.items if it.name == "p-without-cap")
    assert out_without_cap.responses_supported is None
