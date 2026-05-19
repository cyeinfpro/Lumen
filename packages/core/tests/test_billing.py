from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import CheckConstraint

from lumen_core import billing
from lumen_core.billing_cache import BillingCacheService
from lumen_core.models import UserWallet
from lumen_core.pricing import CostBreakdown, UsageTokens, build_request_fingerprint


def test_rmb_micro_conversion_is_decimal_safe():
    assert billing.rmb_to_micro("0.005") == 5_000
    assert billing.rmb_to_micro("12.345678") == 12_345_678
    assert billing.micro_to_rmb_str(12_345_678) == "12.345678"


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_rmb_micro_conversion_rejects_non_finite_values(raw: str):
    with pytest.raises(billing.BillingError):
        billing.rmb_to_micro(raw)


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


def test_parse_thresholds_logs_invalid_json(caplog: pytest.LogCaptureFixture):
    with caplog.at_level("WARNING", logger="lumen_core.billing"):
        thresholds = billing.parse_thresholds("{not-json")

    assert thresholds == billing.DEFAULT_IMAGE_SIZE_THRESHOLDS
    assert "Invalid billing image size thresholds JSON" in caplog.text


def test_parse_bool_setting_matches_zero_one_runtime_settings():
    assert billing.parse_bool_setting("1") is True
    assert billing.parse_bool_setting("0", default=True) is False
    assert billing.parse_bool_setting("yes") is False
    assert billing.parse_bool_setting("true") is False
    assert billing.parse_bool_setting(None, default=True) is True


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
    script, numkeys, key, _ts, amount, limit_5h, limit_1d, limit_7d, _expire = (
        redis.calls[0]
    )
    assert "HINCRBY" in script
    assert numkeys == 1
    assert key == "lumen:billing:rl:cred-1"
    assert (amount, limit_5h, limit_1d, limit_7d) == (123, 500, 1000, 2000)


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
async def test_billing_cache_deduct_lock_refreshes_existing_identity_map():
    row = SimpleNamespace(balance_micro=100, version=0)

    class Result:
        def scalar_one_or_none(self) -> Any:
            return row

    class Session:
        def __init__(self) -> None:
            self.statements: list[Any] = []

        async def execute(self, stmt: Any) -> Result:
            self.statements.append(stmt)
            return Result()

        async def flush(self) -> None:
            return None

    session = Session()
    service = BillingCacheService(redis=None)

    balance = await service.deduct_sync(session, "user-1", 10)  # type: ignore[arg-type]

    assert balance == 90
    assert row.version == 1
    assert session.statements[0].get_execution_options()["populate_existing"] is True


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
async def test_settle_records_lifetime_spend_as_collected_amount(
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

    assert wallet.balance_micro == 0
    assert wallet.hold_micro == 0
    assert wallet.lifetime_spend_micro == 127
    assert tx.amount_micro == -20
    assert tx.meta["overdraw_micro"] == 30


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
async def test_charge_cap_overdraw_applies_even_when_negative_balance_allowed(
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
        cap_overdraw=True,
    )

    assert wallet.balance_micro == 0
    assert wallet.lifetime_spend_micro == 35
    assert tx.amount_micro == -30
    assert tx.meta["overdraw_micro"] == 70


@pytest.mark.asyncio
async def test_charge_does_not_decrease_lifetime_spend_for_existing_debt(
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
        cap_overdraw=True,
    )

    assert wallet.balance_micro == 0
    assert wallet.lifetime_spend_micro == 50
    assert tx.meta["overdraw_micro"] == 130


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
