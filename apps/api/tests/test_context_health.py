from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.routes import admin


class _Redis:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, str]] = {}

    async def get(self, key: str):
        if key == "context:circuit:breaker:state":
            return "open"
        if key == "context:circuit:breaker:until":
            return "2026-04-26T10:10:00Z"
        return None

    async def pttl(self, _key: str) -> int:
        return 600_000

    async def hgetall(self, key: str) -> dict[str, str]:
        return self.rows.get(key, {})


class _BadRedis:
    async def get(self, _key: str):
        raise RuntimeError("redis unavailable")


@pytest.mark.asyncio
async def test_context_health_aggregates_redis_hourly_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    keys = admin._hourly_context_metric_keys(
        admin.datetime(2026, 4, 26, 10, tzinfo=admin.timezone.utc)
    )
    redis.rows[keys[0]] = {
        "summary_attempts": "3",
        "summary_successes": "2",
        "summary_failures": "1",
        "manual_compact_calls": "1",
        "cold_start_count": "1",
        "summary_latency_ms_samples": "[100, 200, 900]",
        "fallback_reason:summary_failed": "1",
    }
    redis.rows[keys[1]] = {
        "summary_attempts": "1",
        "summary_successes": "1",
        "summary_failures": "0",
        "manual_compact_calls": "2",
        "summary_latency_ms_samples": "300",
        "fallback_reasons:rate_limited": "2",
    }

    class _FixedDatetime(admin.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return admin.datetime(2026, 4, 26, 10, 30, tzinfo=tz)

    monkeypatch.setattr(admin, "datetime", _FixedDatetime)
    monkeypatch.setattr(admin, "get_redis", lambda: redis)

    out = await admin.context_health(SimpleNamespace(role="admin"))

    assert out["degraded"] is False
    assert out["degrade_reason"] is None
    assert out["circuit_breaker_state"] == "open"
    assert out["circuit_breaker_until"] == "2026-04-26T10:10:00Z"
    assert out["last_24h"]["summary_attempts"] == 4
    assert out["last_24h"]["summary_successes"] == 3
    assert out["last_24h"]["summary_failures"] == 1
    assert out["last_24h"]["summary_success_rate"] == 0.75
    assert out["last_24h"]["manual_compact_calls"] == 3
    assert out["last_24h"]["cold_start_count"] == 1
    assert out["last_24h"]["summary_p50_latency_ms"] == 250
    assert out["last_24h"]["summary_p95_latency_ms"] == 810
    assert out["last_24h"]["fallback_reasons"] == {
        "summary_failed": 1,
        "rate_limited": 2,
    }


@pytest.mark.asyncio
async def test_context_health_marks_degraded_when_redis_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin, "get_redis", lambda: _BadRedis())

    out = await admin.context_health(SimpleNamespace(role="admin"))

    assert out == admin._context_health_zero(
        degraded=True,
        degrade_reason="redis_unavailable",
    )
