"""ProviderPool 探活测试（文本算术 probe + image probe）。

文本探活：让 gpt-5.4-mini 算 99*99，必须答 9801 才算 healthy（不再是 HTTP <500）
Image probe：1024x1024 低质量生图，必须真返回 base64 (>= 1KB) 才算 healthy
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import httpx
import pytest

from app.provider_pool import ProviderConfig, ProviderHealth, ProviderPool
from app.provider_runtime.contracts import ImageProbeRequest
from app.provider_runtime.probe_runtime import (
    ProviderProbeRuntime,
    build_provider_probe_runtime,
)


@contextmanager
def _probe_runtime(
    pool: ProviderPool,
    **changes: Any,
) -> Iterator[ProviderProbeRuntime]:
    original = pool._probe_runtime
    runtime = replace(original, **changes)
    pool._probe_runtime = runtime
    try:
        yield runtime
    finally:
        pool._probe_runtime = original


def _cfg(name: str = "acc1") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        base_url=f"https://{name}.example",
        api_key=f"sk-{name}",
        priority=0,
        weight=1,
        enabled=True,
    )


def _locked_cfg(
    name: str = "image2-only",
    *,
    endpoint: str = "generations",
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        base_url=f"https://{name}.example",
        api_key=f"sk-{name}",
        priority=0,
        weight=1,
        enabled=True,
        image_jobs_endpoint=endpoint,
        image_jobs_endpoint_lock=True,
    )


def _make_pool(
    *configs: ProviderConfig,
    probe_runtime: ProviderProbeRuntime | None = None,
) -> ProviderPool:
    pool = ProviderPool(
        probe_runtime=probe_runtime or build_provider_probe_runtime(),
    )
    pool._providers = list(configs)
    pool._health = {p.name: ProviderHealth() for p in configs}
    pool._config_loaded_at = time.monotonic() + 60.0
    return pool


# --- _extract_response_output_text ------------------------------------------


@pytest.mark.parametrize(
    "payload,expected_contains",
    [
        # 顶层 output_text
        ({"output_text": "9801"}, "9801"),
        # output[].content[].text 标准结构
        (
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "9801"}],
                    }
                ]
            },
            "9801",
        ),
        # 多 chunk 拼接
        (
            {
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": "the answer is ",
                            },
                            {"type": "output_text", "text": "9801"},
                        ]
                    }
                ]
            },
            "9801",
        ),
        # 兼容网关：output_text part 使用 output_text 字段承载文本
        (
            {
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "output_text": "9801",
                            }
                        ]
                    }
                ]
            },
            "9801",
        ),
    ],
)
def test_extract_response_output_text_variants(
    payload: dict[str, Any], expected_contains: str
) -> None:
    text = ProviderPool()._extract_response_output_text(payload)
    assert expected_contains in text


def test_extract_response_output_text_returns_empty_for_non_dict() -> None:
    pool = ProviderPool()

    assert pool._extract_response_output_text("not a dict") == ""
    assert pool._extract_response_output_text(None) == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"metadata": {"probe_answer": "9801"}},
        {"error": {"message": "expected answer: 9801"}},
        {"usage": {"debug": "9801"}},
        {
            "output": [
                {
                    "content": [
                        {"type": "input_text", "text": "9801"},
                    ]
                }
            ]
        },
        {"output": [{"content": [{"text": "9801"}]}]},
        {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "metadata": {"debug": "9801"},
                        }
                    ]
                }
            ]
        },
    ],
)
def test_extract_response_output_text_ignores_non_output_text(
    payload: dict[str, Any],
) -> None:
    assert ProviderPool()._extract_response_output_text(payload) == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":"9801"}\n\n',
            "9801",
        ),
        (
            "event: response.output_text.done\n"
            'data: {"type":"response.output_text.done","text":"9801"}\n\n',
            "9801",
        ),
        (
            "event: response.content_part.done\n"
            'data: {"type":"response.content_part.done",'
            '"part":{"type":"output_text","text":"9801"}}\n\n',
            "9801",
        ),
        (
            "event: response.completed\n"
            'data: {"type":"response.completed","response":{"output":['
            '{"content":[{"type":"output_text","text":"9801"}]}]}}\n\n',
            "9801",
        ),
        # 兼容仅发送 data JSON 的网关，但仍只接受明确 output_text 路径。
        ('data: {"output_text":"9801"}\n\n', "9801"),
    ],
)
def test_extract_sse_output_text_accepts_whitelisted_output(
    raw: str,
    expected: str,
) -> None:
    assert ProviderPool()._extract_sse_output_text(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "event: response.failed\n"
        'data: {"type":"response.failed","error":{"message":"9801"}}\n\n',
        "event: response.completed\n"
        'data: {"type":"response.completed","response":'
        '{"usage":{"debug":"9801"}}}\n\n',
        "event: response.completed\n"
        'data: {"type":"response.completed","response":'
        '{"metadata":{"probe_answer":"9801"}}}\n\n',
        "event: response.content_part.done\n"
        'data: {"type":"response.content_part.done",'
        '"part":{"type":"input_text","text":"9801"}}\n\n',
        "event: response.output_text.delta\n"
        'data: {"type":"response.failed","delta":"9801"}\n\n',
        'data: {"message":"9801"}\n\n',
    ],
)
def test_extract_sse_output_text_ignores_non_output_events(raw: str) -> None:
    assert ProviderPool()._extract_sse_output_text(raw) == ""


# --- _probe_one：算术验证 ---------------------------------------------------


class _StubResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | BaseException,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict[str, Any]:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _StubAsyncClient:
    """httpx.AsyncClient 替身：按调用 ID 返回不同响应。"""

    def __init__(self, response: _StubResponse) -> None:
        self.response = response
        self.posts: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_StubAsyncClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _StubResponse:
        self.posts.append({"url": url, **kwargs})
        return self.response


@pytest.mark.asyncio
async def test_probe_one_returns_true_when_answer_is_9801(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _make_pool(_cfg("acc1"))
    # 模拟 gpt-5.4-mini 答出 9801
    stub = _StubAsyncClient(_StubResponse(200, {"output_text": "9801"}))

    def fake_async_client(*_a: Any, **_kw: Any) -> _StubAsyncClient:
        return stub

    with _probe_runtime(pool, async_client_factory=fake_async_client):
        ok = await pool._probe_one(pool._providers[0])
    assert ok is True
    # 探活请求一定带正确 prompt + gpt-5.4-mini
    assert stub.posts[0]["json"]["model"] == "gpt-5.4-mini"
    assert stub.posts[0]["json"]["instructions"]
    assert "99 times 99" in stub.posts[0]["json"]["input"][0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_probe_one_returns_false_when_answer_is_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _make_pool(_cfg("acc1"))
    stub = _StubAsyncClient(_StubResponse(200, {"output_text": "9802"}))
    with _probe_runtime(pool, async_client_factory=lambda *a, **kw: stub):
        ok = await pool._probe_one(pool._providers[0])
    assert ok is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"metadata": {"probe_answer": "9801"}},
        {"error": {"message": "expected answer: 9801"}},
        {"usage": {"output_tokens": 9801}},
        {"output": [{"content": [{"type": "input_text", "text": "9801"}]}]},
    ],
)
async def test_probe_one_rejects_9801_outside_output_text(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    pool = _make_pool(_cfg("acc1"))
    stub = _StubAsyncClient(_StubResponse(200, payload))
    with _probe_runtime(pool, async_client_factory=lambda *a, **kw: stub):
        assert await pool._probe_one(pool._providers[0]) is False


@pytest.mark.asyncio
async def test_probe_one_returns_false_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _make_pool(_cfg("acc1"))
    stub = _StubAsyncClient(_StubResponse(503, {"error": "down"}))
    with _probe_runtime(pool, async_client_factory=lambda *a, **kw: stub):
        ok = await pool._probe_one(pool._providers[0])
    assert ok is False


@pytest.mark.asyncio
async def test_probe_one_returns_false_on_4xx_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧实现 4xx 也算 healthy；新算术 probe 4xx 算 fail（auth/quota 都不是真活）。"""
    pool = _make_pool(_cfg("acc1"))
    stub = _StubAsyncClient(_StubResponse(401, {"error": "unauthorized"}))
    with _probe_runtime(pool, async_client_factory=lambda *a, **kw: stub):
        ok = await pool._probe_one(pool._providers[0])
    assert ok is False


@pytest.mark.asyncio
async def test_probe_one_returns_false_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _make_pool(_cfg("acc1"))

    class _BoomClient:
        async def __aenter__(self) -> "_BoomClient":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def post(self, *_a: Any, **_kw: Any) -> Any:
            raise httpx.ConnectError("DNS failed")

    with _probe_runtime(pool, async_client_factory=lambda *a, **kw: _BoomClient()):
        ok = await pool._probe_one(pool._providers[0])
    assert ok is False


@pytest.mark.asyncio
async def test_probe_one_extracts_from_output_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """output_text 缺失但 output[].content[].text 含 9801 时仍算 healthy。"""
    pool = _make_pool(_cfg("acc1"))
    payload = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "9801"}],
            }
        ]
    }
    stub = _StubAsyncClient(_StubResponse(200, payload))
    with _probe_runtime(pool, async_client_factory=lambda *a, **kw: stub):
        assert await pool._probe_one(pool._providers[0]) is True


@pytest.mark.asyncio
async def test_probe_one_extracts_from_sse_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _make_pool(_cfg("acc1"))
    raw = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"9801"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed"}\n\n'
    )
    stub = _StubAsyncClient(_StubResponse(200, ValueError("not json"), raw))
    with _probe_runtime(pool, async_client_factory=lambda *a, **kw: stub):
        assert await pool._probe_one(pool._providers[0]) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "event: response.failed\n"
        'data: {"type":"response.failed","error":{"message":"9801"}}\n\n',
        "event: response.completed\n"
        'data: {"type":"response.completed","response":'
        '{"usage":{"debug":"9801"}}}\n\n',
    ],
)
async def test_probe_one_rejects_9801_in_non_output_sse(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    pool = _make_pool(_cfg("acc1"))
    stub = _StubAsyncClient(_StubResponse(200, ValueError("not json"), raw))
    with _probe_runtime(pool, async_client_factory=lambda *a, **kw: stub):
        assert await pool._probe_one(pool._providers[0]) is False


@pytest.mark.asyncio
async def test_probe_one_skips_generation_locked_provider_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _make_pool(_locked_cfg())

    def fail_async_client(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("locked provider should not call /v1/responses")

    with _probe_runtime(pool, async_client_factory=fail_async_client):
        assert await pool._probe_one(pool._providers[0]) is True


# --- image probe ------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_image_one_succeeds_with_real_b64(
) -> None:
    pool = _make_pool(_cfg("acc1"))
    # 假装上游返回 ~5KB base64
    fake_b64 = "A" * 5000

    async def fake_probe(request: ImageProbeRequest) -> tuple[str, str | None]:
        assert request.size == "1024x1024"
        assert request.quality == "low"
        assert request.provider is pool._providers[0]
        return fake_b64, None

    ok = await pool._probe_image_one(pool._providers[0], fake_probe)
    assert ok is True


@pytest.mark.asyncio
async def test_probe_image_one_fails_when_b64_too_short(
) -> None:
    pool = _make_pool(_cfg("acc1"))

    async def fake_probe(_request: ImageProbeRequest) -> tuple[str, str | None]:
        return "tiny", None

    ok = await pool._probe_image_one(pool._providers[0], fake_probe)
    assert ok is False


@pytest.mark.asyncio
async def test_probe_image_one_fails_on_upstream_error(
) -> None:
    pool = _make_pool(_cfg("acc1"))
    from app import upstream

    async def fake_probe(_request: ImageProbeRequest) -> tuple[str, str | None]:
        raise upstream.UpstreamError(
            "moderation_blocked", error_code="moderation_blocked", status_code=200
        )

    ok = await pool._probe_image_one(pool._providers[0], fake_probe)
    assert ok is False


@pytest.mark.asyncio
async def test_probe_image_one_skips_generation_locked_provider_without_responses_call(
) -> None:
    pool = _make_pool(_locked_cfg())

    async def fail_probe(_request: ImageProbeRequest) -> tuple[str, str | None]:
        raise AssertionError("locked provider should not call /v1/responses")

    assert await pool._probe_image_one(pool._providers[0], fail_probe) is True


# --- image probe 上报：不污染 image_last_used_at / total_requests -----------


@pytest.mark.asyncio
async def test_image_probe_success_does_not_touch_last_used_or_total() -> None:
    pool = _make_pool(_cfg("acc1"))
    h = pool._health["acc1"]
    h.image_consecutive_failures = 2
    h.image_cooldown_until = time.monotonic() + 60.0
    pool.report_image_probe_success("acc1")
    # 关键：image_last_used_at 必须保持 None（probe 不算用户请求）
    assert h.image_last_used_at is None
    # 不动 total_requests
    assert h.total_requests == 0
    assert h.successful_requests == 0
    # 但 image health 复位
    assert h.image_consecutive_failures == 0
    assert h.image_cooldown_until is None


@pytest.mark.asyncio
async def test_image_probe_failure_accumulates_and_triggers_cooldown() -> None:
    pool = _make_pool(_cfg("acc1"))
    h = pool._health["acc1"]
    pool.report_image_probe_failure("acc1")
    pool.report_image_probe_failure("acc1")
    assert h.image_cooldown_until is None
    assert h.image_consecutive_failures == 2
    pool.report_image_probe_failure("acc1")
    # 第 3 次熔断
    assert h.image_cooldown_until is not None
    assert h.image_cooldown_until > time.monotonic()
    # 但不计入 total/failed_requests（probe 不污染真实统计）
    assert h.total_requests == 0
    assert h.failed_requests == 0


@pytest.mark.asyncio
async def test_probe_image_all_calls_per_provider_and_reports(
) -> None:
    """probe_image_all 对每个 enabled provider 跑一次，结果上报到对应 report_*。"""
    pool = _make_pool(_cfg("good"), _cfg("bad"))
    from app import upstream

    async def fake_probe(request: ImageProbeRequest) -> tuple[str, str | None]:
        if request.provider.api_key == "sk-good":
            return "A" * 5000, None
        raise upstream.UpstreamError("server_error", error_code="server_error")

    results = await pool.probe_image_all(fake_probe)
    assert results == {"good": True, "bad": False}
    # bad 失败累加到 image_consecutive_failures
    assert pool._health["bad"].image_consecutive_failures == 1
    # good 健康度被清空（虽然本来就 0）
    assert pool._health["good"].image_consecutive_failures == 0


@pytest.mark.asyncio
async def test_probe_all_skips_generation_locked_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _make_pool(_locked_cfg(), _cfg("responses"))
    stub = _StubAsyncClient(_StubResponse(200, {"output_text": "9801"}))
    with _probe_runtime(pool, async_client_factory=lambda *a, **kw: stub):
        results = await pool.probe_all()

    assert results == {"responses": True}
    assert len(stub.posts) == 1


@pytest.mark.asyncio
async def test_probe_image_all_skips_generation_locked_provider(
) -> None:
    pool = _make_pool(_locked_cfg(), _cfg("responses"))

    calls: list[str] = []

    async def fake_probe(request: ImageProbeRequest) -> tuple[str, str | None]:
        calls.append(request.provider.api_key)
        return "A" * 5000, None

    results = await pool.probe_image_all(fake_probe)

    assert results == {"responses": True}
    assert calls == ["sk-responses"]


def test_text_probe_failure_does_not_open_text_circuit() -> None:
    pool = _make_pool(_cfg("acc1"))
    h = pool._health["acc1"]

    pool.report_failure("acc1", is_probe=True)
    pool.report_failure("acc1", is_probe=True)
    pool.report_failure("acc1", is_probe=True)

    # probe 不毒化熔断（consecutive_failures / cooldown_until 不动），但仍计入
    # total/failed 让 monitoring 看到——否则 probe 阶段一直挂掉运维感知不到。
    assert h.consecutive_failures == 0
    assert h.cooldown_until is None
    assert h.total_requests == 3
    assert h.failed_requests == 3
    assert h.last_failure_at is not None


def test_open_circuit_fallback_failure_keeps_cooldown_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool as provider_pool_module

    pool = _make_pool(_cfg("acc1"))
    h = pool._health["acc1"]
    now = 1000.0
    h.consecutive_failures = 3
    h.cooldown_until = now + 30.0
    monkeypatch.setattr(provider_pool_module.time, "monotonic", lambda: now)

    assert [p.name for p in pool._select_ordered()] == ["acc1"]
    with _probe_runtime(pool, monotonic=lambda: now):
        pool.report_failure("acc1")

    assert h.consecutive_failures == 3
    assert h.cooldown_until == now + 30.0
    assert h.total_requests == 1
    assert h.failed_requests == 1
    assert h.last_failure_at == now


def test_open_fallback_finishing_after_deadline_does_not_reopen_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool as provider_pool_module

    pool = _make_pool(_cfg("acc1"))
    h = pool._health["acc1"]
    clock = [1000.0]
    original_deadline = clock[0] + 1.0
    h.consecutive_failures = 3
    h.cooldown_until = original_deadline
    monkeypatch.setattr(
        provider_pool_module.time,
        "monotonic",
        lambda: clock[0],
    )

    assert [p.name for p in pool._select_ordered()] == ["acc1"]
    assert h.half_open_probe_inflight is False
    clock[0] = original_deadline + 1.0
    with _probe_runtime(pool, monotonic=lambda: clock[0]):
        pool.report_failure("acc1")

    assert h.consecutive_failures == 3
    assert h.cooldown_until == original_deadline
    assert h.half_open_probe_inflight is False


def test_half_open_allows_only_one_probe_until_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool as provider_pool_module

    pool = _make_pool(_cfg("acc1"))
    h = pool._health["acc1"]
    now = 1000.0
    h.consecutive_failures = 3
    h.cooldown_until = now - 1.0
    monkeypatch.setattr(provider_pool_module.time, "monotonic", lambda: now)

    assert [p.name for p in pool._select_ordered()] == ["acc1"]
    assert h.half_open_probe_inflight is True
    with pytest.raises(Exception, match="no upstream providers available"):
        pool._select_ordered()

    pool.report_success("acc1")

    assert h.half_open_probe_inflight is False
    assert h.consecutive_failures == 0
    assert h.cooldown_until is None
    assert [p.name for p in pool._select_ordered()] == ["acc1"]


def test_single_provider_half_open_continuous_request_releases_unreported_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool as provider_pool_module

    pool = _make_pool(_cfg("acc1"))
    h = pool._health["acc1"]
    now = 1000.0
    h.consecutive_failures = 3
    h.cooldown_until = now - 1.0
    monkeypatch.setattr(provider_pool_module.time, "monotonic", lambda: now)

    provider = pool._select_ordered()[0]
    assert provider.text_circuit_state == "half_open"
    assert provider.half_open_probe_token == h.half_open_probe_token
    with pool.text_attempt(provider):
        with pytest.raises(Exception, match="no upstream providers available"):
            pool._select_ordered()

    assert h.half_open_probe_inflight is False
    assert h.half_open_probe_token is None
    next_provider = pool._select_ordered()[0]
    with pool.text_attempt(next_provider) as attempt:
        attempt.report_success()

    assert h.consecutive_failures == 0
    assert h.cooldown_until is None
    assert h.half_open_probe_inflight is False


def test_half_open_probe_failure_reopens_circuit_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool as provider_pool_module

    pool = _make_pool(_cfg("acc1"))
    h = pool._health["acc1"]
    now = 1000.0
    h.consecutive_failures = 3
    h.cooldown_until = now - 1.0
    monkeypatch.setattr(provider_pool_module.time, "monotonic", lambda: now)

    provider = pool._select_ordered()[0]
    assert provider.name == "acc1"
    assert h.half_open_probe_inflight is True

    with _probe_runtime(pool, monotonic=lambda: now):
        with pool.text_attempt(provider) as attempt:
            attempt.report_failure()

    assert h.half_open_probe_inflight is False
    assert h.half_open_probe_token is None
    assert h.consecutive_failures == 4
    assert h.cooldown_until == now + provider_pool_module._CB_COOLDOWN_BASE_S * 2


def test_open_fallback_failure_does_not_release_concurrent_half_open_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool as provider_pool_module

    pool = _make_pool(_cfg("acc1"))
    h = pool._health["acc1"]
    clock = [1000.0]
    h.consecutive_failures = 3
    h.cooldown_until = clock[0] + 1.0
    monkeypatch.setattr(
        provider_pool_module.time,
        "monotonic",
        lambda: clock[0],
    )

    open_fallback = pool._select_ordered()[0]
    assert open_fallback.text_circuit_state == "open"
    clock[0] += 2.0
    half_open_probe = pool._select_ordered()[0]
    half_open_token = half_open_probe.half_open_probe_token
    assert half_open_token is not None

    with pool.text_attempt(open_fallback) as attempt:
        attempt.report_failure()

    assert h.consecutive_failures == 3
    assert h.half_open_probe_inflight is True
    assert h.half_open_probe_token == half_open_token

    with pool.text_attempt(half_open_probe) as attempt:
        attempt.report_failure()

    assert h.consecutive_failures == 4
    assert h.half_open_probe_inflight is False
    assert h.half_open_probe_token is None


@pytest.mark.asyncio
async def test_probe_all_limits_parallelism(monkeypatch: pytest.MonkeyPatch) -> None:

    providers = [_cfg(f"acc{i}") for i in range(5)]
    pool = _make_pool(*providers)
    active = 0
    max_active = 0

    async def fake_probe(_provider: ProviderConfig) -> bool:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            return True
        finally:
            active -= 1

    monkeypatch.setattr(pool, "_probe_one", fake_probe)

    with _probe_runtime(pool, probe_max_concurrency=2):
        results = await pool.probe_all()

    assert results == {provider.name: True for provider in providers}
    assert max_active <= 2


@pytest.mark.asyncio
async def test_probe_image_all_limits_parallelism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    providers = [_cfg(f"acc{i}") for i in range(5)]
    pool = _make_pool(*providers)
    active = 0
    max_active = 0

    async def fake_probe(
        _provider: ProviderConfig,
        _image_probe: object,
    ) -> bool:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            return True
        finally:
            active -= 1

    monkeypatch.setattr(pool, "_probe_image_one", fake_probe)

    async def unused_probe(_request: ImageProbeRequest) -> tuple[str, str | None]:
        return "unused", None

    with _probe_runtime(pool, probe_max_concurrency=2):
        results = await pool.probe_image_all(unused_probe)

    assert results == {provider.name: True for provider in providers}
    assert max_active <= 2


# --- 默认配置：image probe interval=0 关闭 ---------------------------------


def test_image_probe_interval_default_is_zero() -> None:
    """生产默认 image probe 关闭——避免一上线就烧 N 个号的配额。"""
    from app.provider_pool import _DEFAULT_IMAGE_PROBE_INTERVAL_S

    assert _DEFAULT_IMAGE_PROBE_INTERVAL_S == 0


def test_image_probe_spec_registered_in_supported_settings() -> None:
    """admin /admin/settings 能看到这个 key（让运维通过 UI 改）。"""
    from lumen_core.runtime_settings import SUPPORTED_SETTINGS, get_spec

    keys = {s.key for s in SUPPORTED_SETTINGS}
    assert "providers.auto_image_probe_interval" in keys
    spec = get_spec("providers.auto_image_probe_interval")
    assert spec is not None
    assert spec.parser is int
    assert spec.min_value == 0  # 0 = 关闭
    assert spec.env_fallback == "PROVIDERS_AUTO_IMAGE_PROBE_INTERVAL"


@pytest.mark.asyncio
async def test_probe_providers_skips_image_probe_when_interval_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """providers.auto_image_probe_interval=0 时 cron 不应触发 probe_image_all。"""
    from app import provider_pool, runtime_settings

    # 让 resolve_int 返回固定值（text=0 文本也跳，image=0 image 也跳）
    async def fake_resolve_int(key: str, default: int) -> int:
        if key == "providers.auto_probe_interval":
            return 0
        if key == "providers.auto_image_probe_interval":
            return 0
        return default

    monkeypatch.setattr(runtime_settings, "resolve_int", fake_resolve_int)

    pool = _make_pool(_cfg("acc1"))
    pool._config_loaded_at = time.monotonic() + 60.0

    async def fake_get_pool() -> ProviderPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)

    image_probe_called = False

    async def boom_probe_image_all(_image_probe: object) -> dict[str, bool]:
        nonlocal image_probe_called
        image_probe_called = True
        return {}

    monkeypatch.setattr(pool, "probe_image_all", boom_probe_image_all)

    # ctx 没 redis 就跳过统计，flush_image_metrics 在 redis=None 时也只刷 state
    await provider_pool.probe_providers({})
    assert image_probe_called is False


@pytest.mark.asyncio
async def test_probe_providers_runs_image_probe_when_interval_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """interval > 0 时 cron 应触发 probe_image_all 并更新时间戳。"""
    from app import provider_pool, runtime_settings
    from app.upstream_parts.upstream_impl import build_image_upstream_runtime

    async def fake_resolve_int(key: str, default: int) -> int:
        if key == "providers.auto_probe_interval":
            return 0  # 文本 probe 跳过，免得真去发 HTTP
        if key == "providers.auto_image_probe_interval":
            return 1800  # 30 分钟
        return default

    monkeypatch.setattr(runtime_settings, "resolve_int", fake_resolve_int)

    pool = _make_pool(_cfg("acc1"))
    pool._config_loaded_at = time.monotonic() + 60.0

    async def fake_get_pool() -> ProviderPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)

    image_probe_called = False

    async def fake_probe_image_all(_image_probe: object) -> dict[str, bool]:
        nonlocal image_probe_called
        image_probe_called = True
        return {"acc1": True}

    monkeypatch.setattr(pool, "probe_image_all", fake_probe_image_all)
    # 强制 runtime 时间戳远早于 now，让 interval 判定通过。
    # 用 -1e9 而不是 0.0：CI 上 fresh 进程的 time.monotonic() 可能 < 1800，
    # 0.0 - now 不一定 >= image_interval；负大值绝对管够。
    monkeypatch.setattr(provider_pool._RUNTIME, "last_image_probe_at", -1e9)

    await provider_pool.probe_providers(
        {"image_upstream_runtime": build_image_upstream_runtime()}
    )
    assert image_probe_called is True


# --- pytest-asyncio event_loop ----------------------------------------------


@pytest.fixture
def event_loop():  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
