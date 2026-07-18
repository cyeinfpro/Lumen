from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.tasks import memory_extraction
from app.tasks.memory_extraction_parts.delivery import (
    repair_committed_delivery_state,
)
from lumen_core.memory import ExtractedMemory
from lumen_core.models import (
    Base,
    Conversation,
    MemoryAudit,
    MemoryExtractionRun,
    Message,
    User,
    UserMemory,
    UserMemoryScope,
    UserMemoryStaging,
)


_MEMORY_TABLES = [
    User.__table__,
    UserMemoryScope.__table__,
    Conversation.__table__,
    Message.__table__,
    UserMemory.__table__,
    UserMemoryStaging.__table__,
    MemoryAudit.__table__,
    MemoryExtractionRun.__table__,
]


class _TrackedSessionContext:
    def __init__(self, harness: _DbHarness) -> None:
        self._harness = harness
        self._session = harness.factory()

    async def __aenter__(self) -> AsyncSession:
        session = await self._session.__aenter__()
        self._harness.active_worker_sessions += 1
        return session

    async def __aexit__(self, *args: object) -> None:
        try:
            await self._session.__aexit__(*args)
        finally:
            self._harness.active_worker_sessions -= 1


class _DbHarness:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.active_worker_sessions = 0

    def worker_session(self) -> _TrackedSessionContext:
        return _TrackedSessionContext(self)

    def assert_network_safe(self) -> None:
        assert self.active_worker_sessions == 0


@asynccontextmanager
async def _memory_database(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_DbHarness]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=_MEMORY_TABLES,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    harness = _DbHarness(factory)
    monkeypatch.setattr(memory_extraction, "SessionLocal", harness.worker_session)
    try:
        yield harness
    finally:
        await engine.dispose()


async def _seed_memory_context(
    harness: _DbHarness,
    *,
    source_text: str = "我喜欢简洁回答",
    assistant_ids: tuple[str, ...] = ("assistant-msg-1",),
    with_existing_memory: bool = True,
) -> None:
    async with harness.factory() as session:
        user = User(
            id="user-1",
            email="memory-user@example.test",
            display_name="Memory User",
        )
        scope = UserMemoryScope(
            id="scope-1",
            user_id=user.id,
            name="默认",
            is_default=True,
        )
        conversation = Conversation(
            id="conv-1",
            user_id=user.id,
            title="Memory",
        )
        source = Message(
            id="user-msg-1",
            conversation_id=conversation.id,
            role="user",
            status="succeeded",
            content={"text": source_text},
        )
        assistants = [
            Message(
                id=assistant_id,
                conversation_id=conversation.id,
                role="assistant",
                parent_message_id=source.id,
                status="succeeded",
                content={
                    "text": f"answer:{assistant_id}",
                    "tool_state": {"phase": "done"},
                },
            )
            for assistant_id in assistant_ids
        ]
        session.add_all([user, scope, conversation, source, *assistants])
        if with_existing_memory:
            session.add(
                UserMemory(
                    id="memory-1",
                    user_id=user.id,
                    type="preference",
                    content="用户喜欢简洁回答",
                    source_message_id=source.id,
                    source_excerpt="我喜欢简洁回答",
                    source="auto",
                    embedding="[1.0]",
                    confidence=0.9,
                    scope_id=scope.id,
                )
            )
        await session.commit()


def _prepared_candidate() -> memory_extraction._PreparedMemoryCandidate:
    return memory_extraction._PreparedMemoryCandidate(  # noqa: SLF001
        candidate=ExtractedMemory(
            type="preference",
            content="用户喜欢简洁回答",
            confidence=0.9,
            source_excerpt="我喜欢简洁回答",
            intent_kind="statement",
        ),
        embedding="[1.0]",
    )


class _Redis:
    def __init__(self, harness: _DbHarness, *, fail: bool = False) -> None:
        self.harness = harness
        self.fail = fail
        self.setex_calls: list[tuple[str, int, str]] = []

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.harness.assert_network_safe()
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.setex_calls.append((key, ttl, value))


def test_committed_delivery_repair_is_pure_and_preserves_projection_fields() -> None:
    now = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
    authoritative_write = {
        "id": "memory-1",
        "kind": "added",
        "type": "preference",
        "content": "用户喜欢简洁回答",
        "source_excerpt": "我喜欢简洁回答",
        "undo_token": None,
        "scope_id": "scope-1",
        "recommended_scope_id": "scope-1",
    }
    unrelated_write = {
        "id": "memory-other",
        "kind": "added",
        "type": "project",
        "content": "用户正在做另一个项目",
        "source_excerpt": None,
        "undo_token": "other-token",
        "scope_id": "scope-1",
        "recommended_scope_id": "scope-1",
    }
    stale_projection = {**authoritative_write, "undo_token": "stale-token"}
    raw_operations = [
        {
            "write_index": 0,
            "token": "",
            "payload": {"user_id": "user-1", "action": "added"},
            "expires_at": None,
        },
        {
            "write_index": True,
            "token": "invalid-token",
            "payload": {"user_id": "user-1", "action": "added"},
            "expires_at": None,
        },
    ]

    repair = repair_committed_delivery_state(
        memory_writes=[authoritative_write],
        undo_operations=raw_operations,
        undo_status="corrupt",
        undo_expires_at=None,
        assistant_writes=[unrelated_write, stale_projection],
        now=now,
        undo_ttl_seconds=300,
        token_factory=lambda: "repaired-token",
    )

    assert repair.run_changed is True
    assert repair.assistant_projection_changed is True
    assert repair.undo_status == "pending"
    assert repair.undo_expires_at == now + timedelta(seconds=300)
    assert repair.undo_operations == [
        {
            "write_index": 0,
            "token": "repaired-token",
            "payload": {"user_id": "user-1", "action": "added"},
            "expires_at": (now + timedelta(seconds=300)).isoformat(),
        }
    ]
    assert repair.writes[0]["undo_token"] == "repaired-token"
    assert repair.assistant_projection[0] == unrelated_write
    assert repair.assistant_projection[1]["undo_token"] == "repaired-token"
    assert authoritative_write["undo_token"] is None
    assert raw_operations[0]["token"] == ""


@pytest.mark.asyncio
async def test_network_awaits_run_outside_transactions_and_publish_retry_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _memory_database(monkeypatch) as harness:
        await _seed_memory_context(harness)
        provider_checks = 0
        embedding_calls = 0
        published: list[dict[str, Any]] = []
        redis = _Redis(harness)

        async def provider_available(_ctx: dict[str, Any] | None) -> bool:
            nonlocal provider_checks
            harness.assert_network_safe()
            provider_checks += 1
            return True

        async def no_llm_candidates(*_args: Any, **_kwargs: Any) -> list[Any]:
            harness.assert_network_safe()
            return []

        async def embedding_literal(
            _ctx: dict[str, Any] | None,
            content: str,
        ) -> str:
            nonlocal embedding_calls
            harness.assert_network_safe()
            assert content == "用户喜欢简洁回答"
            embedding_calls += 1
            return "[1.0]"

        async def publish_event(
            _redis: Any,
            _user_id: str,
            _channel: str,
            _event_name: str,
            data: dict[str, Any],
        ) -> None:
            harness.assert_network_safe()
            published.append(json.loads(json.dumps(data)))
            if len(published) == 1:
                raise RuntimeError("publish unavailable")

        monkeypatch.setattr(
            memory_extraction,
            "_embedding_provider_available",
            provider_available,
        )
        monkeypatch.setattr(memory_extraction, "_try_llm_extract", no_llm_candidates)
        monkeypatch.setattr(
            memory_extraction,
            "_embedding_literal_async",
            embedding_literal,
        )
        monkeypatch.setattr(memory_extraction, "publish_event", publish_event)
        monkeypatch.setattr(
            memory_extraction,
            "extract_memories",
            lambda _text, *, explicit_only: (
                [_prepared_candidate().candidate],
                False,
            ),
        )

        with pytest.raises(RuntimeError, match="publish unavailable"):
            await memory_extraction.memory_extract(
                {"redis": redis, "job_id": "memory-job-1"},
                "conv-1",
                "user-msg-1",
                "assistant-msg-1",
            )
        await memory_extraction.memory_extract(
            {"redis": redis, "job_id": "memory-job-1"},
            "conv-1",
            "user-msg-1",
            "assistant-msg-1",
        )

        async with harness.factory() as session:
            run = (await session.execute(select(MemoryExtractionRun))).scalar_one()
            source = await session.get(Message, "user-msg-1")
            assistant = await session.get(Message, "assistant-msg-1")
            memory = await session.get(UserMemory, "memory-1")
            audit_count = (
                await session.execute(select(func.count()).select_from(MemoryAudit))
            ).scalar_one()

        assert run.status == "committed"
        assert run.attempt == 1
        assert run.fence == 1
        assert run.undo_status == "ready"
        assert source is not None and source.content == {"text": "我喜欢简洁回答"}
        assert "_memory_extraction" not in source.content
        assert assistant is not None
        assert assistant.content["tool_state"] == {"phase": "done"}
        assert len(assistant.content["memory_writes"]) == 1
        token = assistant.content["memory_writes"][0]["undo_token"]
        assert token == run.memory_writes[0]["undo_token"]
        assert token == run.undo_operations[0]["token"]
        assert memory is not None and memory.positive_signal == 1
        assert audit_count == 1
        assert provider_checks == 1
        assert embedding_calls == 1
        assert len(redis.setex_calls) == 2
        assert {call[0] for call in redis.setex_calls} == {f"memory:undo:{token}"}
        assert all(0 < call[1] <= 300 for call in redis.setex_calls)
        assert len(published) == 2
        assert published[0] == published[1]
        assert published[0]["event_id"] == ("memory-extract:user-msg-1:assistant-msg-1")


@pytest.mark.asyncio
async def test_active_lease_blocks_same_job_with_different_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _memory_database(monkeypatch) as harness:
        await _seed_memory_context(harness)
        event_id = "memory-extract:user-msg-1:assistant-msg-1"
        first = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="worker-owner-1",
            job_id="same-arq-job",
        )
        assert isinstance(first, memory_extraction._MemoryExtractionClaim)  # noqa: SLF001

        duplicate = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="worker-owner-2",
            job_id="same-arq-job",
        )
        assert duplicate is None

        async with harness.factory() as session:
            run = (await session.execute(select(MemoryExtractionRun))).scalar_one()
        assert run.status == "running"
        assert run.owner == "worker-owner-1"
        assert run.fence == 1
        assert run.attempt == 1
        assert run.recovery_count == 0


@pytest.mark.asyncio
async def test_expired_lease_reclaims_with_higher_fence_and_regenerate_is_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _memory_database(monkeypatch) as harness:
        await _seed_memory_context(
            harness,
            assistant_ids=("assistant-msg-1", "assistant-msg-2"),
        )
        event_id = "memory-extract:user-msg-1:assistant-msg-1"
        first = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="old-owner",
            job_id="old-job",
        )
        assert isinstance(first, memory_extraction._MemoryExtractionClaim)  # noqa: SLF001

        async with harness.factory() as session:
            run = (
                await session.execute(
                    select(MemoryExtractionRun).where(
                        MemoryExtractionRun.event_id == event_id
                    )
                )
            ).scalar_one()
            run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

        reclaimed = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="new-owner",
            job_id="new-job",
        )
        assert isinstance(
            reclaimed,
            memory_extraction._MemoryExtractionClaim,  # noqa: SLF001
        )
        assert reclaimed.fence == 2

        blocked_retry = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="retry-owner",
            job_id="new-job",
        )
        assert blocked_retry is None

        async with harness.factory() as session:
            active_run = (
                await session.execute(
                    select(MemoryExtractionRun).where(
                        MemoryExtractionRun.event_id == event_id
                    )
                )
            ).scalar_one()
            assert active_run.fence == 2
            assert active_run.attempt == 2
            assert active_run.recovery_count == 1
            active_run.lease_expires_at = datetime.now(timezone.utc) - timedelta(
                seconds=1
            )
            await session.commit()

        retry = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="retry-owner",
            job_id="new-job",
        )
        assert isinstance(retry, memory_extraction._MemoryExtractionClaim)  # noqa: SLF001
        assert retry.fence == 3

        stale_finalize = await memory_extraction._finalize_memory_extraction(  # noqa: SLF001
            first,
            prepared_candidates=[_prepared_candidate()],
            rejected_pii=False,
        )
        assert stale_finalize is None

        second_event_id = "memory-extract:user-msg-1:assistant-msg-2"
        second = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-2",
            event_id=second_event_id,
            owner="second-owner",
            job_id="second-job",
        )
        assert isinstance(second, memory_extraction._MemoryExtractionClaim)  # noqa: SLF001
        assert second.fence == 1

        async with harness.factory() as session:
            runs = (
                (
                    await session.execute(
                        select(MemoryExtractionRun).order_by(
                            MemoryExtractionRun.assistant_message_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            memory = await session.get(UserMemory, "memory-1")
            source = await session.get(Message, "user-msg-1")

        assert len(runs) == 2
        assert runs[0].assistant_message_id == "assistant-msg-1"
        assert runs[0].fence == 3
        assert runs[0].recovery_count == 2
        assert runs[1].assistant_message_id == "assistant-msg-2"
        assert memory is not None and memory.positive_signal == 0
        assert source is not None and "_memory_extraction" not in source.content


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("user_deleted", "user_deleted"),
        ("conversation_deleted", "conversation_deleted"),
        ("source_deleted", "source_message_deleted"),
        ("assistant_deleted", "assistant_message_deleted"),
        ("assistant_canceled", "assistant_message_canceled"),
    ],
)
@pytest.mark.asyncio
async def test_finalize_rechecks_soft_delete_and_cancel_state(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_reason: str,
) -> None:
    async with _memory_database(monkeypatch) as harness:
        await _seed_memory_context(harness)
        event_id = "memory-extract:user-msg-1:assistant-msg-1"
        claim = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="owner-1",
            job_id="job-1",
        )
        assert isinstance(claim, memory_extraction._MemoryExtractionClaim)  # noqa: SLF001

        now = datetime.now(timezone.utc)
        async with harness.factory() as session:
            if mutation == "user_deleted":
                row = await session.get(User, "user-1")
                assert row is not None
                row.deleted_at = now
            elif mutation == "conversation_deleted":
                row = await session.get(Conversation, "conv-1")
                assert row is not None
                row.deleted_at = now
            elif mutation == "source_deleted":
                row = await session.get(Message, "user-msg-1")
                assert row is not None
                row.deleted_at = now
            else:
                row = await session.get(Message, "assistant-msg-1")
                assert row is not None
                if mutation == "assistant_deleted":
                    row.deleted_at = now
                else:
                    row.status = "canceled"
            await session.commit()

        finalized = await memory_extraction._finalize_memory_extraction(  # noqa: SLF001
            claim,
            prepared_candidates=[_prepared_candidate()],
            rejected_pii=False,
        )
        assert finalized is None

        async with harness.factory() as session:
            run = (
                await session.execute(
                    select(MemoryExtractionRun).where(
                        MemoryExtractionRun.event_id == event_id
                    )
                )
            ).scalar_one()
            memory = await session.get(UserMemory, "memory-1")
            assistant = await session.get(Message, "assistant-msg-1")
            audit_count = (
                await session.execute(select(func.count()).select_from(MemoryAudit))
            ).scalar_one()

        assert run.status == "canceled"
        assert run.cancel_reason == expected_reason
        assert run.fence == claim.fence + 1
        assert memory is not None and memory.positive_signal == 0
        assert audit_count == 0
        assert assistant is not None
        assert "memory_writes" not in assistant.content


@pytest.mark.parametrize("cancel_stage", ["provider", "prepare"])
@pytest.mark.asyncio
async def test_cancelled_worker_marks_run_retryable_and_retry_reclaims(
    monkeypatch: pytest.MonkeyPatch,
    cancel_stage: str,
) -> None:
    async with _memory_database(monkeypatch) as harness:
        await _seed_memory_context(harness)

        async def provider_available(_ctx: dict[str, Any] | None) -> bool:
            harness.assert_network_safe()
            if cancel_stage == "provider":
                raise asyncio.CancelledError
            return True

        async def canceled_prepare(
            _ctx: dict[str, Any],
            _claim: memory_extraction._MemoryExtractionClaim,  # noqa: SLF001
        ) -> tuple[list[Any], bool]:
            harness.assert_network_safe()
            if cancel_stage == "prepare":
                raise asyncio.CancelledError
            return [], False

        monkeypatch.setattr(
            memory_extraction,
            "_embedding_provider_available",
            provider_available,
        )
        monkeypatch.setattr(
            memory_extraction,
            "_prepare_memory_extraction",
            canceled_prepare,
        )

        with pytest.raises(asyncio.CancelledError):
            await memory_extraction.memory_extract(
                {"redis": None, "job_id": "memory-job-1"},
                "conv-1",
                "user-msg-1",
                "assistant-msg-1",
            )

        event_id = "memory-extract:user-msg-1:assistant-msg-1"
        async with harness.factory() as session:
            run = (
                await session.execute(
                    select(MemoryExtractionRun).where(
                        MemoryExtractionRun.event_id == event_id
                    )
                )
            ).scalar_one()
            assert run.status == "retryable"
            assert run.retry_reason == "worker_cancelled"
            assert run.fence == 1
            assert run.attempt == 1

        retried = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="retry-owner",
            job_id="memory-job-1",
        )
        assert isinstance(
            retried,
            memory_extraction._MemoryExtractionClaim,  # noqa: SLF001
        )
        assert retried.fence == 2

        async with harness.factory() as session:
            run = (await session.execute(select(MemoryExtractionRun))).scalar_one()
            assert run.status == "running"
            assert run.owner == "retry-owner"
            assert run.attempt == 2


@pytest.mark.asyncio
async def test_pii_rejection_never_persists_source_excerpt_or_internal_message_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "记住我的密码是 abc-123456"
    async with _memory_database(monkeypatch) as harness:
        await _seed_memory_context(
            harness,
            source_text=secret,
            with_existing_memory=False,
        )
        event_id = "memory-extract:user-msg-1:assistant-msg-1"
        claim = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="owner-1",
            job_id="job-1",
        )
        assert isinstance(claim, memory_extraction._MemoryExtractionClaim)  # noqa: SLF001

        completed = await memory_extraction._finalize_memory_extraction(  # noqa: SLF001
            claim,
            prepared_candidates=[],
            rejected_pii=True,
        )
        assert completed is not None

        async with harness.factory() as session:
            run = (await session.execute(select(MemoryExtractionRun))).scalar_one()
            source = await session.get(Message, "user-msg-1")
            assistant = await session.get(Message, "assistant-msg-1")

        persisted_internal = json.dumps(
            {
                "writes": run.memory_writes,
                "undo": run.undo_operations,
            },
            ensure_ascii=False,
        )
        assert secret not in persisted_internal
        assert run.memory_writes[0]["kind"] == "rejected_pii"
        assert run.memory_writes[0]["source_excerpt"] is None
        assert source is not None and "_memory_extraction" not in source.content
        assert assistant is not None
        assert assistant.content["memory_writes"][0]["source_excerpt"] is None


@pytest.mark.asyncio
async def test_undo_replay_repairs_from_run_and_preserves_concurrent_assistant_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _memory_database(monkeypatch) as harness:
        await _seed_memory_context(harness)
        event_id = "memory-extract:user-msg-1:assistant-msg-1"
        claim = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="owner-1",
            job_id="job-1",
        )
        assert isinstance(claim, memory_extraction._MemoryExtractionClaim)  # noqa: SLF001
        completed = await memory_extraction._finalize_memory_extraction(  # noqa: SLF001
            claim,
            prepared_candidates=[_prepared_candidate()],
            rejected_pii=False,
        )
        assert completed is not None
        authoritative_token = completed.undo_operations[0]["token"]

        async with harness.factory() as session:
            run = (
                await session.execute(
                    select(MemoryExtractionRun).where(
                        MemoryExtractionRun.event_id == event_id
                    )
                )
            ).scalar_one()
            run.undo_status = "pending"

            assistant = await session.get(Message, "assistant-msg-1")
            assert assistant is not None
            assistant_writes = [
                dict(write) for write in assistant.content["memory_writes"]
            ]
            assistant_writes[0]["undo_token"] = None
            assistant.content = {
                **assistant.content,
                "memory_writes": assistant_writes,
                "tool_result": {"status": "finished"},
            }
            await session.commit()

        failing_redis = _Redis(harness, fail=True)
        with pytest.raises(RuntimeError, match="redis unavailable"):
            await memory_extraction._prepare_committed_memory_extraction_for_delivery(  # noqa: SLF001
                failing_redis,
                event_id,
            )

        redis = _Redis(harness)
        repaired = (
            await memory_extraction._prepare_committed_memory_extraction_for_delivery(  # noqa: SLF001
                redis,
                event_id,
            )
        )
        assert repaired is not None
        assert repaired.writes[0]["undo_token"] == authoritative_token

        async with harness.factory() as session:
            run = (await session.execute(select(MemoryExtractionRun))).scalar_one()
            assistant = await session.get(Message, "assistant-msg-1")

        assert run.undo_status == "ready"
        assert run.memory_writes[0]["undo_token"] == authoritative_token
        assert run.undo_operations[0]["token"] == authoritative_token
        assert assistant is not None
        assert assistant.content["tool_result"] == {"status": "finished"}
        assert (
            assistant.content["memory_writes"][0]["undo_token"] == authoritative_token
        )
        assert redis.setex_calls[0][0] == f"memory:undo:{authoritative_token}"


@pytest.mark.asyncio
async def test_expired_undo_operation_is_removed_from_run_and_assistant_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _memory_database(monkeypatch) as harness:
        await _seed_memory_context(harness)
        event_id = "memory-extract:user-msg-1:assistant-msg-1"
        claim = await memory_extraction._claim_memory_extraction(  # noqa: SLF001
            conversation_id="conv-1",
            source_message_id="user-msg-1",
            assistant_message_id="assistant-msg-1",
            event_id=event_id,
            owner="owner-1",
            job_id="job-1",
        )
        assert isinstance(claim, memory_extraction._MemoryExtractionClaim)  # noqa: SLF001
        completed = await memory_extraction._finalize_memory_extraction(  # noqa: SLF001
            claim,
            prepared_candidates=[_prepared_candidate()],
            rejected_pii=False,
        )
        assert completed is not None
        expired_token = completed.undo_operations[0]["token"]

        async with harness.factory() as session:
            run = (await session.execute(select(MemoryExtractionRun))).scalar_one()
            operations = [dict(operation) for operation in run.undo_operations]
            expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            operations[0]["expires_at"] = expired_at.isoformat()
            run.undo_operations = operations
            run.undo_status = "ready"
            run.undo_expires_at = expired_at

            assistant = await session.get(Message, "assistant-msg-1")
            assert assistant is not None
            assistant.status = "canceled"
            assistant.content = {
                **assistant.content,
                "tool_result": {"status": "finished"},
            }
            await session.commit()

        cleaned = await memory_extraction._cleanup_expired_memory_extraction_undo(  # noqa: SLF001
            datetime.now(timezone.utc)
        )
        assert cleaned == 1

        async with harness.factory() as session:
            run = (await session.execute(select(MemoryExtractionRun))).scalar_one()
            assistant = await session.get(Message, "assistant-msg-1")

        assert run.undo_status == "none"
        assert run.undo_expires_at is None
        assert run.undo_operations == []
        assert "undo_token" not in run.memory_writes[0]
        assert assistant is not None
        assert assistant.status == "canceled"
        assert assistant.content["tool_result"] == {"status": "finished"}
        assert "undo_token" not in assistant.content["memory_writes"][0]
        assert expired_token not in json.dumps(assistant.content)
