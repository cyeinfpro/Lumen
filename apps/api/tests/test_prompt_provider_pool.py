from __future__ import annotations

import json
from typing import Any

import pytest


class _StubStreamResponse:
    def __init__(
        self,
        status_code: int,
        chunks: list[str] | None = None,
        raw: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._chunks = chunks or []
        self._raw = raw

    async def __aenter__(self) -> "_StubStreamResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def aread(self) -> bytes:
        return self._raw

    async def aiter_text(self):
        for chunk in self._chunks:
            yield chunk


class _StubAsyncClient:
    def __init__(self, responses: list[_StubStreamResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_StubAsyncClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: Any) -> _StubStreamResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected extra stream call")
        return self.responses.pop(0)


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def test_prompt_enhance_normalizes_responses_url() -> None:
    from app.routes import prompts

    assert prompts._responses_url("https://upstream.example") == (
        "https://upstream.example/v1/responses"
    )
    assert prompts._responses_url("https://upstream.example/v1") == (
        "https://upstream.example/v1/responses"
    )


@pytest.mark.asyncio
async def test_prompt_enhance_resolves_provider_pool_without_legacy_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts

    values = {
        "providers": json.dumps(
            [
                {
                    "name": "primary",
                    "base_url": "https://primary.example",
                    "api_key": "sk-primary",
                    "priority": 10,
                }
            ]
        ),
    }

    async def fake_get_setting(_db: object, spec: object) -> str | None:
        return values.get(getattr(spec, "key", ""))

    monkeypatch.setattr(prompts, "get_setting", fake_get_setting)
    prompts._PROVIDER_RR_COUNTERS.clear()

    providers = await prompts._resolve_provider_order(object())  # type: ignore[arg-type]

    assert [(p.name, p.base_url, p.api_key, p.priority) for p in providers] == [
        ("primary", "https://primary.example", "sk-primary", 10),
    ]


@pytest.mark.asyncio
async def test_prompt_enhance_skips_providers_locked_to_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts

    values = {
        "providers": json.dumps(
            [
                {
                    "name": "image2-only",
                    "base_url": "https://image2.example",
                    "api_key": "sk-image2",
                    "priority": 10,
                    "image_jobs_endpoint": "generations",
                    "image_jobs_endpoint_lock": True,
                },
                {
                    "name": "responses",
                    "base_url": "https://responses.example",
                    "api_key": "sk-responses",
                    "priority": 5,
                },
            ]
        ),
    }

    async def fake_get_setting(_db: object, spec: object) -> str | None:
        return values.get(getattr(spec, "key", ""))

    monkeypatch.setattr(prompts, "get_setting", fake_get_setting)
    prompts._PROVIDER_RR_COUNTERS.clear()

    providers = await prompts._resolve_provider_order(object())  # type: ignore[arg-type]

    assert [p.name for p in providers] == ["responses"]


@pytest.mark.asyncio
async def test_prompt_enhance_checks_per_user_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: list[tuple[object, str]] = []

    async def fake_check(redis: object, key: str) -> None:
        calls.append((redis, key))

    async def fake_resolve_provider_order(_db: object) -> list[ProviderDefinition]:
        return [
            ProviderDefinition(
                name="primary",
                base_url="https://primary.example",
                api_key="sk-primary",
            )
        ]

    redis = object()
    monkeypatch.setattr(prompts.PROMPTS_ENHANCE_LIMITER, "check", fake_check)
    monkeypatch.setattr(prompts, "get_redis", lambda: redis)
    monkeypatch.setattr(prompts, "_resolve_provider_order", fake_resolve_provider_order)

    await prompts.enhance_prompt(
        prompts.EnhanceIn(text="cat"),
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert calls == [(redis, "rl:prompt_enhance:user-1")]


@pytest.mark.asyncio
async def test_prompt_enhance_uses_legacy_env_when_providers_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts

    async def fake_get_setting(_db: object, spec: object) -> str | None:
        assert getattr(spec, "key", "") == "providers"
        return None

    monkeypatch.setattr(prompts, "get_setting", fake_get_setting)
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://legacy.example")
    monkeypatch.setenv("UPSTREAM_API_KEY", "sk-legacy")
    prompts._PROVIDER_RR_COUNTERS.clear()

    providers = await prompts._resolve_provider_order(object())  # type: ignore[arg-type]

    assert [(p.name, p.base_url, p.api_key, p.priority) for p in providers] == [
        ("default", "https://legacy.example", "sk-legacy", 0),
    ]


@pytest.mark.asyncio
async def test_prompt_enhance_falls_back_to_gpt54_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(500, raw=b"server down"),
            _StubStreamResponse(
                200,
                [
                    _sse({"type": "response.output_text.delta", "delta": "better prompt"}),
                    _sse({"type": "response.completed"}),
                ],
            ),
        ]
    )
    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)

    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [chunk async for chunk in prompts._stream_enhance("cat", [provider])]

    assert chunks == [
        'data: {"text": "better prompt"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert client.calls[0]["json"]["model"] == "gpt-5.5"
    assert client.calls[1]["json"]["model"] == "gpt-5.4"
    assert client.calls[1]["json"]["reasoning"] == {"effort": "low"}
    assert client.calls[1]["json"]["service_tier"] == "priority"


@pytest.mark.asyncio
async def test_prompt_enhance_fallback_can_drop_priority_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(400, raw=b"model not found"),
            _StubStreamResponse(400, raw=b"unsupported service_tier"),
            _StubStreamResponse(
                200,
                [
                    _sse({"type": "response.output_text.done", "text": "clean prompt"}),
                ],
            ),
        ]
    )
    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)

    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [chunk async for chunk in prompts._stream_enhance("cat", [provider])]

    assert chunks == [
        'data: {"text": "clean prompt"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert client.calls[2]["json"]["model"] == "gpt-5.4"
    assert client.calls[2]["json"]["reasoning"] == {"effort": "low"}
    assert "service_tier" not in client.calls[2]["json"]


@pytest.mark.asyncio
async def test_prompt_enhance_retries_response_failed_before_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(
                200,
                [
                    _sse(
                        {
                            "type": "response.failed",
                            "error": {"message": "temporary upstream failure"},
                        }
                    ),
                ],
            ),
            _StubStreamResponse(
                200,
                [
                    _sse(
                        {
                            "type": "response.completed",
                            "response": {
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "fallback prompt",
                                            }
                                        ],
                                    }
                                ]
                            },
                        }
                    ),
                ],
            ),
        ]
    )
    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)

    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [chunk async for chunk in prompts._stream_enhance("cat", [provider])]

    assert chunks == [
        'data: {"text": "fallback prompt"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert client.calls[1]["json"]["model"] == "gpt-5.4"
