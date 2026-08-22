from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app import billing as worker_billing
from app.agent_billing import (
    release_agent_text_hold,
    settle_agent_text_actual,
    settle_agent_text_unknown,
)
from lumen_core import billing as billing_core
from lumen_core.model_base import Base
from lumen_core.model_entities import AuditLog, User, UserWallet, WalletTransaction
from lumen_core.pricing import ModelPricing


USER_ID = "agent-billing-user"
INITIAL_BALANCE = 100_000


@pytest_asyncio.fixture
async def billing_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    User.__table__,
                    UserWallet.__table__,
                    WalletTransaction.__table__,
                    AuditLog.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(
            User(
                id=USER_ID,
                email="agent-billing@example.test",
                display_name="Agent Billing",
                account_mode="wallet",
            )
        )
        db.add(
            UserWallet(
                user_id=USER_ID,
                balance_micro=INITIAL_BALANCE,
                hold_micro=0,
                lifetime_topup_micro=INITIAL_BALANCE,
                lifetime_spend_micro=0,
                version=0,
            )
        )
        await db.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


def _run(run_id: str, hold_micro: int) -> Any:
    pricing = ModelPricing(
        input_per_1k_micro=1_000,
        output_per_1k_micro=2_000,
        pricing_source="admin",
    ).with_defaults()
    return SimpleNamespace(
        id=run_id,
        user_id=USER_ID,
        account_mode_snapshot="wallet",
        text_hold_micro=hold_micro,
        billing_jsonb={
            "pricing_snapshot": pricing.model_dump(),
            "rate_multiplier_x10000": 10_000,
            "reserved_input_tokens": 10_000,
            "reserved_output_tokens": 10_000,
            "state": "held",
        },
        model="agent-billing-model",
        provider_name="agent-billing-provider",
        turn_count=1,
        tool_call_count=0,
    )


async def _hold(
    factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    amount: int,
) -> None:
    async with factory() as db:
        async with db.begin():
            transaction = await billing_core.hold(
                db,
                USER_ID,
                amount,
                ref_type="agent_run",
                ref_id=run_id,
                idempotency_key=f"hold:{run_id}",
            )
            assert transaction is not None


async def _wallet_and_transactions(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[UserWallet, list[WalletTransaction]]:
    async with factory() as db:
        wallet = await db.get(UserWallet, USER_ID)
        assert wallet is not None
        transactions = list(
            (
                await db.execute(
                    select(WalletTransaction).order_by(
                        WalletTransaction.created_at.asc(),
                        WalletTransaction.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return wallet, transactions


@pytest.mark.asyncio
async def test_agent_hold_to_actual_settlement_conserves_wallet_and_replays_once(
    billing_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run("agent-actual", 10_000)
    await _hold(billing_db, run_id=run.id, amount=run.text_hold_micro)

    async def disallow_negative() -> bool:
        return False

    monkeypatch.setattr(worker_billing, "allow_negative_balance", disallow_negative)
    usage = {
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_write_1h_tokens": 0,
        "reasoning_tokens": 0,
    }
    async with billing_db() as db:
        async with db.begin():
            result = await settle_agent_text_actual(db, run=run, usage=usage)
        assert result.action == "settled"
        assert result.actual_micro == 2_000

    wallet, transactions = await _wallet_and_transactions(billing_db)
    assert (wallet.balance_micro, wallet.hold_micro, wallet.lifetime_spend_micro) == (
        98_000,
        0,
        2_000,
    )
    assert wallet.balance_micro + wallet.lifetime_spend_micro == INITIAL_BALANCE
    assert [transaction.kind for transaction in transactions] == ["hold", "settle"]

    async with billing_db() as db:
        async with db.begin():
            replay = await settle_agent_text_actual(db, run=run, usage=usage)
        assert replay.actual_micro == 2_000
    _wallet, replayed_transactions = await _wallet_and_transactions(billing_db)
    assert len(replayed_transactions) == 2


@pytest.mark.asyncio
async def test_agent_proven_absent_release_restores_full_hold(
    billing_db: async_sessionmaker[AsyncSession],
) -> None:
    run = _run("agent-release", 7_000)
    await _hold(billing_db, run_id=run.id, amount=run.text_hold_micro)
    async with billing_db() as db:
        async with db.begin():
            result = await release_agent_text_hold(
                db,
                run=run,
                reason="provider_not_dispatched",
            )
        assert result.action == "released"
        assert result.actual_micro == 0

    wallet, transactions = await _wallet_and_transactions(billing_db)
    assert (wallet.balance_micro, wallet.hold_micro, wallet.lifetime_spend_micro) == (
        INITIAL_BALANCE,
        0,
        0,
    )
    assert [transaction.kind for transaction in transactions] == ["hold", "release"]


@pytest.mark.asyncio
async def test_agent_unknown_settlement_consumes_exactly_the_reserved_hold(
    billing_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run("agent-unknown", 6_000)
    await _hold(billing_db, run_id=run.id, amount=run.text_hold_micro)

    async def disallow_negative() -> bool:
        return False

    monkeypatch.setattr(worker_billing, "allow_negative_balance", disallow_negative)
    async with billing_db() as db:
        async with db.begin():
            result = await settle_agent_text_unknown(
                db,
                run=run,
                reason="provider_result_unknown",
            )
        assert result.action == "settled"
        assert result.actual_micro == 6_000

    wallet, transactions = await _wallet_and_transactions(billing_db)
    assert (wallet.balance_micro, wallet.hold_micro, wallet.lifetime_spend_micro) == (
        94_000,
        0,
        6_000,
    )
    assert wallet.balance_micro + wallet.lifetime_spend_micro == INITIAL_BALANCE
    assert [transaction.kind for transaction in transactions] == ["hold", "settle"]
    assert transactions[-1].meta["upstream_cost_knowledge"] == "unknown"
