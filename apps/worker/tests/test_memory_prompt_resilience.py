from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.tasks import memory_extraction
from app.tasks.completion_parts import request_context
from app.tasks.memory_extraction_parts.prompt_assembly import (
    ranked_prompt_memories,
)
from app.tasks.memory_extraction_parts.run_state import (
    MEMORY_FINALIZATION_ROW_LIMIT,
    active_memories_for_finalization,
)
from app.tasks.memory_prompt_storage import (
    PROMPT_MEMORY_ROW_LIMIT,
    _prompt_memory_rows,
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
async def test_completion_memory_and_tool_settings_run_after_main_session_exit() -> None:
    active = {"value": False}
    message_model = object()
    target = SimpleNamespace(parent_message_id="parent-1")
    parent = SimpleNamespace(
        content={"reasoning_effort": "high", "fast": True},
    )

    class Session:
        async def __aenter__(self) -> Session:
            assert active["value"] is False
            active["value"] = True
            return self

        async def __aexit__(self, *_args: Any) -> None:
            active["value"] = False

        async def get(self, model: Any, row_id: str) -> Any:
            assert active["value"] is True
            assert model is message_model
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
        return {"used_memory_ids": [], "used_memory_summary": []}

    async def chat_tools(_content: dict[str, Any]) -> list[str]:
        assert active["value"] is False
        return ["web_search"]

    state = SimpleNamespace(
        ports=SimpleNamespace(
            persistence=SimpleNamespace(
                SessionLocal=Session,
                Message=message_model,
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
