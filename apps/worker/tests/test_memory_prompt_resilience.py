from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.tasks import memory_extraction
from app.tasks.completion_parts import context as completion_context
from app.tasks.completion_parts import request_context
from app.tasks.completion_parts.default_runtime_parts import context_runtime
from app.tasks.memory_extraction_parts.prompt_assembly import (
    QUERY_EMBEDDING_CACHE_TTL_SECONDS,
    pack_float16_vector,
    pick_confirmation_candidate,
    query_embedding_cache_key,
    ranked_prompt_memories,
    resolve_query_embedding_for_prompt,
)
from app.tasks.memory_extraction_parts.run_state import (
    MEMORY_FINALIZATION_ROW_LIMIT,
    active_memories_for_finalization,
)
from app.tasks.memory_prompt_storage import (
    PROMPT_MEMORY_ROW_LIMIT,
    PROMPT_PG_VECTOR_CANDIDATE_LIMIT,
    _prompt_memory_rows,
    build_pg_mandatory_memories_statement,
    build_pg_vector_candidates_statement,
)
from lumen_core.constants import DEFAULT_CHAT_INSTRUCTIONS, Role
from lumen_core.context_window import (
    MESSAGE_OVERHEAD_TOKENS,
    SUMMARY_KIND,
    SUMMARY_VERSION,
    estimate_summary_tokens,
    estimate_system_prompt_tokens,
    estimate_text_tokens,
)
from lumen_core.memory import deterministic_embedding, embedding_literal
from lumen_core.models import (
    Conversation,
    MemoryAudit,
    Message,
    User,
    UserMemory,
    UserMemoryScope,
)


@pytest.mark.asyncio
async def test_memory_prompt_releases_transaction_before_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        committed = False

        async def get(self, model: Any, _row_id: str) -> Any:
            if model is memory_extraction.User:
                return SimpleNamespace(memory_disabled=False)
            if model is memory_extraction.Conversation:
                return SimpleNamespace(memory_disabled=False)
            return None

        async def commit(self) -> None:
            self.committed = True

    session = Session()

    async def available(_ctx: Any) -> bool:
        return True

    async def disabled_ids(_redis: Any, _conversation_id: str) -> set[str]:
        return set()

    async def scope_context(*_args: Any, **_kwargs: Any) -> tuple[set[str], None]:
        return {"scope-1"}, None

    async def prompt_rows(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def ranked(*_args: Any, **_kwargs: Any) -> tuple[list[Any], ...]:
        assert session.committed is True
        return [], [], [], None

    async def record_used(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def confirmation(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(memory_extraction, "_embedding_provider_available", available)
    monkeypatch.setattr(
        memory_extraction,
        "_conversation_disabled_memory_ids",
        disabled_ids,
    )
    monkeypatch.setattr(memory_extraction, "_memory_scope_context", scope_context)
    monkeypatch.setattr(memory_extraction, "_prompt_memory_rows", prompt_rows)
    monkeypatch.setattr(memory_extraction, "_ranked_prompt_memories", ranked)
    monkeypatch.setattr(memory_extraction, "_record_used_memories", record_used)
    monkeypatch.setattr(
        memory_extraction,
        "_pick_confirmation_candidate",
        confirmation,
    )

    await memory_extraction.assemble_user_memory_prompt(
        session,
        user_id="user-1",
        conversation_id="conversation-1",
        user_text="current request",
    )


@pytest.mark.asyncio
async def test_memory_ranking_skips_embedding_without_candidates() -> None:
    calls = 0

    async def embedding(_ctx: Any, _content: str) -> list[float]:
        nonlocal calls
        calls += 1
        return [1.0]

    profiles, avoids, context, query = await ranked_prompt_memories(
        [],
        user_text="long enough request",
        now=memory_extraction._utc_now(),  # noqa: SLF001
        embedding_vector=embedding,
        decay=lambda _memory, _now: 1.0,
    )

    assert (profiles, avoids, context, query) == ([], [], [], None)
    assert calls == 0


@pytest.mark.asyncio
async def test_prompt_memory_query_is_bounded_before_rows_are_materialized() -> None:
    captured: dict[str, Any] = {}

    class Scalars:
        def all(self) -> list[Any]:
            return []

    class Result:
        def scalars(self) -> Scalars:
            return Scalars()

    class Session:
        async def execute(self, statement: Any) -> Result:
            captured["statement"] = statement
            return Result()

    rows = await _prompt_memory_rows(
        Session(),
        user_id="user-1",
        scope_ids={"scope-1"},
        disabled_ids={"disabled-1"},
    )

    assert rows == []
    limit_clause = captured["statement"]._limit_clause
    assert limit_clause is not None
    assert int(limit_clause.value) == PROMPT_MEMORY_ROW_LIMIT


@pytest.mark.asyncio
async def test_memory_finalization_query_is_bounded_under_advisory_lock() -> None:
    captured: dict[str, Any] = {}

    class Scalars:
        def all(self) -> list[Any]:
            return []

    class Result:
        def scalars(self) -> Scalars:
            return Scalars()

    class Session:
        async def execute(self, statement: Any) -> Result:
            captured["statement"] = statement
            return Result()

    rows = await active_memories_for_finalization(
        Session(),
        user_id="user-1",
    )

    assert rows == []
    limit_clause = captured["statement"]._limit_clause
    assert limit_clause is not None
    assert int(limit_clause.value) == MEMORY_FINALIZATION_ROW_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion",
    [
        None,
        SimpleNamespace(upstream_request={}),
        SimpleNamespace(upstream_request={"artifact_format": "text"}),
    ],
    ids=["missing-completion", "ordinary-completion", "explicit-text-completion"],
)
async def test_completion_memory_and_tool_settings_run_after_main_session_exit(
    completion: Any,
) -> None:
    active = {"value": False}
    message_model = object()
    completion_model = object()
    lookups = []
    events = []
    target = SimpleNamespace(parent_message_id="parent-1")
    parent = SimpleNamespace(
        content={"reasoning_effort": "high", "fast": True},
    )

    class Session:
        async def __aenter__(self) -> Session:
            assert active["value"] is False
            active["value"] = True
            events.append("session-enter")
            return self

        async def __aexit__(self, *_args: Any) -> None:
            active["value"] = False
            events.append("session-exit")

        async def get(self, model: Any, row_id: str) -> Any:
            assert active["value"] is True
            lookups.append((model, row_id))
            if model is completion_model:
                assert row_id == "completion-1"
                return completion
            assert model is message_model
            assert row_id in {"message-1", "parent-1"}
            return target if row_id == "message-1" else parent

    async def pack(*_args: Any, **_kwargs: Any) -> Any:
        assert active["value"] is True
        return SimpleNamespace(
            input_list=[],
            summary_used=False,
            sticky_used=False,
        )

    async def record(*_args: Any, **_kwargs: Any) -> None:
        assert active["value"] is True

    async def inject(*_args: Any, **_kwargs: Any) -> dict[str, list[Any]]:
        assert active["value"] is False
        assert _args == (None,)
        events.append("inject-memory")
        return {"used_memory_ids": [], "used_memory_summary": []}

    async def chat_tools(_content: dict[str, Any]) -> list[str]:
        assert active["value"] is False
        assert _content == parent.content
        events.append("resolve-tools")
        return ["web_search"]

    state = SimpleNamespace(
        ports=SimpleNamespace(
            persistence=SimpleNamespace(
                SessionLocal=Session,
                Message=message_model,
                Completion=completion_model,
            ),
            context=SimpleNamespace(
                _pack_recent_history=pack,
                _instructions_with_summary_guardrail=(
                    lambda prompt, **_kwargs: prompt or "default"
                ),
                _record_completion_context_metadata=record,
                _inject_user_memory_context=inject,
            ),
            tools=SimpleNamespace(_chat_tools_from_content=chat_tools),
            retry=SimpleNamespace(_LeaseLost=RuntimeError),
        ),
        request=SimpleNamespace(redis=object(), task_id="completion-1"),
        preparation=SimpleNamespace(
            system_prompt=None,
            message_id="message-1",
            conversation_id="conversation-1",
            chat_model="gpt-5.4",
            account_mode="wallet",
            user_id="user-1",
            attempt_epoch=1,
            target_msg=None,
            reasoning_effort=None,
            fast_mode=False,
        ),
        streaming=SimpleNamespace(
            instructions="",
            input_list=[],
            chat_tools=[],
        ),
        usage=SimpleNamespace(memory_meta_for_event={}),
        settlement=SimpleNamespace(lease_lost=asyncio.Event()),
    )

    await request_context.load_request_context(state)

    assert active["value"] is False
    assert state.preparation.reasoning_effort == "high"
    assert state.preparation.fast_mode is True
    assert state.streaming.chat_tools == ["web_search"]
    assert lookups == [
        (completion_model, "completion-1"),
        (message_model, "message-1"),
        (message_model, "parent-1"),
    ]
    assert events == [
        "session-enter", "session-exit",
        "session-enter", "session-exit",
        "inject-memory", "resolve-tools",
    ]
    assert state.preparation.target_msg is target
    assert state.streaming.instructions == "default"
    assert state.usage.memory_meta_for_event == {
        "used_memory_ids": [], "used_memory_summary": [],
    }


def test_pg_vector_pushdown_sql_excludes_embedding_and_uses_hnsw_weighted_cosine() -> (
    None
):
    pg_dialect = postgresql.dialect()
    mandatory_sql = str(
        build_pg_mandatory_memories_statement(
            user_id="user-1",
            scope_ids={"scope-1"},
            disabled_ids={"disabled-1"},
        ).compile(dialect=pg_dialect, compile_kwargs={"literal_binds": False})
    )
    select_head_mandatory = mandatory_sql.split("FROM", 1)[0]
    assert "embedding" not in select_head_mandatory

    vector_sql = str(
        build_pg_vector_candidates_statement(
            user_id="user-1",
            scope_ids={"scope-1"},
            query_embedding_literal="[0.1,0.2]",
            disabled_ids={"disabled-1"},
        ).compile(dialect=pg_dialect)
    )
    select_head_vector = vector_sql.split("FROM", 1)[0]
    assert "CAST(user_memories.embedding AS TEXT)" not in select_head_vector
    assert "user_memories.embedding <=>" in vector_sql
    assert "CAST(%(q)s AS halfvec(3072))" in vector_sql
    assert "ORDER BY (1.0 - (user_memories.embedding <=> CAST(%(q)s AS halfvec(3072)))) *" in vector_sql
    assert "0.1 * user_memories.positive_signal" in vector_sql
    assert "0.15 * user_memories.negative_signal) DESC" in vector_sql
    assert PROMPT_PG_VECTOR_CANDIDATE_LIMIT == 16


@pytest.mark.asyncio
async def test_pg_prompt_assembly_releases_tx_before_embedding_and_uses_pushdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    now = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)
    profile_mem = UserMemory(
        id="mem-profile",
        user_id="user-1",
        type="profile",
        content="用户是全栈架构师",
        source="manual",
        confidence=0.95,
        pinned=False,
        positive_signal=1,
        negative_signal=0,
        scope_id="scope-1",
        created_at=now,
        updated_at=now,
    )
    deep_candidate = UserMemory(
        id="mem-250",
        user_id="user-1",
        type="project",
        content="正在推进 Lumen OPT-06 halfvec 向量下推",
        source="auto",
        confidence=0.88,
        pinned=False,
        positive_signal=4,
        negative_signal=0,
        scope_id="scope-1",
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=1),
    )

    class PgSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        commits = 0

        async def get(self, model: Any, _row_id: str) -> Any:
            if model is memory_extraction.User:
                return SimpleNamespace(
                    id="user-1",
                    memory_disabled=False,
                    confirmation_enabled=False,
                )
            if model is memory_extraction.Conversation:
                return SimpleNamespace(
                    id="conv-1",
                    memory_disabled=False,
                    active_scope_id=None,
                )
            return None

        async def commit(self) -> None:
            self.commits += 1
            events.append(f"commit-{self.commits}")

    session = PgSession()

    async def available(_ctx: Any) -> bool:
        return True

    async def disabled_ids(_redis: Any, _conv_id: str) -> set[str]:
        return set()

    async def scope_context(*_args: Any, **_kwargs: Any) -> tuple[set[str], None]:
        return {"scope-1"}, None

    async def mandatory_rows(*_args: Any, **_kwargs: Any) -> tuple[list[UserMemory], bool]:
        events.append("mandatory")
        return [profile_mem], True

    async def fake_embedding(_ctx: Any, _text: str) -> list[float]:
        assert session.commits >= 1
        events.append("embed")
        return [0.25] * 8

    async def pg_vector_candidates(
        *_args: Any,
        **_kwargs: Any,
    ) -> tuple[list[UserMemory], dict[str, float]]:
        events.append("pg-vector-top16")
        return [deep_candidate], {"mem-250": 0.96}

    async def record_used(*_args: Any, **_kwargs: Any) -> None:
        events.append("record-used")

    monkeypatch.setattr(memory_extraction, "_embedding_provider_available", available)
    monkeypatch.setattr(
        memory_extraction,
        "_conversation_disabled_memory_ids",
        disabled_ids,
    )
    monkeypatch.setattr(memory_extraction, "_memory_scope_context", scope_context)
    monkeypatch.setattr(
        memory_extraction,
        "_prompt_mandatory_memory_rows",
        mandatory_rows,
    )
    monkeypatch.setattr(memory_extraction, "_embedding_vector", fake_embedding)
    monkeypatch.setattr(
        memory_extraction,
        "_prompt_pg_vector_candidates",
        pg_vector_candidates,
    )
    monkeypatch.setattr(memory_extraction, "_record_used_memories", record_used)

    assembled = await memory_extraction.assemble_user_memory_prompt(
        session,
        user_id="user-1",
        conversation_id="conv-1",
        user_text="请检查 OPT-06 halfvec 向量下推细节",
    )

    assert events[:4] == ["mandatory", "commit-1", "embed", "pg-vector-top16"]
    assert assembled.used_memory_ids == ["mem-profile", "mem-250"]
    assert "halfvec" in (assembled.context_text or "")


@pytest.mark.asyncio
async def test_query_embedding_redis_float16_cache_and_soft_timeout_fallback() -> None:
    store: dict[str, tuple[bytes, int]] = {}

    class FakeRedis:
        async def get(self, key: str) -> bytes | None:
            entry = store.get(key)
            return entry[0] if entry is not None else None

        async def set(self, key: str, value: bytes, *, ex: int) -> None:
            store[key] = (value, ex)

    redis = FakeRedis()
    embed_calls = 0

    async def fake_embed(_ctx: Any, _text: str) -> list[float]:
        nonlocal embed_calls
        embed_calls += 1
        return [0.5, -0.25, 0.125, 0.75]

    vec1, degraded1 = await resolve_query_embedding_for_prompt(
        "  请帮我检查   HNSW  索引  ",
        embedding_vector=fake_embed,
        redis=redis,
    )
    vec2, degraded2 = await resolve_query_embedding_for_prompt(
        "请帮我检查 HNSW 索引",
        embedding_vector=fake_embed,
        redis=redis,
    )

    expected_key = query_embedding_cache_key("请帮我检查 HNSW 索引")
    assert degraded1 is False and degraded2 is False
    assert embed_calls == 1
    assert expected_key in store
    cached_bytes, ttl = store[expected_key]
    assert ttl == QUERY_EMBEDDING_CACHE_TTL_SECONDS == 600
    assert cached_bytes == pack_float16_vector([0.5, -0.25, 0.125, 0.75])
    assert len(cached_bytes) == 4 * 2
    assert vec1 is not None and vec2 is not None
    assert [round(v, 3) for v in vec2] == [0.5, -0.25, 0.125, 0.75]

    now = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)
    candidates = [
        UserMemory(
            id=f"cand-{idx:02d}",
            user_id="user-1",
            type="preference",
            content=f"偏好项 {idx}",
            source="auto",
            confidence=0.8,
            pinned=False,
            positive_signal=0,
            negative_signal=0,
            scope_id="scope-1",
            last_used_at=now - timedelta(hours=20 - idx),
            created_at=now - timedelta(days=5),
            updated_at=now - timedelta(days=1),
        )
        for idx in range(12)
    ]
    profile = UserMemory(
        id="prof-1",
        user_id="user-1",
        type="profile",
        content="工程师",
        source="manual",
        confidence=1.0,
        pinned=False,
        scope_id="scope-1",
        created_at=now,
        updated_at=now,
    )
    avoid = UserMemory(
        id="avoid-1",
        user_id="user-1",
        type="avoid",
        content="不要空话套话",
        source="manual",
        confidence=1.0,
        pinned=False,
        scope_id="scope-1",
        created_at=now,
        updated_at=now,
    )
    pinned = UserMemory(
        id="pin-1",
        user_id="user-1",
        type="project",
        content="置顶项目",
        source="manual",
        confidence=1.0,
        pinned=True,
        scope_id="scope-1",
        created_at=now,
        updated_at=now,
    )

    async def slow_embed(_ctx: Any, _text: str) -> list[float]:
        await asyncio.sleep(0.2)
        return [1.0] * 4

    profiles, avoids, context_memories, query_vec = await ranked_prompt_memories(
        [profile, avoid, pinned, *candidates],
        user_text="测试上游向量超时降级路径",
        now=now,
        embedding_vector=slow_embed,
        decay=lambda _m, _now: 1.0,
        soft_timeout_s=0.02,
    )

    assert query_vec is None
    assert [m.id for m in profiles] == ["prof-1"]
    assert [m.id for m in avoids] == ["avoid-1"]
    assert [m.id for m in context_memories] == [
        "pin-1",
        *[f"cand-{idx:02d}" for idx in range(11, 3, -1)],
    ]


@pytest.mark.asyncio
async def test_pick_confirmation_candidate_batches_audit_count_into_single_group_by_query() -> (
    None
):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    tables = [
        User.__table__,
        UserMemoryScope.__table__,
        Conversation.__table__,
        Message.__table__,
        UserMemory.__table__,
        MemoryAudit.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: User.metadata.create_all(sync_conn, tables=tables)
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    user_text = "请帮我写一段架构评审意见"
    shared_vec = embedding_literal(deterministic_embedding(user_text))

    try:
        async with factory() as session:
            user = User(
                id="u-confirm",
                email="confirm@example.test",
                display_name="Confirm",
                confirmation_enabled=True,
            )
            scope = UserMemoryScope(
                id="s-confirm",
                user_id=user.id,
                name="default",
                is_default=True,
            )
            conv = Conversation(id="c-confirm", user_id=user.id, title="Confirm")
            old_msg = Message(
                id="msg-old",
                conversation_id=conv.id,
                role="user",
                content={"text": "earlier"},
                created_at=now - timedelta(days=2),
            )
            cur_msg = Message(
                id="msg-cur",
                conversation_id=conv.id,
                role="user",
                content={"text": user_text},
                created_at=now,
            )
            memories = [
                UserMemory(
                    id=f"mem-{idx}",
                    user_id=user.id,
                    type="preference",
                    content=f"偏好 {idx}",
                    source="auto",
                    embedding=shared_vec,
                    confidence=0.9,
                    positive_signal=10 - idx,
                    negative_signal=0,
                    scope_id=scope.id,
                    created_at=now - timedelta(days=10),
                    updated_at=now - timedelta(days=5),
                )
                for idx in range(5)
            ]
            session.add_all([user, scope, conv, old_msg, cur_msg, *memories])
            session.add_all(
                [
                    MemoryAudit(
                        id="audit-0",
                        user_id=user.id,
                        memory_id="mem-0",
                        event_type="confirm_prompted",
                        source_message_id=old_msg.id,
                        created_at=now - timedelta(days=2),
                    ),
                    MemoryAudit(
                        id="audit-1",
                        user_id=user.id,
                        memory_id="mem-1",
                        event_type="confirm_prompted",
                        source_message_id=old_msg.id,
                        created_at=now - timedelta(days=2),
                    ),
                ]
            )
            await session.commit()

        async with factory() as session:
            executed_sql: list[str] = []
            orig_execute = session.execute

            async def tracked_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
                executed_sql.append(str(statement))
                return await orig_execute(statement, *args, **kwargs)

            session.execute = tracked_execute  # type: ignore[method-assign]
            user_row = await session.get(User, "u-confirm")
            assert user_row is not None

            async def noop_lock(_s: Any, _k: str) -> None:
                return None

            chosen = await pick_confirmation_candidate(
                session,
                memories,
                user=user_row,
                user_text=user_text,
                now=now,
                conversation_id="c-confirm",
                parent_user_message_id="msg-cur",
                try_advisory_xact_lock=noop_lock,
                confirm_weekly_limit=5,
                query_vec=deterministic_embedding(user_text),
            )
            await session.commit()

            assert chosen is not None
            assert chosen.id == "mem-2"
            assert len(executed_sql) == 3
            per_memory_audit_queries = [
                sql for sql in executed_sql if "memory_audit.memory_id" in sql
            ]
            assert len(per_memory_audit_queries) == 1
            assert "memory_audit.memory_id IN" in per_memory_audit_queries[0]
            assert "GROUP BY memory_audit.memory_id" in per_memory_audit_queries[0]
    finally:
        await engine.dispose()


def test_opt07_single_copy_system_prompt_budget_in_runtime_and_summary_pack() -> None:
    custom_prompt = "你是一名严谨的系统架构顾问。" * 20
    once_tokens = context_runtime.estimate_system_prompt_tokens_once(
        custom_prompt,
        default_instructions=DEFAULT_CHAT_INSTRUCTIONS,
        estimate_system_prompt_tokens=estimate_system_prompt_tokens,
        estimate_text_tokens=estimate_text_tokens,
    )
    assert once_tokens == estimate_system_prompt_tokens(custom_prompt)

    now = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)
    first_user = Message(
        id="msg-1",
        conversation_id="conv-1",
        role=Role.USER.value,
        content={"text": "initial goal"},
        created_at=now,
    )
    boundary_msg = Message(
        id="msg-2",
        conversation_id="conv-1",
        role=Role.ASSISTANT.value,
        content={"text": "earlier reply"},
        created_at=now + timedelta(seconds=1),
    )
    current_user = Message(
        id="msg-3",
        conversation_id="conv-1",
        role=Role.USER.value,
        content={"text": "follow up"},
        created_at=now + timedelta(seconds=2),
    )
    summary = {
        "version": SUMMARY_VERSION,
        "kind": SUMMARY_KIND,
        "up_to_message_id": boundary_msg.id,
        "up_to_created_at": boundary_msg.created_at.isoformat(),
        "first_user_message_id": first_user.id,
        "text": "summary body",
        "tokens": 24,
    }
    packed = completion_context._pack_with_existing_summary(
        system_prompt=custom_prompt,
        all_rows_desc=[current_user, boundary_msg, first_user],
        summary=summary,
        summary_model="gpt-5.4-mini",
        input_budget=10_000,
        current_user=current_user,
        first_user=first_user,
    )
    guarded = completion_context._with_summary_guardrail(custom_prompt, enabled=True)
    single_sys = estimate_system_prompt_tokens(guarded, duplication_factor=1)
    double_sys = estimate_system_prompt_tokens(guarded, duplication_factor=2)
    assert double_sys - single_sys == estimate_text_tokens(guarded)
    assert packed.estimated_tokens < (
        double_sys + MESSAGE_OVERHEAD_TOKENS + estimate_summary_tokens(summary)
    )


def test_migration_0074_halfvec_hnsw_contract_and_offline_sql() -> None:
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "apps/api/alembic/versions/0074_memory_halfvec_hnsw.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0074_under_test",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0074_memory_halfvec_hnsw"
    assert module.down_revision == "0073_seedance_25_flash_defaults"

    buffer = StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer},
    )
    with Operations.context(ctx):
        module.upgrade()
    sql_out = buffer.getvalue()
    assert "ALTER TABLE user_memories ALTER COLUMN embedding TYPE halfvec(3072)" in sql_out
    assert (
        "ALTER TABLE user_memory_staging ALTER COLUMN embedding TYPE halfvec(3072)"
        in sql_out
    )
    assert (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_user_memories_embedding_hnsw "
        "ON user_memories USING hnsw (embedding halfvec_cosine_ops) "
        "WHERE disabled = false AND superseded_by IS NULL AND embedding IS NOT NULL"
    ) in sql_out
