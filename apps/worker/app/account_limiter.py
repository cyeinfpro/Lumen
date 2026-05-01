"""账号级 image 配额管理（Redis 滑动窗口 + UTC 当日计数）。

设计取自 sub2api_lumen_responses_image_optimization.md §Lumen 调度方向：sub2api
把每个 OAuth 账号暴露成独立 API key，Lumen 把这些 key 配成多 provider，每个
provider = 一个账号。account_limiter 提供"该账号还有几次额度"的判定。

策略：
- rate_limit / daily_quota **都为 None 时短路**——直接放行，不查 Redis。先放开
  跑一段时间，等到看清各账号的真实订阅额度，再按账号填具体值。
- 配置了 rate_limit（"5/min" / "50/h" / "200/d"）时用 Redis ZSET 滑动窗口；
  配置了 daily_quota 时用 UTC 当日计数。两者可独立组合或同时启用。
- 任何 Redis 错误都吞掉：quota 是软约束，Redis 抖动不应让生图任务失败。

成功调用时调 record_image_call() 入账；选号阶段调 check_quota() 决定是否跳过。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

# Redis key 模板（约定：lumen:acct:{name}:image:...）
_KEY_TS = "lumen:acct:{name}:image:ts"
_KEY_DAILY = "lumen:acct:{name}:image:daily:{day}"
_TS_TTL_S = 86400 * 2

_CHECK_WINDOW_LUA = """
local key = KEYS[1]
local cutoff = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

redis.call("ZREMRANGEBYSCORE", key, 0, cutoff)
local used = redis.call("ZCARD", key)
if used >= limit then
  local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
  if oldest[2] then
    return {used, oldest[2]}
  end
end
return {used, ""}
"""

_RECORD_IMAGE_CALL_LUA = """
local ts_key = KEYS[1]
local day_key = KEYS[2]
local member = ARGV[1]
local score = tonumber(ARGV[2])
local ts_ttl_s = tonumber(ARGV[3])
local day_expire_at = tonumber(ARGV[4])

redis.call("ZADD", ts_key, score, member)
redis.call("EXPIRE", ts_key, ts_ttl_s)
redis.call("INCR", day_key)
redis.call("EXPIREAT", day_key, day_expire_at)
return 1
"""

_UNIT_SECONDS: dict[str, int] = {
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


def _today_utc_key(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")


def _seconds_until_next_utc_day(now: float) -> float:
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    midnight = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).timestamp()
    return max(1.0, (midnight + 86400.0) - now)


def parse_rate_limit(s: str | None) -> tuple[int, int] | None:
    """Parse "5/min" / "50/h" / "200/d" → (count, window_seconds). None on invalid.

    Note: only integer coefficients are supported (e.g. "5/min" not "0.5/min").
    ``int("0.5")`` would raise ValueError → None, effectively ignoring the limit.
    Decimal coefficients are not currently used in production; if needed, switch
    to ``float(count_s)`` and treat the parsed count as a fractional window permit.
    """
    if not isinstance(s, str) or "/" not in s:
        return None
    count_s, _, unit_s = s.partition("/")
    try:
        count = int(count_s.strip())
    except ValueError:
        return None
    if count <= 0:
        return None
    window = _UNIT_SECONDS.get(unit_s.strip().lower())
    if window is None:
        return None
    return count, window


async def _check_window_fallback(
    redis: Any,
    ts_key: str,
    *,
    cutoff: float,
    count_limit: int,
) -> tuple[int, float | None]:
    try:
        await redis.zremrangebyscore(ts_key, 0, cutoff)
        zcard_raw = await redis.zcard(ts_key)
    except Exception:  # noqa: BLE001
        zcard_raw = 0
    try:
        used = int(zcard_raw or 0)
    except (TypeError, ValueError):
        used = 0
    if used < count_limit:
        return used, None
    try:
        head = await redis.zrange(ts_key, 0, 0, withscores=True)
    except Exception:  # noqa: BLE001
        head = []
    if not head:
        return used, None
    _member, oldest = head[0]
    try:
        return used, float(oldest)
    except (TypeError, ValueError):
        return used, None


async def _check_window(
    redis: Any,
    ts_key: str,
    *,
    cutoff: float,
    count_limit: int,
) -> tuple[int, float | None]:
    eval_fn = getattr(redis, "eval", None)
    if callable(eval_fn):
        try:
            raw = await eval_fn(
                _CHECK_WINDOW_LUA,
                1,
                ts_key,
                str(cutoff),
                str(count_limit),
            )
            used_raw = raw[0] if isinstance(raw, (list, tuple)) and raw else 0
            oldest_raw = (
                raw[1]
                if isinstance(raw, (list, tuple)) and len(raw) > 1
                else None
            )
            used = int(used_raw or 0)
            if oldest_raw in (None, "", b""):
                return used, None
            return used, float(oldest_raw)
        except Exception:  # noqa: BLE001
            # P1: Lua eval 异常（Redis 短暂不可达等）时保守拒绝，返回 (count_limit, None)
            # 即假设窗口已满。这是安全侧策略：宁拒不放。建议加 Prometheus Counter
            # `lumen_account_limiter_lua_eval_errors_total` 监控该路径触发频率，
            # 若频繁触发说明 Redis 需要扩容或切换主从。
            return count_limit, None
    return await _check_window_fallback(
        redis, ts_key, cutoff=cutoff, count_limit=count_limit
    )


async def _record_image_call_fallback(
    redis: Any,
    *,
    ts_key: str,
    day_key: str,
    member: str,
    cur_now: float,
) -> None:
    try:
        await redis.zadd(ts_key, {member: cur_now})
    except Exception:  # noqa: BLE001
        pass
    # ZSET 最多保 2 天，给最长 1d 窗口留缓冲
    try:
        await redis.expire(ts_key, _TS_TTL_S)
    except Exception:  # noqa: BLE001
        pass
    try:
        await redis.incr(day_key)
    except Exception:  # noqa: BLE001
        return
    try:
        await redis.expireat(
            day_key, int(cur_now + _seconds_until_next_utc_day(cur_now))
        )
    except Exception:  # noqa: BLE001
        pass


async def check_quota(
    redis: Any,
    account: str,
    rate_limit: str | None,
    daily_quota: int | None,
    *,
    now: float | None = None,
) -> tuple[bool, float]:
    """检查账号当前是否还能跑一次 image_generation。

    daily_quota 优先级高于 rate_limit：当日配额耗尽时直接拒绝并返回次日恢复时间，
    不检查滑动窗口。

    设计决策：daily_quota 是每日总量配额，rate_limit 是滑动窗口速率限制，
    两者独立。当 daily_quota 耗尽时，返回的 retry_after 指向 UTC 次日零点，
    此时 rate_limit 窗口的恢复时间被忽略——因为日配额优先级更高。如果日配额
    还有余额但 rate_limit 窗口满载，retry_after 才指向窗口恢复时间。

    Returns:
        (allowed, retry_after_s)
        - allowed=True 时 retry_after_s=0.0
        - allowed=False 时 retry_after_s 是"最早可重新可用"的估计秒数（用于
          ProviderHealth.image_rate_limited_until 缓存，避免下次选号再查一遍 Redis）

    rate_limit / daily_quota 都未配置 → 短路 (True, 0.0)，不查 Redis。
    redis=None（测试或启动早期未注入）→ 同样短路，让 limiter 不阻塞主路径。

    P2-5 时间源约定：参数 ``now`` 必须是 wall-clock 秒（``time.time()``），不要混入
    ``time.monotonic()``——daily_quota 是按 UTC 日切换的、滑动窗口的成员也是 wall
    clock 戳，monotonic 不能直接进 ZSET 用 score 算窗口边界。provider_pool 那侧
    显式 cache 了 ``wall_now = time.time()`` 传入，本函数不应再做单位混用。
    """
    parsed = parse_rate_limit(rate_limit)
    has_daily = isinstance(daily_quota, int) and daily_quota > 0
    if parsed is None and not has_daily:
        return True, 0.0
    if redis is None:
        return True, 0.0

    # P2-5: 防御式校验——如果调用方误传 monotonic（小数量级，比如 worker 启动后
    # 几十秒），会被识别为 1970 年附近时间戳，直接退回 wall clock 兜底。判断阈值
    # 取 2001-09-09（10^9）：所有合理 wall_clock 都远超此值。
    cur_now = now if now is not None else time.time()
    if cur_now < 1_000_000_000:
        cur_now = time.time()

    # 1) 当日上限（UTC day）
    if has_daily:
        assert daily_quota is not None  # for type checker
        day_key = _KEY_DAILY.format(name=account, day=_today_utc_key(cur_now))
        try:
            raw = await redis.get(day_key)
        except Exception:  # noqa: BLE001
            raw = None
        used = 0
        if raw is not None:
            try:
                used = int(raw)
            except (TypeError, ValueError):
                used = 0
        if used >= daily_quota:
            return False, _seconds_until_next_utc_day(cur_now)

    # 2) 滑动窗口
    if parsed is not None:
        count_limit, window_s = parsed
        ts_key = _KEY_TS.format(name=account)
        cutoff = cur_now - window_s
        used, oldest = await _check_window(
            redis, ts_key, cutoff=cutoff, count_limit=count_limit
        )
        if used >= count_limit:
            retry_after = float(window_s)
            if oldest is not None:
                try:
                    retry_after = max(1.0, (float(oldest) + window_s) - cur_now)
                except (TypeError, ValueError):
                    pass
            return False, retry_after

    return True, 0.0


async def record_image_call(
    redis: Any,
    account: str,
    *,
    task_id: str = "",
    now: float | None = None,
) -> None:
    """记录一次成功 image_generation 调用。

    入账：ZADD <ts_key> {member: cur_now} + INCR <daily_key>
    member 用 task_id（保证唯一）；没传时退化为 ts:<秒.6>。

    redis=None / 任何 redis 错误 → 静默吞掉：quota 是软约束，Redis 抖动不应让
    主路径失败。
    """
    if redis is None:
        return
    cur_now = now if now is not None else time.time()
    member = task_id or f"ts:{cur_now:.6f}"
    ts_key = _KEY_TS.format(name=account)
    day_key = _KEY_DAILY.format(name=account, day=_today_utc_key(cur_now))
    day_expire_at = int(cur_now + _seconds_until_next_utc_day(cur_now))
    eval_fn = getattr(redis, "eval", None)
    if callable(eval_fn):
        try:
            await eval_fn(
                _RECORD_IMAGE_CALL_LUA,
                2,
                ts_key,
                day_key,
                member,
                str(cur_now),
                str(_TS_TTL_S),
                str(day_expire_at),
            )
            return
        except Exception:  # noqa: BLE001
            return
    await _record_image_call_fallback(
        redis,
        ts_key=ts_key,
        day_key=day_key,
        member=member,
        cur_now=cur_now,
    )


__all__ = [
    "parse_rate_limit",
    "check_quota",
    "record_image_call",
]
