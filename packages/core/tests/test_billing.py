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

    async def fail_insert(*_args, **_kwargs):
        raise AssertionError("release with no outstanding hold must not insert a tx")

    monkeypatch.setattr(billing, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing, "_held_amount_for_ref", fake_held_amount)
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
