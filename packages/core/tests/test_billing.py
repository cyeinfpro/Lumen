import asyncio
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import CheckConstraint

from lumen_core import billing
from lumen_core.billing_cache import BillingCacheService, MAX_WINDOW_INCREMENT_MICRO
from lumen_core.models import UserWallet
from lumen_core.pricing import (
    CostBreakdown,
    ModelPricing,
    UsageTokens,
    build_request_fingerprint,
)


def test_rmb_micro_conversion_is_decimal_safe():
    assert billing.rmb_to_micro("0.005") == 5_000
    assert billing.rmb_to_micro("12.345678") == 12_345_678
    assert billing.micro_to_rmb_str(12_345_678) == "12.345678"


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_rmb_micro_conversion_rejects_non_finite_values(raw: str):
    with pytest.raises(billing.BillingError):
        billing.rmb_to_micro(raw)


def test_rmb_micro_conversion_warns_on_dropped_precision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """低于 µRMB 的位数只能取整，但不能静默取整。

    F-21：运营把单价填成 ``0.0000004`` 会得到 0 micro（这个模型从此免费），
    填成 ``1.9999995`` 会被抬到 2.0。金额本身误差极小（上界 0.5 µRMB），
    真正的问题是无声无息 —— 所以保留取整、补一条 warning 把它暴露出来。
    """
    with caplog.at_level("WARNING", logger="lumen_core.billing"):
        assert billing.rmb_to_micro("0.0000004") == 0
    assert any("dropped sub-micro precision" in r.message for r in caplog.records)


def test_rmb_micro_conversion_is_silent_when_exact(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """精度够用时不能刷告警，否则日志噪音会把真问题淹掉。"""
    with caplog.at_level("WARNING", logger="lumen_core.billing"):
        assert billing.rmb_to_micro("12.345678") == 12_345_678
        assert billing.rmb_to_micro("100") == 100_000_000
    assert not [r for r in caplog.records if "dropped sub-micro" in r.message]


def test_rate_multiplier_conversion_avoids_float_truncation() -> None:
    """倍率换算必须走 Decimal：float 中转会把四位小数少算一档。

    1.0009 用 float 表示是 1.00089999...，``int(float(raw) * 10_000)``
    截断成 10008，比准确的 10009 少一档；倍率越接近这类边界，
    平台就越是在替用户垫付那 0.0001 的差价。
    """
    assert int(float("1.0009") * 10_000) == 10_008  # 旧实现的错值，作为对照
    assert billing.parse_rate_multiplier_x10000("1.0009") == 10_009
    assert billing.parse_rate_multiplier_x10000(Decimal("1.0009")) == 10_009
    assert billing.parse_rate_multiplier_x10000(1.0009) == 10_009


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 10_000),
        (1, 10_000),
        ("1.0000", 10_000),
        (Decimal("0"), 0),  # 显式 0 = 运营配置的免费账号，保留
        (Decimal("2.5"), 25_000),
        (Decimal("0.0001"), 1),
        (Decimal("9999.9999"), 99_999_999),  # Numeric(8,4) 的上界，合法
        ("nonsense", 10_000),  # 解析失败退回 1.0，不静默变成 0 折
        ("NaN", 10_000),
        ("Infinity", 10_000),
    ],
)
def test_rate_multiplier_conversion_edge_values(raw: Any, expected: int) -> None:
    assert billing.parse_rate_multiplier_x10000(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        Decimal("-1"),
        Decimal("-0.0001"),
        "-2.5",
        -1.0,
        Decimal("10000"),  # 超出 Numeric(8,4) 的 9999.9999 上界
        Decimal("1E+9"),
    ],
)
def test_rate_multiplier_out_of_domain_falls_back_to_full_price(
    raw: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """越界倍率必须退回 1.0（原价），绝不能夹到 0 变成永久免费。

    F-11：旧实现用 ``max(0, ...)`` 把负倍率夹成 0。一条脏数据（迁移遗留、
    直连改库）就能让该账号所有生成都算成 0 元 —— 上游照扣，平台全额吸收，
    正是「纯转嫁」明令禁止的方向。非法输入的正确落点是「按原价收」，
    与解析失败 / NaN / Infinity 的处理保持一致，并且必须留下告警。
    """
    with caplog.at_level("WARNING", logger="lumen_core.billing"):
        assert billing.parse_rate_multiplier_x10000(raw) == 10_000
    assert any("rate multiplier out of range" in r.message for r in caplog.records)


def test_rate_multiplier_conversion_never_undercharges_over_column_domain() -> None:
    """遍历 Numeric(8,4) 的四位小数域，换算值必须始终 >= 精确值。"""
    for step in range(0, 30_000, 137):
        raw = Decimal(step).scaleb(-4)  # 0.0000 ~ 2.9999，步进覆盖各种尾数
        exact = raw * Decimal(10_000)
        assert Decimal(billing.parse_rate_multiplier_x10000(str(raw))) >= exact


def test_image_tier_thresholds_pick_largest_lower_bound():
    thresholds = {"1k": 100, "2k": 200, "4k": 400}
    assert billing.tier_for_pixels(99, thresholds) == "1k"
    assert billing.tier_for_pixels(200, thresholds) == "2k"
    assert billing.tier_for_pixels(999, thresholds) == "4k"


def test_parse_thresholds_keeps_custom_tiers():
    thresholds = billing.parse_thresholds(
        '{"1k": 100, "2k": 200, "4k": 400, "8k": 800}'
    )
    assert thresholds["8k"] == 800
    assert billing.tier_for_pixels(900, thresholds) == "8k"


def test_completion_breakdown_uses_captured_pricing_snapshot() -> None:
    snapshot = ModelPricing(
        input_per_1k_micro=1_000,
        output_per_1k_micro=2_000,
        pricing_source="db",
    ).model_dump()

    breakdown = billing.completion_breakdown_from_snapshot(
        snapshot,
        model="gpt-test",
        tokens=UsageTokens(input_tokens=100, output_tokens=50),
    )

    assert breakdown.input_cost_micro == 100
    assert breakdown.output_cost_micro == 100
    assert breakdown.actual_cost_micro == 200
    assert breakdown.pricing_source == "snapshot"


def test_completion_breakdown_allows_zero_rate_multiplier() -> None:
    snapshot = ModelPricing(
        input_per_1k_micro=1_000,
        output_per_1k_micro=2_000,
        pricing_source="db",
    ).model_dump()

    breakdown = billing.completion_breakdown_from_snapshot(
        snapshot,
        model="gpt-test",
        tokens=UsageTokens(input_tokens=100, output_tokens=50),
        rate_multiplier_x10000=0,
    )

    assert breakdown.total_cost_micro == 200
    assert breakdown.actual_cost_micro == 0
    assert breakdown.rate_multiplier_x10000 == 0


def test_completion_breakdown_rejects_incomplete_snapshot() -> None:
    with pytest.raises(billing.BillingError) as exc_info:
        billing.completion_breakdown_from_snapshot(
            {"input_per_1k_micro": 1_000},
            model="gpt-test",
            tokens=UsageTokens(input_tokens=1, output_tokens=1),
        )

    assert exc_info.value.code == "PRICING_SNAPSHOT_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize("unit_price", [None, 0])
async def test_estimate_image_cost_fails_closed_without_positive_pricing(
    monkeypatch: pytest.MonkeyPatch,
    unit_price: int | None,
):
    async def fake_price(*_args: Any, **_kwargs: Any) -> int | None:
        return unit_price

    monkeypatch.setattr(billing, "pricing_price_micro", fake_price)

    with pytest.raises(billing.BillingError) as exc:
        await billing.estimate_image_cost(
            object(),  # type: ignore[arg-type]
            size_px=1024,
            thresholds={"1k": 1024},
            n=1,
        )

    assert exc.value.code == "PRICING_MISSING"
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_estimate_image_cost_for_tier_fails_closed_for_zero_pricing(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_price(*_args: Any, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(billing, "pricing_price_micro", fake_price)

    with pytest.raises(billing.BillingError) as exc:
        await billing.estimate_image_cost_for_tier(
            object(),  # type: ignore[arg-type]
            tier="1k",
            n=1,
        )

    assert exc.value.code == "PRICING_MISSING"


@pytest.mark.asyncio
async def test_estimate_completion_breakdown_fails_closed_for_missing_rates() -> None:
    class Resolver:
        async def resolve(self, *_args: Any, **_kwargs: Any) -> ModelPricing:
            return ModelPricing(pricing_source="missing")

    with pytest.raises(billing.BillingError) as exc:
        await billing.estimate_completion_breakdown(
            object(),  # type: ignore[arg-type]
            model="unpriced-model",
            tokens=UsageTokens(input_tokens=10, output_tokens=5),
            resolver=Resolver(),  # type: ignore[arg-type]
        )

    assert exc.value.code == "PRICING_MISSING"
    assert exc.value.status_code == 503


def test_parse_thresholds_logs_invalid_json(caplog: pytest.LogCaptureFixture):
    with caplog.at_level("WARNING", logger="lumen_core.billing"):
        thresholds = billing.parse_thresholds("{not-json")

    assert thresholds == billing.DEFAULT_IMAGE_SIZE_THRESHOLDS
    assert "Invalid billing image size thresholds JSON" in caplog.text


def test_parse_thresholds_rejects_fractional_bool_and_negative_values():
    thresholds = billing.parse_thresholds(
        '{"1k": 1.9, "2k": true, "4k": -1, "8k": 800.5, "16k": 1600}'
    )

    assert thresholds["1k"] == billing.DEFAULT_IMAGE_SIZE_THRESHOLDS["1k"]
    assert thresholds["2k"] == billing.DEFAULT_IMAGE_SIZE_THRESHOLDS["2k"]
    assert thresholds["4k"] == billing.DEFAULT_IMAGE_SIZE_THRESHOLDS["4k"]
    assert "8k" not in thresholds
    assert thresholds["16k"] == 1600


def test_parse_bool_setting_matches_zero_one_runtime_settings():
    assert billing.parse_bool_setting("1") is True
    assert billing.parse_bool_setting("0", default=True) is False
    assert billing.parse_bool_setting("yes") is False
    assert billing.parse_bool_setting("true") is False
    assert billing.parse_bool_setting(None, default=True) is True


def test_retry_billing_refs_use_retry_suffix_only_after_first_attempt():
    assert billing.retry_billing_ref_id("task-1", None) == "task-1"
    assert billing.retry_billing_ref_id("task-1", 0) == "task-1"
    assert billing.retry_billing_ref_id("task-1", "bad") == "task-1"
    assert billing.retry_billing_ref_id("task-1", 2) == "task-1:retry:2"


def test_generation_billing_ref_id_reads_persisted_retry_count():
    generation = SimpleNamespace(id="gen-1", billing_retry_count="2")
    invalid = SimpleNamespace(id="gen-2", billing_retry_count="invalid")

    assert billing.generation_billing_retry_count(generation) == 2
    assert billing.generation_billing_ref_id(generation) == "gen-1:retry:2"
    assert billing.generation_billing_ref_id(invalid) == "gen-2"


def test_completion_billing_ref_id_reads_retry_count_from_upstream_request():
    completion = SimpleNamespace(
        id="comp-1",
        upstream_request={"billing_retry_count": "3"},
    )
    invalid = SimpleNamespace(
        id="comp-2",
        upstream_request={"billing_retry_count": "invalid"},
    )

    assert billing.completion_billing_retry_count(completion) == 3
    assert billing.completion_billing_ref_id(completion) == "comp-1:retry:3"
    assert billing.completion_billing_ref_id(invalid) == "comp-2"


def test_redemption_code_normalization_and_hash_are_dash_tolerant():
    secret = "test-secret"
    code = "LMN-ABCD-EFGH-JK23"
    assert billing.normalize_redemption_code(" lmn abcd-efgh-jk23 ") == "ABCDEFGHJK23"
    assert billing.hash_redemption_code(code, secret) == billing.hash_redemption_code(
        "abcd efgh jk23", secret
    )


def test_wallet_schema_allows_negative_balance_for_graylist_overdraw():
    checks = [
        str(constraint.sqltext)
        for constraint in UserWallet.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert "hold_micro >= 0" in checks
    assert all("balance_micro" not in check for check in checks)


def _breakdown() -> CostBreakdown:
    return CostBreakdown(
        input_cost_micro=10,
        output_cost_micro=20,
        cache_read_cost_micro=0,
        cache_creation_cost_micro=0,
        image_output_cost_micro=0,
        reasoning_cost_micro=0,
        long_context_applied=False,
        priority_tier_applied=False,
        rate_multiplier_x10000=10_000,
        total_cost_micro=30,
        actual_cost_micro=30,
        pricing_source="test",
    )


def test_request_fingerprint_is_scoped_to_request_identity():
    usage = UsageTokens(input_tokens=100, output_tokens=50)
    first = build_request_fingerprint(
        user_id="user-1",
        account_type="user",
        api_key_id=None,
        request_id="completion-1",
        idempotency_key="complete:completion-1",
        model="gpt-5.5",
        service_tier="standard",
        billing_type=0,
        tokens=usage,
        cost=_breakdown(),
    )
    second = build_request_fingerprint(
        user_id="user-1",
        account_type="user",
        api_key_id=None,
        request_id="completion-2",
        idempotency_key="complete:completion-2",
        model="gpt-5.5",
        service_tier="standard",
        billing_type=0,
        tokens=usage,
        cost=_breakdown(),
    )

    assert first != second
    assert first.startswith("v2:")


@pytest.mark.asyncio
async def test_billing_cache_window_increment_uses_atomic_lua():
    class Redis:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def eval(self, *args: Any) -> int:
            self.calls.append(args)
            return 1

        async def hgetall(self, _key: str) -> dict[Any, Any]:
            raise AssertionError("window increment must not use read-then-write")

        def pipeline(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("window increment must not use a pipeline fallback")

    redis = Redis()
    service = BillingCacheService(redis=redis)

    await service._apply_window_increment(  # noqa: SLF001
        "cred-1",
        123,
        {"5h": 500, "1d": 1000, "7d": 2000},
        datetime(2026, 5, 15, tzinfo=timezone.utc),
    )

    assert len(redis.calls) == 1
    (
        script,
        numkeys,
        key,
        _ts,
        amount,
        limit_5h,
        limit_1d,
        limit_7d,
        _expire,
        max_amount,
    ) = redis.calls[0]
    assert "HINCRBY" in script
    assert numkeys == 1
    assert key == "lumen:billing:rl:cred-1"
    assert (amount, limit_5h, limit_1d, limit_7d) == (123, 500, 1000, 2000)
    assert max_amount == MAX_WINDOW_INCREMENT_MICRO


@pytest.mark.asyncio
async def test_billing_cache_set_balance_invalidates_when_write_fails() -> None:
    """缓存写失败必须删掉旧值，不能把结算前的余额继续供出去。

    F-18：以前 set 抛异常就直接吞掉，Redis 里留着**结算前**的高余额且 TTL
    完好。用户刚被扣钱，限额判断却继续按旧余额放行，可以一路超额消费到
    TTL 自然过期。删掉之后 get_balance 会回源数据库拿到真实余额。
    """

    class Redis:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def set(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("redis down")

        async def delete(self, key: str) -> int:
            self.deleted.append(key)
            return 1

    redis = Redis()
    service = BillingCacheService(redis=redis)

    await service.set_balance("user-1", 500)

    assert redis.deleted == ["lumen:billing:balance:user-1"]


@pytest.mark.asyncio
async def test_billing_cache_set_balance_survives_failed_invalidation() -> None:
    """连 delete 都失败（Redis 整体不可用）时只能靠 TTL 兜底，但不得抛给调用方。

    调用点是「账本已经写完，现在同步缓存」——此时抛异常会把一笔已经成功的
    结算事务连带回滚掉，比读到陈旧余额严重得多。
    """

    class Redis:
        async def set(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("redis down")

        async def delete(self, *_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("redis still down")

    service = BillingCacheService(redis=Redis())

    await service.set_balance("user-1", 500)  # 不抛异常即为通过


@pytest.mark.asyncio
async def test_billing_cache_set_balance_does_not_delete_on_success() -> None:
    """正常写入路径不能顺手删 key，否则缓存永远命不中。"""

    class Redis:
        def __init__(self) -> None:
            self.sets: list[tuple[Any, ...]] = []
            self.deleted: list[str] = []

        async def set(self, key: str, value: Any, **_kwargs: Any) -> None:
            self.sets.append((key, value))

        async def delete(self, key: str) -> int:
            self.deleted.append(key)
            return 1

    redis = Redis()
    service = BillingCacheService(redis=redis)

    await service.set_balance("user-1", 500)

    assert redis.sets == [("lumen:billing:balance:user-1", 500)]
    assert redis.deleted == []


@pytest.mark.asyncio
async def test_billing_cache_window_increment_ignores_nonpositive_amounts():
    class Redis:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def eval(self, *args: Any) -> int:
            self.calls.append(args)
            return 1

    redis = Redis()
    service = BillingCacheService(redis=redis)

    await service._apply_window_increment("cred-1", -10)  # noqa: SLF001
    await service.queue_window_increment("cred-1", -10)
    await service.queue_window_increment("cred-1", 0)

    assert redis.calls == []


@pytest.mark.asyncio
async def test_billing_cache_window_increment_rejects_excessive_amounts():
    class Redis:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def eval(self, *args: Any) -> int:
            self.calls.append(args)
            return 1

    redis = Redis()
    service = BillingCacheService(redis=redis)
    too_large = MAX_WINDOW_INCREMENT_MICRO + 1

    await service._apply_window_increment("cred-1", too_large)  # noqa: SLF001
    await service.queue_window_increment("cred-1", too_large)

    assert redis.calls == []


@pytest.mark.asyncio
async def test_billing_cache_window_increment_is_applied_before_returning():
    class Redis:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def eval(self, *args: Any) -> int:
            self.calls.append(args)
            return 1

    redis = Redis()
    service = BillingCacheService(redis=redis)

    await service.increment_window_usage("cred-1", 25, {"5h": 100})
    await service.stop_workers()

    assert len(redis.calls) == 1


@pytest.mark.asyncio
async def test_billing_cache_rate_limits_read_durable_window_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BillingCacheService(redis=None)
    calls: list[tuple[str, str, int]] = []

    async def credential_limits(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"5h": 100, "1d": 0, "7d": 0}

    async def ledger_usage(
        _db: Any,
        key_id: str,
        window: str,
        *,
        limit_micro: int,
        now: datetime | None = None,
    ) -> Any:
        calls.append((key_id, window, limit_micro))
        return SimpleNamespace(
            used_micro=80,
            limit_micro=limit_micro,
            resets_at=now,
        )

    monkeypatch.setattr(service, "credential_limits", credential_limits)
    monkeypatch.setattr(service, "ledger_window_usage", ledger_usage)

    allowed, window, usage = await service.evaluate_rate_limits(
        object(),  # type: ignore[arg-type]
        "cred-1",
        25,
    )

    assert allowed is False
    assert window == "5h"
    assert usage.used_micro == 80
    assert calls == [("cred-1", "5h", 100)]


@pytest.mark.asyncio
async def test_billing_window_ledger_scopes_user_credential_and_ref_type():
    statements: list[str] = []
    earliest = datetime(2026, 7, 11, 10, tzinfo=timezone.utc)

    class Result:
        def one(self) -> tuple[int, datetime]:
            return 75, earliest

    class Db:
        async def execute(self, stmt: Any) -> Result:
            statements.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
            return Result()

    service = BillingCacheService(redis=None)
    out = await service.ledger_window_usage(
        Db(),  # type: ignore[arg-type]
        "cred-1",
        "5h",
        limit_micro=100,
        now=datetime(2026, 7, 11, 12, tzinfo=timezone.utc),
        user_id="user-1",
    )

    assert out.used_micro == 75
    assert out.limit_micro == 100
    assert out.resets_at == earliest.replace(hour=15)
    statement = statements[0]
    assert "JOIN wallet_transactions" in statement
    assert "JOIN user_api_credentials" in statement
    assert "billing_window_usage_events.user_id = 'user-1'" in statement
    assert "wallet_transactions.ref_type = 'completion'" in statement
    assert (
        "wallet_transactions.kind IN ('charge', 'charge_completion', 'settle')"
        in statement
    )
    assert "billing_window_usage_events.created_at <=" in statement


@pytest.mark.asyncio
async def test_billing_cache_window_usage_accepts_bytes_hash_keys():
    started = int(datetime(2026, 5, 15, tzinfo=timezone.utc).timestamp())

    class Redis:
        async def hgetall(self, _key: str) -> dict[Any, Any]:
            return {
                b"usage_5h": b"1200",
                b"limit_5h_micro": b"5000",
                b"window_5h_started_at_unix": str(started).encode("ascii"),
            }

    service = BillingCacheService(redis=Redis())

    out = await service.get_window_usage("cred-1", "5h")

    assert out.used_micro == 1200
    assert out.limit_micro == 5000
    assert out.resets_at == datetime.fromtimestamp(
        started + 5 * 3600,
        tz=timezone.utc,
    )


@pytest.mark.asyncio
async def test_billing_cache_balance_locks_self_clean_after_distinct_users():
    class Result:
        def scalar_one_or_none(self) -> int:
            return 100

    class Session:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    service = BillingCacheService(redis=None)

    for idx in range(100):
        assert await service.get_balance(Session(), f"user-{idx}") == 100  # type: ignore[arg-type]

    assert service._locks == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_billing_cache_balance_lock_is_not_removed_while_waiter_exists():
    service = BillingCacheService(redis=None)
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()
    waiter_entered = asyncio.Event()
    release_waiter = asyncio.Event()

    async def hold_lock() -> None:
        async with service._lock("user-1"):  # noqa: SLF001
            holder_entered.set()
            await release_holder.wait()

    async def wait_for_lock() -> None:
        async with service._lock("user-1"):  # noqa: SLF001
            waiter_entered.set()
            await release_waiter.wait()

    holder_task = asyncio.create_task(hold_lock())
    waiter_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(holder_entered.wait(), timeout=1)

        waiter_task = asyncio.create_task(wait_for_lock())
        await asyncio.sleep(0)

        entry = service._locks["user-1"]  # noqa: SLF001
        assert entry.users == 2
        assert waiter_entered.is_set() is False

        release_holder.set()
        await asyncio.wait_for(waiter_entered.wait(), timeout=1)

        assert service._locks.get("user-1") is entry  # noqa: SLF001
        assert entry.users == 1
    finally:
        release_holder.set()
        release_waiter.set()
        tasks = [holder_task]
        if waiter_task is not None:
            tasks.append(waiter_task)
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=1,
        )

    assert "user-1" not in service._locks  # noqa: SLF001


@pytest.mark.asyncio
async def test_get_wallet_lock_refreshes_existing_identity_map():
    class Result:
        def scalar_one_or_none(self) -> UserWallet:
            return UserWallet(user_id="user-1", balance_micro=100)

    class Session:
        def __init__(self) -> None:
            self.statements: list[Any] = []

        async def execute(self, stmt: Any) -> Result:
            self.statements.append(stmt)
            return Result()

    session = Session()

    wallet = await billing.get_wallet(session, "user-1", lock=True)  # type: ignore[arg-type]

    assert wallet is not None
    assert session.statements[0].get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["hold", "settle", "release", "charge", "adjust", "topup_redeem"],
)
async def test_wallet_mutations_fail_closed_when_wallet_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    async def no_existing_tx(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def missing_wallet(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(billing, "_existing_tx", no_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", missing_wallet)

    with pytest.raises(
        billing.BillingError,
        match="wallet could not be initialized",
    ) as exc:
        match operation:
            case "hold":
                await billing.hold(
                    object(),  # type: ignore[arg-type]
                    "user-1",
                    1,
                    ref_type="generation",
                    ref_id="gen-1",
                    idempotency_key="hold:gen-1",
                )
            case "settle":
                await billing.settle(
                    object(),  # type: ignore[arg-type]
                    "user-1",
                    ref_type="generation",
                    ref_id="gen-1",
                    actual_micro=1,
                    idempotency_key="settle:gen-1",
                )
            case "release":
                await billing.release(
                    object(),  # type: ignore[arg-type]
                    "user-1",
                    ref_type="generation",
                    ref_id="gen-1",
                    idempotency_key="release:gen-1",
                )
            case "charge":
                await billing.charge(
                    object(),  # type: ignore[arg-type]
                    "user-1",
                    1,
                    ref_type="generation",
                    ref_id="gen-1",
                    idempotency_key="charge:gen-1",
                )
            case "adjust":
                await billing.adjust(
                    object(),  # type: ignore[arg-type]
                    "user-1",
                    1,
                    admin_id="admin-1",
                    reason="test",
                    idempotency_key="adjust:user-1",
                )
            case "topup_redeem":
                await billing.topup_redeem(
                    object(),  # type: ignore[arg-type]
                    "user-1",
                    1,
                    usage_id="usage-1",
                    code_id="code-1",
                )
            case _:  # pragma: no cover - parametrization is exhaustive
                raise RuntimeError(f"unsupported operation: {operation}")

    assert exc.value.code == "WALLET_UNAVAILABLE"
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_hold_rechecks_idempotency_after_wallet_lock(
    monkeypatch: pytest.MonkeyPatch,
):
    wallet = SimpleNamespace(balance_micro=1_000, hold_micro=0, version=0)
    existing_tx = SimpleNamespace(id="tx-existing")
    calls = 0

    async def fake_existing_tx(*_args):
        nonlocal calls
        calls += 1
        return None if calls == 1 else existing_tx

    async def fake_get_wallet(*_args, **_kwargs):
        return wallet

    async def fail_insert(*_args, **_kwargs):
        raise AssertionError("duplicate idempotency path must not insert a tx")

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_insert_tx", fail_insert)

    result = await billing.hold(
        object(),  # type: ignore[arg-type]
        "user-1",
        500,
        ref_type="generation",
        ref_id="gen-1",
        idempotency_key="hold:gen-1",
    )

    assert result is existing_tx
    assert calls == 2
    assert wallet.balance_micro == 1_000
    assert wallet.hold_micro == 0
    assert wallet.version == 0


@pytest.mark.asyncio
async def test_hold_rejects_nonpositive_amount(monkeypatch: pytest.MonkeyPatch):
    async def fake_existing_tx(*_args: Any) -> None:
        return None

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)

    with pytest.raises(billing.BillingError) as exc:
        await billing.hold(
            object(),  # type: ignore[arg-type]
            "user-1",
            0,
            ref_type="generation",
            ref_id="gen-1",
            idempotency_key="hold:gen-1",
        )

    assert exc.value.code == "INVALID_AMOUNT"


@pytest.mark.asyncio
async def test_settle_returns_existing_ref_consumption_when_hold_is_gone(
    monkeypatch: pytest.MonkeyPatch,
):
    wallet = SimpleNamespace(
        balance_micro=0, hold_micro=0, lifetime_spend_micro=100, version=4
    )
    consumed_tx = SimpleNamespace(id="settle-existing")

    async def fake_existing_tx(*_args: Any) -> None:
        return None

    async def fake_get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def fake_held_amount(*_args: Any) -> int:
        return 0

    async def fake_ref_consumption(*_args: Any) -> Any:
        return consumed_tx

    async def fail_insert(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("duplicate settle must not mutate or insert")

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_held_amount_for_ref", fake_held_amount)
    monkeypatch.setattr(billing, "_existing_ref_consumption_tx", fake_ref_consumption)
    monkeypatch.setattr(billing, "_insert_tx", fail_insert)

    result = await billing.settle(
        object(),  # type: ignore[arg-type]
        "user-1",
        ref_type="generation",
        ref_id="gen-1",
        actual_micro=100,
        idempotency_key="settle:gen-1:retry",
    )

    assert result is consumed_tx
    assert wallet.balance_micro == 0
    assert wallet.version == 4


@pytest.mark.asyncio
async def test_settle_rejects_negative_actual_amount(monkeypatch: pytest.MonkeyPatch):
    async def fail_existing_tx(*_args: Any) -> None:
        raise AssertionError("negative settle must fail before DB access")

    monkeypatch.setattr(billing, "_existing_tx", fail_existing_tx)

    with pytest.raises(billing.BillingError) as exc:
        await billing.settle(
            object(),  # type: ignore[arg-type]
            "user-1",
            ref_type="generation",
            ref_id="gen-1",
            actual_micro=-1,
            idempotency_key="settle:gen-1",
        )

    assert exc.value.code == "NEGATIVE_AMOUNT"


@pytest.mark.asyncio
async def test_settle_charges_full_cost_beyond_authorized_hold(
    monkeypatch: pytest.MonkeyPatch,
):
    wallet = SimpleNamespace(
        balance_micro=20,
        hold_micro=100,
        lifetime_spend_micro=7,
        version=1,
    )

    async def fake_existing_tx(*_args: Any) -> None:
        return None

    async def fake_get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def fake_held_amount(*_args: Any) -> int:
        return 100

    async def fake_ref_consumption(*_args: Any) -> None:
        return None

    async def fake_insert(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_held_amount_for_ref", fake_held_amount)
    monkeypatch.setattr(billing, "_existing_ref_consumption_tx", fake_ref_consumption)
    monkeypatch.setattr(billing, "_insert_tx", fake_insert)

    tx = await billing.settle(
        object(),  # type: ignore[arg-type]
        "user-1",
        ref_type="generation",
        ref_id="gen-1",
        actual_micro=150,
        idempotency_key="settle:gen-1",
    )

    # The hold gates dispatch before cost exists. Once the provider has charged
    # 150, the 50 overage must remain user debt instead of platform loss.
    assert wallet.balance_micro == -30
    assert wallet.hold_micro == 0
    assert wallet.lifetime_spend_micro == 157
    assert tx.amount_micro == -50
    assert tx.meta["actual_micro"] == 150
    assert tx.meta["reported_actual_micro"] == 150
    assert tx.meta["unauthorized_micro"] == 50
    assert tx.meta["overdraw_micro"] == 30


@pytest.mark.asyncio
async def test_settle_without_hold_charges_full_cost_as_debt(
    monkeypatch: pytest.MonkeyPatch,
):
    """A missing hold cannot erase a provider cost that already occurred."""
    wallet = SimpleNamespace(
        balance_micro=0,
        hold_micro=0,
        lifetime_spend_micro=0,
        version=1,
    )

    async def fake_existing_tx(*_args: Any) -> None:
        return None

    async def fake_get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def fake_held_amount(*_args: Any) -> int:
        return 0

    async def fake_ref_consumption(*_args: Any) -> None:
        return None

    async def fake_insert(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_held_amount_for_ref", fake_held_amount)
    monkeypatch.setattr(billing, "_existing_ref_consumption_tx", fake_ref_consumption)
    monkeypatch.setattr(billing, "_insert_tx", fake_insert)

    tx = await billing.settle(
        object(),  # type: ignore[arg-type]
        "user-1",
        ref_type="video_generation",
        ref_id="video-1",
        actual_micro=900,
        idempotency_key="settle:video-1",
        allow_negative=False,
    )

    assert wallet.balance_micro == -900
    assert wallet.lifetime_spend_micro == 900
    assert tx.amount_micro == -900
    assert tx.meta["actual_micro"] == 900
    assert tx.meta["reported_actual_micro"] == 900
    assert tx.meta["unauthorized_micro"] == 900
    assert tx.meta["overdraw_micro"] == 900


@pytest.mark.asyncio
async def test_settle_existing_negative_wallet_does_not_block_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing debt must not turn every later settlement into a permanent 409."""
    wallet = SimpleNamespace(
        balance_micro=-100,
        hold_micro=50,
        lifetime_spend_micro=10,
        version=1,
    )

    async def fake_existing_tx(*_args: Any) -> None:
        return None

    async def fake_get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def fake_held_amount(*_args: Any) -> int:
        return 50

    async def fake_ref_consumption(*_args: Any) -> None:
        return None

    async def fake_insert(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_held_amount_for_ref", fake_held_amount)
    monkeypatch.setattr(billing, "_existing_ref_consumption_tx", fake_ref_consumption)
    monkeypatch.setattr(billing, "_insert_tx", fake_insert)

    tx = await billing.settle(
        object(),  # type: ignore[arg-type]
        "user-1",
        ref_type="generation",
        ref_id="gen-debt",
        actual_micro=50,
        idempotency_key="settle:gen-debt",
        allow_negative=False,
    )

    assert wallet.balance_micro == -100
    assert wallet.hold_micro == 0
    assert wallet.lifetime_spend_micro == 60
    assert tx.amount_micro == 0
    assert tx.meta["overdraw_micro"] == 100


@pytest.mark.asyncio
async def test_settle_within_hold_records_no_overdraw(
    monkeypatch: pytest.MonkeyPatch,
):
    """结算金额不超过 hold 时余额只增不减，overdraw 必须为 0。"""
    wallet = SimpleNamespace(
        balance_micro=50,
        hold_micro=200,
        lifetime_spend_micro=0,
        version=1,
    )

    async def fake_existing_tx(*_args: Any) -> None:
        return None

    async def fake_get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def fake_held_amount(*_args: Any) -> int:
        return 200

    async def fake_ref_consumption(*_args: Any) -> None:
        return None

    async def fake_insert(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_held_amount_for_ref", fake_held_amount)
    monkeypatch.setattr(billing, "_existing_ref_consumption_tx", fake_ref_consumption)
    monkeypatch.setattr(billing, "_insert_tx", fake_insert)

    tx = await billing.settle(
        object(),  # type: ignore[arg-type]
        "user-1",
        ref_type="generation",
        ref_id="gen-2",
        actual_micro=120,
        idempotency_key="settle:gen-2",
    )

    assert wallet.balance_micro == 130
    assert wallet.hold_micro == 0
    assert tx.meta["overdraw_micro"] == 0


@pytest.mark.asyncio
async def test_settle_rejects_zero_actual_amount(monkeypatch: pytest.MonkeyPatch):
    async def fail_existing_tx(*_args: Any) -> None:
        raise AssertionError("zero settle must fail before DB access")

    monkeypatch.setattr(billing, "_existing_tx", fail_existing_tx)

    with pytest.raises(billing.BillingError) as exc:
        await billing.settle(
            object(),  # type: ignore[arg-type]
            "user-1",
            ref_type="generation",
            ref_id="gen-1",
            actual_micro=0,
            idempotency_key="settle:gen-1",
        )

    assert exc.value.code == "ZERO_SETTLEMENT"


@pytest.mark.asyncio
async def test_settle_records_explicit_zero_rate_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = SimpleNamespace(
        balance_micro=100,
        hold_micro=0,
        lifetime_spend_micro=7,
        version=0,
    )

    async def no_existing(*_args: Any) -> None:
        return None

    async def get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def no_hold(*_args: Any) -> int:
        return 0

    async def no_consumption(*_args: Any) -> None:
        return None

    async def insert(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(billing, "_existing_tx", no_existing)
    monkeypatch.setattr(billing, "get_wallet", get_wallet)
    monkeypatch.setattr(billing, "_held_amount_for_ref", no_hold)
    monkeypatch.setattr(billing, "_existing_ref_consumption_tx", no_consumption)
    monkeypatch.setattr(billing, "_insert_tx", insert)

    tx = await billing.settle(
        object(),  # type: ignore[arg-type]
        "user-1",
        ref_type="completion",
        ref_id="completion-1",
        actual_micro=0,
        idempotency_key="complete:completion-1",
        record_zero=True,
    )

    assert tx.amount_micro == 0
    assert tx.meta["actual_micro"] == 0
    assert wallet.balance_micro == 100
    assert wallet.hold_micro == 0
    assert wallet.lifetime_spend_micro == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_negative", [False, True])
async def test_charge_never_absorbs_cost_regardless_of_allow_negative(
    monkeypatch: pytest.MonkeyPatch,
    allow_negative: bool,
) -> None:
    """余额 5 扣 10：无论 allow_negative 取什么值都必须扣满 10、余额落到 -5。

    allow_negative 只影响 overdraw_micro 这个欠费标记，不影响扣款金额本身；
    平台任何情况下都不吞下缺口（纯转嫁）。
    """
    wallet = SimpleNamespace(balance_micro=5, lifetime_spend_micro=0, version=0)

    async def no_existing(*_args: Any) -> None:
        return None

    async def get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def insert(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(billing, "_existing_tx", no_existing)
    monkeypatch.setattr(billing, "get_wallet", get_wallet)
    monkeypatch.setattr(billing, "_insert_tx", insert)

    tx = await billing.charge(
        object(),  # type: ignore[arg-type]
        "user-1",
        10,
        ref_type="test",
        ref_id="ref-1",
        idempotency_key="charge-1",
        allow_negative=allow_negative,
    )

    assert wallet.balance_micro == -5
    assert wallet.lifetime_spend_micro == 10
    assert tx is not None
    assert tx.amount_micro == -10
    assert tx.meta["cost_micro"] == 10
    assert tx.meta["overdraw_micro"] == (0 if allow_negative else 5)


def test_charge_has_no_cap_overdraw_switch() -> None:
    """cap_overdraw 曾让余额封顶为 0 从而由平台吸收差额，不允许复活。"""
    params = inspect.signature(billing.charge).parameters

    assert "cap_overdraw" not in params
    # 只看可执行语句：注释里保留了这些关键词用于说明历史坑位。
    source = "\n".join(
        line
        for line in inspect.getsource(billing.charge).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "wallet.balance_micro = 0" not in source
    assert "INSUFFICIENT_BALANCE" not in source


@pytest.mark.asyncio
async def test_charge_with_zero_balance_bills_full_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """零余额直扣：上游成本 10_000 必须整笔记账，不得静默清零。"""
    wallet = SimpleNamespace(balance_micro=0, lifetime_spend_micro=0, version=0)

    async def no_existing(*_args: Any) -> None:
        return None

    async def get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def insert(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(billing, "_existing_tx", no_existing)
    monkeypatch.setattr(billing, "get_wallet", get_wallet)
    monkeypatch.setattr(billing, "_insert_tx", insert)

    tx = await billing.charge(
        object(),  # type: ignore[arg-type]
        "user-1",
        10_000,
        ref_type="prompt_enhance",
        ref_id="ref-1",
        idempotency_key="charge-zero-balance",
    )

    assert wallet.balance_micro == -10_000
    assert wallet.lifetime_spend_micro == 10_000
    assert tx is not None
    assert tx.amount_micro == -10_000
    assert tx.meta["overdraw_micro"] == 10_000


@pytest.mark.asyncio
async def test_release_recomputes_held_amount_after_wallet_lock(
    monkeypatch: pytest.MonkeyPatch,
):
    wallet = SimpleNamespace(balance_micro=1_000, hold_micro=500, version=3)

    async def fake_existing_tx(*_args):
        return None

    async def fake_get_wallet(*_args, **_kwargs):
        return wallet

    async def fake_held_amount(*_args):
        return 0

    async def fake_ref_consumption(*_args: Any) -> None:
        return None

    async def fail_insert(*_args, **_kwargs):
        raise AssertionError("release with no outstanding hold must not insert a tx")

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_held_amount_for_ref", fake_held_amount)
    monkeypatch.setattr(billing, "_existing_ref_consumption_tx", fake_ref_consumption)
    monkeypatch.setattr(billing, "_insert_tx", fail_insert)

    result = await billing.release(
        object(),  # type: ignore[arg-type]
        "user-1",
        ref_type="generation",
        ref_id="gen-1",
        idempotency_key="release:gen-1",
    )

    assert result is None
    assert wallet.balance_micro == 1_000
    assert wallet.hold_micro == 500
    assert wallet.version == 3


@pytest.mark.asyncio
async def test_release_returns_existing_ref_consumption_when_hold_is_gone(
    monkeypatch: pytest.MonkeyPatch,
):
    wallet = SimpleNamespace(balance_micro=1_000, hold_micro=0, version=3)
    consumed_tx = SimpleNamespace(id="release-existing")

    async def fake_existing_tx(*_args: Any) -> None:
        return None

    async def fake_get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def fake_held_amount(*_args: Any) -> int:
        return 0

    async def fake_ref_consumption(*_args: Any) -> Any:
        return consumed_tx

    async def fail_insert(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("duplicate release must not insert")

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_held_amount_for_ref", fake_held_amount)
    monkeypatch.setattr(billing, "_existing_ref_consumption_tx", fake_ref_consumption)
    monkeypatch.setattr(billing, "_insert_tx", fail_insert)

    result = await billing.release(
        object(),  # type: ignore[arg-type]
        "user-1",
        ref_type="generation",
        ref_id="gen-1",
        idempotency_key="release:gen-1:retry",
    )

    assert result is consumed_tx
    assert wallet.balance_micro == 1_000
    assert wallet.version == 3


@pytest.mark.asyncio
async def test_charge_rejects_negative_amount(monkeypatch: pytest.MonkeyPatch):
    async def fail_existing_tx(*_args: Any) -> None:
        raise AssertionError("negative charge must fail before DB access")

    monkeypatch.setattr(billing, "_existing_tx", fail_existing_tx)

    with pytest.raises(billing.BillingError) as exc:
        await billing.charge(
            object(),  # type: ignore[arg-type]
            "user-1",
            -1,
            ref_type="generation",
            ref_id="gen-1",
            idempotency_key="charge:gen-1",
        )

    assert exc.value.code == "NEGATIVE_AMOUNT"


@pytest.mark.asyncio
async def test_charge_allow_negative_marks_no_overdraw_debt(
    monkeypatch: pytest.MonkeyPatch,
):
    wallet = SimpleNamespace(
        balance_micro=30,
        hold_micro=0,
        lifetime_spend_micro=5,
        version=1,
    )

    async def fake_existing_tx(*_args: Any) -> None:
        return None

    async def fake_get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def fake_insert(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_insert_tx", fake_insert)

    tx = await billing.charge(
        object(),  # type: ignore[arg-type]
        "user-1",
        100,
        ref_type="generation",
        ref_id="gen-1",
        idempotency_key="charge:gen-1",
        allow_negative=True,
    )

    assert wallet.balance_micro == -70
    assert wallet.lifetime_spend_micro == 105
    assert tx.amount_micro == -100
    assert tx.meta["overdraw_micro"] == 0


@pytest.mark.asyncio
async def test_charge_records_gross_lifetime_spend_for_existing_debt(
    monkeypatch: pytest.MonkeyPatch,
):
    wallet = SimpleNamespace(
        balance_micro=-30,
        hold_micro=0,
        lifetime_spend_micro=50,
        version=1,
    )

    async def fake_existing_tx(*_args: Any) -> None:
        return None

    async def fake_get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return wallet

    async def fake_insert(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_insert_tx", fake_insert)

    tx = await billing.charge(
        object(),  # type: ignore[arg-type]
        "user-1",
        100,
        ref_type="generation",
        ref_id="gen-1",
        idempotency_key="charge:gen-1",
    )

    # 已欠 30 再扣 100：余额继续下探到 -130，欠费全部记在用户账上；
    # 旧实现会把余额抹平成 0，等于平台替用户还了这 130。
    assert wallet.balance_micro == -130
    assert wallet.lifetime_spend_micro == 150
    assert tx.amount_micro == -100
    assert tx.meta["overdraw_micro"] == 130


@pytest.mark.asyncio
async def test_adjust_enforces_min_balance_after_wallet_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = SimpleNamespace(balance_micro=100, lifetime_topup_micro=0, version=0)
    lock_calls: list[bool] = []

    async def fake_existing_tx(*_args: Any) -> None:
        return None

    async def fake_get_wallet(*_args: Any, **kwargs: Any) -> Any:
        lock_calls.append(bool(kwargs.get("lock")))
        return wallet

    async def fail_insert(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("over-limit adjustment must fail before insert")

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_insert_tx", fail_insert)

    with pytest.raises(billing.BillingError) as exc:
        await billing.adjust(
            object(),  # type: ignore[arg-type]
            "user-1",
            -250,
            admin_id="admin-1",
            reason="test",
            allow_negative=True,
            min_balance_micro=-100,
        )

    assert exc.value.code == "negative_balance_limit_exceeded"
    assert wallet.balance_micro == 100
    assert lock_calls == [True]


@pytest.mark.asyncio
async def test_topup_redeem_rejects_nonpositive_amount(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fail_existing_tx(*_args: Any) -> None:
        raise AssertionError("invalid redeem amount must fail before DB access")

    monkeypatch.setattr(billing, "_existing_tx", fail_existing_tx)

    with pytest.raises(billing.BillingError) as exc:
        await billing.topup_redeem(
            object(),  # type: ignore[arg-type]
            "user-1",
            0,
            usage_id="usage-1",
            code_id="code-1",
        )

    assert exc.value.code == "INVALID_AMOUNT"


@pytest.mark.asyncio
async def test_ensure_wallet_ignores_non_callable_connection_attribute():
    class Nested:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: Any) -> None:
            return None

    class Session:
        connection = object()

        def __init__(self) -> None:
            self.added: list[Any] = []

        def begin_nested(self) -> Nested:
            return Nested()

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            return None

    session = Session()

    await billing._ensure_wallet(session, "user-1")  # type: ignore[arg-type]  # noqa: SLF001

    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_user_wallet_version_column_blocks_lost_updates() -> None:
    """F-17：version 必须真的进 UPDATE 的 WHERE，而不只是被递增。

    lumen_core.billing 的 6 条写路径当前都先 ``SELECT ... FOR UPDATE``，靠行锁
    串行化，所以现在不会丢更新 —— 但那是**约定**不是**约束**：任何新写入路径
    忘了 lock=True，丢失更新就会静默发生在钱包余额上。挂上 version_id_col 之后
    并发写的后手会撞 StaleDataError 而不是无声覆盖前手的结果。

    这里用 SQLite 起两个独立 session 复现「两边都读到 version=0，各自 +1 回写」：
    第二个 flush 必须炸。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm.exc import StaleDataError

    from lumen_core.model_base import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[UserWallet.__table__])
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async with maker() as setup:
            setup.add(UserWallet(user_id="user-1", balance_micro=1_000, version=0))
            await setup.commit()

        async with maker() as first, maker() as second:
            wallet_a = await first.get(UserWallet, "user-1")
            wallet_b = await second.get(UserWallet, "user-1")
            assert wallet_a is not None and wallet_b is not None

            # 先手：扣 100，version 0 -> 1
            wallet_a.balance_micro -= 100
            wallet_a.version += 1
            await first.commit()

            # 后手拿的还是 version=0 的快照，回写必须被拒
            wallet_b.balance_micro -= 300
            wallet_b.version += 1
            with pytest.raises(StaleDataError):
                await second.commit()

        async with maker() as check:
            final = await check.get(UserWallet, "user-1")
            assert final is not None
            # 先手的扣款完整保留，后手没有覆盖掉它
            assert final.balance_micro == 900
            assert final.version == 1
    finally:
        await engine.dispose()
