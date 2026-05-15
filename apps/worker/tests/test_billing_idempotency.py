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

    async def existing_tx(*_args: Any) -> SimpleNamespace:
        return _tx("settle", "settle:gen-1")

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(worker_billing.billing_core, "estimate_image_cost", fail_estimate)
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
    monkeypatch.setattr(worker_billing, "_allow_negative_balance", allow_negative_balance)
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", existing_tx)
    monkeypatch.setattr(worker_billing.billing_core, "estimate_image_cost", fail_pixel_estimate)
    monkeypatch.setattr(worker_billing.billing_core, "estimate_image_cost_for_tier", estimate_for_tier)
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)

    await worker_billing.settle_generation(  # type: ignore[arg-type]
        session,
        generation,
        width=1792,
        height=1024,
    )

    settle_audit = next(row for row in session.added if row.event_type == "wallet.settle.image")
    assert settle_audit.details["actual_micro"] == 400
    assert session.added[0].details["tier_source"] == "request"


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
    monkeypatch.setattr(worker_billing.billing_core, "estimate_completion_cost", fail_estimate)
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", existing_tx)

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    assert len(session.added) == 1
    assert session.added[0].event_type == "wallet.charge.replay"
    assert session.added[0].details["idempotency_key"] == "complete:completion-1"


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

    async def fail_charge(*_args: Any, **_kwargs: Any) -> None:
        raise IntegrityError("insert", {}, Exception("duplicate"))

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(worker_billing, "_allow_negative_balance", allow_negative_balance)
    monkeypatch.setattr(worker_billing.billing_core, "estimate_completion_cost", estimate_cost)
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing_then_found)
    monkeypatch.setattr(worker_billing.billing_core, "charge", fail_charge)

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

    async def charge(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        captured["meta"] = kwargs.get("meta")
        return SimpleNamespace(
            id="tx-1",
            amount_micro=-100,
            balance_after=900,
            meta={},
        )

    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", billing_enabled)
    monkeypatch.setattr(worker_billing, "_allow_negative_balance", allow_negative_balance)
    monkeypatch.setattr(worker_billing.billing_core, "estimate_completion_cost", estimate_cost)
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_existing_fingerprint_tx", no_existing)
    monkeypatch.setattr(worker_billing.billing_core, "charge", charge)

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

    async def charge(*_args: Any, **kwargs: Any) -> SimpleNamespace:
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
    monkeypatch.setattr(worker_billing, "_allow_negative_balance", allow_negative_balance)
    monkeypatch.setattr(worker_billing, "_rate_multiplier_x10000", rate_multiplier)
    monkeypatch.setattr(
        worker_billing.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_existing_fingerprint_tx", no_existing)
    monkeypatch.setattr(worker_billing.billing_core, "charge", charge)

    await worker_billing.charge_completion(session, completion)  # type: ignore[arg-type]

    tokens = captured["tokens"]
    assert tokens.input_tokens == 100
    assert tokens.output_tokens == 20
    assert tokens.cache_read_tokens == 0
    assert tokens.cache_creation_tokens == 0
    assert captured["meta"]["tokens_in"] == 100
