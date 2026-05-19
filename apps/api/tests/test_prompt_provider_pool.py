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
        SimpleNamespace(id="user-1", account_mode="byok"),  # type: ignore[arg-type]
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
                    _sse(
                        {"type": "response.output_text.delta", "delta": "better prompt"}
                    ),
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


@pytest.mark.asyncio
async def test_prompt_enhance_charges_completed_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(
                200,
                [
                    _sse({"type": "response.output_text.delta", "delta": "better"}),
                    _sse(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-1",
                                "model": "gpt-5.5",
                                "usage": {
                                    "input_tokens": 12,
                                    "output_tokens": 5,
                                },
                            },
                        }
                    ),
                ],
            ),
        ]
    )
    charged: list[
        tuple[prompts._EnhanceBillingContext, prompts._EnhanceUsageCapture]
    ] = []

    async def fake_charge(
        billing: prompts._EnhanceBillingContext,
        capture: prompts._EnhanceUsageCapture,
    ) -> None:
        charged.append((billing, capture))

    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(prompts, "_charge_prompt_enhance", fake_charge)
    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == [
        'data: {"text": "better"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert len(charged) == 1
    _billing, capture = charged[0]
    assert capture.response_id == "resp-1"
    assert capture.model == "gpt-5.5"
    assert capture.service_tier == "priority"
    assert capture.usage == {"input_tokens": 12, "output_tokens": 5}


@pytest.mark.asyncio
async def test_prompt_enhance_charge_uses_completion_wallet_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts
    from lumen_core.pricing import CostBreakdown

    calls: dict[str, Any] = {}
    audits: list[dict[str, Any]] = []

    class Db:
        async def commit(self) -> None:
            calls["committed"] = True

    async def estimate_breakdown(*_args: Any, **kwargs: Any) -> CostBreakdown:
        calls["estimate"] = kwargs
        return CostBreakdown(
            input_cost_micro=12,
            output_cost_micro=10,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=True,
            rate_multiplier_x10000=kwargs["rate_multiplier_x10000"],
            total_cost_micro=22,
            actual_cost_micro=22,
            pricing_source="db",
        )

    async def charge(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["charge"] = kwargs
        return SimpleNamespace(amount_micro=-22, balance_after=978)

    async def write_audit(_db: object, **kwargs: Any) -> bool:
        audits.append(kwargs)
        return True

    monkeypatch.setattr(
        prompts.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(prompts.billing_core, "charge", charge)
    monkeypatch.setattr(prompts, "write_audit", write_audit)

    billing = prompts._EnhanceBillingContext(
        db=Db(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=15_000,
        cache_aware=True,
        allow_negative=False,
    )
    capture = prompts._EnhanceUsageCapture(
        provider_name="primary",
        model="gpt-5.5",
        service_tier="priority",
        response_id="resp-1",
        usage={"input_tokens": 12, "output_tokens": 5},
    )

    await prompts._charge_prompt_enhance(billing, capture)

    assert calls["estimate"]["model"] == "gpt-5.5"
    assert calls["estimate"]["tokens"].input_tokens == 12
    assert calls["estimate"]["tokens"].output_tokens == 5
    assert calls["estimate"]["service_tier"] == "priority"
    assert calls["estimate"]["rate_multiplier_x10000"] == 15_000
    assert calls["charge"]["kind"] == "charge_completion"
    assert calls["charge"]["ref_type"] == "prompt_enhance"
    assert calls["charge"]["ref_id"] == "resp-1"
    assert calls["charge"]["idempotency_key"] == "prompt_enhance:resp-1"
    assert calls["committed"] is True
    assert audits[-1]["event_type"] == "wallet.charge.completion"
    assert audits[-1]["details"]["route"] == "prompts.enhance"


@pytest.mark.asyncio
async def test_prompt_enhance_billing_preauthorizes_before_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts

    calls: dict[str, Any] = {}

    class Db:
        committed = False

        async def commit(self) -> None:
            self.committed = True

    async def true_setting(_db: Any) -> bool:
        return True

    async def false_setting(_db: Any) -> bool:
        return False

    async def estimate(*_args: Any, **kwargs: Any) -> int:
        calls["estimate"] = kwargs
        return 123

    async def hold(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["hold"] = kwargs
        return SimpleNamespace(id="tx-hold")

    monkeypatch.setattr(prompts, "_billing_enabled", true_setting)
    monkeypatch.setattr(prompts, "_billing_cache_aware", true_setting)
    monkeypatch.setattr(prompts, "_billing_allow_negative", false_setting)
    monkeypatch.setattr(prompts.billing_core, "estimate_completion_cost", estimate)
    monkeypatch.setattr(prompts.billing_core, "hold", hold)

    db = Db()
    out = await prompts._prepare_prompt_enhance_billing(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        SimpleNamespace(
            id="user-1",
            email="u@example.com",
            account_mode="wallet",
            billing_rate_multiplier=1,
        ),
    )

    assert out is not None
    assert db.committed is True
    assert out.hold_amount_micro == 10_000
    assert calls["estimate"]["rate_multiplier_x10000"] == 10_000
    assert calls["hold"]["ref_type"] == "prompt_enhance"
    assert calls["hold"]["ref_id"] == out.request_id
    assert calls["hold"]["idempotency_key"] == f"prompt_enhance:hold:{out.request_id}"


@pytest.mark.asyncio
async def test_prompt_enhance_charge_settles_existing_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts
    from lumen_core.pricing import CostBreakdown

    calls: dict[str, Any] = {}

    class Db:
        committed = False

        async def commit(self) -> None:
            self.committed = True

    async def estimate_breakdown(*_args: Any, **kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=12,
            output_cost_micro=10,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=kwargs["rate_multiplier_x10000"],
            total_cost_micro=22,
            actual_cost_micro=22,
            pricing_source="db",
        )

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["settle"] = kwargs
        return SimpleNamespace(amount_micro=-22, balance_after=978)

    async def charge(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("preauthorized enhance must settle, not charge")

    async def write_audit(_db: object, **kwargs: Any) -> bool:
        calls["audit"] = kwargs
        return True

    monkeypatch.setattr(
        prompts.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(prompts.billing_core, "settle", settle)
    monkeypatch.setattr(prompts.billing_core, "charge", charge)
    monkeypatch.setattr(prompts, "write_audit", write_audit)

    billing = prompts._EnhanceBillingContext(
        db=Db(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    capture = prompts._EnhanceUsageCapture(
        provider_name="primary",
        model="gpt-5.5",
        response_id="resp-1",
        usage={"input_tokens": 12, "output_tokens": 5},
    )

    await prompts._charge_prompt_enhance(billing, capture)  # noqa: SLF001

    assert calls["settle"]["ref_type"] == "prompt_enhance"
    assert calls["settle"]["ref_id"] == "enhance-1"
    assert calls["settle"]["actual_micro"] == 22
    assert calls["settle"]["idempotency_key"] == "prompt_enhance:settle:enhance-1"
    assert calls["audit"]["details"]["response_id"] == "resp-1"
