"""image-stability-hardening §P2 provider capability 路由过滤测试。

覆盖：
- 旧配置（capability 字段缺失）行为不变
- responses_supported=False → text / models 路由排除
- image_responses_supported=False → image responses endpoint 排除
- image_generations_supported=False → image generations endpoint 排除
- capability=False 但所有候选都被排除时抛 NO_PROVIDERS
- capability=None 仍然走现有 endpoint_lock + 健康度选号
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app import account_limiter, provider_pool, upstream
from app.provider_pool import ProviderConfig, ProviderHealth, ProviderPool
from lumen_core.providers import provider_supports_route


def _pool(*configs: ProviderConfig) -> ProviderPool:
    pool = ProviderPool()
    pool._providers = list(configs)
    pool._health = {p.name: ProviderHealth() for p in configs}
    pool._config_loaded_at = time.monotonic() + 60.0
    return pool


def _cfg(
    name: str,
    *,
    responses_supported: bool | None = None,
    image_generations_supported: bool | None = None,
    image_responses_supported: bool | None = None,
    image_jobs_endpoint: str = "auto",
    image_jobs_endpoint_lock: bool = False,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        base_url=f"https://{name}.example",
        api_key=f"sk-{name}",
        priority=0,
        weight=1,
        enabled=True,
        image_jobs_endpoint=image_jobs_endpoint,
        image_jobs_endpoint_lock=image_jobs_endpoint_lock,
        responses_supported=responses_supported,
        image_generations_supported=image_generations_supported,
        image_responses_supported=image_responses_supported,
    )


# --- pure helper ------------------------------------------------------------


def test_provider_supports_route_unknown_capability_allows_all() -> None:
    p = _cfg("unknown")
    assert provider_supports_route(p, route="text", endpoint_kind="responses")
    assert provider_supports_route(p, route="image", endpoint_kind="responses")
    assert provider_supports_route(p, route="image", endpoint_kind="generations")
    assert provider_supports_route(p, route="models", endpoint_kind="models")


def test_provider_supports_route_explicit_true_allows() -> None:
    p = _cfg(
        "yes",
        responses_supported=True,
        image_generations_supported=True,
        image_responses_supported=True,
    )
    assert provider_supports_route(p, route="image", endpoint_kind="responses")
    assert provider_supports_route(p, route="image", endpoint_kind="generations")


def test_provider_supports_route_responses_false_blocks_text_and_models() -> None:
    p = _cfg("noresp", responses_supported=False)
    assert provider_supports_route(p, route="text", endpoint_kind="responses") is False
    assert provider_supports_route(p, route="models", endpoint_kind="models") is False
    # image+responses 也被屏蔽
    assert provider_supports_route(p, route="image", endpoint_kind="responses") is False


def test_provider_supports_route_image_generations_false_only_blocks_generations() -> None:
    p = _cfg("nogen", image_generations_supported=False)
    assert provider_supports_route(p, route="image", endpoint_kind="generations") is False
    assert provider_supports_route(p, route="image", endpoint_kind="responses") is True
    # text 路由不依赖 image generations 能力
    assert provider_supports_route(p, route="text", endpoint_kind="responses") is True


def test_provider_supports_route_image_responses_false_only_blocks_image_responses() -> None:
    p = _cfg("noimgresp", image_responses_supported=False)
    assert provider_supports_route(p, route="image", endpoint_kind="responses") is False
    assert provider_supports_route(p, route="image", endpoint_kind="generations") is True


def test_provider_supports_route_image_unknown_endpoint_kind_allows_when_any_path_open() -> None:
    """endpoint_kind 未知（auto）时，只要至少一种 image 能力非显式 False 就允许。"""
    p_one_blocked = _cfg("partial", image_generations_supported=False)
    assert provider_supports_route(p_one_blocked, route="image", endpoint_kind=None)
    p_all_blocked = _cfg(
        "blocked",
        image_generations_supported=False,
        image_responses_supported=False,
    )
    assert (
        provider_supports_route(p_all_blocked, route="image", endpoint_kind=None) is False
    )


# --- pool routing -----------------------------------------------------------


@pytest.mark.asyncio
async def test_select_image_excludes_provider_with_image_responses_unsupported() -> None:
    pool = _pool(
        _cfg("good"),
        _cfg("nogen", image_responses_supported=False),
    )
    providers = await pool.select(
        route="image", endpoint_kind="responses", acquire_inflight=False
    )
    assert [p.name for p in providers] == ["good"]
    assert providers[0].image_responses_supported is None


@pytest.mark.asyncio
async def test_select_image_excludes_provider_with_image_generations_unsupported() -> None:
    pool = _pool(
        _cfg("good"),
        _cfg("noimg", image_generations_supported=False),
    )
    providers = await pool.select(
        route="image", endpoint_kind="generations", acquire_inflight=False
    )
    assert [p.name for p in providers] == ["good"]


@pytest.mark.asyncio
async def test_resolved_provider_preserves_capability_fields() -> None:
    pool = _pool(
        _cfg(
            "good",
            responses_supported=True,
            image_generations_supported=False,
            image_responses_supported=True,
        )
    )
    providers = await pool.select(
        route="image", endpoint_kind="responses", acquire_inflight=False
    )
    assert providers[0].responses_supported is True
    assert providers[0].image_generations_supported is False
    assert providers[0].image_responses_supported is True


@pytest.mark.asyncio
async def test_select_text_excludes_provider_with_responses_unsupported() -> None:
    pool = _pool(
        _cfg("good"),
        _cfg("noresp", responses_supported=False),
    )
    providers = await pool.select(route="text")
    assert [p.name for p in providers] == ["good"]


@pytest.mark.asyncio
async def test_select_image_raises_when_all_capabilities_blocked() -> None:
    pool = _pool(
        _cfg("noimg1", image_responses_supported=False),
        _cfg("noimg2", image_responses_supported=False),
    )
    with pytest.raises(Exception) as exc:
        await pool.select(
            route="image", endpoint_kind="responses", acquire_inflight=False
        )
    # _select_for_image 在所有候选都被滤掉时抛 ALL_ACCOUNTS_FAILED
    # （上游消费方对此熟悉的错误码，含 skipped reasons）。
    assert exc.value.__class__.__name__ == "UpstreamError"
    assert exc.value.error_code == "all_accounts_failed"
    skipped = exc.value.payload.get("skipped") or []
    assert any("capability_unsupported" in str(reason) for _, reason in skipped)


@pytest.mark.asyncio
async def test_capability_unknown_keeps_existing_endpoint_lock_behavior() -> None:
    """capability=None 时，endpoint_lock 仍按旧逻辑工作（不会被 capability gate 误伤）。"""
    locked = _cfg(
        "locked-gen",
        image_jobs_endpoint="generations",
        image_jobs_endpoint_lock=True,
    )
    free = _cfg("free")
    pool = _pool(locked, free)

    # text 路由（endpoint_kind=responses）：locked-gen 被 endpoint_lock 排除
    text_providers = await pool.select(route="text")
    assert {p.name for p in text_providers} == {"free"}

    # image responses：同样被 endpoint_lock 排除
    image_resp = await pool.select(
        route="image", endpoint_kind="responses", acquire_inflight=False
    )
    assert {p.name for p in image_resp} == {"free"}

    # image generations：locked-gen 解锁
    image_gen = await pool.select(
        route="image", endpoint_kind="generations", acquire_inflight=False
    )
    assert {p.name for p in image_gen} == {"locked-gen", "free"}


@pytest.mark.asyncio
async def test_image_job_endpoint_chain_skips_capability_unsupported_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = provider_pool.ResolvedProvider(
        name="partial",
        base_url="https://partial.example",
        api_key="sk-partial",
        image_jobs_enabled=True,
        image_generations_supported=False,
        image_responses_supported=True,
    )
    calls: list[str] = []

    class FakePool:
        def endpoint_chain(
            self, _provider_name: str, _action: str, _configured: str
        ) -> list[str]:
            return ["generations", "responses"]

        def record_endpoint_success(
            self, _provider_name: str, _endpoint: str, *, latency_ms: float | None = None
        ) -> None:
            return None

        def record_endpoint_failure(self, _provider_name: str, _endpoint: str) -> None:
            return None

        def report_image_success(
            self, _provider_name: str, *, endpoint_kind: str | None = None
        ) -> None:
            return None

        def get_redis(self) -> None:
            return None

    async def fake_get_pool() -> FakePool:
        return FakePool()

    async def fake_resolve_image_job_base_url() -> str:
        return "http://image-job"

    async def fake_run_once(**kwargs: Any) -> tuple[str, str | None]:
        calls.append(kwargs["endpoint"])
        return "BBBB", None

    async def fake_record_image_call(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(upstream.provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        upstream, "_resolve_image_job_base_url", fake_resolve_image_job_base_url
    )
    monkeypatch.setattr(upstream, "_image_job_run_once", fake_run_once)
    monkeypatch.setattr(account_limiter, "record_image_call", fake_record_image_call)

    result = await upstream._image_job_with_failover(
        action="generate",
        prompt="p",
        size="1024x1024",
        images=None,
        n=1,
        quality="low",
        progress_callback=None,
        provider_override=provider,
    )

    assert result == ("BBBB", None)
    assert calls == ["responses"]


@pytest.mark.asyncio
async def test_direct_generate_provider_override_blocks_unsupported_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = provider_pool.ResolvedProvider(
        name="nogen",
        base_url="https://nogen.example",
        api_key="sk-nogen",
        image_generations_supported=False,
    )
    calls = 0

    class FakePool:
        def get_redis(self) -> None:
            return None

    async def fake_get_pool() -> FakePool:
        return FakePool()

    async def fake_direct_once(**_kwargs: Any) -> tuple[str, str | None]:
        nonlocal calls
        calls += 1
        return "BBBB", None

    monkeypatch.setattr(upstream.provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "_direct_generate_image_once", fake_direct_once)

    with pytest.raises(Exception) as exc:
        await upstream._direct_generate_image_with_failover(
            prompt="p",
            size="1024x1024",
            n=1,
            quality="low",
            progress_callback=None,
            provider_override=provider,
        )

    assert calls == 0
    assert exc.value.__class__.__name__ == "UpstreamError"
    assert exc.value.error_code == "all_direct_image_providers_failed"
    errors = exc.value.payload.get("provider_errors") or []
    assert any("capability_unsupported" in str(item) for item in errors)


@pytest.mark.asyncio
async def test_image2_dispatch_skips_responses_fallback_when_capability_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = provider_pool.ResolvedProvider(
        name="noresp",
        base_url="https://noresp.example",
        api_key="sk-noresp",
        image_generations_supported=True,
        image_responses_supported=False,
    )
    fallback_calls = 0

    async def fake_direct_fail(**_kwargs: Any) -> tuple[str, str | None]:
        raise upstream.UpstreamError(
            "temporary direct failure",
            status_code=502,
            error_code="upstream_error",
        )

    async def fake_responses_fallback(**_kwargs: Any) -> tuple[str, str | None]:
        nonlocal fallback_calls
        fallback_calls += 1
        return "BBBB", None

    monkeypatch.setattr(upstream, "_direct_generate_image_with_failover", fake_direct_fail)
    monkeypatch.setattr(upstream, "_race_responses_image", fake_responses_fallback)

    with pytest.raises(Exception) as exc:
        async for _ in upstream._run_image_once_for_provider(
            action="generate",
            provider=provider,
            channel="stream",
            engine="image2",
            prompt="p",
            size="1024x1024",
            images=None,
            n=1,
            quality="low",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
        ):
            pass

    assert fallback_calls == 0
    assert exc.value.__class__.__name__ == "UpstreamError"
    assert exc.value.error_code == "provider_exhausted"
    path_errors = exc.value.payload.get("path_errors") or []
    assert any("capability_unsupported" in str(item) for item in path_errors)
