from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app import provider_pool, upstream
from app.provider_pool import ProviderConfig, ProviderHealth, ProviderPool
from app.tasks import (
    auto_title,
    context_image_caption,
    memory_extraction,
    model_library_tagging,
    poster_style_tagging,
)


_TASK_CASES = ("auto_title", "caption", "model_tagging", "poster_tagging")


def _make_pool(
    *,
    purposes: tuple[str, ...] = ("chat", "embedding"),
    provider_count: int = 1,
) -> tuple[ProviderPool, ProviderHealth]:
    pool = ProviderPool()
    pool._providers = [  # noqa: SLF001
        ProviderConfig(
            name=f"provider-{index}",
            base_url=f"https://provider-{index}.example/v1",
            api_key=f"sk-provider-{index}",
            purposes=purposes,
        )
        for index in range(provider_count)
    ]
    pool._health = {  # noqa: SLF001
        provider.name: ProviderHealth() for provider in pool._providers  # noqa: SLF001
    }
    health = pool._health["provider-0"]  # noqa: SLF001
    health.consecutive_failures = 3
    health.cooldown_until = time.monotonic() - 1.0
    pool._config_loaded_at = time.monotonic() + 3600.0  # noqa: SLF001
    return pool, health


def _patch_pool(
    monkeypatch: pytest.MonkeyPatch,
    pool: ProviderPool,
) -> None:
    async def fake_get_pool() -> ProviderPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)


def _assert_released(health: ProviderHealth) -> None:
    assert health.half_open_probe_inflight is False
    assert health.half_open_probe_token is None


async def _invoke_task_case(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str | BaseException,
) -> str | None:
    async def fake_call(*_args: Any, **_kwargs: Any) -> str:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    image = SimpleNamespace(id="image-1")
    if case == "auto_title":
        monkeypatch.setattr(auto_title, "_PER_PROVIDER_RETRY_ATTEMPTS", 1)
        monkeypatch.setattr(auto_title, "_call_upstream_one", fake_call)
        return await auto_title._call_upstream(
            [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
        )
    if case == "caption":
        monkeypatch.setattr(
            context_image_caption,
            "_PER_PROVIDER_RETRY_ATTEMPTS",
            1,
        )
        monkeypatch.setattr(context_image_caption, "_call_upstream_one", fake_call)
        return await context_image_caption._call_upstream(
            image,
            "data:image/png;base64,AA==",
            model="gpt-test",
        )
    if case == "model_tagging":
        monkeypatch.setattr(
            model_library_tagging,
            "_PER_PROVIDER_RETRY_ATTEMPTS",
            1,
        )
        monkeypatch.setattr(model_library_tagging, "_call_upstream_one", fake_call)
        return await model_library_tagging._call_upstream(
            image,
            "data:image/png;base64,AA==",
            model="gpt-test",
        )
    if case == "poster_tagging":
        monkeypatch.setattr(
            poster_style_tagging,
            "_PER_PROVIDER_RETRY_ATTEMPTS",
            1,
        )
        monkeypatch.setattr(poster_style_tagging, "_call_upstream_one", fake_call)
        return await poster_style_tagging._call_upstream(
            image,
            "data:image/png;base64,AA==",
            model="gpt-test",
        )
    raise AssertionError(f"unknown task case: {case}")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _TASK_CASES)
async def test_task_text_call_reports_success(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool()
    _patch_pool(monkeypatch, pool)

    assert await _invoke_task_case(case, monkeypatch, "ok") == "ok"

    assert health.consecutive_failures == 0
    assert health.cooldown_until is None
    assert health.total_requests == 1
    assert health.successful_requests == 1
    assert health.failed_requests == 0
    _assert_released(health)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _TASK_CASES)
async def test_task_text_call_reports_provider_failure(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool()
    _patch_pool(monkeypatch, pool)
    failure = upstream.UpstreamError(
        "provider unavailable",
        error_code="service_unavailable",
        status_code=503,
    )

    try:
        await _invoke_task_case(case, monkeypatch, failure)
    except upstream.UpstreamError:
        pass

    assert health.consecutive_failures == 4
    assert health.cooldown_until is not None
    assert health.cooldown_until > time.monotonic()
    assert health.total_requests == 1
    assert health.successful_requests == 0
    assert health.failed_requests == 1
    _assert_released(health)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _TASK_CASES)
async def test_task_text_call_local_error_does_not_poison_provider(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool()
    _patch_pool(monkeypatch, pool)

    try:
        await _invoke_task_case(case, monkeypatch, ValueError("local parser failed"))
    except upstream.UpstreamError:
        pass

    assert health.consecutive_failures == 3
    assert health.total_requests == 0
    assert health.successful_requests == 0
    assert health.failed_requests == 0
    _assert_released(health)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _TASK_CASES)
async def test_task_single_provider_continues_after_cancellation(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool()
    _patch_pool(monkeypatch, pool)

    with pytest.raises(asyncio.CancelledError):
        await _invoke_task_case(case, monkeypatch, asyncio.CancelledError())

    assert health.consecutive_failures == 3
    assert health.total_requests == 0
    _assert_released(health)

    assert await _invoke_task_case(case, monkeypatch, "recovered") == "recovered"
    assert health.consecutive_failures == 0
    assert health.total_requests == 1
    assert health.successful_requests == 1
    _assert_released(health)


@pytest.mark.asyncio
async def test_embedding_capability_peek_never_claims_half_open() -> None:
    pool, health = _make_pool(purposes=("embedding",))

    assert await memory_extraction._embedding_provider_available(
        {"provider_pool": pool}
    )
    assert await memory_extraction._embedding_provider_available(
        {"provider_pool": pool}
    )

    assert health.consecutive_failures == 3
    assert health.total_requests == 0
    _assert_released(health)


@pytest.mark.asyncio
async def test_embedding_capability_peek_preserves_existing_half_open_owner() -> None:
    pool, health = _make_pool(purposes=("embedding",))
    provider = (await pool.select(purpose="embedding"))[0]
    token = health.half_open_probe_token
    assert token is not None

    assert await memory_extraction._embedding_provider_available(
        {"provider_pool": pool}
    )

    assert health.half_open_probe_inflight is True
    assert health.half_open_probe_token == token
    pool.release_text_attempt(provider)
    _assert_released(health)


@pytest.mark.asyncio
async def test_provider_peek_does_not_advance_round_robin() -> None:
    pool, health = _make_pool(provider_count=2)

    first = [provider.name for provider in await pool.peek(purpose="chat")]
    second = [provider.name for provider in await pool.peek(purpose="chat")]

    assert first == second
    assert pool._rr_state == {}  # noqa: SLF001
    assert health.total_requests == 0
    _assert_released(health)


class _EmbeddingResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict[str, Any] | BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {"data": [{"embedding": [0.1, 0.2]}]}

    def json(self) -> dict[str, Any]:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _EmbeddingClient:
    def __init__(self, outcome: _EmbeddingResponse | BaseException) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> _EmbeddingClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, *_args: Any, **_kwargs: Any) -> _EmbeddingResponse:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _patch_embedding_client(
    monkeypatch: pytest.MonkeyPatch,
    outcome: _EmbeddingResponse | BaseException,
) -> None:
    monkeypatch.setattr(
        memory_extraction.httpx,
        "AsyncClient",
        lambda **_kwargs: _EmbeddingClient(outcome),
    )


@pytest.mark.asyncio
async def test_memory_embedding_reports_success_before_local_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool(purposes=("embedding",))
    _patch_embedding_client(
        monkeypatch,
        _EmbeddingResponse(payload=ValueError("local json parse failed")),
    )

    vector = await memory_extraction._embedding_vector(
        {"provider_pool": pool},
        "remember this",
    )

    assert len(vector) == 3072
    assert health.consecutive_failures == 0
    assert health.total_requests == 1
    assert health.successful_requests == 1
    assert health.failed_requests == 0
    _assert_released(health)


@pytest.mark.asyncio
async def test_memory_embedding_reports_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool(purposes=("embedding",))
    _patch_embedding_client(monkeypatch, _EmbeddingResponse(status_code=503))

    vector = await memory_extraction._embedding_vector(
        {"provider_pool": pool},
        "remember this",
    )

    assert len(vector) == 3072
    assert health.consecutive_failures == 4
    assert health.total_requests == 1
    assert health.failed_requests == 1
    _assert_released(health)


@pytest.mark.asyncio
async def test_memory_embedding_reports_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool(purposes=("embedding",))
    _patch_embedding_client(monkeypatch, httpx.ConnectError("connection failed"))

    vector = await memory_extraction._embedding_vector(
        {"provider_pool": pool},
        "remember this",
    )

    assert len(vector) == 3072
    assert health.consecutive_failures == 4
    assert health.total_requests == 1
    assert health.failed_requests == 1
    _assert_released(health)


@pytest.mark.asyncio
async def test_memory_embedding_single_provider_continues_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool(purposes=("embedding",))
    _patch_embedding_client(monkeypatch, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await memory_extraction._embedding_vector(
            {"provider_pool": pool},
            "remember this",
        )

    assert health.total_requests == 0
    _assert_released(health)

    _patch_embedding_client(monkeypatch, _EmbeddingResponse())
    assert await memory_extraction._embedding_vector(
        {"provider_pool": pool},
        "remember this",
    ) == [0.1, 0.2]
    assert health.total_requests == 1
    assert health.successful_requests == 1
    _assert_released(health)


def _memory_payload() -> dict[str, Any]:
    return {
        "output_text": (
            '{"items":[{"type":"preference","content":"concise replies",'
            '"confidence":0.9,"source_excerpt":"concise",'
            '"intent_kind":"statement"}]}'
        )
    }


@pytest.mark.asyncio
async def test_memory_llm_reports_success_before_candidate_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool(purposes=("chat",))
    _patch_pool(monkeypatch, pool)

    async def fake_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return _memory_payload()

    def fail_parse(_raw: str) -> list[Any]:
        raise ValueError("local candidate parse failed")

    monkeypatch.setattr(upstream, "responses_call", fake_call)
    monkeypatch.setattr(memory_extraction, "_parse_llm_candidates", fail_parse)

    assert (
        await memory_extraction._try_llm_extract(
            "I prefer concise replies",
            explicit_only=False,
        )
        == []
    )
    assert health.consecutive_failures == 0
    assert health.total_requests == 1
    assert health.successful_requests == 1
    assert health.failed_requests == 0
    _assert_released(health)


@pytest.mark.asyncio
async def test_memory_llm_reports_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool(purposes=("chat",))
    _patch_pool(monkeypatch, pool)

    async def fail_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise upstream.UpstreamError(
            "provider unavailable",
            error_code="service_unavailable",
            status_code=503,
        )

    monkeypatch.setattr(upstream, "responses_call", fail_call)

    assert (
        await memory_extraction._try_llm_extract(
            "I prefer concise replies",
            explicit_only=False,
        )
        == []
    )
    assert health.consecutive_failures == 4
    assert health.total_requests == 1
    assert health.failed_requests == 1
    _assert_released(health)


@pytest.mark.asyncio
async def test_memory_llm_single_provider_continues_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool(purposes=("chat",))
    _patch_pool(monkeypatch, pool)

    async def cancel_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise asyncio.CancelledError

    monkeypatch.setattr(upstream, "responses_call", cancel_call)
    with pytest.raises(asyncio.CancelledError):
        await memory_extraction._try_llm_extract(
            "I prefer concise replies",
            explicit_only=False,
        )

    assert health.total_requests == 0
    _assert_released(health)

    async def successful_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return _memory_payload()

    monkeypatch.setattr(upstream, "responses_call", successful_call)
    items = await memory_extraction._try_llm_extract(
        "I prefer concise replies",
        explicit_only=False,
    )
    assert len(items) == 1
    assert health.total_requests == 1
    assert health.successful_requests == 1
    _assert_released(health)


@pytest.mark.asyncio
async def test_upstream_responses_call_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool(purposes=("chat",))
    _patch_pool(monkeypatch, pool)
    seen: dict[str, Any] = {}

    async def fake_call(
        _body: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        seen.update(kwargs)
        return {"output_text": "ok"}

    monkeypatch.setattr(upstream.upstream_responses_client, "responses_call", fake_call)

    assert await upstream.responses_call({"model": "gpt-test"}) == {
        "output_text": "ok"
    }
    assert seen["base_url_override"] == "https://provider-0.example/v1"
    assert seen["api_key_override"] == "sk-provider-0"
    assert health.consecutive_failures == 0
    assert health.total_requests == 1
    assert health.successful_requests == 1
    _assert_released(health)


@pytest.mark.asyncio
async def test_upstream_responses_call_reports_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool(purposes=("chat",))
    _patch_pool(monkeypatch, pool)

    async def fail_call(
        _body: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        raise upstream.UpstreamError(
            "provider unavailable",
            error_code="service_unavailable",
            status_code=503,
        )

    monkeypatch.setattr(upstream.upstream_responses_client, "responses_call", fail_call)

    with pytest.raises(upstream.UpstreamError):
        await upstream.responses_call({"model": "gpt-test"})

    assert health.consecutive_failures == 4
    assert health.total_requests == 1
    assert health.failed_requests == 1
    _assert_released(health)


@pytest.mark.asyncio
async def test_upstream_single_provider_continues_after_local_error_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, health = _make_pool(purposes=("chat",))
    _patch_pool(monkeypatch, pool)
    outcomes: list[dict[str, Any] | BaseException] = [
        ValueError("local validation failed"),
        asyncio.CancelledError(),
        {"output_text": "recovered"},
    ]

    async def fake_call(
        _body: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(upstream.upstream_responses_client, "responses_call", fake_call)

    with pytest.raises(ValueError, match="local validation"):
        await upstream.responses_call({"model": "gpt-test"})
    assert health.total_requests == 0
    _assert_released(health)

    with pytest.raises(asyncio.CancelledError):
        await upstream.responses_call({"model": "gpt-test"})
    assert health.total_requests == 0
    _assert_released(health)

    assert await upstream.responses_call({"model": "gpt-test"}) == {
        "output_text": "recovered"
    }
    assert health.total_requests == 1
    assert health.successful_requests == 1
    _assert_released(health)
