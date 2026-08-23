from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.tasks.agent_run_parts import persistence
from app.agent_runtime_client import AgentRuntimeEvent
from lumen_core.model_base import Base
from lumen_core.model_entities import (
    AgentRun,
    AgentSession,
    AgentToolCall,
    Conversation,
    Generation,
    Message,
    OutboxEvent,
    User,
)


@pytest_asyncio.fixture
async def agent_db(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    generation_default = Generation.__table__.c.input_image_ids.server_default
    Generation.__table__.c.input_image_ids.server_default = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        User.__table__,
                        Conversation.__table__,
                        AgentSession.__table__,
                        Message.__table__,
                        AgentRun.__table__,
                        AgentToolCall.__table__,
                        Generation.__table__,
                        OutboxEvent.__table__,
                    ],
                )
            )
    finally:
        Generation.__table__.c.input_image_ids.server_default = generation_default
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(persistence, "SessionLocal", factory)

    async def no_publish(_redis: object, _data: dict[str, object]) -> None:
        return None

    monkeypatch.setattr(persistence, "publish_agent_event_fast_path", no_publish)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        user = User(
            id="user-agent-persist",
            email="agent-persist@example.com",
            display_name="Agent",
            account_mode="byok",
        )
        conversation = Conversation(
            id="conversation-agent-persist",
            user_id=user.id,
            title="Agent persistence",
            default_params={},
        )
        session = AgentSession(
            id="session-agent-persist",
            user_id=user.id,
            conversation_id=conversation.id,
            runtime_version="pi-0.84.2",
        )
        user_message = Message(
            id="message-user-persist",
            conversation_id=conversation.id,
            role="user",
            content={"source": "agent", "text": "Create an image"},
            intent="agent",
        )
        assistant = Message(
            id="message-assistant-persist",
            conversation_id=conversation.id,
            role="assistant",
            content={
                "source": "agent",
                "agent_run_id": "run-agent-persist",
                "text": "",
                "tool_calls": [],
                "generation_ids": [],
            },
            parent_message_id=user_message.id,
            intent="agent",
            status="pending",
        )
        run = AgentRun(
            id="run-agent-persist",
            agent_session_id=session.id,
            user_id=user.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant.id,
            status="queued",
            execution_epoch=0,
            attempt=0,
            last_event_seq=0,
            idempotency_key="persist-key",
            request_fingerprint="f" * 64,
            request_snapshot_jsonb={
                "limits": {"max_turns": 6},
                "allowed_tools": ["lumen_create_image"],
            },
            account_mode_snapshot="byok",
            model="gpt-agent",
            text_hold_micro=0,
            billing_jsonb={},
            dispatch_jsonb={},
            usage_jsonb={},
            turn_count=0,
            tool_call_count=0,
        )
        db.add_all([user, conversation, session, user_message, assistant, run])
        await db.commit()


@pytest.mark.asyncio
async def test_pi_compaction_checkpoint_is_epoch_fenced_and_usage_accounted(
    agent_db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(agent_db)
    claim, _started = await persistence.claim_agent_run("run-agent-persist")
    event = AgentRuntimeEvent(
        version=1,
        type="compaction.completed",
        seq=2,
        run_id=claim.run_id,
        execution_epoch=claim.execution_epoch,
        checkpoint_version=1,
        pi_runtime_version="pi-0.84.2",
        summary="## Goal\nPreserve the complete Agent task context.",
        first_kept_message_id="message-user-persist",
        tokens_before=260_000,
        provider_call_count=1,
        usage={
            "input_tokens": 120,
            "output_tokens": 20,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_write_1h_tokens": 0,
            "reasoning_tokens": 5,
            "total_tokens": 140,
        },
    )

    assert not await persistence.record_runtime_checkpoint(
        claim.run_id,
        claim.execution_epoch + 1,
        event,
    )
    assert await persistence.record_runtime_checkpoint(
        claim.run_id,
        claim.execution_epoch,
        event,
    )
    assert await persistence.record_runtime_checkpoint(
        claim.run_id,
        claim.execution_epoch,
        event,
    )

    async with agent_db() as db:
        run = await db.get(AgentRun, claim.run_id)
        assert run is not None
        checkpoint = run.dispatch_jsonb["pi_compaction"]
        assert checkpoint["first_kept_message_id"] == "message-user-persist"
        assert checkpoint["tokens_before"] == 260_000
        assert checkpoint["next_message_id"] == run.user_message_id
        assert checkpoint["source_run_id"] == run.id
        assert checkpoint["source_execution_epoch"] == claim.execution_epoch
        assert run.usage_jsonb["input_tokens"] == 120
        assert run.usage_jsonb["reasoning_tokens"] == 5
        assert run.dispatch_jsonb["pi_compaction_count"] == 1
        assert run.dispatch_jsonb["provider_completed_count"] == 1
        assert run.dispatch_jsonb["runtime_delivery"] == "compaction_ready"

    reclaimed, _started = await persistence.claim_agent_run(claim.run_id)
    assert reclaimed.action == "result_unknown"
    assert reclaimed.execution_epoch == claim.execution_epoch


@pytest.mark.asyncio
async def test_agent_claim_flush_epoch_and_partial_terminal_are_atomic(
    agent_db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(agent_db)

    claim, started = await persistence.claim_agent_run("run-agent-persist")

    assert claim.action == "execute"
    assert claim.execution_epoch == 1
    assert started is not None
    assert (
        await persistence.flush_agent_text(
            object(),
            run_id=claim.run_id,
            execution_epoch=0,
            text="stale",
            delta="stale",
        )
        is False
    )
    assert (
        await persistence.flush_agent_text(
            object(),
            run_id=claim.run_id,
            execution_epoch=claim.execution_epoch,
            text="Image submission started.",
            delta="Image submission started.",
        )
        is True
    )

    async with agent_db() as db:
        run = await db.get(AgentRun, claim.run_id)
        message = await db.get(Message, "message-assistant-persist")
        assert run is not None and run.status == "running"
        assert run.execution_epoch == 1
        assert run.last_event_seq == 2
        assert message is not None
        assert message.content["text"] == "Image submission started."
        events = list(
            (
                await db.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.kind == "sse")
                    .order_by(OutboxEvent.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        assert [event.payload["event_name"] for event in events] == [
            "agent.run.started",
            "agent.output.delta",
        ]
        assert events[-1].payload["data"]["text_delta"] == ("Image submission started.")

        tool = AgentToolCall(
            id="tool-agent-persist",
            agent_run_id=run.id,
            capability_id="capability-agent-persist",
            pi_tool_call_id="pi-tool-agent-persist",
            ordinal=0,
            execution_epoch=run.execution_epoch,
            name="lumen_create_image",
            mode="text_to_image",
            status="succeeded",
            request_hash="a" * 64,
            semantic_key="b" * 64,
            arguments_jsonb={"prompt": "Create an image"},
            result_jsonb={"generation_ids": ["generation-agent-persist"]},
            generation_count=1,
        )
        generation = Generation(
            id="generation-agent-persist",
            message_id=run.assistant_message_id,
            user_id=run.user_id,
            action="generate",
            model="image-model",
            prompt="Create an image",
            size_requested="1024x1024",
            aspect_ratio="1:1",
            input_image_ids=[],
            upstream_request={
                "source": "agent",
                "agent_tool_call_id": tool.id,
            },
            status="queued",
            progress_stage="queued",
            attempt=0,
            idempotency_key="generation-agent-key",
        )
        db.add_all([tool, generation])
        await db.commit()

    status, billing_result, conversation_id = await persistence.finalize_agent_run(
        object(),
        run_id=claim.run_id,
        execution_epoch=claim.execution_epoch,
        requested_status="failed",
        text="Image submission started.",
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": 0,
        },
        turn_count=1,
        runtime_tool_count=1,
        error_code="agent_runtime_disconnected",
        knowledge="proven_absent",
        reason="test_disconnect",
    )

    assert status == "partial"
    assert billing_result is not None
    assert billing_result.action == "not_applicable"
    assert conversation_id == "conversation-agent-persist"
    async with agent_db() as db:
        run = await db.get(AgentRun, claim.run_id)
        message = await db.get(Message, "message-assistant-persist")
        assert run is not None and run.status == "partial"
        assert run.text_hold_micro == 0
        assert run.last_event_seq == 3
        assert message is not None and message.status == "partial"
        assert message.content["generation_ids"] == ["generation-agent-persist"]
        memory_events = list(
            (
                await db.execute(
                    select(OutboxEvent).where(OutboxEvent.kind == "memory_extract")
                )
            )
            .scalars()
            .all()
        )
        assert len(memory_events) == 1


@pytest.mark.asyncio
async def test_runtime_timeout_with_durable_text_is_persisted_as_partial(
    agent_db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(agent_db)
    claim, _started = await persistence.claim_agent_run("run-agent-persist")
    assert await persistence.flush_agent_text(
        object(),
        run_id=claim.run_id,
        execution_epoch=claim.execution_epoch,
        text="durable before restart",
        delta="durable before restart",
    )

    status, _billing, _conversation = await persistence.finalize_agent_run(
        object(),
        run_id=claim.run_id,
        execution_epoch=claim.execution_epoch,
        requested_status="failed",
        text="",
        usage={},
        turn_count=1,
        runtime_tool_count=0,
        error_code="agent_run_timeout",
        knowledge="unknown",
        reason="recovered_terminal",
    )

    assert status == "partial"
    async with agent_db() as db:
        run = await db.get(AgentRun, claim.run_id)
        message = await db.get(Message, "message-assistant-persist")
        assert run is not None and run.error_code == "agent_run_timeout"
        assert message is not None and message.status == "partial"
        assert message.content["text"] == "durable before restart"


@pytest.mark.asyncio
async def test_runtime_heartbeat_advances_only_the_private_checkpoint(
    agent_db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(agent_db)
    claim, _started = await persistence.claim_agent_run("run-agent-persist")
    stale = datetime(2000, 1, 1)
    async with agent_db() as db:
        run = await db.get(AgentRun, claim.run_id)
        assert run is not None
        run.updated_at = stale
        await db.commit()

    assert await persistence.record_runtime_checkpoint(
        claim.run_id,
        claim.execution_epoch,
        AgentRuntimeEvent(
            version=1,
            type="run.heartbeat",
            seq=1,
            run_id=claim.run_id,
            execution_epoch=claim.execution_epoch,
        ),
    )

    async with agent_db() as db:
        run = await db.get(AgentRun, claim.run_id)
        assert run is not None
        assert run.dispatch_jsonb["last_runtime_seq"] == 1
        assert "runtime_heartbeat_at" in run.dispatch_jsonb
        assert run.updated_at > stale
        assert run.last_event_seq == 1
        public_events = list(
            (await db.execute(select(OutboxEvent).where(OutboxEvent.kind == "sse")))
            .scalars()
            .all()
        )
        assert [item.payload["event_name"] for item in public_events] == [
            "agent.run.started"
        ]


@pytest.mark.asyncio
async def test_runtime_checkpoints_accumulate_turns_and_keep_terminal_zero_monotonic(
    agent_db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(agent_db)
    claim, _started = await persistence.claim_agent_run("run-agent-persist")
    usage = {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_tokens": 1,
        "cache_write_tokens": 0,
        "cache_write_1h_tokens": 0,
        "reasoning_tokens": 1,
        "total_tokens": 13,
    }
    for sequence, turn in ((1, 1), (2, 2)):
        assert await persistence.record_runtime_checkpoint(
            claim.run_id,
            claim.execution_epoch,
            AgentRuntimeEvent(
                version=1,
                type="turn.completed",
                seq=sequence,
                run_id=claim.run_id,
                execution_epoch=claim.execution_epoch,
                turn=turn,
                usage=usage,
            ),
        )
    assert await persistence.record_runtime_checkpoint(
        claim.run_id,
        claim.execution_epoch,
        AgentRuntimeEvent(
            version=1,
            type="run.failed",
            seq=3,
            run_id=claim.run_id,
            execution_epoch=claim.execution_epoch,
            status="failed",
            usage={key: 0 for key in usage},
            turn_count=2,
            tool_call_count=0,
            provider_dispatch_count=3,
            provider_completed_count=2,
        ),
    )

    async with agent_db() as db:
        run = await db.get(AgentRun, claim.run_id)
        assert run is not None
        assert run.usage_jsonb["input_tokens"] == 20
        assert run.usage_jsonb["reasoning_tokens"] == 2
        assert run.dispatch_jsonb["provider_dispatch_count"] == 3
        assert run.dispatch_jsonb["provider_completed_count"] == 2


@pytest.mark.asyncio
async def test_gateway_unreachable_persists_unknown_tool_terminal(
    agent_db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(agent_db)
    claim, _started = await persistence.claim_agent_run("run-agent-persist")
    assert await persistence.record_runtime_checkpoint(
        claim.run_id,
        claim.execution_epoch,
        AgentRuntimeEvent(
            version=1,
            type="tool.failed",
            seq=1,
            run_id=claim.run_id,
            execution_epoch=claim.execution_epoch,
            tool_call_id="pi-unacknowledged",
            ordinal=0,
            name="lumen_create_image",
            mode="text_to_image",
            error_code="agent_tool_result_unknown",
            result_unknown=True,
        ),
    )

    async with agent_db() as db:
        tool = (await db.execute(select(AgentToolCall))).scalar_one()
        assert tool.status == "timed_out"
        assert tool.error_code == "agent_tool_result_unknown"
        assert tool.arguments_jsonb == {}


@pytest.mark.asyncio
async def test_runtime_started_persists_actual_runtime_version(
    agent_db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(agent_db)
    claim, _started = await persistence.claim_agent_run("run-agent-persist")
    assert await persistence.record_runtime_checkpoint(
        claim.run_id,
        claim.execution_epoch,
        AgentRuntimeEvent(
            version=1,
            type="run.started",
            seq=1,
            run_id=claim.run_id,
            execution_epoch=claim.execution_epoch,
            runtime_version="pi-0.84.2+runtime-build",
            tools=[],
        ),
    )
    async with agent_db() as db:
        session = await db.get(AgentSession, "session-agent-persist")
        assert session is not None
        assert session.runtime_version == "pi-0.84.2+runtime-build"


@pytest.mark.asyncio
async def test_cancellation_after_later_dispatch_settles_unknown_not_prior_actual(
    agent_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(agent_db)
    claim, _started = await persistence.claim_agent_run("run-agent-persist")
    zero = {
        "input_tokens": 5,
        "output_tokens": 1,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_write_1h_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 6,
    }
    events = (
        AgentRuntimeEvent(
            version=1,
            type="provider.dispatched",
            seq=1,
            run_id=claim.run_id,
            execution_epoch=claim.execution_epoch,
        ),
        AgentRuntimeEvent(
            version=1,
            type="turn.completed",
            seq=2,
            run_id=claim.run_id,
            execution_epoch=claim.execution_epoch,
            turn=1,
            usage=zero,
        ),
        AgentRuntimeEvent(
            version=1,
            type="provider.dispatched",
            seq=3,
            run_id=claim.run_id,
            execution_epoch=claim.execution_epoch,
        ),
    )
    for event in events:
        assert await persistence.record_runtime_checkpoint(
            claim.run_id,
            claim.execution_epoch,
            event,
        )
    async with agent_db() as db:
        async with db.begin():
            run = await db.get(AgentRun, claim.run_id)
            assert run is not None
            run.status = "cancelled"
            run.execution_epoch += 1
            run.text_hold_micro = 100
            run.billing_jsonb = {"state": "held"}

    calls: list[str] = []

    async def unknown(*_args: object, **_kwargs: object) -> object:
        calls.append("unknown")
        return SimpleNamespace()

    async def unexpected(*_args: object, **_kwargs: object) -> object:
        pytest.fail("cancellation must not settle only the prior turn as actual")

    monkeypatch.setattr(persistence, "settle_agent_text_unknown", unknown)
    monkeypatch.setattr(persistence, "settle_agent_text_actual", unexpected)
    monkeypatch.setattr(persistence, "release_agent_text_hold", unexpected)

    assert await persistence.reconcile_cancelled_agent_hold(claim.run_id)
    assert calls == ["unknown"]
