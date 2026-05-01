"""account_limiter 测试。

覆盖 sub2api"一号一 key + Lumen 调度"路径的核心：
- 速率字符串解析（"5/min" / "50/h" / "200/d"）
- 不限速 / redis 缺失时短路放行
- 滑动窗口超限返回 (False, retry_after)，retry_after 由最早一条时间戳推得
- 当日上限超限返回 (False, 距离次日 UTC 午夜的秒数)
- record_image_call 同时 ZADD + INCR daily counter
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import account_limiter


class FakeRedis:
    """最小 redis mock：支持 zadd/zcard/zrange/zremrangebyscore/incr/get/expire(at)。"""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.kv: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        zset = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in zset:
                added += 1
            zset[member] = float(score)
        return added

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def zremrangebyscore(self, key: str, mn: float, mx: float) -> int:
        zset = self.zsets.get(key)
        if not zset:
            return 0
        removed = [m for m, s in zset.items() if mn <= s <= mx]
        for m in removed:
            del zset[m]
        return len(removed)

    async def zrange(
        self, key: str, start: int, stop: int, withscores: bool = False
    ) -> list[Any]:
        zset = self.zsets.get(key)
        if not zset:
            return []
        # ASC by score
        items = sorted(zset.items(), key=lambda kv: kv[1])
        if stop == -1:
            sl = items[start:]
        else:
            sl = items[start : stop + 1]
        if withscores:
            return [(m, s) for m, s in sl]
        return [m for m, _ in sl]

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def incr(self, key: str) -> int:
        cur = int(self.kv.get(key) or 0) + 1
        self.kv[key] = str(cur)
        return cur

    async def expire(self, key: str, seconds: int) -> int:
        self.expirations[key] = int(seconds)
        return 1

    async def expireat(self, key: str, ts: int) -> int:
        self.expirations[key] = int(ts)
        return 1


# --- parse_rate_limit -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5/min", (5, 60)),
        ("50/h", (50, 3600)),
        ("200/d", (200, 86400)),
        ("10/sec", (10, 1)),
        ("3/MINUTE", (3, 60)),  # case-insensitive
        ("100/Hour", (100, 3600)),
        ("1/day", (1, 86400)),
    ],
)
def test_parse_rate_limit_valid(raw: str, expected: tuple[int, int]) -> None:
    assert account_limiter.parse_rate_limit(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "abc", "5", "/min", "5/", "0/min", "-1/min", "5/year", "five/min"],
)
def test_parse_rate_limit_invalid(raw: str | None) -> None:
    assert account_limiter.parse_rate_limit(raw) is None


# --- check_quota ------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_quota_short_circuits_when_no_limits_configured() -> None:
    redis = FakeRedis()
    allowed, retry_after = await account_limiter.check_quota(
        redis, "acc1", rate_limit=None, daily_quota=None
    )
    assert allowed is True
    assert retry_after == 0.0
    # 不应该查 Redis（zsets/kv 都是空的，间接验证）
    assert redis.zsets == {}
    assert redis.kv == {}


@pytest.mark.asyncio
async def test_check_quota_short_circuits_when_redis_is_none() -> None:
    allowed, retry_after = await account_limiter.check_quota(
        None, "acc1", rate_limit="5/min", daily_quota=80
    )
    assert allowed is True
    assert retry_after == 0.0


@pytest.mark.asyncio
async def test_check_quota_passes_below_window_limit() -> None:
    redis = FakeRedis()
    now = 1_700_000_000.0
    # 预填 4 条历史时间戳（窗口 60s 内），limit=5 → 仍可放行
    for i in range(4):
        await redis.zadd(
            "lumen:acct:acc1:image:ts", {f"old{i}": now - 30 + i}
        )
    allowed, retry_after = await account_limiter.check_quota(
        redis, "acc1", rate_limit="5/min", daily_quota=None, now=now
    )
    assert allowed is True
    assert retry_after == 0.0


@pytest.mark.asyncio
async def test_check_quota_blocks_at_window_limit_with_retry_after() -> None:
    redis = FakeRedis()
    now = 1_700_000_000.0
    # 5 条都在窗口内，最早的离 now 只 50s，60s 窗口要等 ~10s 才出窗口
    earliest = now - 50.0
    for i in range(5):
        await redis.zadd(
            "lumen:acct:acc1:image:ts", {f"t{i}": earliest + i * 10.0}
        )
    allowed, retry_after = await account_limiter.check_quota(
        redis, "acc1", rate_limit="5/min", daily_quota=None, now=now
    )
    assert allowed is False
    # 最早的 ts=now-50，窗口 60s → retry_after = (earliest + 60) - now = 10s
    assert 9.0 <= retry_after <= 11.0


@pytest.mark.asyncio
async def test_check_quota_blocks_when_daily_quota_exhausted() -> None:
    redis = FakeRedis()
    now = 1_700_000_000.0
    day_key = f"lumen:acct:acc1:image:daily:{account_limiter._today_utc_key(now)}"
    redis.kv[day_key] = "80"
    allowed, retry_after = await account_limiter.check_quota(
        redis, "acc1", rate_limit=None, daily_quota=80, now=now
    )
    assert allowed is False
    # retry_after ≈ 距离次日 0:00 UTC 的秒数（>0，<=86400）
    assert 0.0 < retry_after <= 86400.0


@pytest.mark.asyncio
async def test_check_quota_expires_old_window_entries() -> None:
    redis = FakeRedis()
    now = 1_700_000_000.0
    # 4 条 60s 之外的旧戳 + 1 条新戳：清理后只剩 1 条 → 仍可放行 limit=5
    for i in range(4):
        await redis.zadd(
            "lumen:acct:acc1:image:ts", {f"old{i}": now - 600 - i}
        )
    await redis.zadd("lumen:acct:acc1:image:ts", {"recent": now - 5})
    allowed, _ = await account_limiter.check_quota(
        redis, "acc1", rate_limit="5/min", daily_quota=None, now=now
    )
    assert allowed is True
    # 旧戳已被 zremrangebyscore 清掉
    assert await redis.zcard("lumen:acct:acc1:image:ts") == 1


# --- record_image_call ------------------------------------------------------


@pytest.mark.asyncio
async def test_record_image_call_writes_zset_and_daily() -> None:
    redis = FakeRedis()
    now = 1_700_000_000.0
    await account_limiter.record_image_call(
        redis, "acc1", task_id="task-abc", now=now
    )
    ts_key = "lumen:acct:acc1:image:ts"
    day_key = f"lumen:acct:acc1:image:daily:{account_limiter._today_utc_key(now)}"
    assert ts_key in redis.zsets
    assert "task-abc" in redis.zsets[ts_key]
    assert redis.zsets[ts_key]["task-abc"] == now
    assert redis.kv[day_key] == "1"
    # ZSET 和 daily 都设置了 expire
    assert ts_key in redis.expirations
    assert day_key in redis.expirations


@pytest.mark.asyncio
async def test_record_image_call_no_redis_is_noop() -> None:
    # redis=None 不抛错，静默跳过
    await account_limiter.record_image_call(None, "acc1", task_id="x")


@pytest.mark.asyncio
async def test_record_then_check_quota_round_trip() -> None:
    """端到端：连续记 5 条 → 第 6 次 check_quota 必返回 False。"""
    redis = FakeRedis()
    now = 1_700_000_000.0
    for i in range(5):
        await account_limiter.record_image_call(
            redis, "acc1", task_id=f"t{i}", now=now + i * 0.1
        )
    # rate_limit=5/min → 第 6 次必拒
    allowed, retry_after = await account_limiter.check_quota(
        redis, "acc1", rate_limit="5/min", daily_quota=None, now=now + 0.5
    )
    assert allowed is False
    assert retry_after > 0.0


@pytest.mark.asyncio
async def test_record_image_call_swallow_redis_errors() -> None:
    """Redis 抖动时 record 不应让主路径失败。"""

    class BrokenRedis:
        async def zadd(self, *_a: Any, **_kw: Any) -> int:
            raise RuntimeError("redis down")

        async def expire(self, *_a: Any, **_kw: Any) -> int:
            raise RuntimeError("redis down")

        async def incr(self, *_a: Any, **_kw: Any) -> int:
            raise RuntimeError("redis down")

        async def expireat(self, *_a: Any, **_kw: Any) -> int:
            raise RuntimeError("redis down")

    # 不应该抛
    await account_limiter.record_image_call(BrokenRedis(), "acc1", task_id="t")


# --- pytest-asyncio 兼容 ----------------------------------------------------

# conftest 里没有显式 asyncio fixture loop，使用 asyncio mode=auto 不可保证；显式
# 提供一个 event_loop fixture 让所有 @pytest.mark.asyncio 都能跑。
@pytest.fixture
def event_loop():  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
