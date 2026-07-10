from __future__ import annotations

import json
import asyncio
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


def test_video_prompt_enhance_body_uses_media_content() -> None:
    from app.routes import prompts

    content = [
        {"type": "input_text", "text": "视频提示词"},
        {"type": "input_image", "image_url": "https://example.com/ref.png"},
    ]

    body = prompts._build_enhance_body(  # noqa: SLF001
        "",
        prompts._ENHANCE_ATTEMPTS[0],  # noqa: SLF001
        system_prompt=prompts.VIDEO_ENHANCE_SYSTEM_PROMPT,
        content=content,
        metadata={"purpose": "video_prompt_enhance"},
    )

    assert "AI video generation" in body["instructions"]
    assert body["input"][0]["content"] == content
    assert body["metadata"] == {"purpose": "video_prompt_enhance"}


def test_video_prompt_enhance_defaults_to_single_motion_first_prompt() -> None:
    from app.routes import prompts

    body = prompts.VideoEnhanceIn(text="一个女孩站在城市街头")
    system_prompt = prompts._video_enhance_system_prompt(  # noqa: SLF001
        body.variant_count
    )

    assert body.variant_count == 1
    assert system_prompt == prompts.VIDEO_ENHANCE_SYSTEM_PROMPT
    assert "<variant" not in system_prompt
    assert "motion/camera-first" in system_prompt
    assert "Volcano/Seedance-style video generation prompts" in system_prompt
    assert "Also apply Vibe Creating when appropriate" in system_prompt
    assert (
        "visual anchor, main action/state, local mood, or video theme/style"
        in system_prompt
    )
    assert "Preserve exact dialogue, voiceover, music, sound effects" in system_prompt
    assert "keep identity, outfit/product details" in system_prompt
    assert "Do NOT repeat or inventory existing subjects" in system_prompt
    assert "motion trajectory" in system_prompt
    assert "camera movement" in system_prompt
    assert "De-emphasize low-value technical camera controls" in system_prompt
    assert "[ref:image:1]" in system_prompt
    assert "ambiguous phrase without an anchor" in system_prompt
    assert "Do not invent subtitles" in system_prompt
    assert "seed values" in system_prompt
    assert "Output ONLY the enhanced video prompt text" in system_prompt


@pytest.mark.asyncio
async def test_video_prompt_enhance_variant_count_three_requires_parseable_variants() -> (
    None
):
    from app.routes import prompts

    body = prompts.VideoEnhanceIn(
        text="一个女孩站在城市街头",
        variant_count=3,
    )

    system_prompt = prompts._video_enhance_system_prompt(  # noqa: SLF001
        body.variant_count
    )
    content, token_changed = await prompts._build_video_enhance_content(  # noqa: SLF001
        body,
        request=object(),  # type: ignore[arg-type]
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
    )
    content_text = content[0]["text"]

    assert token_changed is False
    assert "Output exactly 3 variants" in system_prompt
    assert (
        '<variant action="direct_rewrite" title="short unique title">' in system_prompt
    )
    assert "ask_first" in system_prompt
    assert "keep_original" in system_prompt
    assert "optional_vc" in system_prompt
    assert "output only one ask_first variant" in system_prompt
    assert "</variant>" in system_prompt
    assert "The first <variant> must be the recommended best option" in system_prompt
    assert "distinct generation strategy" in system_prompt
    assert "候选方案数量：3" in content_text
    assert '<variant action="direct_rewrite" title="...">...</variant>' in content_text
    assert "第一项为推荐最佳" in content_text
    assert "火山/Seedance 视频提示词结构" in content_text
    assert "Vibe Creating 判断" in content_text
    assert "视觉锚点、行为/状态、局部调性或视频主题/风格" in content_text
    assert "参考素材锚点合同" in content_text
    assert 'action="ask_first"' in content_text
    assert "direct_pass、light_refine、direct_rewrite、ask_first" in content_text
    assert "动作轨迹" in content_text
    assert "运镜/镜头语言" in content_text
    assert "不要生成字幕、水印、UI 文案、seed 或命令参数" in content_text


@pytest.mark.asyncio
async def test_video_prompt_enhance_content_accepts_reference_only_input() -> None:
    from app.routes import prompts
    from lumen_core.schemas import VideoReferenceMediaIn

    body = prompts.VideoEnhanceIn(
        text="",
        action="reference",
        reference_media=[
            VideoReferenceMediaIn(
                kind="image",
                url="https://example.com/ref.png",
                label="产品图",
                ref_id="ref:image:1",
            ),
            VideoReferenceMediaIn(
                kind="video",
                url="https://example.com/motion.mp4",
                label="动作参考",
                ref_id="ref:video:1",
            ),
        ],
    )

    content, token_changed = await prompts._build_video_enhance_content(  # noqa: SLF001
        body,
        request=object(),  # type: ignore[arg-type]
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
    )

    assert token_changed is False
    assert {
        "type": "input_image",
        "image_url": "https://example.com/ref.png",
    } in content
    assert any(
        item.get("type") == "input_text" and "[ref:image:1]" in item.get("text", "")
        for item in content
    )
    assert any(
        item.get("type") == "input_text"
        and "https://example.com/motion.mp4" in item.get("text", "")
        for item in content
    )
    assert any(
        item.get("type") == "input_text" and "[ref:video:1]" in item.get("text", "")
        for item in content
    )


@pytest.mark.asyncio
async def test_video_prompt_enhance_content_accepts_asset_reference() -> None:
    from app.routes import prompts
    from lumen_core.schemas import VideoReferenceMediaIn

    body = prompts.VideoEnhanceIn(
        text="",
        action="reference",
        reference_media=[
            VideoReferenceMediaIn(
                kind="image",
                url="asset://asset-20260609161523-stlqd",
                label="真人素材",
                ref_id="ref:image:3",
            ),
        ],
    )

    content, token_changed = await prompts._build_video_enhance_content(  # noqa: SLF001
        body,
        request=object(),  # type: ignore[arg-type]
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
    )

    assert token_changed is False
    assert all(item.get("type") != "input_image" for item in content)
    assert any(
        item.get("type") == "input_text"
        and "asset://asset-20260609161523-stlqd" in item.get("text", "")
        and "[ref:image:3]" in item.get("text", "")
        for item in content
    )


def test_video_prompt_enhance_media_budget_downgrades_large_data_urls() -> None:
    from app.routes import prompts

    content: list[dict[str, Any]] = []
    small_url = "data:image/png;base64,abc"
    appended, used_bytes = prompts._append_input_image_with_budget(  # noqa: SLF001
        content,
        small_url,
        media_payload_bytes=0,
    )

    assert appended is True
    assert content == [{"type": "input_image", "image_url": small_url}]

    huge_url = (
        "data:image/png;base64,"
        + "a" * (prompts._PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES + 1)  # noqa: SLF001
    )
    appended, next_used_bytes = prompts._append_input_image_with_budget(  # noqa: SLF001
        content,
        huge_url,
        media_payload_bytes=used_bytes,
    )

    assert appended is False
    assert next_used_bytes == used_bytes
    assert all(item.get("image_url") != huge_url for item in content)


@pytest.mark.asyncio
async def test_video_prompt_enhance_content_does_not_echo_large_data_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.schemas import VideoReferenceMediaIn

    monkeypatch.setattr(prompts, "_PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES", 64)
    huge_url = "data:image/png;base64," + "a" * 128
    body = prompts.VideoEnhanceIn.model_construct(
        text="",
        action="reference",
        reference_media=[
            VideoReferenceMediaIn.model_construct(
                kind="image",
                url=huge_url,
                label="大图",
            ),
        ],
    )

    content, token_changed = await prompts._build_video_enhance_content(  # noqa: SLF001
        body,
        request=object(),  # type: ignore[arg-type]
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
    )

    assert token_changed is False
    assert all(item.get("image_url") != huge_url for item in content)
    assert all(huge_url not in item.get("text", "") for item in content)
    assert any(
        item.get("type") == "input_text"
        and "外部图片数据 URL 过大" in item.get("text", "")
        for item in content
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
async def test_prompt_enhance_skips_image_only_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts

    values = {
        "providers": json.dumps(
            [
                {
                    "name": "image-only",
                    "base_url": "https://image.example",
                    "api_key": "sk-image",
                    "priority": 10,
                    "purposes": ["image"],
                },
                {
                    "name": "chat",
                    "base_url": "https://chat.example",
                    "api_key": "sk-chat",
                    "priority": 5,
                    "purposes": ["chat", "image"],
                },
            ]
        ),
    }

    async def fake_get_setting(_db: object, spec: object) -> str | None:
        return values.get(getattr(spec, "key", ""))

    monkeypatch.setattr(prompts, "get_setting", fake_get_setting)
    prompts._PROVIDER_RR_COUNTERS.clear()

    providers = await prompts._resolve_provider_order(object())  # type: ignore[arg-type]

    assert [p.name for p in providers] == ["chat"]


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
async def test_video_prompt_enhance_does_not_forward_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    captured: dict[str, Any] = {}

    async def fake_check(_redis: object, _key: str) -> None:
        return None

    async def fake_resolve_provider_order(_db: object) -> list[ProviderDefinition]:
        return [
            ProviderDefinition(
                name="primary",
                base_url="https://primary.example",
                api_key="sk-primary",
            )
        ]

    async def fake_prepare_billing(_db: object, _user: object) -> None:
        return None

    async def fake_stream_enhance(*args: Any, **kwargs: Any):
        captured["args"] = args
        captured["kwargs"] = kwargs
        yield "data: [DONE]\n\n"

    def passthrough_stream(source, **_kwargs: Any):
        return source

    monkeypatch.setattr(prompts.PROMPTS_ENHANCE_LIMITER, "check", fake_check)
    monkeypatch.setattr(prompts, "get_redis", lambda: object())
    monkeypatch.setattr(prompts, "_resolve_provider_order", fake_resolve_provider_order)
    monkeypatch.setattr(
        prompts, "_prepare_prompt_enhance_billing", fake_prepare_billing
    )
    monkeypatch.setattr(prompts, "_stream_enhance", fake_stream_enhance)
    monkeypatch.setattr(prompts, "_stream_with_keepalive", passthrough_stream)

    response = await prompts.enhance_video_prompt(
        prompts.VideoEnhanceIn(text="一个女孩在城市街头奔跑"),
        object(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1", account_mode="byok"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == ["data: [DONE]\n\n"]
    assert "metadata" not in captured["kwargs"]
    assert captured["kwargs"]["system_prompt"] == prompts.VIDEO_ENHANCE_SYSTEM_PROMPT
    assert captured["kwargs"]["content"][0]["type"] == "input_text"


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
async def test_prompt_enhance_uses_bounded_upstream_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(
                200,
                [
                    _sse({"type": "response.output_text.delta", "delta": "ok"}),
                    _sse({"type": "response.completed"}),
                ],
            ),
        ]
    )
    captured: dict[str, Any] = {}

    def make_client(**kwargs: Any) -> _StubAsyncClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(prompts.httpx, "AsyncClient", make_client)

    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [chunk async for chunk in prompts._stream_enhance("cat", [provider])]

    assert chunks == ['data: {"text": "ok"}\n\n', "data: [DONE]\n\n"]
    timeout = captured["timeout"]
    assert timeout.connect == prompts._PROMPT_ENHANCE_CONNECT_TIMEOUT_SECONDS
    assert timeout.read == prompts._PROMPT_ENHANCE_READ_TIMEOUT_SECONDS
    assert timeout.write == prompts._PROMPT_ENHANCE_WRITE_TIMEOUT_SECONDS
    assert timeout.pool == prompts._PROMPT_ENHANCE_POOL_TIMEOUT_SECONDS


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
async def test_prompt_enhance_keepalive_wraps_slow_stream() -> None:
    from app.routes import prompts

    async def delayed_stream():
        await asyncio.sleep(0.02)
        yield 'data: {"text": "ready"}\n\n'

    stream = prompts._stream_with_keepalive(  # noqa: SLF001
        delayed_stream(),
        interval_seconds=0.001,
    )

    assert await anext(stream) == ": keep-alive\n\n"
    assert await anext(stream) == ": keep-alive\n\n"
    chunks: list[str] = []
    for _ in range(20):
        chunk = await anext(stream)
        if chunk.startswith("data:"):
            chunks.append(chunk)
            break
    assert chunks == ['data: {"text": "ready"}\n\n']


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
async def test_prompt_enhance_releases_hold_when_charge_fails(
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
    calls: dict[str, Any] = {}

    async def fail_charge(
        _billing: prompts._EnhanceBillingContext,
        _capture: prompts._EnhanceUsageCapture,
    ) -> None:
        raise RuntimeError("db commit failed")

    async def release_hold(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["release_reason"] = reason

    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(prompts, "_charge_prompt_enhance", fail_charge)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release_hold)

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
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
        'data: {"error": "billing_failed"}\n\n',
    ]
    assert calls["release_reason"] == "charge_failed"


@pytest.mark.asyncio
async def test_prompt_enhance_releases_hold_when_success_has_no_usage(
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
                            },
                        }
                    ),
                ],
            ),
        ]
    )
    calls: dict[str, Any] = {}

    async def release_hold(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["release_user"] = billing.user_id if billing is not None else None
        calls["release_reason"] = reason

    async def estimate_breakdown(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("missing usage must release the hold before pricing")

    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release_hold)
    monkeypatch.setattr(
        prompts.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == ['data: {"text": "better"}\n\n', "data: [DONE]\n\n"]
    assert calls == {"release_user": "user-1", "release_reason": "missing_usage"}


@pytest.mark.asyncio
async def test_prompt_enhance_releases_hold_when_stream_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    async def cancelled_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
    ):
        yield 'data: {"text": "partial"}\n\n'
        raise asyncio.CancelledError()

    async def release_after_cancel(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["release_user"] = billing.user_id if billing is not None else None
        calls["release_reason"] = reason

    monkeypatch.setattr(prompts, "_stream_enhance_one", cancelled_stream)
    monkeypatch.setattr(
        prompts,
        "_release_prompt_enhance_hold_after_cancel",
        release_after_cancel,
    )

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )
    stream = prompts._stream_enhance("cat", [provider], billing)

    assert await anext(stream) == 'data: {"text": "partial"}\n\n'
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)

    assert calls == {"release_user": "user-1", "release_reason": "stream_cancelled"}


@pytest.mark.asyncio
async def test_prompt_enhance_releases_hold_when_stream_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    async def slow_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
    ):
        yield 'data: {"text": "partial"}\n\n'
        await asyncio.sleep(60)

    async def release_hold(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["release_user"] = billing.user_id if billing is not None else None
        calls["release_reason"] = reason

    monkeypatch.setattr(prompts, "_stream_enhance_one", slow_stream)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release_hold)

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )
    stream = prompts._stream_enhance("cat", [provider], billing)

    assert await anext(stream) == 'data: {"text": "partial"}\n\n'
    await stream.aclose()

    assert calls == {"release_user": "user-1", "release_reason": "stream_cancelled"}


@pytest.mark.asyncio
async def test_prompt_enhance_billing_preauthorizes_before_stream(
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

    async def true_setting(_db: Any) -> bool:
        return True

    async def false_setting(_db: Any) -> bool:
        return False

    async def snapshot(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.setdefault("snapshots", []).append(kwargs)
        return {"model": kwargs["model"]}

    def breakdown(
        _snapshot: dict[str, Any],
        **kwargs: Any,
    ) -> CostBreakdown:
        calls.setdefault("breakdowns", []).append(kwargs)
        return CostBreakdown(
            input_cost_micro=100,
            output_cost_micro=23,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=kwargs["rate_multiplier_x10000"],
            total_cost_micro=123,
            actual_cost_micro=123,
            pricing_source="snapshot",
        )

    async def hold(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["hold"] = kwargs
        return SimpleNamespace(id="tx-hold")

    async def invalidate(user_id: str) -> None:
        calls["invalidated"] = user_id

    monkeypatch.setattr(prompts, "_billing_enabled", true_setting)
    monkeypatch.setattr(prompts, "_billing_cache_aware", true_setting)
    monkeypatch.setattr(prompts, "_billing_allow_negative", false_setting)
    monkeypatch.setattr(prompts.billing_core, "completion_pricing_snapshot", snapshot)
    monkeypatch.setattr(
        prompts.billing_core,
        "completion_breakdown_from_snapshot",
        breakdown,
    )
    monkeypatch.setattr(prompts.billing_core, "hold", hold)
    monkeypatch.setattr(prompts, "invalidate_balance_cache", invalidate)

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
    assert {item["model"] for item in calls["snapshots"]} == {"gpt-5.4", "gpt-5.5"}
    assert all(
        item["rate_multiplier_x10000"] == 10_000
        for item in calls["breakdowns"]
    )
    assert calls["hold"]["ref_type"] == "prompt_enhance"
    assert calls["hold"]["ref_id"] == out.request_id
    assert calls["hold"]["idempotency_key"] == f"prompt_enhance:hold:{out.request_id}"
    assert len(calls["hold"]["meta"]["pricing_snapshots"]) == 3
    assert calls["invalidated"] == "user-1"


@pytest.mark.asyncio
async def test_prompt_enhance_zero_rate_skips_preauthorization(
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

    async def true_setting(_db: Any) -> bool:
        return True

    async def false_setting(_db: Any) -> bool:
        return False

    async def snapshot(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"model": kwargs["model"]}

    def breakdown(_snapshot: dict[str, Any], **kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=1,
            output_cost_micro=1,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=kwargs["rate_multiplier_x10000"],
            total_cost_micro=2,
            actual_cost_micro=0,
            pricing_source="snapshot",
        )

    async def fail_hold(*_args: Any, **_kwargs: Any) -> None:
        calls["hold"] = True
        raise AssertionError("zero-rate enhance must not reserve wallet balance")

    monkeypatch.setattr(prompts, "_billing_enabled", true_setting)
    monkeypatch.setattr(prompts, "_billing_cache_aware", true_setting)
    monkeypatch.setattr(prompts, "_billing_allow_negative", false_setting)
    monkeypatch.setattr(prompts.billing_core, "completion_pricing_snapshot", snapshot)
    monkeypatch.setattr(
        prompts.billing_core,
        "completion_breakdown_from_snapshot",
        breakdown,
    )
    monkeypatch.setattr(prompts.billing_core, "hold", fail_hold)

    db = Db()
    out = await prompts._prepare_prompt_enhance_billing(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        SimpleNamespace(
            id="user-1",
            email="u@example.com",
            account_mode="wallet",
            billing_rate_multiplier=0,
        ),
    )

    assert out is not None
    assert out.rate_multiplier_x10000 == 0
    assert out.hold_amount_micro == 0
    assert db.committed is False
    assert "hold" not in calls


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

    async def invalidate(user_id: str) -> None:
        calls["invalidated"] = user_id

    monkeypatch.setattr(
        prompts.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(prompts.billing_core, "settle", settle)
    monkeypatch.setattr(prompts.billing_core, "charge", charge)
    monkeypatch.setattr(prompts, "write_audit", write_audit)
    monkeypatch.setattr(prompts, "invalidate_balance_cache", invalidate)

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
    assert calls["invalidated"] == "user-1"
