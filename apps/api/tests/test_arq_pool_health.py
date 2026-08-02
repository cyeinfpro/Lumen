"""Regression tests: arq pool health-check / pool-creation pings must be
bounded so a half-open Redis (TCP alive, no responses) cannot pin the
process-wide global lock and block every enqueue forever."""

from __future__ import annotations

import asyncio

import pytest

from app.arq_pool import _pool_is_healthy, _pool_state, get_arq_pool


class _HangingPing:
    """Pool whose ping never answers, simulating a half-open connection."""

    async def ping(self) -> bool:
        await asyncio.sleep(30)
        return True

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_health_check_ping_timeout_marks_unhealthy(monkeypatch) -> None:
    monkeypatch.setattr("app.arq_pool._ARQ_PING_TIMEOUT_SECONDS", 0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    healthy = await _pool_is_healthy(_HangingPing())
    elapsed = loop.time() - start
    assert healthy is False
    # 若去掉 wait_for,ping 会挂 30s 才返回 True,此处应快速失败
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_pool_creation_timeout_releases_global_lock(monkeypatch) -> None:
    monkeypatch.setattr("app.arq_pool._ARQ_POOL_CREATE_TIMEOUT_SECONDS", 0.05)

    async def hanging_create_pool(*_args: object, **_kwargs: object) -> object:
        await asyncio.sleep(30)

    monkeypatch.setattr("app.arq_pool.create_pool", hanging_create_pool)
    _reset_pool_state()
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(asyncio.TimeoutError):
            await get_arq_pool()
        elapsed = loop.time() - start
        # 若去掉 wait_for,create_pool 的 ping 会挂住全局锁 30s,此处应快速失败
        assert elapsed < 5.0
    finally:
        _reset_pool_state()


def _reset_pool_state() -> None:
    _pool_state.pool = None
    _pool_state.loop = None
    _pool_state.loop_id = None
    _pool_state.checked_at = 0.0
