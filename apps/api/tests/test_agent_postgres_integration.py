from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.services.agent import message_submission as agent_messages
from app.services.agent import runs as agent_runs
from app.services.agent import tools as agent_tools
from app.services.agent.common import AgentProviderPreflight, AgentTextReservation
from lumen_core.agent_capability import AgentCapabilityClaims
from lumen_core.agent_events import AGENT_TOOL_CREATE_IMAGE
from lumen_core.model_base import Base
from lumen_core.model_entities import (
    AgentCapabilityGrant,
    AgentRun,
    AgentRunReference,
    AgentSession,
    AgentToolCall,
    ApiSupplierTemplate,
    AuditLog,
    Conversation,
    Generation,
    Image,
    Message,
    OutboxEvent,
    PricingRule,
    SystemSetting,
    SystemPrompt,
    User,
    UserApiCredential,
    UserMemoryScope,
    UserWallet,
    WalletTransaction,
)
from lumen_core.schema_models import AgentMessageCreateIn, AgentToolCreateImageIn


def _postgres_url() -> str:
    raw = os.getenv("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is not configured")
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not raw.startswith("postgresql+asyncpg://"):
        pytest.fail("LUMEN_TEST_POSTGRES_URL must use PostgreSQL")
    return raw


_AGENT_TABLES = (
    User,
    ApiSupplierTemplate,
    SystemPrompt,
    UserMemoryScope,
    Conversation,
    UserApiCredential,
    AgentSession,
    Message,
    AgentRun,
    Generation,
    AgentCapabilityGrant,
    AgentToolCall,
    Image,
    AgentRunReference,
    OutboxEvent,
    AuditLog,
    SystemSetting,
    PricingRule,
    UserWallet,
    WalletTransaction,
)


@pytest_asyncio.fixture
async def agent_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = _postgres_url()
    schema = f"test_agent_{uuid.uuid4().hex[:12]}"
    admin_engine = create_async_engine(url, pool_pre_ping=True)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[model.__table__ for model in _AGENT_TABLES],
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await _patch_dependencies(monkeypatch)
        yield factory
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _patch_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    async def active_snapshot(
        db: AsyncSession,
        user_id: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        user = await db.get(User, user_id)
        assert user is not None
        return SimpleNamespace(user=user, account_mode=user.account_mode)

    async def provider(*_args: Any, **_kwargs: Any) -> AgentProviderPreflight:
        return AgentProviderPreflight("agent-postgres-model", ("provider",))

    async def reserve(*_args: Any, **_kwargs: Any) -> AgentTextReservation:
        return AgentTextReservation(0, {})

    async def setting(_db: Any, key: str, _default: int | None = None) -> int:
        return {
            "agent.max_turns": 6,
            "agent.max_tool_calls": 3,
            "agent.max_image_tool_calls": 2,
            "agent.max_images_per_run": 4,
            "agent.max_reference_images": 4,
            "agent.max_output_tokens": 4096,
            "agent.run_timeout_seconds": 180,
            "agent.tool_timeout_seconds": 30,
            "agent.capability_ttl_seconds": 120,
        }[key]

    async def no_audit(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def no_publish(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_system_prompt(*_args: Any, **_kwargs: Any) -> None:
        return None

    class Redis:
        async def set(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(agent_messages, "lock_active_user_snapshot", active_snapshot)
    monkeypatch.setattr(agent_messages, "wallet_chat_provider_preflight", provider)
    monkeypatch.setattr(agent_messages, "reserve_agent_text", reserve)
    monkeypatch.setattr(agent_messages, "agent_setting_int", setting)
    monkeypatch.setattr(agent_messages, "resolve_system_prompt_for_message", no_system_prompt)
    monkeypatch.setattr(agent_messages, "write_audit", no_audit)
    monkeypatch.setattr(agent_messages, "publish_agent_events_best_effort", no_publish)
    monkeypatch.setattr(agent_tools, "lock_active_user", no_lock)
    monkeypatch.setattr(agent_tools, "agent_setting_int", setting)
    monkeypatch.setattr(
        agent_tools,
        "wallet_image_provider_preflight",
        lambda *_args, **_kwargs: _async_value(("provider",)),
    )
    monkeypatch.setattr(agent_tools, "write_audit", no_audit)
    monkeypatch.setattr(agent_tools, "publish_agent_events_best_effort", no_publish)
    monkeypatch.setattr(agent_tools, "publish_assistant_task", no_publish)
    monkeypatch.setattr(agent_tools, "get_redis", Redis)
    monkeypatch.setattr(agent_runs, "lock_active_user_snapshot", active_snapshot)
    monkeypatch.setattr(agent_runs, "write_audit", no_audit)
    monkeypatch.setattr(agent_runs, "publish_agent_events_best_effort", no_publish)
    monkeypatch.setattr(agent_runs, "get_redis", Redis)


async def _async_value(value: Any) -> Any:
    return value


async def _seed_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[str, str]:
    user_id = f"agent-user-{suffix}"
    session_id = f"agent-session-{suffix}"
    async with factory() as db:
        user = User(
            id=user_id,
            email=f"{user_id}@example.test",
            display_name="Agent PG",
            account_mode="wallet",
        )
        conversation = Conversation(
            id=f"agent-conversation-{suffix}",
            user_id=user_id,
            title="Agent PostgreSQL",
            default_params={},
        )
        session = AgentSession(
            id=session_id,
            user_id=user_id,
            conversation_id=conversation.id,
            runtime_version="",
        )
        db.add_all([user, conversation])
        await db.flush()
        db.add(session)
        await db.commit()
    return user_id, session_id


@pytest.mark.asyncio
async def test_concurrent_duplicate_agent_messages_create_one_run_and_one_hold(
    agent_postgres: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, session_id = await _seed_session(agent_postgres, suffix="idempotency")
    body = AgentMessageCreateIn(
        idempotency_key="concurrent-message-key",
        text="concurrent Agent submission",
    )
    reserve_calls = 0

    async def counted_reserve(*_args: Any, **_kwargs: Any) -> AgentTextReservation:
        nonlocal reserve_calls
        reserve_calls += 1
        return AgentTextReservation(0, {})

    monkeypatch.setattr(agent_messages, "reserve_agent_text", counted_reserve)
    start = asyncio.Event()

    async def submit() -> Any:
        async with agent_postgres() as db:
            user = await db.get(User, user_id)
            assert user is not None
            await start.wait()
            return await agent_messages.submit_agent_message(
                db,
                session_id=session_id,
                user=user,
                body=body,
                request=None,
            )

    tasks = [asyncio.create_task(submit()), asyncio.create_task(submit())]
    start.set()
    first, second = await asyncio.gather(*tasks)

    assert first.agent_run.id == second.agent_run.id
    assert first.user_message.id == second.user_message.id
    assert reserve_calls == 1
    async with agent_postgres() as db:
        assert await db.scalar(select(func.count(AgentRun.id))) == 1
        assert await db.scalar(select(func.count(Message.id))) == 2


async def _seed_running_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[str, str, AgentCapabilityClaims]:
    user_id, session_id = await _seed_session(factory, suffix=suffix)
    now = datetime.now(timezone.utc)
    async with factory() as db:
        session = await db.get(AgentSession, session_id)
        assert session is not None
        user_message = Message(
            id=f"agent-user-message-{suffix}",
            conversation_id=session.conversation_id,
            role="user",
            content={"source": "agent", "text": "create image"},
            intent="agent",
        )
        assistant_message = Message(
            id=f"agent-asst-message-{suffix}",
            conversation_id=session.conversation_id,
            role="assistant",
            content={"source": "agent", "text": "", "tool_calls": []},
            parent_message_id=user_message.id,
            intent="agent",
            status="streaming",
        )
        db.add_all([user_message, assistant_message])
        await db.flush()
        run = AgentRun(
            id=f"agent-run-{suffix}",
            agent_session_id=session_id,
            user_id=user_id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            status="running",
            execution_epoch=1,
            idempotency_key=f"run-key-{suffix}",
            request_fingerprint="a" * 64,
            request_snapshot_jsonb={
                "allowed_tools": [AGENT_TOOL_CREATE_IMAGE],
                "image_defaults": {
                    "count": 1,
                    "aspect_ratio": "1:1",
                    "quality": "1k",
                    "render_quality": "low",
                    "background": "auto",
                    "output_format": "webp",
                },
                "limits": {
                    "max_tool_calls": 3,
                    "max_image_tool_calls": 2,
                    "max_images_per_run": 4,
                },
            },
            account_mode_snapshot="wallet",
            model="agent-postgres-model",
        )
        claims = AgentCapabilityClaims(
            capability_id=f"capability-{suffix}-123456",
            nonce=f"nonce-{suffix}-1234567890",
            run_id=run.id,
            user_id=user_id,
            agent_session_id=session_id,
            execution_epoch=1,
            allowed_tools=[AGENT_TOOL_CREATE_IMAGE],
            allowed_reference_labels=[],
            issued_at=int(now.timestamp()),
            expires_at=int((now + timedelta(minutes=2)).timestamp()),
        )
        grant = AgentCapabilityGrant(
            capability_id=claims.capability_id,
            nonce=claims.nonce,
            agent_run_id=run.id,
            user_id=user_id,
            agent_session_id=session_id,
            execution_epoch=1,
            expires_at=now + timedelta(minutes=2),
            max_redemptions=3,
            redeemed_count=0,
        )
        db.add(run)
        await db.flush()
        db.add(grant)
        await db.commit()
        return user_id, run.id, claims


def _tool_body() -> AgentToolCreateImageIn:
    return AgentToolCreateImageIn(
        pi_tool_call_id="postgres-race-tool",
        ordinal=0,
        execution_epoch=1,
        arguments={"prompt": "single billed image", "count": 1},
    )


@pytest.mark.asyncio
async def test_billed_image_tool_replay_creates_one_generation_and_one_hold(
    agent_postgres: async_sessionmaker[AsyncSession],
) -> None:
    user_id, run_id, claims = await _seed_running_run(
        agent_postgres,
        suffix="billed-replay",
    )
    async with agent_postgres() as db:
        db.add(
            UserWallet(
                user_id=user_id,
                balance_micro=100_000,
                hold_micro=0,
                lifetime_topup_micro=100_000,
                lifetime_spend_micro=0,
                version=0,
            )
        )
        db.add(
            PricingRule(
                scope="image_size",
                key="1k",
                variant="default",
                unit="per_image",
                price_micro=5_000,
                enabled=True,
            )
        )
        db.add(SystemSetting(key="billing.enabled", value="1"))
        await db.commit()

    async with agent_postgres() as db:
        first = await agent_tools.submit_create_image_tool(
            db,
            run_id=run_id,
            claims=claims,
            body=_tool_body(),
        )
    async with agent_postgres() as db:
        replay = await agent_tools.submit_create_image_tool(
            db,
            run_id=run_id,
            claims=claims,
            body=_tool_body(),
        )

    assert replay.replayed is True
    assert replay.generation_ids == first.generation_ids
    async with agent_postgres() as db:
        assert await db.scalar(select(func.count(Generation.id))) == 1
        holds = list(
            (
                await db.execute(
                    select(WalletTransaction).where(
                        WalletTransaction.kind == "hold",
                        WalletTransaction.ref_type == "generation",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(holds) == 1
        assert holds[0].ref_id == first.generation_ids[0]
        wallet = await db.get(UserWallet, user_id)
        assert wallet is not None
        assert (wallet.balance_micro, wallet.hold_micro) == (95_000, 5_000)


async def _fake_generation_batch(command: Any) -> Any:
    return SimpleNamespace(
        generation_ids=["postgres-race-generation"],
        assistant_msg=command.assistant_msg,
        outbox_payloads=[],
        outbox_rows=[],
    )


@pytest.mark.asyncio
async def test_tool_commit_then_cancel_serializes_without_duplicate_side_effect(
    agent_postgres: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, run_id, claims = await _seed_running_run(
        agent_postgres,
        suffix="tool-first",
    )
    locked = asyncio.Event()
    release = asyncio.Event()
    enforce_limits = agent_tools._enforce_tool_limits  # noqa: SLF001

    async def pausing_limits(*args: Any, **kwargs: Any) -> None:
        await enforce_limits(*args, **kwargs)
        locked.set()
        await release.wait()

    monkeypatch.setattr(agent_tools, "_enforce_tool_limits", pausing_limits)
    monkeypatch.setattr(
        agent_tools,
        "create_generation_batch_for_message",
        _fake_generation_batch,
    )

    async def submit_tool() -> Any:
        async with agent_postgres() as db:
            return await agent_tools.submit_create_image_tool(
                db,
                run_id=run_id,
                claims=claims,
                body=_tool_body(),
            )

    async def cancel() -> Any:
        async with agent_postgres() as db:
            user = await db.get(User, user_id)
            assert user is not None
            return await agent_runs.cancel_agent_run(
                db,
                run_id=run_id,
                user=user,
                request=None,
            )

    tool_task = asyncio.create_task(submit_tool())
    await asyncio.wait_for(locked.wait(), timeout=5)
    cancel_task = asyncio.create_task(cancel())
    await asyncio.sleep(0.1)
    assert not cancel_task.done()
    release.set()
    tool_result, cancel_result = await asyncio.gather(tool_task, cancel_task)

    assert tool_result.generation_ids == ["postgres-race-generation"]
    assert cancel_result.status == "cancelled"
    async with agent_postgres() as db:
        assert await db.scalar(select(func.count(AgentToolCall.id))) == 1
        run = await db.get(AgentRun, run_id)
        assert run is not None and run.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_commit_then_tool_submit_is_rejected_before_side_effect(
    agent_postgres: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, run_id, claims = await _seed_running_run(
        agent_postgres,
        suffix="cancel-first",
    )
    locked = asyncio.Event()
    release = asyncio.Event()

    async def pausing_audit(*_args: Any, **_kwargs: Any) -> bool:
        locked.set()
        await release.wait()
        return True

    monkeypatch.setattr(agent_runs, "write_audit", pausing_audit)
    monkeypatch.setattr(
        agent_tools,
        "create_generation_batch_for_message",
        _fake_generation_batch,
    )

    async def cancel() -> Any:
        async with agent_postgres() as db:
            user = await db.get(User, user_id)
            assert user is not None
            return await agent_runs.cancel_agent_run(
                db,
                run_id=run_id,
                user=user,
                request=None,
            )

    async def submit_tool() -> Any:
        async with agent_postgres() as db:
            return await agent_tools.submit_create_image_tool(
                db,
                run_id=run_id,
                claims=claims,
                body=_tool_body(),
            )

    cancel_task = asyncio.create_task(cancel())
    await asyncio.wait_for(locked.wait(), timeout=5)
    tool_task = asyncio.create_task(submit_tool())
    await asyncio.sleep(0.1)
    assert not tool_task.done()
    release.set()
    cancel_result = await cancel_task
    with pytest.raises(HTTPException) as captured:
        await tool_task

    assert cancel_result.status == "cancelled"
    assert captured.value.detail["error"]["code"] == "agent_stale_execution_epoch"
    async with agent_postgres() as db:
        assert await db.scalar(select(func.count(AgentToolCall.id))) == 0
