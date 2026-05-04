"""image-stability-hardening §P2 取消语义 / drain 不变量回归测试。

invariant：
1. 用户显式取消（POST /tasks/.../cancel 写 ``task:{id}:cancel``）
   → ``_await_with_lease_guard`` 检测到 → 抛 ``_TaskCancelled``。
2. lease 丢失（lease_lost 被 set） → 抛 ``_LeaseLost``。
3. work 正常完成 → 直接返回结果，不触发任何 cancel 路径。
4. 浏览器 SSE 断开（events.py is_disconnected）：本测试静态校验
   ``apps/api/app/routes/events.py`` 没有写 ``task:*:cancel`` 的代码路径，
   保证浏览器关页不会误杀 worker 任务。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.tasks import generation


class _FakeRedis:
    """最小 Redis stub。``set_cancel(task_id)`` 等价于 PUT /cancel。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set_cancel(self, task_id: str) -> None:
        self.store[f"task:{task_id}:cancel"] = "1"


@pytest.mark.asyncio
async def test_lease_guard_returns_work_result_when_no_cancel_or_lease_lost() -> None:
    redis = _FakeRedis()
    lease_lost = asyncio.Event()

    async def work() -> str:
        await asyncio.sleep(0.01)
        return "done"

    result = await generation._await_with_lease_guard(
        work(),
        lease_lost,
        redis=redis,
        task_id="t1",
        cancel_poll_interval_s=0.01,
    )
    assert result == "done"


@pytest.mark.asyncio
async def test_lease_guard_raises_task_cancelled_when_redis_cancel_set() -> None:
    """用户显式取消 → 上游 work_task 被 cancel，抛 _TaskCancelled。"""
    redis = _FakeRedis()
    lease_lost = asyncio.Event()

    cancelled_in_work = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(2.0)
            return "should not reach"
        except asyncio.CancelledError:
            cancelled_in_work.set()
            raise

    async def trigger_cancel_after_delay() -> None:
        await asyncio.sleep(0.05)
        redis.set_cancel("t-cancel")

    asyncio.create_task(trigger_cancel_after_delay())

    with pytest.raises(generation._TaskCancelled):
        await generation._await_with_lease_guard(
            work(),
            lease_lost,
            redis=redis,
            task_id="t-cancel",
            cancel_poll_interval_s=0.02,
        )
    # work_task 真的被 cancel 了，上游 iterator 的 finally 才能跑
    assert cancelled_in_work.is_set()


@pytest.mark.asyncio
async def test_lease_guard_raises_lease_lost_when_event_set() -> None:
    """lease 续约失败时抛 _LeaseLost，并取消 work_task。"""
    redis = _FakeRedis()
    lease_lost = asyncio.Event()

    cancelled_in_work = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(2.0)
            return "x"
        except asyncio.CancelledError:
            cancelled_in_work.set()
            raise

    async def lose_lease_after_delay() -> None:
        await asyncio.sleep(0.05)
        lease_lost.set()

    asyncio.create_task(lose_lease_after_delay())

    with pytest.raises(generation._LeaseLost):
        await generation._await_with_lease_guard(
            work(),
            lease_lost,
            redis=redis,
            task_id="t-lease",
            cancel_poll_interval_s=0.02,
        )
    assert cancelled_in_work.is_set()


@pytest.mark.asyncio
async def test_lease_guard_pre_check_lease_lost_raises_immediately() -> None:
    """进入函数前 lease_lost 已 set → 立即抛，不进 await。"""
    redis = _FakeRedis()
    lease_lost = asyncio.Event()
    lease_lost.set()

    async def work() -> str:
        return "ignored"

    coro = work()
    try:
        with pytest.raises(generation._LeaseLost):
            await generation._await_with_lease_guard(
                coro,
                lease_lost,
                redis=redis,
                task_id="t-pre",
            )
    finally:
        coro.close()  # 函数 pre-check 直接 raise，未 await coro，显式关掉避免 RuntimeWarning


@pytest.mark.asyncio
async def test_lease_guard_external_cancel_propagates() -> None:
    """外层调用方 cancel 当前协程 → CancelledError 透传，work_task 也被清理。"""
    redis = _FakeRedis()
    lease_lost = asyncio.Event()

    cancelled_in_work = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(5.0)
            return "x"
        except asyncio.CancelledError:
            cancelled_in_work.set()
            raise

    async def runner() -> Any:
        return await generation._await_with_lease_guard(
            work(),
            lease_lost,
            redis=redis,
            task_id="t-outer",
            cancel_poll_interval_s=0.05,
        )

    task = asyncio.create_task(runner())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 等一拍让 finally 链跑完
    await asyncio.sleep(0.01)
    assert cancelled_in_work.is_set()


def test_events_route_does_not_write_task_cancel_key() -> None:
    """静态守门：apps/api/app/routes/events.py 不能出现写 cancel key 的代码。

    image-stability-hardening §P2 invariant：浏览器 SSE 断开 → 不杀 worker。
    一旦未来有人在 events.py 里写了 ``task:{id}:cancel``，本测试立即报警。
    """
    repo_root = Path(__file__).resolve().parents[3]
    events_path = repo_root / "apps" / "api" / "app" / "routes" / "events.py"
    assert events_path.exists(), f"events route file missing: {events_path}"
    text = events_path.read_text(encoding="utf-8")
    # 容忍注释中提及（用 codeblock 关键短语精确匹配可疑写入）
    forbidden_snippets = (
        '"task:{',  # f-string 拼 cancel key 的常见前缀
        ":cancel\"",
        ":cancel'",
    )
    for snippet in forbidden_snippets:
        # 不能出现写入 redis 的 set / setex / hset 调用紧贴这些 key
        assert "redis.set(" + snippet not in text, snippet
        assert "redis.setex(" + snippet not in text, snippet
