from __future__ import annotations

import asyncio

import pytest

from app.tasks import completion


@pytest.mark.asyncio
async def test_completion_lease_renewer_sets_event_and_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenRedis:
        async def expire(self, *_args, **_kwargs) -> None:
            raise RuntimeError("redis down")

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(completion.asyncio, "sleep", no_sleep)
    lease_lost = completion.asyncio.Event()

    await completion._lease_renewer(BrokenRedis(), "comp-1", lease_lost)

    assert lease_lost.is_set()


@pytest.mark.asyncio
async def test_completion_lease_renewer_cancellation_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel() lease renewer task 不应 raise 出 _lease_renewer 之外。

    主路径在 finally 里 cancel renewer + suppress(CancelledError) 等它完成；
    如果 _lease_renewer 处理 cancel 时漏抛非 CancelledError 异常或吞掉
    CancelledError，外层 await 会失去取消语义。这里直接 cancel 一个 sleep
    阻塞中的 renewer，确认 await 拿回 CancelledError、redis.expire 没被
    调用、lease_lost 没被误 set。
    """
    expire_calls: list[tuple] = []

    class HealthyRedis:
        async def expire(self, *args, **_kwargs) -> None:
            expire_calls.append(args)

    lease_lost = completion.asyncio.Event()

    task = asyncio.create_task(
        completion._lease_renewer(HealthyRedis(), "comp-cancel", lease_lost)
    )
    # 让 task 进入 sleep 状态再 cancel
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled() or task.done()
    assert not lease_lost.is_set()
    assert expire_calls == []  # cancel 在第一次 sleep 内即触发，没机会调 expire


@pytest.mark.asyncio
async def test_completion_lease_renewer_clears_fail_streak_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续失败后再连续 2 轮成功，fail streak 应归零，避免后续失败累加触阈值。

    实现里 ``consecutive_failures = 0`` 紧跟在 expire 成功路径，覆盖：
    fail → fail → success → success → fail → fail（streak 应该是 2，不是 4），
    第三次 fail 才触发 >=3 的 lease_lost。这里 sequence 故意只到第 6 步就
    再来 2 次 fail（第 7、8 步）让 streak 触阈值退出循环；如果实现没在 success
    时 reset，第 5 步那次 fail 就已经触发 lease_lost（4 次累计），断言失败。
    """
    sequence: list[bool] = [
        False,  # 1: fail (streak=1)
        False,  # 2: fail (streak=2)
        True,   # 3: success → reset to 0
        True,   # 4: success → still 0
        False,  # 5: fail (streak=1, NOT 3 if reset works)
        False,  # 6: fail (streak=2)
        False,  # 7: fail (streak=3 → set lease_lost & return)
    ]
    call_idx = {"i": 0}

    class FlakyRedis:
        async def expire(self, *_args, **_kwargs) -> None:
            i = call_idx["i"]
            call_idx["i"] += 1
            # 越界视作 fail（兜底），但 streak=3 会先 return 出来
            ok = sequence[i] if i < len(sequence) else False
            if not ok:
                raise RuntimeError("redis blip")

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(completion.asyncio, "sleep", no_sleep)
    lease_lost = completion.asyncio.Event()

    await completion._lease_renewer(FlakyRedis(), "comp-streak", lease_lost)

    # 关键：必须走完第 5、6、7 步才退（streak 在第 7 步=3）。如果 reset 没生效
    # ，第 5 步就已经是 streak=3 退出，call_idx 只会到 5。
    assert call_idx["i"] == 7
    # 第 7 步触阈值后 lease_lost 才 set
    assert lease_lost.is_set()
