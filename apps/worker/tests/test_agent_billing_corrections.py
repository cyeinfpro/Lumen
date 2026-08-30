from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import agent_billing_corrections as corrections
from app.agent_billing_corrections import (
    AgentBillingCorrection,
    correct_unknown_agent_charge,
)
from lumen_core import billing as billing_core
from lumen_core.model_base import Base
from lumen_core.model_entities import (
    AgentRun,
    AgentSession,
    AuditLog,
    Conversation,
    Message,
    User,
    UserWallet,
    WalletTransaction,
)
from lumen_core.pricing import ModelPricing


USER_ID = "agent-correction-user"
RUN_ID = "agent-correction-run"


@pytest_asyncio.fixture
async def correction_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
                    Conversation.__table__,
                    Message.__table__,
                    AgentSession.__table__,
                    AgentRun.__table__,
                    UserWallet.__table__,
                    WalletTransaction.__table__,
                    AuditLog.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    pricing = ModelPricing(
        input_per_1k_micro=1_000,
        output_per_1k_micro=2_000,
        pricing_source="admin",
    ).with_defaults()
    async with factory() as db:
        user = User(
            id=USER_ID,
            email="agent-correction@example.test",
            display_name="Correction",
            account_mode="wallet",
        )
        conversation = Conversation(
            id="agent-correction-conversation",
            user_id=USER_ID,
            title="Correction",
            default_params={},
        )
        session = AgentSession(
            id="agent-correction-session",
            user_id=USER_ID,
            conversation_id=conversation.id,
        )
        user_message = Message(
            id="agent-correction-user-message",
            conversation_id=conversation.id,
            role="user",
            content={"source": "agent", "text": "hello"},
            intent="agent",
        )
        assistant = Message(
            id="agent-correction-assistant-message",
            conversation_id=conversation.id,
            role="assistant",
            content={"source": "agent", "text": ""},
            intent="agent",
        )
        run = AgentRun(
            id=RUN_ID,
            agent_session_id=session.id,
            user_id=USER_ID,
            user_message_id=user_message.id,
            assistant_message_id=assistant.id,
            status="failed",
            idempotency_key="correction-idempotency",
            request_fingerprint="f" * 64,
            account_mode_snapshot="wallet",
            model="agent-model",
            text_hold_micro=0,
            billing_jsonb={
                "pricing_snapshot": pricing.model_dump(),
                "rate_multiplier_x10000": 10_000,
            },
            dispatch_jsonb={"provider_dispatch_count": 1},
            usage_jsonb={},
        )
        db.add_all(
            [
                user,
                conversation,
                session,
                user_message,
                assistant,
                run,
                UserWallet(
                    user_id=USER_ID,
                    balance_micro=100_000,
                    hold_micro=0,
                    lifetime_topup_micro=100_000,
                    lifetime_spend_micro=0,
                    version=0,
                ),
            ]
        )
        await db.flush()
        await billing_core.hold(
            db,
            USER_ID,
            6_000,
            ref_type="agent_run",
            ref_id=RUN_ID,
            idempotency_key=f"hold:{RUN_ID}",
        )
        await billing_core.settle(
            db,
            USER_ID,
            ref_type="agent_run",
            ref_id=RUN_ID,
            actual_micro=6_000,
            idempotency_key=f"settle:{RUN_ID}",
            meta={
                "agent_run_id": RUN_ID,
                "actual_micro": 6_000,
                "upstream_cost_knowledge": "unknown",
                "tier_source": "upstream_result_unknown",
            },
        )
        await db.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_charge_correction_is_dry_run_idempotent_and_append_only(
    correction_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(corrections, "SessionLocal", correction_db)
    batch_preview = await corrections.run_agent_unknown_charge_backfill(
        dry_run=True,
        batch_size=1,
    )
    assert batch_preview["scanned"] == 1
    assert batch_preview["candidates"] == 1
    async with correction_db() as db:
        async with db.begin():
            preview = await correct_unknown_agent_charge(
                db,
                run_id=RUN_ID,
                dry_run=True,
            )
    assert preview is not None
    assert preview.credit_micro == 6_000
    assert preview.applied is False

    async with correction_db() as db:
        async with db.begin():
            applied = await correct_unknown_agent_charge(
                db,
                run_id=RUN_ID,
                dry_run=False,
            )
    assert applied is not None and applied.applied is True

    async with correction_db() as db:
        async with db.begin():
            replay = await correct_unknown_agent_charge(
                db,
                run_id=RUN_ID,
                dry_run=False,
            )
    assert replay is None

    async with correction_db() as db:
        wallet = await db.get(UserWallet, USER_ID)
        run = await db.get(AgentRun, RUN_ID)
        transactions = list(
            (
                await db.execute(
                    select(WalletTransaction).order_by(
                        WalletTransaction.created_at,
                        WalletTransaction.id,
                    )
                )
            ).scalars()
        )
        audits = list((await db.execute(select(AuditLog))).scalars())
    assert wallet is not None
    assert (wallet.balance_micro, wallet.hold_micro, wallet.lifetime_spend_micro) == (
        100_000,
        0,
        0,
    )
    assert [transaction.kind for transaction in transactions] == [
        "hold",
        "settle",
        "correction_credit",
    ]
    assert run is not None
    assert run.billing_jsonb["unknown_result_correction"]["credited_micro"] == 6_000
    assert [audit.event_type for audit in audits] == [
        "billing.agent_unknown_result_corrected"
    ]
    rerun = await corrections.run_agent_unknown_charge_backfill(
        dry_run=False,
        batch_size=1,
    )
    assert rerun["scanned"] == 0
    assert rerun["applied"] == 0


@pytest.mark.asyncio
async def test_zero_credit_legacy_row_gets_durable_correction_marker(
    correction_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(corrections, "SessionLocal", correction_db)
    async with correction_db() as db:
        async with db.begin():
            run = await db.get(AgentRun, RUN_ID)
            assert run is not None
            run.usage_jsonb = {
                "input_tokens": 0,
                "output_tokens": 3_000,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cache_write_1h_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 3_000,
            }
            correction = await correct_unknown_agent_charge(
                db,
                run_id=RUN_ID,
                dry_run=False,
            )
    assert correction is not None
    assert correction.credit_micro == 0
    assert correction.applied is True

    async with correction_db() as db:
        marker = (
            await db.execute(
                select(WalletTransaction).where(
                    WalletTransaction.idempotency_key
                    == f"agent-unknown-correction:{RUN_ID}:v1"
                )
            )
        ).scalar_one()
    assert marker.kind == "correction_credit"
    assert marker.amount_micro == 0
    assert marker.meta["zero_credit_marker"] is True
    rerun = await corrections.run_agent_unknown_charge_backfill(
        dry_run=False,
        batch_size=1,
    )
    assert rerun["scanned"] == 0


@pytest.mark.asyncio
async def test_backfill_drains_multiple_pages_and_skips_corrected_or_zero_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_cursors: list[str | None] = []

    async def page(*, after_transaction_id: str | None, limit: int):
        assert limit == 2
        page_cursors.append(after_transaction_id)
        return {
            None: [("tx-1", "run-1"), ("tx-2", "run-2")],
            "tx-2": [("tx-3", "run-3")],
        }.get(after_transaction_id, [])

    async def correct(_db, *, run_id: str, dry_run: bool):
        assert dry_run is False
        return {
            "run-1": AgentBillingCorrection("run-1", 10, 0, 10, True),
            "run-2": None,
            "run-3": AgentBillingCorrection("run-3", 5, 5, 0, False),
        }[run_id]

    class Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def begin(self):
            return Transaction()

    monkeypatch.setattr(corrections, "_legacy_unknown_page", page)
    monkeypatch.setattr(corrections, "correct_unknown_agent_charge", correct)
    monkeypatch.setattr(corrections, "SessionLocal", Session)

    result = await corrections.run_agent_unknown_charge_backfill(
        dry_run=False,
        batch_size=2,
    )

    assert page_cursors == [None, "tx-2"]
    assert result == {
        "dry_run": False,
        "scanned": 3,
        "candidates": 1,
        "applied": 1,
        "credited_micro": 10,
        "last_transaction_id": "tx-3",
        "items": [
            {
                "run_id": "run-1",
                "charged_micro": 10,
                "evidenced_micro": 0,
                "credit_micro": 10,
                "applied": True,
            }
        ],
    }
