"""ProviderPool image route 调度测试。

部署形态：sub2api 一号一 key，Lumen 把这些 key 配成多 provider，每个 provider
对应一个 OpenAI 账号。这套测试覆盖账号级调度的关键不变量：
- select(route="image") / route="image_jobs" 都返回可用账号；image_jobs 能力由 dispatch 层判定
- 一个号 429 / quota → image_rate_limited_until 设置 → 下次 select 跳过该号
- 一个号普通失败 3 次 → image_cooldown_until 设置 → 下次 select 跳过
- terminal 错误（外层不调 report_image_*）不影响 image 健康
- 全部不可用 → 抛 all_accounts_failed
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.provider_pool import ProviderConfig, ProviderHealth, ProviderPool


def _make_pool(*configs: ProviderConfig) -> ProviderPool:
    """构造 pool 并直接灌入 providers / health，跳过 _maybe_reload 的配置校验。"""
    pool = ProviderPool()
    pool._providers = list(configs)
    pool._health = {p.name: ProviderHealth() for p in configs}
    # 让 _maybe_reload 直接 no-op（已加载）
    pool._config_loaded_at = time.monotonic() + 60.0
    return pool


def _cfg(
    name: str,
    *,
    rate_limit: str | None = None,
    daily_quota: int | None = None,
    priority: int = 0,
    image_jobs_enabled: bool = False,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        base_url=f"https://{name}.example",
        api_key=f"sk-{name}",
        priority=priority,
        weight=1,
        enabled=True,
        image_rate_limit=rate_limit,
        image_daily_quota=daily_quota,
        image_jobs_enabled=image_jobs_enabled,
    )


# --- 基础选号 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_image_returns_all_enabled_providers_when_fresh() -> None:
    pool = _make_pool(_cfg("acc1"), _cfg("acc2"), _cfg("acc3"))
    providers = await pool.select(route="image")
    assert {p.name for p in providers} == {"acc1", "acc2", "acc3"}


@pytest.mark.asyncio
async def test_select_image_jobs_no_longer_filters_marked_providers() -> None:
    pool = _make_pool(
        _cfg("plain"),
        _cfg("async-a", image_jobs_enabled=True),
        _cfg("async-b", image_jobs_enabled=True),
    )

    image_providers = await pool.select(route="image")
    async_providers = await pool.select(route="image_jobs")

    assert {p.name for p in image_providers} == {"plain", "async-a", "async-b"}
    assert {p.name for p in async_providers} == {"plain", "async-a", "async-b"}
    assert {
        p.name for p in async_providers if p.image_jobs_enabled
    } == {"async-a", "async-b"}


@pytest.mark.asyncio
async def test_select_image_jobs_keeps_plain_providers_for_dispatch_layer() -> None:
    pool = _make_pool(_cfg("plain-a"), _cfg("plain-b"))

    providers = await pool.select(route="image_jobs")

    assert {p.name for p in providers} == {"plain-a", "plain-b"}
    assert all(not p.image_jobs_enabled for p in providers)


@pytest.mark.asyncio
async def test_select_image_orders_by_image_last_used_at_ascending() -> None:
    pool = _make_pool(_cfg("acc1"), _cfg("acc2"), _cfg("acc3"))
    now = time.monotonic()
    # 排序按 endpoint_kind 维度查 image_last_used_at_per_ek；下面 select 不传
    # endpoint_kind，dict key 用 ""（聚合）。
    pool._health["acc1"].image_last_used_at_per_ek[""] = now - 10.0  # 最近用过
    pool._health["acc2"].image_last_used_at_per_ek[""] = now - 100.0  # 较早
    # acc3 从未用过 → key 不存在 → -inf 排最前

    providers = await pool.select(route="image", acquire_inflight=False)
    # 顺序：从未用过 → 较早 → 最近
    assert [p.name for p in providers] == ["acc3", "acc2", "acc1"]


# --- image cooldown ----------------------------------------------------------


@pytest.mark.asyncio
async def test_image_cooldown_sets_after_three_failures() -> None:
    pool = _make_pool(_cfg("acc1"), _cfg("acc2"))
    pool.report_image_failure("acc1")
    pool.report_image_failure("acc1")
    # 还没到阈值
    assert pool._health["acc1"].image_cooldown_until is None
    pool.report_image_failure("acc1")
    # 第 3 次触发 cooldown
    assert pool._health["acc1"].image_cooldown_until is not None
    assert pool._health["acc1"].image_cooldown_until > time.monotonic()


@pytest.mark.asyncio
async def test_select_image_skips_provider_in_image_cooldown() -> None:
    pool = _make_pool(_cfg("acc1"), _cfg("acc2"))
    # 强制 acc1 进 image cooldown
    pool._health["acc1"].image_cooldown_until = time.monotonic() + 60.0
    providers = await pool.select(route="image")
    assert [p.name for p in providers] == ["acc2"]


@pytest.mark.asyncio
async def test_select_image_ignore_cooldown_returns_cooled_providers() -> None:
    """ignore_cooldown=True：单任务遍历场景下，image_cooldown / image_rate_limited
    中的号也返回，让任务把所有 enabled 账号都试一遍。"""
    pool = _make_pool(_cfg("acc1"), _cfg("acc2"), _cfg("acc3"))
    now = time.monotonic()
    pool._health["acc1"].image_cooldown_until = now + 60.0
    pool._health["acc2"].image_rate_limited_until = now + 60.0
    # 默认行为：两个号都被过滤
    providers = await pool.select(route="image")
    assert [p.name for p in providers] == ["acc3"]
    # ignore_cooldown=True：三个号都返回
    providers = await pool.select(route="image", ignore_cooldown=True)
    assert {p.name for p in providers} == {"acc1", "acc2", "acc3"}


@pytest.mark.asyncio
async def test_select_image_ignore_cooldown_still_skips_text_circuit() -> None:
    """ignore_cooldown 只放过 image cooldown / rate_limited；text circuit_open
    是硬故障（auth/网络），即便 ignore_cooldown=True 也要跳过。"""
    pool = _make_pool(_cfg("acc1"), _cfg("acc2"))
    h = pool._health["acc1"]
    h.consecutive_failures = 5
    h.cooldown_until = time.monotonic() + 60.0
    providers = await pool.select(route="image", ignore_cooldown=True)
    assert [p.name for p in providers] == ["acc2"]


@pytest.mark.asyncio
async def test_image_success_resets_consecutive_failures() -> None:
    pool = _make_pool(_cfg("acc1"))
    pool.report_image_failure("acc1")
    pool.report_image_failure("acc1")
    pool.report_image_success("acc1")
    h = pool._health["acc1"]
    assert h.image_consecutive_failures == 0
    assert h.image_cooldown_until is None
    assert h.image_last_used_at is not None


# --- image rate limited ------------------------------------------------------


@pytest.mark.asyncio
async def test_report_image_rate_limited_with_explicit_retry_after() -> None:
    pool = _make_pool(_cfg("acc1"))
    pool.report_image_rate_limited("acc1", retry_after_s=42.0)
    h = pool._health["acc1"]
    assert h.image_rate_limited_until is not None
    assert h.image_rate_limited_until - time.monotonic() == pytest.approx(
        42.0, abs=1.0
    )
    # rate_limited 不计入 image_consecutive_failures（"号没额度" 不是 "号坏了"）
    assert h.image_consecutive_failures == 0


@pytest.mark.asyncio
async def test_report_image_rate_limited_default_when_no_retry_after() -> None:
    pool = _make_pool(_cfg("acc1"))
    pool.report_image_rate_limited("acc1", retry_after_s=None)
    h = pool._health["acc1"]
    # 默认 60s
    assert h.image_rate_limited_until is not None
    assert h.image_rate_limited_until - time.monotonic() == pytest.approx(
        60.0, abs=2.0
    )


@pytest.mark.asyncio
async def test_select_image_skips_provider_in_rate_limited() -> None:
    pool = _make_pool(_cfg("acc1"), _cfg("acc2"))
    pool._health["acc1"].image_rate_limited_until = time.monotonic() + 30.0
    providers = await pool.select(route="image")
    assert [p.name for p in providers] == ["acc2"]


# --- text circuit 影响 image 选号 -------------------------------------------


@pytest.mark.asyncio
async def test_select_image_skips_provider_with_open_text_circuit() -> None:
    """text route 熔断意味着该号基础健康都不行，image route 也跳过。"""
    pool = _make_pool(_cfg("acc1"), _cfg("acc2"))
    # 模拟 text circuit open
    h = pool._health["acc1"]
    h.consecutive_failures = 5
    h.cooldown_until = time.monotonic() + 60.0
    providers = await pool.select(route="image")
    assert [p.name for p in providers] == ["acc2"]


# --- 全部不可用 → all_accounts_failed ---------------------------------------


@pytest.mark.asyncio
async def test_select_image_raises_when_all_unavailable() -> None:
    from app.upstream import UpstreamError

    pool = _make_pool(_cfg("acc1"), _cfg("acc2"))
    pool._health["acc1"].image_cooldown_until = time.monotonic() + 60.0
    pool._health["acc2"].image_rate_limited_until = time.monotonic() + 60.0
    with pytest.raises(UpstreamError) as exc_info:
        await pool.select(route="image")
    assert exc_info.value.error_code == "all_accounts_failed"
    # payload 带 skip 原因，便于线上诊断
    assert "skipped" in exc_info.value.payload


# --- quota 用满 → 跳过该号 + 缓存到 image_rate_limited_until ----------------


@pytest.mark.asyncio
async def test_select_image_skips_when_redis_quota_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import account_limiter

    pool = _make_pool(
        _cfg("acc1", rate_limit="5/min"),
        _cfg("acc2", rate_limit="5/min"),
    )
    # 注入"假"redis（不需要真的用）；check_quota 我们整个 monkey-patch
    pool.attach_redis(object())

    async def fake_check_quota(
        _redis: Any, name: str, _rl: str | None, _dq: int | None, **_kw: Any
    ) -> tuple[bool, float]:
        if name == "acc1":
            return False, 25.0
        return True, 0.0

    monkeypatch.setattr(account_limiter, "check_quota", fake_check_quota)

    providers = await pool.select(route="image")
    assert [p.name for p in providers] == ["acc2"]
    # acc1 被缓存到 image_rate_limited_until，避免下次再查 Redis
    h_acc1 = pool._health["acc1"]
    assert h_acc1.image_rate_limited_until is not None


@pytest.mark.asyncio
async def test_select_image_fail_open_when_limiter_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_quota 抛意外错时按"放开"处理：quota 是软约束，不应阻塞 image 主路径。"""
    from app import account_limiter

    pool = _make_pool(_cfg("acc1", rate_limit="5/min"))
    pool.attach_redis(object())

    async def boom_check_quota(*_a: Any, **_kw: Any) -> tuple[bool, float]:
        raise RuntimeError("redis exploded")

    monkeypatch.setattr(account_limiter, "check_quota", boom_check_quota)

    providers = await pool.select(route="image")
    # 应该仍能返回 acc1（fail-open）
    assert [p.name for p in providers] == ["acc1"]


# --- get_status 暴露 image 状态 ---------------------------------------------


@pytest.mark.asyncio
async def test_get_status_exposes_image_state() -> None:
    pool = _make_pool(
        _cfg("acc1", rate_limit="5/min", daily_quota=80),
        _cfg("acc2"),
    )
    pool._health["acc2"].image_rate_limited_until = time.monotonic() + 30.0

    status = pool.get_status()
    by_name = {s["name"]: s for s in status}
    assert by_name["acc1"]["image"]["state"] == "closed"
    assert by_name["acc1"]["image"]["rate_limit"] == "5/min"
    assert by_name["acc1"]["image"]["daily_quota"] == 80
    assert by_name["acc2"]["image"]["state"] == "rate_limited"
    assert by_name["acc2"]["image"]["rate_limited_remaining_s"] > 0


# --- attach_redis 幂等 + 短路 ------------------------------------------------


@pytest.mark.asyncio
async def test_attach_redis_is_idempotent() -> None:
    pool = _make_pool(_cfg("acc1"))
    r1 = object()
    r2 = object()
    pool.attach_redis(r1)
    assert pool.get_redis() is r1
    pool.attach_redis(r2)
    assert pool.get_redis() is r2  # 后注入覆盖


@pytest.mark.asyncio
async def test_flush_stats_exchanges_counters_before_pipeline_operations() -> None:
    pool = _make_pool(_cfg("acc1"))
    pool.report_success("acc1")

    class RacingPipeline:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []
            self.injected = False

        def hincrby(self, key: str, field: str, amount: int) -> None:
            self.calls.append((key, field, amount))
            if not self.injected:
                self.injected = True
                pool.report_success("acc1")

        async def execute(self) -> None:
            return None

    class RacingRedis:
        def __init__(self) -> None:
            self.pipe = RacingPipeline()

        def pipeline(self, *, transaction: bool = False) -> RacingPipeline:
            assert transaction is False
            return self.pipe

    redis = RacingRedis()
    await pool.flush_stats_to_redis(redis)

    h = pool._health["acc1"]
    assert redis.pipe.calls == [
        ("lumen:provider_stats:acc1", "total", 1),
        ("lumen:provider_stats:acc1", "success", 1),
        ("lumen:provider_stats:acc1", "fail", 0),
    ]
    assert h.total_requests == 1
    assert h.successful_requests == 1
    assert h.failed_requests == 0


@pytest.mark.asyncio
async def test_flush_stats_restores_counters_when_redis_execute_fails() -> None:
    pool = _make_pool(_cfg("acc1"))
    pool.report_success("acc1")

    class FailingPipeline:
        def hincrby(self, _key: str, _field: str, _amount: int) -> None:
            return None

        async def execute(self) -> None:
            raise RuntimeError("redis unavailable")

    class FailingRedis:
        def pipeline(self, *, transaction: bool = False) -> FailingPipeline:
            assert transaction is False
            return FailingPipeline()

    await pool.flush_stats_to_redis(FailingRedis())

    h = pool._health["acc1"]
    assert h.total_requests == 1
    assert h.successful_requests == 1
    assert h.failed_requests == 0


# --- _is_image_rate_limit_error 分类 -----------------------------------------


@pytest.mark.parametrize(
    "exc_kwargs,expected_is_rl,expected_retry_after",
    [
        # HTTP 429 直接命中
        (
            {"status_code": 429, "error_code": "rate_limit_error"},
            True,
            None,
        ),
        # error_code=rate_limit_exceeded
        (
            {"status_code": 200, "error_code": "rate_limit_exceeded"},
            True,
            None,
        ),
        # message 含 quota
        (
            {
                "status_code": 200,
                "error_code": "server_error",
                "message_in": "you have exceeded your image quota",
            },
            True,
            None,
        ),
        # message 含 "concurrency limit exceeded"
        (
            {
                "status_code": 200,
                "error_code": "server_error",
                "message_in": "concurrency limit exceeded",
            },
            True,
            None,
        ),
        # 普通 5xx 不算 rate limit
        (
            {"status_code": 502, "error_code": "upstream_error"},
            False,
            None,
        ),
        # SSE 拒图 / policy 不算 rate limit
        (
            {"status_code": 200, "error_code": "moderation_blocked"},
            False,
            None,
        ),
        # 带 retry_after 的 429
        (
            {
                "status_code": 429,
                "error_code": "rate_limit_error",
                "payload": {"error": {"retry_after": 23.5}},
            },
            True,
            23.5,
        ),
    ],
)
def test_is_image_rate_limit_error_classification(
    exc_kwargs: dict[str, Any],
    expected_is_rl: bool,
    expected_retry_after: float | None,
) -> None:
    from app.upstream import UpstreamError, _is_image_rate_limit_error

    msg = exc_kwargs.pop("message_in", "boom")
    exc = UpstreamError(msg, **exc_kwargs)
    is_rl, retry_after = _is_image_rate_limit_error(exc)
    assert is_rl is expected_is_rl
    if expected_retry_after is not None:
        assert retry_after == expected_retry_after


# --- Prometheus metric ------------------------------------------------------


def _counter_value(metric: Any, labels: dict[str, str]) -> float:
    """读 prometheus_client Counter 指定 label 的当前值。"""
    sample = metric.labels(**labels)
    return float(sample._value.get())


def _gauge_value(metric: Any, labels: dict[str, str]) -> float:
    sample = metric.labels(**labels)
    return float(sample._value.get())


@pytest.mark.asyncio
async def test_report_image_calls_increments_counter() -> None:
    from app.observability import account_image_calls_total

    pool = _make_pool(_cfg("acc_metric_a"), _cfg("acc_metric_b"))

    before_success = _counter_value(
        account_image_calls_total,
        {"account": "acc_metric_a", "outcome": "success"},
    )
    before_failure = _counter_value(
        account_image_calls_total,
        {"account": "acc_metric_a", "outcome": "failure"},
    )
    before_rl = _counter_value(
        account_image_calls_total,
        {"account": "acc_metric_b", "outcome": "rate_limited"},
    )

    pool.report_image_success("acc_metric_a")
    pool.report_image_failure("acc_metric_a")
    pool.report_image_rate_limited("acc_metric_b", retry_after_s=10.0)

    assert (
        _counter_value(
            account_image_calls_total,
            {"account": "acc_metric_a", "outcome": "success"},
        )
        == before_success + 1.0
    )
    assert (
        _counter_value(
            account_image_calls_total,
            {"account": "acc_metric_a", "outcome": "failure"},
        )
        == before_failure + 1.0
    )
    assert (
        _counter_value(
            account_image_calls_total,
            {"account": "acc_metric_b", "outcome": "rate_limited"},
        )
        == before_rl + 1.0
    )


@pytest.mark.asyncio
async def test_flush_image_metrics_sets_state_gauges() -> None:
    from app.observability import account_image_state

    pool = _make_pool(
        _cfg("acc_state_x"), _cfg("acc_state_y"), _cfg("acc_state_z")
    )
    # acc_state_x: closed
    # acc_state_y: cooldown
    # acc_state_z: rate_limited
    now = time.monotonic()
    pool._health["acc_state_y"].image_cooldown_until = now + 60.0
    pool._health["acc_state_z"].image_rate_limited_until = now + 30.0

    await pool.flush_image_metrics()

    # acc_state_x 应当 closed=1，其余为 0
    assert _gauge_value(
        account_image_state, {"account": "acc_state_x", "state": "closed"}
    ) == 1.0
    assert _gauge_value(
        account_image_state, {"account": "acc_state_x", "state": "cooldown"}
    ) == 0.0
    assert _gauge_value(
        account_image_state, {"account": "acc_state_x", "state": "rate_limited"}
    ) == 0.0
    # acc_state_y cooldown=1
    assert _gauge_value(
        account_image_state, {"account": "acc_state_y", "state": "cooldown"}
    ) == 1.0
    # acc_state_z rate_limited=1
    assert _gauge_value(
        account_image_state, {"account": "acc_state_z", "state": "rate_limited"}
    ) == 1.0


@pytest.mark.asyncio
async def test_flush_image_metrics_reads_quota_from_redis() -> None:
    """配置了 rate_limit 时，quota_used gauge 应反映 Redis ZCARD / daily counter。"""
    from app import account_limiter
    from app.observability import account_image_quota_used

    # 用 test_account_limiter 的 FakeRedis
    from tests.test_account_limiter import FakeRedis  # type: ignore[import-not-found]

    pool = _make_pool(_cfg("acc_quota", rate_limit="5/min", daily_quota=80))
    redis = FakeRedis()
    pool.attach_redis(redis)

    now = time.time()
    # 预填 3 条戳（窗口 60s 内）+ daily=12
    for i in range(3):
        await redis.zadd("lumen:acct:acc_quota:image:ts", {f"t{i}": now - 5 - i})
    redis.kv[
        f"lumen:acct:acc_quota:image:daily:"
        f"{account_limiter._today_utc_key(now)}"
    ] = "12"

    await pool.flush_image_metrics()

    assert _gauge_value(
        account_image_quota_used,
        {"account": "acc_quota", "window": "current_window"},
    ) == 3.0
    assert _gauge_value(
        account_image_quota_used,
        {"account": "acc_quota", "window": "daily"},
    ) == 12.0


@pytest.mark.asyncio
async def test_flush_image_metrics_no_redis_only_sets_state() -> None:
    """没注入 redis 时不应抛错，只刷 state gauge。"""
    pool = _make_pool(_cfg("acc_no_redis"))
    pool._redis = None
    # 不应抛
    await pool.flush_image_metrics()


# --- inflight 软占座 + 按 endpoint_kind 分散 ---------------------------------


@pytest.mark.asyncio
async def test_concurrent_select_distributes_across_providers() -> None:
    """并发 select 不应全压在同一个号上——这是 dual_race 雪崩的修复回归测试。

    场景：6 个并发请求几乎同时进 select。修复前所有请求看到相同的 last_used_at 排序
    都会拿到候选 #1（A）；修复后 select 在 return 前 incr inflight 软占座，紧接着
    发起 select 的请求看到 A 的 inflight=1 自然落到 B。
    """
    pool = _make_pool(_cfg("A"), _cfg("B"), _cfg("C"))

    chosen: list[str] = []

    async def use_one() -> None:
        providers = await pool.select(route="image", endpoint_kind="responses")
        first = providers[0]
        chosen.append(first.name)
        # 模拟"实际发请求"——给一段持有时间，让其他并发 select 看到 inflight 状态
        await asyncio.sleep(0.005)
        pool.release_image_inflight(first.name, "responses")

    await asyncio.gather(*(use_one() for _ in range(6)))

    counts: dict[str, int] = {}
    for name in chosen:
        counts[name] = counts.get(name, 0) + 1
    # 所有 3 个 provider 都被使用过
    assert set(counts.keys()) == {"A", "B", "C"}
    # 且分布合理（最多 3 次落在同一个号；6 个请求理想分布是 2/2/2）
    assert max(counts.values()) <= 3


@pytest.mark.asyncio
async def test_select_acquire_inflight_false_does_not_increment() -> None:
    """reserve 路径走 acquire_inflight=False，不应留下软占座状态。"""
    pool = _make_pool(_cfg("A"), _cfg("B"))

    providers = await pool.select(
        route="image", endpoint_kind="responses", acquire_inflight=False
    )
    first = providers[0]
    # 没 acquire 过，第二次 select 看到的 inflight 仍然 0，排序应该一致
    again = await pool.select(
        route="image", endpoint_kind="responses", acquire_inflight=False
    )
    assert again[0].name == first.name


@pytest.mark.asyncio
async def test_inflight_dimension_is_per_endpoint_kind() -> None:
    """一个号在 responses lane 被 acquire 不应污染 generations lane 排序。"""
    pool = _make_pool(_cfg("A"), _cfg("B"))

    # responses lane 占住 A
    pool.acquire_image_inflight("A", "responses")
    try:
        # generations lane 不受 A 在 responses 维度的占用影响
        providers = await pool.select(
            route="image", endpoint_kind="generations", acquire_inflight=False
        )
        # A 在 generations 维度 inflight=0，priority/last_used 都和 B 一样 → A 仍排前
        assert providers[0].name == "A"

        # 但 responses lane 看 A 已被占，应该拿到 B
        responses_view = await pool.select(
            route="image", endpoint_kind="responses", acquire_inflight=False
        )
        assert responses_view[0].name == "B"
    finally:
        pool.release_image_inflight("A", "responses")


@pytest.mark.asyncio
async def test_release_image_inflight_floor_at_zero() -> None:
    """release 多于 acquire 不应让计数变负数（保护下界）。"""
    pool = _make_pool(_cfg("A"))
    pool.acquire_image_inflight("A", "responses")
    pool.release_image_inflight("A", "responses")
    pool.release_image_inflight("A", "responses")  # 多余的 release
    pool.release_image_inflight("A", "responses")  # 还有
    # 不应抛；下次 acquire 行为正常
    pool.acquire_image_inflight("A", "responses")
    h = pool._health["A"]
    assert h.image_inflight.get("responses", 0) == 1


@pytest.mark.asyncio
async def test_report_image_success_updates_per_endpoint_last_used_at() -> None:
    """report_image_success(endpoint_kind=...) 只更新对应维度，不污染其他 lane 排序。"""
    pool = _make_pool(_cfg("A"), _cfg("B"))

    # A 在 responses lane 成功一次
    pool.report_image_success("A", endpoint_kind="responses")

    # generations lane 上 A 的 last_used 应该仍然 None（A 在 generations 上从没成功过）
    h_a = pool._health["A"]
    assert h_a.image_last_used_at_per_ek.get("responses") is not None
    assert h_a.image_last_used_at_per_ek.get("generations") is None

    # 因此 generations lane 的排序里 A 仍然是"从未用过"，sort key = -inf 排前面
    providers = await pool.select(
        route="image", endpoint_kind="generations", acquire_inflight=False
    )
    # A 和 B 在 generations 维度 sort key 都是 (0, -inf, ...)，但既然 last_used 相同
    # 应该至少能保证 A 没被推到 B 后面（不被 responses 成功污染）
    # 如果污染了，A 的 sort_key 会是 (0, success_time, ...) > B 的 (0, -inf, ...)
    # 此时 B 会排前——这种情况是修复前的 bug。
    a_idx = next(i for i, p in enumerate(providers) if p.name == "A")
    b_idx = next(i for i, p in enumerate(providers) if p.name == "B")
    # A 不应被 responses lane 的成功推后
    assert a_idx <= b_idx, (
        "A 在 generations lane 不应被它在 responses lane 的成功污染排序"
    )


# --- pytest-asyncio event_loop ----------------------------------------------


@pytest.fixture
def event_loop():  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
