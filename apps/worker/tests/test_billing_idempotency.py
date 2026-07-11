from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app import billing as worker_billing
from lumen_core.pricing import CostBreakdown


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.info: dict[str, Any] = {}

    def add(self, row: Any) -> None:
        self.added.append(row)


def _tx(kind: str, key: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="tx-1",
        kind=kind,
        amount_micro=-100,
        balance_after=900,
        hold_after=0,
        ref_type="generation" if kind != "charge" else "completion",
        ref_id="ref-1",
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_settle_generation_replay_writes_audit_without_reestimating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    generation = SimpleNamespace(id="gen-1", user_id="user-1", model="gpt-image-2")

    async def fail_estimate(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
        raise AssertionError("replay path should not estimate price again")

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def allow_negative_balance() -> bool:
        return False

    async def cache_aware_enabled() -> bool:
        return True

    async def rate_multiplier(*_args: Any) -> int:
        return 10_000

    async def existing_tx(*_args: Any) -> SimpleNamespace:
        return _tx("settle", "settle:gen-1")

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing.billing_core, "estimate_image_cost", fail_estimate
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", existing_tx)

    await worker_billing.settle_generation(  # type: ignore[arg-type]
        session,
        generation,
        width=1024,
        height=1024,
    )

    assert len(session.added) == 1
    assert session.added[0].event_type == "wallet.settle.replay"
    assert session.added[0].details["idempotency_key"] == "settle:gen-1"
    assert session.added[0].details["replay_source"] == "precheck"


@pytest.mark.asyncio
async def test_settle_generation_settles_hold_when_pricing_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    generation = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        model="gpt-image-2",
        upstream_request={"billing_tier": "1k"},
    )

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def no_existing(*_args: Any) -> None:
        return None

    async def missing_price(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
        raise worker_billing.billing_core.BillingError(
            "PRICING_MISSING",
            "pricing disappeared",
            503,
        )

    async def held_amount(*_args: Any, **_kwargs: Any) -> int:
        return 10_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["actual_micro"] == 10_000
        return SimpleNamespace(
            id="settle-1",
            amount_micro=0,
            balance_after=0,
            hold_after=0,
            meta=kwargs["meta"],
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "held_amount_for_ref", held_amount)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_image_cost_for_tier",
        missing_price,
    )
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)

    await worker_billing.settle_generation(  # type: ignore[arg-type]
        session,
        generation,
        width=1024,
        height=1024,
    )

    assert [row.event_type for row in session.added] == [
        "billing.pricing.hold_fallback_after_upstream",
        "wallet.settle.image",
    ]


@pytest.mark.asyncio
async def test_settle_generation_uses_requested_billing_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    generation = SimpleNamespace(
        id="gen-2",
        user_id="user-1",
        model="gpt-image-2",
        upstream_request={"billing_tier": "2k"},
    )

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def allow_negative_balance() -> bool:
        return False

    async def existing_tx(*_args: Any) -> None:
        return None

    async def fail_pixel_estimate(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
        raise AssertionError("requested billing tier should bypass pixel thresholds")

    async def estimate_for_tier(*_args: Any, **kwargs: Any) -> tuple[int, str]:
        assert kwargs["tier"] == "2k"
        return 400, "2k"

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            id="tx-2",
            amount_micro=-400,
            balance_after=600,
            hold_after=0,
            meta=kwargs["meta"],
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", existing_tx)
    monkeypatch.setattr(
        worker_billing.billing_core, "estimate_image_cost", fail_pixel_estimate
    )
    monkeypatch.setattr(
        worker_billing.billing_core, "estimate_image_cost_for_tier", estimate_for_tier
    )
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)

    await worker_billing.settle_generation(  # type: ignore[arg-type]
        session,
        generation,
        width=1792,
        height=1024,
    )

    settle_audit = next(
        row for row in session.added if row.event_type == "wallet.settle.image"
    )
    assert settle_audit.details["actual_micro"] == 400
    assert session.added[0].details["tier_source"] == "request"


@pytest.mark.asyncio
async def test_settle_generation_records_zero_rate_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    generation = SimpleNamespace(
        id="gen-free",
        user_id="user-1",
        model="gpt-image-2",
        upstream_request={
            "billing_rate_multiplier_x10000": 0,
            "billing_pricing_snapshot": {
                "kind": "image",
                "tier": "1k",
                "unit_price_micro": 400,
            },
        },
    )
    calls: dict[str, Any] = {}

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def allow_negative_balance() -> bool:
        return False

    async def no_existing(*_args: Any) -> None:
        return None

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["settle"] = kwargs
        return SimpleNamespace(
            id="tx-free",
            amount_micro=0,
            balance_after=123,
            hold_after=0,
            meta=kwargs["meta"],
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)

    await worker_billing.settle_generation(  # type: ignore[arg-type]
        session,
        generation,
        width=1024,
        height=1024,
    )

    assert calls["settle"]["actual_micro"] == 0
    assert calls["settle"]["record_zero"] is True
    assert calls["settle"]["meta"]["rate_multiplier_x10000"] == 0
    assert [row.event_type for row in session.added] == [
        "wallet.settle.image",
        "wallet.charge.zero_rate",
    ]


@pytest.mark.asyncio
async def test_settle_generation_uses_image_count_for_requested_billing_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    generation = SimpleNamespace(
        id="gen-3",
        user_id="user-1",
        model="gpt-image-2",
        upstream_request={"billing_tier": "4k"},
    )
    calls: dict[str, Any] = {}

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def allow_negative_balance() -> bool:
        return False

    async def existing_tx(*_args: Any) -> None:
        return None

    async def estimate_for_tier(*_args: Any, **kwargs: Any) -> tuple[int, str]:
        calls["estimate"] = kwargs
        return 1200, "4k"

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["settle"] = kwargs
        return SimpleNamespace(
            id="tx-3",
            amount_micro=-1200,
            balance_after=600,
            hold_after=0,
            meta=kwargs["meta"],
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", existing_tx)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_image_cost_for_tier",
        estimate_for_tier,
    )
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)

    await worker_billing.settle_generation(  # type: ignore[arg-type]
        session,
        generation,
        width=3840,
        height=2160,
        image_count=3,
    )

    assert calls["estimate"]["tier"] == "4k"
    assert calls["estimate"]["n"] == 3
    assert calls["settle"]["actual_micro"] == 1200
    assert calls["settle"]["meta"]["image_count"] == 3
    settle_audit = next(
        row for row in session.added if row.event_type == "wallet.settle.image"
    )
    assert settle_audit.details["image_count"] == 3


@pytest.mark.asyncio
async def test_settle_generation_uses_retry_billing_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    generation = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        model="gpt-image-2",
        billing_retry_count=1,
        upstream_request={"billing_tier": "2k"},
    )
    calls: dict[str, Any] = {}

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def allow_negative_balance() -> bool:
        return False

    async def existing_tx(*_args: Any) -> None:
        return None

    async def estimate_for_tier(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
        return 400, "2k"

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["settle"] = kwargs
        return SimpleNamespace(
            id="tx-2",
            amount_micro=-400,
            balance_after=600,
            hold_after=0,
            meta=kwargs["meta"],
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", existing_tx)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_image_cost_for_tier",
        estimate_for_tier,
    )
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)

    await worker_billing.settle_generation(  # type: ignore[arg-type]
        session,
        generation,
        width=1792,
        height=1024,
    )

    assert calls["settle"]["ref_id"] == "gen-1:retry:1"
    assert calls["settle"]["idempotency_key"] == "settle:gen-1:retry:1"
    assert calls["settle"]["meta"]["generation_id"] == "gen-1"
    assert calls["settle"]["meta"]["retry_count"] == 1


@pytest.mark.asyncio
async def test_charge_completion_replay_writes_audit_without_estimating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    completion = SimpleNamespace(
        id="completion-1",
        user_id="user-1",
        model="gpt-5.5",
        tokens_in=100,
        tokens_out=50,
    )

    async def fail_estimate(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("replay path should not estimate price again")

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def existing_tx(*_args: Any) -> SimpleNamespace:
        return _tx("charge", "complete:completion-1")

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing.billing_core, "estimate_completion_cost", fail_estimate
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", existing_tx)

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert len(session.added) == 1
    assert session.added[0].event_type == "wallet.charge.replay"
    assert session.added[0].details["idempotency_key"] == "complete:completion-1"


@pytest.mark.asyncio
async def test_charge_completion_settles_hold_when_pricing_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    completion = SimpleNamespace(
        id="completion-1",
        user_id="user-1",
        model="unpriced-model",
        tokens_in=100,
        tokens_out=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=0,
        user_api_credential_id=None,
        upstream_request={},
    )

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def cache_aware_enabled() -> bool:
        return True

    async def rate_multiplier(*_args: Any) -> int:
        return 10_000

    async def no_existing(*_args: Any) -> None:
        return None

    async def missing_price(*_args: Any, **_kwargs: Any) -> int:
        raise worker_billing.billing_core.BillingError(
            "PRICING_MISSING",
            "pricing disappeared",
            503,
        )

    async def held_amount(*_args: Any, **_kwargs: Any) -> int:
        return 10_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["actual_micro"] == 10_000
        return SimpleNamespace(
            id="settle-1",
            kind="settle",
            amount_micro=0,
            balance_after=0,
            hold_after=0,
            meta=kwargs["meta"],
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(worker_billing, "_cache_aware_enabled", cache_aware_enabled)
    monkeypatch.setattr(worker_billing, "_rate_multiplier_x10000", rate_multiplier)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_existing_fingerprint_tx", no_existing)
    monkeypatch.setattr(worker_billing, "held_amount_for_ref", held_amount)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_completion_cost",
        missing_price,
    )
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)
    monkeypatch.setattr(worker_billing, "get_billing_cache", lambda: None)

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert [row.event_type for row in session.added] == [
        "billing.pricing.hold_fallback_after_upstream",
        "wallet.charge.completion",
    ]


@pytest.mark.asyncio
async def test_charge_completion_records_zero_rate_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    completion = SimpleNamespace(
        id="completion-free",
        user_id="user-1",
        model="gpt-5.5",
        tokens_in=100,
        tokens_out=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=0,
        user_api_credential_id=None,
        upstream_request={"billing_rate_multiplier_x10000": 0},
    )
    calls: dict[str, Any] = {}

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def allow_negative_balance() -> bool:
        return False

    async def no_existing(*_args: Any) -> None:
        return None

    async def zero_breakdown(*_args: Any, **_kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=100,
            output_cost_micro=100,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=0,
            total_cost_micro=200,
            actual_cost_micro=0,
            pricing_source="snapshot",
        )

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["settle"] = kwargs
        return SimpleNamespace(
            id="tx-free",
            kind="settle",
            amount_micro=0,
            balance_after=123,
            hold_after=0,
            meta=kwargs["meta"],
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_completion_cost_breakdown", zero_breakdown)
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)
    monkeypatch.setattr(worker_billing, "get_billing_cache", lambda: None)

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert calls["settle"]["actual_micro"] == 0
    assert calls["settle"]["record_zero"] is True
    assert [row.event_type for row in session.added] == [
        "wallet.charge.completion",
        "wallet.charge.zero_rate",
    ]


@pytest.mark.asyncio
async def test_charge_completion_uses_retry_billing_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    completion = SimpleNamespace(
        id="completion-1",
        user_id="user-1",
        model="gpt-5.5",
        tokens_in=100,
        tokens_out=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=0,
        user_api_credential_id=None,
        upstream_request={"billing_retry_count": 1},
    )
    calls: dict[str, Any] = {}

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def cache_aware_enabled() -> bool:
        return True

    async def rate_multiplier(*_args: Any) -> int:
        return 10_000

    async def allow_negative_balance() -> bool:
        return False

    async def no_existing(*_args: Any) -> None:
        return None

    async def estimate_breakdown(*_args: Any, **_kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=60,
            output_cost_micro=40,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=10_000,
            total_cost_micro=100,
            actual_cost_micro=100,
            pricing_source="db",
        )

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["settle"] = kwargs
        return SimpleNamespace(
            id="tx-1",
            amount_micro=-100,
            balance_after=900,
            hold_after=0,
            meta=kwargs["meta"],
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(worker_billing, "_cache_aware_enabled", cache_aware_enabled)
    monkeypatch.setattr(worker_billing, "_rate_multiplier_x10000", rate_multiplier)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_existing_fingerprint_tx", no_existing)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)
    monkeypatch.setattr(worker_billing, "get_billing_cache", lambda: None)

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert calls["settle"]["ref_id"] == "completion-1:retry:1"
    assert calls["settle"]["idempotency_key"] == "complete:completion-1:retry:1"
    assert calls["settle"]["meta"]["completion_id"] == "completion-1"


@pytest.mark.asyncio
async def test_charge_completion_image_cost_guard_locks_wallet_before_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    completion = SimpleNamespace(
        id="completion-1",
        user_id="user-1",
        model="gpt-5.5",
        tokens_in=100,
        tokens_out=200,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=200,
        user_api_credential_id=None,
        upstream_request={},
    )
    wallet_locks: list[tuple[bool, bool]] = []

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def cache_aware_enabled() -> bool:
        return True

    async def rate_multiplier(*_args: Any) -> int:
        return 10_000

    async def allow_negative_balance() -> bool:
        return False

    async def no_existing(*_args: Any) -> None:
        return None

    async def estimate_breakdown(*_args: Any, **_kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=0,
            output_cost_micro=0,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=100,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=10_000,
            total_cost_micro=100,
            actual_cost_micro=100,
            pricing_source="db",
        )

    async def get_wallet(*_args: Any, **kwargs: Any) -> Any:
        wallet_locks.append((bool(kwargs.get("lock")), bool(kwargs.get("create"))))
        return SimpleNamespace(balance_micro=10)

    async def held_amount_for_ref(*_args: Any, **_kwargs: Any) -> int:
        return 20

    async def fail_settle(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("insufficient image budget must fail before settle")

    monkeypatch.setattr(worker_billing, "AsyncSession", _Session)
    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(worker_billing, "_cache_aware_enabled", cache_aware_enabled)
    monkeypatch.setattr(worker_billing, "_rate_multiplier_x10000", rate_multiplier)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_existing_fingerprint_tx", no_existing)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(worker_billing.billing_core, "get_wallet", get_wallet)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "_held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(worker_billing.billing_core, "settle", fail_settle)
    monkeypatch.setattr(worker_billing, "get_billing_cache", lambda: None)

    with pytest.raises(worker_billing.billing_core.BillingError) as excinfo:
        await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert excinfo.value.code == "INSUFFICIENT_BALANCE"
    assert wallet_locks == [(True, False)]


@pytest.mark.asyncio
async def test_charge_completion_integrity_error_bubbles_after_billing_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    completion = SimpleNamespace(
        id="completion-1",
        user_id="user-1",
        model="gpt-5.5",
        tokens_in=100,
        tokens_out=50,
    )

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def allow_negative_balance() -> bool:
        return False

    async def estimate_cost(*_args: Any, **_kwargs: Any) -> int:
        return 100

    async def no_existing_then_found(*_args: Any) -> SimpleNamespace | None:
        return None

    async def fail_settle(*_args: Any, **_kwargs: Any) -> None:
        raise IntegrityError("insert", {}, Exception("duplicate"))

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(
        worker_billing.billing_core, "estimate_completion_cost", estimate_cost
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing_then_found)
    monkeypatch.setattr(worker_billing.billing_core, "settle", fail_settle)

    with pytest.raises(IntegrityError):
        await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert session.added == []


@pytest.mark.asyncio
async def test_charge_completion_passes_priority_service_tier_to_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    completion = SimpleNamespace(
        id="completion-1",
        user_id="user-1",
        model="gpt-5.5",
        tokens_in=100,
        tokens_out=50,
        upstream_request={"service_tier": "priority"},
    )
    captured: dict[str, Any] = {}

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def allow_negative_balance() -> bool:
        return False

    async def estimate_cost(*_args: Any, **kwargs: Any) -> int:
        captured["service_tier"] = kwargs.get("service_tier")
        return 100

    async def no_existing(*_args: Any) -> None:
        return None

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        captured["meta"] = kwargs.get("meta")
        return SimpleNamespace(
            id="tx-1",
            amount_micro=-100,
            balance_after=900,
            meta={},
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(
        worker_billing.billing_core, "estimate_completion_cost", estimate_cost
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_existing_fingerprint_tx", no_existing)
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert captured["service_tier"] == "priority"
    assert captured["meta"]["service_tier"] == "priority"
    assert captured["meta"]["request_fingerprint"].startswith("v2:")


@pytest.mark.asyncio
async def test_charge_completion_legacy_mode_folds_cache_tokens_into_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    completion = SimpleNamespace(
        id="completion-1",
        user_id="user-1",
        model="gpt-5.5",
        tokens_in=80,
        tokens_out=20,
        cache_read_tokens=15,
        cache_creation_tokens=5,
        upstream_request={},
    )
    captured: dict[str, Any] = {}

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def cache_aware_enabled() -> bool:
        return False

    async def allow_negative_balance() -> bool:
        return False

    async def rate_multiplier(*_args: Any) -> int:
        return 10_000

    async def estimate_breakdown(*_args: Any, **kwargs: Any) -> CostBreakdown:
        tokens = kwargs["tokens"]
        captured["tokens"] = tokens
        return CostBreakdown(
            input_cost_micro=100,
            output_cost_micro=0,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=10_000,
            total_cost_micro=100,
            actual_cost_micro=100,
            pricing_source="test",
        )

    async def no_existing(*_args: Any) -> None:
        return None

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        captured["meta"] = kwargs.get("meta")
        return SimpleNamespace(
            id="tx-1",
            amount_micro=-100,
            balance_after=900,
            meta={},
        )

    monkeypatch.setattr(worker_billing, "AsyncSession", _Session)
    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(worker_billing, "_cache_aware_enabled", cache_aware_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_rate_multiplier_x10000", rate_multiplier)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_existing_fingerprint_tx", no_existing)
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    tokens = captured["tokens"]
    assert tokens.input_tokens == 100
    assert tokens.output_tokens == 20
    assert tokens.cache_read_tokens == 0
    assert tokens.cache_creation_tokens == 0
    assert captured["meta"]["tokens_in"] == 100


@pytest.mark.asyncio
async def test_charge_completion_refreshes_balance_cache_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cache:
        def __init__(self) -> None:
            self.sets: list[tuple[str, int]] = []
            self.window_increments: list[tuple[str, int, dict[str, int] | None]] = []

        async def set_balance(self, user_id: str, balance_micro: int) -> None:
            self.sets.append((user_id, balance_micro))

        async def queue_deduct(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("settle must not decr an already-held balance cache")

        async def queue_window_increment(
            self,
            key_id: str,
            micro: int,
            _limits: dict[str, int],
        ) -> None:
            self.window_increments.append((key_id, micro, _limits))

        async def credential_limits(self, *_args: Any) -> dict[str, int]:
            return {"5h": 0, "1d": 0, "7d": 0}

    session = _Session()
    completion = SimpleNamespace(
        id="completion-1",
        user_id="user-1",
        model="gpt-5.5",
        tokens_in=100,
        tokens_out=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        upstream_request={},
        user_api_credential_id="cred-1",
    )
    cache = Cache()

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def allow_negative_balance() -> bool:
        return False

    async def window_rate_limit_enabled() -> bool:
        return False

    async def estimate_cost(*_args: Any, **_kwargs: Any) -> int:
        return 40

    async def estimate_breakdown(*_args: Any, **_kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=40,
            output_cost_micro=0,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=10_000,
            total_cost_micro=40,
            actual_cost_micro=40,
            pricing_source="db",
        )

    async def no_existing(*_args: Any) -> None:
        return None

    async def settle(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            id="tx-1",
            amount_micro=10,
            balance_after=960,
            meta={},
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(
        worker_billing,
        "_window_rate_limit_enabled",
        window_rate_limit_enabled,
    )
    monkeypatch.setattr(
        worker_billing.billing_core, "estimate_completion_cost", estimate_cost
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_existing_fingerprint_tx", no_existing)
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)
    monkeypatch.setattr(worker_billing, "get_billing_cache", lambda: cache)

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert cache.sets == []
    assert cache.window_increments == []
    await worker_billing.flush_balance_cache_refreshes(session)  # type: ignore[arg-type]
    assert cache.sets == [("user-1", 960)]
    assert cache.window_increments == [("cred-1", 40, {"5h": 0, "1d": 0, "7d": 0})]


@pytest.mark.asyncio
async def test_charge_completion_defers_window_increment_until_post_commit_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cache:
        def __init__(self) -> None:
            self.window_increments: list[tuple[str, int, dict[str, int] | None]] = []

        async def set_balance(self, _user_id: str, _balance_micro: int) -> None:
            return None

        async def queue_deduct(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("settle must not decr an already-held balance cache")

        async def queue_window_increment(
            self,
            key_id: str,
            micro: int,
            limits: dict[str, int] | None,
        ) -> None:
            self.window_increments.append((key_id, micro, limits))

        async def credential_limits(self, *_args: Any) -> dict[str, int]:
            return {"5h": 10, "1d": 20, "7d": 30}

        async def evaluate_rate_limits(
            self,
            *_args: Any,
            **_kwargs: Any,
        ) -> tuple[bool, str, SimpleNamespace]:
            return (
                False,
                "5h",
                SimpleNamespace(used_micro=10, limit_micro=10),
            )

    session = _Session()
    completion = SimpleNamespace(
        id="completion-2",
        user_id="user-1",
        model="gpt-5.5",
        tokens_in=100,
        tokens_out=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        upstream_request={},
        user_api_credential_id="cred-2",
    )
    cache = Cache()

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def billing_enabled() -> bool:
        return True

    async def allow_negative_balance() -> bool:
        return False

    async def cache_aware_enabled() -> bool:
        return True

    async def rate_multiplier(*_args: Any) -> int:
        return 10_000

    async def window_rate_limit_enabled() -> bool:
        return True

    async def estimate_cost(*_args: Any, **_kwargs: Any) -> int:
        return 40

    async def estimate_breakdown(*_args: Any, **_kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=40,
            output_cost_micro=0,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=10_000,
            total_cost_micro=40,
            actual_cost_micro=40,
            pricing_source="db",
        )

    async def no_existing(*_args: Any) -> None:
        return None

    async def settle(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            id="tx-2",
            amount_micro=10,
            balance_after=960,
            meta={},
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(worker_billing, "_cache_aware_enabled", cache_aware_enabled)
    monkeypatch.setattr(worker_billing, "_rate_multiplier_x10000", rate_multiplier)
    monkeypatch.setattr(
        worker_billing,
        "_window_rate_limit_enabled",
        window_rate_limit_enabled,
    )
    monkeypatch.setattr(worker_billing, "AsyncSession", _Session)
    monkeypatch.setattr(
        worker_billing.billing_core, "estimate_completion_cost", estimate_cost
    )
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_existing_fingerprint_tx", no_existing)
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)
    monkeypatch.setattr(worker_billing, "get_billing_cache", lambda: cache)

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert any(
        getattr(row, "event_type", None) == "billing.rate_limit.exceeded_after_upstream"
        for row in session.added
    )
    assert cache.window_increments == []
    await worker_billing.flush_balance_cache_refreshes(session)  # type: ignore[arg-type]
    assert cache.window_increments == [("cred-2", 40, {"5h": 10, "1d": 20, "7d": 30})]


@pytest.mark.asyncio
async def test_completion_window_rate_limit_blocks_before_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cache:
        async def evaluate_rate_limits(
            self,
            *_args: Any,
            **_kwargs: Any,
        ) -> tuple[bool, str, SimpleNamespace]:
            return (
                False,
                "5h",
                SimpleNamespace(
                    used_micro=90,
                    limit_micro=100,
                    resets_at=None,
                ),
            )

    session = _Session()
    completion = SimpleNamespace(
        id="completion-window",
        user_id="user-1",
        model="gpt-5.5",
        upstream_request={"billing_rate_multiplier_x10000": 15_000},
        user_api_credential_id="cred-1",
    )

    async def enabled() -> bool:
        return True

    async def held(*_args: Any, **_kwargs: Any) -> int:
        return 20

    monkeypatch.setattr(worker_billing, "AsyncSession", _Session)
    monkeypatch.setattr(worker_billing, "_window_rate_limit_enabled", enabled)
    monkeypatch.setattr(worker_billing, "held_amount_for_ref", held)
    monkeypatch.setattr(worker_billing, "get_billing_cache", lambda: Cache())

    failure = await worker_billing.completion_window_rate_limit_failure(
        session,  # type: ignore[arg-type]
        completion,  # type: ignore[arg-type]
    )

    assert failure == (
        "billing_window_rate_limit",
        "5h spending window limit exceeded",
    )
    assert any(
        getattr(row, "event_type", None) == "billing.rate_limit.preflight_blocked"
        for row in session.added
    )


@pytest.mark.asyncio
async def test_completion_rate_multiplier_uses_task_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_dynamic(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("task multiplier snapshot must win")

    monkeypatch.setattr(worker_billing, "_rate_multiplier_x10000", fail_dynamic)
    completion = SimpleNamespace(
        user_id="user-1",
        upstream_request={"billing_rate_multiplier_x10000": 17_500},
    )

    assert (
        await worker_billing.completion_rate_multiplier_x10000(
            object(),  # type: ignore[arg-type]
            completion,  # type: ignore[arg-type]
        )
        == 17_500
    )


@pytest.mark.asyncio
async def test_completion_rate_multiplier_preserves_zero_task_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_dynamic(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("zero task multiplier snapshot must win")

    monkeypatch.setattr(worker_billing, "_rate_multiplier_x10000", fail_dynamic)
    completion = SimpleNamespace(
        user_id="user-1",
        upstream_request={"billing_rate_multiplier_x10000": 0},
    )

    assert (
        await worker_billing.completion_rate_multiplier_x10000(
            object(),  # type: ignore[arg-type]
            completion,  # type: ignore[arg-type]
        )
        == 0
    )


@pytest.mark.asyncio
async def test_flush_balance_cache_refreshes_clears_pending_when_no_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    worker_billing._record_balance_cache_refresh(  # noqa: SLF001
        session,  # type: ignore[arg-type]
        user_id="user-1",
        balance_after=123,
    )
    monkeypatch.setattr(worker_billing, "get_billing_cache", lambda: None)

    await worker_billing.flush_balance_cache_refreshes(session)  # type: ignore[arg-type]

    assert session.info == {}


@pytest.mark.asyncio
async def test_charge_completion_skips_byok_task_without_wallet_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    completion = SimpleNamespace(
        id="completion-1",
        user_id="user-1",
        model="gpt-5.5",
        tokens_in=100,
        tokens_out=50,
        upstream_request={},
    )

    async def account_mode(*_args: Any) -> str:
        return "byok"

    async def held_amount_for_ref(*_args: Any, **_kwargs: Any) -> int:
        return 0

    async def fail_estimate(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("BYOK task without wallet hold must not be billed")

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "_held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_completion_cost",
        fail_estimate,
    )

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert session.added == []


@pytest.mark.asyncio
async def test_release_completion_runs_for_byok_task_with_existing_wallet_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    completion = SimpleNamespace(id="completion-1", user_id="user-1")
    released: list[dict[str, Any]] = []

    async def account_mode(*_args: Any) -> str:
        return "byok"

    async def billing_enabled() -> bool:
        return True

    async def held_amount_for_ref(*_args: Any, **_kwargs: Any) -> int:
        return 100

    async def no_existing(*_args: Any) -> None:
        return None

    async def release(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        released.append(kwargs)
        return SimpleNamespace(
            id="tx-1",
            amount_micro=100,
            balance_after=900,
            hold_after=0,
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "_held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing.billing_core, "release", release)

    await worker_billing.release_completion(  # type: ignore[arg-type]
        session,
        completion,
        reason="cancelled",
    )

    assert released == [
        {
            "ref_type": "completion",
            "ref_id": "completion-1",
            "idempotency_key": "release:completion-1",
            "meta": {
                "completion_id": "completion-1",
                "reason": "cancelled",
                "billing_retry_count": 0,
            },
        }
    ]
    assert session.added[0].event_type == "wallet.release.completion"


@pytest.mark.asyncio
async def test_settle_generation_runs_for_byok_task_with_existing_wallet_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    generation = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        model="gpt-image-2",
        upstream_request={},
    )
    settled: list[dict[str, Any]] = []

    async def account_mode(*_args: Any) -> str:
        return "byok"

    async def billing_enabled() -> bool:
        return True

    async def held_amount_for_ref(*_args: Any, **_kwargs: Any) -> int:
        return 200

    async def no_existing(*_args: Any) -> None:
        return None

    async def thresholds() -> dict[str, int]:
        return {"1k": 0}

    async def estimate_image_cost(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
        return 150, "1k"

    async def allow_negative_balance() -> bool:
        return False

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        settled.append(kwargs)
        return SimpleNamespace(
            id="tx-1",
            amount_micro=50,
            balance_after=850,
            hold_after=0,
            meta={"overdraw_micro": 0},
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(worker_billing, "_thresholds", thresholds)
    monkeypatch.setattr(
        worker_billing, "_allow_negative_balance", allow_negative_balance
    )
    monkeypatch.setattr(
        worker_billing.billing_core,
        "_held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_image_cost",
        estimate_image_cost,
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)

    await worker_billing.settle_generation(  # type: ignore[arg-type]
        session,
        generation,
        width=1024,
        height=1024,
    )

    assert settled[0]["ref_type"] == "generation"
    assert settled[0]["ref_id"] == "gen-1"
    assert settled[0]["actual_micro"] == 150
    assert session.added[0].event_type == "wallet.settle.image"
