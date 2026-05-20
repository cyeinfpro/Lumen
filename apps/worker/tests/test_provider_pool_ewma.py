from __future__ import annotations

import time

import pytest

from app.provider_pool import EndpointStat, ProviderConfig, ProviderHealth, ProviderPool


def _cfg(name: str, *, priority: int = 0) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        base_url=f"https://{name}.example",
        api_key=f"sk-{name}",
        priority=priority,
        weight=1,
        enabled=True,
    )


def _pool(*configs: ProviderConfig) -> ProviderPool:
    pool = ProviderPool()
    pool._providers = list(configs)
    pool._health = {p.name: ProviderHealth() for p in configs}
    pool._config_loaded_at = time.monotonic() + 60.0
    return pool


def test_endpoint_success_and_failure_update_ewma() -> None:
    pool = _pool(_cfg("acc"))

    pool.record_endpoint_failure("acc", "generations")
    stat = pool._health["acc"].endpoint_stats["generations"]
    assert stat.failure_ewma == pytest.approx(0.35)
    assert stat.consecutive_failures == 1

    pool.record_endpoint_success("acc", "generations", latency_ms=100)
    assert stat.failure_ewma == pytest.approx(0.2275)
    assert stat.latency_ewma_ms == pytest.approx(100)
    assert stat.consecutive_failures == 0

    pool.record_endpoint_success("acc", "generations", latency_ms=300)
    assert stat.latency_ewma_ms == pytest.approx(150)


def test_image_report_failure_can_feed_endpoint_ewma() -> None:
    pool = _pool(_cfg("acc"))

    pool.report_image_failure("acc", endpoint_kind="responses")

    stat = pool._health["acc"].endpoint_stats["responses"]
    assert stat.failures == 1
    assert stat.failure_ewma > 0


@pytest.mark.asyncio
async def test_image_select_uses_ewma_when_baseline_keys_tie() -> None:
    pool = _pool(_cfg("slow"), _cfg("fast"))
    pool._health["slow"].endpoint_stats["generations"] = EndpointStat(
        latency_ewma_ms=2500,
        failure_ewma=0.8,
    )
    pool._health["fast"].endpoint_stats["generations"] = EndpointStat(
        latency_ewma_ms=200,
        failure_ewma=0.0,
    )

    providers = await pool.select(
        route="image",
        endpoint_kind="generations",
        acquire_inflight=False,
    )

    assert [p.name for p in providers[:2]] == ["fast", "slow"]


@pytest.mark.asyncio
async def test_image_select_ewma_can_beat_last_used_and_priority() -> None:
    pool = _pool(_cfg("bad", priority=10), _cfg("good", priority=0))
    now = time.monotonic()
    pool._health["bad"].endpoint_stats["generations"] = EndpointStat(
        latency_ewma_ms=300,
        failure_ewma=1.0,
        consecutive_failures=2,
        last_failure_at=now,
    )
    pool._health["good"].endpoint_stats["generations"] = EndpointStat(
        latency_ewma_ms=700,
        failure_ewma=0.0,
        last_success_at=now,
    )
    pool._health["good"].image_last_used_at_per_ek["generations"] = now

    providers = await pool.select(
        route="image",
        endpoint_kind="generations",
        acquire_inflight=False,
    )

    assert [p.name for p in providers[:2]] == ["good", "bad"]


@pytest.mark.asyncio
async def test_image_select_auto_endpoint_reads_specific_endpoint_ewma() -> None:
    pool = _pool(_cfg("bad-auto"), _cfg("good-auto"))
    pool._health["bad-auto"].endpoint_stats["generations"] = EndpointStat(
        failure_ewma=1.0,
        consecutive_failures=2,
    )
    pool._health["good-auto"].endpoint_stats["responses"] = EndpointStat(
        latency_ewma_ms=180,
        failure_ewma=0.0,
    )

    providers = await pool.select(route="image", acquire_inflight=False)

    assert [p.name for p in providers[:2]] == ["good-auto", "bad-auto"]


def test_endpoint_chain_prefers_lower_ewma_latency() -> None:
    pool = _pool(_cfg("acc"))
    pool.record_endpoint_success("acc", "generations", latency_ms=1200)
    pool.record_endpoint_success("acc", "responses", latency_ms=180)

    assert pool.endpoint_chain("acc", "generate", "auto") == [
        "responses",
        "generations",
    ]
