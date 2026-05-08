from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app import byok_service
from lumen_core.byok import ArithmeticChallenge


class _StubResponse:
    def __init__(self, status_code: int, payload: object, text: str | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if text is None else text

    def json(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _StubAsyncClient:
    def __init__(self, response: _StubResponse) -> None:
        self.response = response
        self.posts: list[dict[str, Any]] = []
        self.init_kwargs: dict[str, Any] = {}

    async def __aenter__(self) -> "_StubAsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _StubResponse:
        self.posts.append({"url": url, **kwargs})
        return self.response


def _supplier(**overrides: Any) -> SimpleNamespace:
    values = {
        "base_url": "https://upstream.example",
        "validation_model": "gpt-validate",
        "validation_timeout_ms": 15_000,
        "proxy_name": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _challenge() -> ArithmeticChallenge:
    return ArithmeticChallenge(
        expression="40 + 2",
        expected=42,
        operands=(40, 2),
        operator="+",
        created_at=datetime(2026, 5, 8, tzinfo=timezone.utc).isoformat(),
    )


def test_normalize_base_url_blocks_private_hosts_outside_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(byok_service.settings, "app_env", "production")

    with pytest.raises(ValueError, match="private supplier URLs"):
        byok_service.normalize_base_url("http://127.0.0.1:8000/")

    monkeypatch.setattr(byok_service.settings, "app_env", "local")
    assert byok_service.normalize_base_url("http://127.0.0.1:8000/") == (
        "http://127.0.0.1:8000"
    )


def test_normalize_base_url_rejects_credentials() -> None:
    with pytest.raises(ValueError, match="username or password"):
        byok_service.normalize_base_url("https://user:pass@upstream.example")


@pytest.mark.asyncio
async def test_validate_api_key_with_supplier_calls_responses_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubAsyncClient(_StubResponse(200, {"output_text": "42"}))

    def fake_client(**kwargs: Any) -> _StubAsyncClient:
        client.init_kwargs = kwargs
        return client

    challenge = _challenge()
    monkeypatch.setattr(byok_service, "generate_arithmetic_challenge", lambda: challenge)
    monkeypatch.setattr(byok_service.httpx, "AsyncClient", fake_client)

    outcome = await byok_service.validate_api_key_with_supplier(
        object(),  # type: ignore[arg-type]
        _supplier(base_url="https://upstream.example/v1"),
        "  sk-user-token  ",
        timeout_ms=20_000,
    )

    assert outcome.ok is True
    assert outcome.error_code is None
    assert outcome.key_hint == "sk-u...oken"
    assert client.init_kwargs["proxy"] is None
    assert client.posts[0]["url"] == "https://upstream.example/v1/responses"
    assert client.posts[0]["headers"]["authorization"] == "Bearer sk-user-token"
    assert client.posts[0]["json"]["model"] == "gpt-validate"
    # Why: pin to the monkeypatched challenge.expression instead of the literal
    # "40 + 2" so future ArithmeticChallenge factory changes (or a different
    # _challenge() return) don't break this test silently.
    posted_text = client.posts[0]["json"]["input"][0]["content"][0]["text"]
    assert challenge.expression in posted_text


@pytest.mark.parametrize(
    ("status_code", "payload", "expected_code"),
    [
        (401, {"error": {"code": "invalid_api_key"}}, "invalid_api_key"),
        (404, {"error": {"code": "not_found"}}, "supplier_unsupported"),
        (429, {"error": {"code": "rate_limit_exceeded"}}, "key_rate_limited"),
        (500, {"error": {"code": "server_error"}}, "supplier_transient_error"),
        (400, {"error": {"code": "model_not_found"}}, "model_not_available"),
    ],
)
@pytest.mark.asyncio
async def test_validate_api_key_with_supplier_classifies_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    payload: object,
    expected_code: str,
) -> None:
    monkeypatch.setattr(byok_service, "generate_arithmetic_challenge", _challenge)
    monkeypatch.setattr(
        byok_service.httpx,
        "AsyncClient",
        lambda **_kwargs: _StubAsyncClient(_StubResponse(status_code, payload)),
    )

    outcome = await byok_service.validate_api_key_with_supplier(
        object(),  # type: ignore[arg-type]
        _supplier(),
        "sk-user",
    )

    assert outcome.ok is False
    assert outcome.error_code == expected_code
    assert outcome.http_status == status_code


@pytest.mark.asyncio
async def test_validate_api_key_with_supplier_rejects_wrong_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(byok_service, "generate_arithmetic_challenge", _challenge)
    monkeypatch.setattr(
        byok_service.httpx,
        "AsyncClient",
        lambda **_kwargs: _StubAsyncClient(_StubResponse(200, {"output_text": "41"})),
    )

    outcome = await byok_service.validate_api_key_with_supplier(
        object(),  # type: ignore[arg-type]
        _supplier(),
        "sk-user",
    )

    assert outcome.ok is False
    assert outcome.error_code == "validation_wrong_answer"


@pytest.mark.asyncio
async def test_validate_api_key_with_supplier_rejects_blank_key_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_client(**_kwargs: Any) -> _StubAsyncClient:
        raise AssertionError("blank keys should not call upstream")

    monkeypatch.setattr(byok_service.httpx, "AsyncClient", fail_client)

    outcome = await byok_service.validate_api_key_with_supplier(
        object(),  # type: ignore[arg-type]
        _supplier(),
        "   ",
    )

    assert outcome.ok is False
    assert outcome.error_code == "invalid_api_key"
    assert outcome.http_status is None
    assert outcome.challenge_jsonb == {}
