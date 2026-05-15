from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app import billing as worker_billing


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
