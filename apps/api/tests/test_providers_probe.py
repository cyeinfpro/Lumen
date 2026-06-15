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


@pytest.fixture(autouse=True)
def _isolate_desktop_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # These route tests call provider persistence helpers directly. Keep a
    # developer shell with LUMEN_RUNTIME=desktop from writing into the real
    # desktop app data directory.
    monkeypatch.delenv("LUMEN_RUNTIME", raising=False)
    monkeypatch.delenv("LUMEN_DATA_ROOT", raising=False)
    monkeypatch.delenv("LUMEN_DESKTOP_PROVIDER_FILE", raising=False)


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


def test_write_desktop_provider_config_strips_metadata_secrets(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    metadata_path = tmp_path / "providers.json"
    runtime_path = tmp_path / "providers.runtime.json"
    monkeypatch.setattr(
        providers, "desktop_provider_metadata_path", lambda: metadata_path
    )
    monkeypatch.setattr(
        providers, "desktop_provider_runtime_file", lambda: runtime_path
    )

    providers._write_desktop_provider_config(
        [
            {
                "name": "primary",
                "base_url": "https://upstream.example",
                "api_key": "sk-secret",
                "enabled": True,
            }
        ],
        [
            {
                "name": "egress",
                "type": "socks5",
                "host": "127.0.0.1",
                "port": 1080,
                "password": "proxy-secret",
                "enabled": True,
            }
        ],
    )

    metadata = json.loads(metadata_path.read_text())
    runtime = json.loads(runtime_path.read_text())
    assert "api_key" not in metadata["providers"][0]
    assert "password" not in metadata["proxies"][0]
    assert runtime["providers"][0]["api_key"] == "sk-secret"
    assert runtime["proxies"][0]["password"] == "proxy-secret"


@pytest.mark.asyncio
async def test_manual_provider_probe_calls_responses_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    captured: dict[str, Any] = {}
    client = _StubAsyncClient(_StubResponse(200, {"output_text": "9801"}))

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
    assert "99 times 99" in client.posts[0]["json"]["input"][0]["content"][0]["text"]
    assert client.posts[0]["json"]["stream"] is False
    assert captured["follow_redirects"] is False
    assert captured["trust_env"] is False


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
async def test_manual_provider_probe_treats_string_false_enabled_as_disabled(
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
async def test_update_providers_allows_disabled_provider_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import providers

    db = _FakeProvidersDb(json.dumps({"providers": [], "proxies": []}))

    async def fake_write_audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(providers, "write_audit", fake_write_audit)

    from app.routes import admin_models

    monkeypatch.setattr(admin_models, "invalidate_admin_models_cache", lambda: None)

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

    client = _StubAsyncClient(_StubResponse(200, {"output_text": "9801"}))
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

    # 引入 admin_models 避免 invalidate cache 报错
    from app.routes import admin_models

    monkeypatch.setattr(admin_models, "invalidate_admin_models_cache", lambda: None)

    body = ProvidersUpdateIn(
        items=[
            {
                "name": "p-with-cap",
                "base_url": "https://up.example",
                "api_key": "sk-cap",
                "responses_supported": True,
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
    assert p_with["image_generations_supported"] is False
    assert p_with["image_responses_supported"] is True
    assert p_with["image_edit_input_transport"] == "file"
    p_without = next(it for it in items if it["name"] == "p-without-cap")
    assert "responses_supported" not in p_without
    assert "image_generations_supported" not in p_without

    # API 返回值里 capability 也透出
    out_with_cap = next(it for it in out.items if it.name == "p-with-cap")
    assert out_with_cap.responses_supported is True
    assert out_with_cap.image_generations_supported is False
    assert out_with_cap.image_edit_input_transport == "file"
    out_without_cap = next(it for it in out.items if it.name == "p-without-cap")
    assert out_without_cap.responses_supported is None
