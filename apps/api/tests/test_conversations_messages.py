from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.routes import conversations
from lumen_core.constants import CompletionStatus, GenerationStatus
from lumen_core.schemas import ConversationPatchIn


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> "_Result":
        return self

    def __iter__(self):
        return iter(self.rows)

    def all(self) -> list[Any]:
        return self.rows

    def scalar_one_or_none(self) -> Any:
        return self.rows[0] if self.rows else None


class _Db:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)


class _ContextDb:
    def __init__(self, rows: list[Any], by_id: dict[str, Any] | None = None) -> None:
        self.rows = rows
        self.by_id = by_id or {getattr(row, "id", ""): row for row in rows}
        self.message_selects = 0

    async def get(self, _model: Any, object_id: str) -> Any:
        return self.by_id.get(object_id)

    async def execute(self, statement: Any) -> _Result:
        rendered = str(statement)
        if "FROM system_prompts" in rendered:
            return _Result([])
        if "FROM messages" in rendered:
            self.message_selects += 1
            return _Result(self.rows if self.message_selects == 1 else [])
        return _Result([])


class _WriteResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _WriteDb:
    def __init__(self, rowcount: int = 0) -> None:
        self.rowcount = rowcount
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _WriteResult:
        self.statements.append(statement)
        return _WriteResult(self.rowcount)


class _ActiveTaskDb:
    def __init__(self, responses: list[list[Any]]) -> None:
        self.responses = responses
        self.statements: list[Any] = []
        self.committed = False

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.responses.pop(0) if self.responses else [])

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_patch_conversation_can_clear_nullable_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    conv = SimpleNamespace(
        id="conv-1",
        title="Conversation",
        pinned=False,
        archived=False,
        memory_disabled=False,
        active_scope_id=None,
        last_activity_at=now,
        default_params={},
        default_system="old system",
        default_system_prompt_id="prompt-1",
        created_at=now,
    )

    async def fake_owned_conv(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return conv

    class Db:
        executed = False

        async def execute(self, _statement: Any) -> _Result:
            self.executed = True
            return _Result([])

        async def commit(self) -> None:
            return None

        async def refresh(self, _row: Any) -> None:
            return None

    db = Db()
    monkeypatch.setattr(conversations, "_get_owned_visible_conv", fake_owned_conv)

    out = await conversations.patch_conversation(
        "conv-1",
        ConversationPatchIn(
            default_system=None,
            default_system_prompt_id=None,
        ),
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out.default_system is None
    assert out.default_system_prompt_id is None
    assert db.executed is False


@pytest.mark.asyncio
async def test_list_conversations_filters_workflow_backing_conversations() -> None:
    db = _Db([])

    out = await conversations.list_conversations(
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
        limit=30,
    )

    assert out.items == []
    rendered = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "(conversations.default_params ->> 'workflow_type') IS NULL" in rendered


def _message(message_id: str, created_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        conversation_id="conv-1",
        role="user",
        content={"text": message_id},
        intent="chat",
        status="succeeded",
        parent_message_id=None,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_delete_conversation_soft_deletes_generated_images() -> None:
    db = _WriteDb(rowcount=2)
    deleted_at = datetime.now(timezone.utc)

    count = await conversations._soft_delete_conversation_generated_images(
        db,  # type: ignore[arg-type]
        conv_id="conv-1",
        user_id="user-1",
        deleted_at=deleted_at,
    )

    assert count == 2
    rendered = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "UPDATE images" in rendered
    assert "images.deleted_at IS NULL" in rendered
    assert "images.owner_generation_id IN" in rendered
    assert "FROM generations JOIN messages" in rendered
    assert "messages.conversation_id" in rendered
    assert "generations.user_id" in rendered


@pytest.mark.asyncio
async def test_cancel_conversation_active_tasks_releases_only_queued_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen_queued = SimpleNamespace(
        id="gen-queued",
        status=GenerationStatus.QUEUED.value,
        progress_stage="queued",
        finished_at=None,
        error_code=None,
        error_message=None,
        billing_retry_count=1,
    )
    gen_running = SimpleNamespace(
        id="gen-running",
        status=GenerationStatus.RUNNING.value,
        progress_stage="rendering",
        finished_at=None,
        error_code=None,
        error_message=None,
    )
    comp_queued = SimpleNamespace(
        id="comp-queued",
        status=CompletionStatus.QUEUED.value,
        progress_stage="queued",
        finished_at=None,
        error_code=None,
        error_message=None,
        upstream_request={"billing_retry_count": 1},
    )
    comp_streaming = SimpleNamespace(
        id="comp-streaming",
        status=CompletionStatus.STREAMING.value,
        progress_stage="streaming",
        finished_at=None,
        error_code=None,
        error_message=None,
    )
    db = _ActiveTaskDb(
        [[gen_queued, gen_running], [comp_queued, comp_streaming]]
    )
    released: list[dict[str, Any]] = []

    async def release_conversation_task_hold(
        db: _ActiveTaskDb,
        *,
        user_id: str,
        ref_type: str,
        ref_id: str,
        reason: str,
    ) -> bool:
        released.append(
            {
                "committed": db.committed,
                "user_id": user_id,
                "ref_type": ref_type,
                "ref_id": ref_id,
                "reason": reason,
            }
        )
        return True

    monkeypatch.setattr(
        conversations,
        "_release_conversation_task_hold",
        release_conversation_task_hold,
    )
    monkeypatch.setattr(
        conversations,
        "_conversation_wallet_exists",
        lambda *_args, **_kwargs: False,
    )

    cleanup = await conversations._cancel_conversation_active_tasks(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        conv_id="conv-1",
        user_id="user-1",
        canceled_at=datetime.now(timezone.utc),
    )

    assert cleanup == {
        "generations_canceled": 2,
        "completions_canceled": 2,
        "holds_released": 2,
        "active_generation_ids": ["gen-queued", "gen-running"],
        "active_completion_ids": ["comp-queued", "comp-streaming"],
        "queued_generation_ids": ["gen-queued"],
        "running_generation_ids": ["gen-running"],
        "streaming_completion_ids": ["comp-streaming"],
    }
    assert [call["ref_id"] for call in released] == [
        "gen-queued:retry:1",
        "comp-queued:retry:1",
    ]
    assert all(call["committed"] is False for call in released)
    assert gen_queued.status == GenerationStatus.CANCELED.value
    assert gen_running.status == GenerationStatus.RUNNING.value
    assert comp_queued.status == CompletionStatus.CANCELED.value
    assert comp_streaming.status == CompletionStatus.STREAMING.value


@pytest.mark.asyncio
async def test_cancel_conversation_active_tasks_skips_holds_for_byok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = SimpleNamespace(id="gen-1", status=GenerationStatus.RUNNING.value)
    comp = SimpleNamespace(id="comp-1", status=CompletionStatus.STREAMING.value)
    db = _ActiveTaskDb([[gen], [comp]])
    released: list[str] = []

    async def release_conversation_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    monkeypatch.setattr(
        conversations,
        "_release_conversation_task_hold",
        release_conversation_task_hold,
    )

    cleanup = await conversations._cancel_conversation_active_tasks(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        conv_id="conv-1",
        user_id="user-1",
        canceled_at=datetime.now(timezone.utc),
        account_mode="byok",
    )

    assert cleanup["holds_released"] == 0
    assert released == []
    assert gen.status == GenerationStatus.RUNNING.value
    assert comp.status == CompletionStatus.STREAMING.value


@pytest.mark.asyncio
async def test_post_commit_conversation_task_cleanup_runs_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _ActiveTaskDb([])
    invalidated: list[tuple[str, bool]] = []
    redis_calls: list[tuple[str, str, int]] = []
    queue_released: list[tuple[str, bool]] = []

    class Redis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            redis_calls.append((key, value, ex))

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    async def release_generation_queue_state(_redis: Redis, task_id: str) -> None:
        queue_released.append((task_id, db.committed))

    monkeypatch.setattr(conversations, "get_redis", lambda: Redis())
    monkeypatch.setattr(
        conversations,
        "invalidate_balance_cache",
        invalidate_balance_cache,
    )
    monkeypatch.setattr(
        conversations,
        "_release_conversation_generation_queue_state",
        release_generation_queue_state,
    )

    await db.commit()
    await conversations._post_commit_conversation_task_cleanup(  # noqa: SLF001
        user_id="user-1",
        cleanup={
            "holds_released": 1,
            "queued_generation_ids": ["gen-queued"],
            "running_generation_ids": ["gen-running"],
            "streaming_completion_ids": ["comp-1"],
        },
    )

    assert invalidated == [("user-1", True)]
    assert queue_released == [("gen-queued", True)]
    assert redis_calls == [
        ("task:gen-running:cancel", "1", 3600),
        ("task:comp-1:cancel", "1", 3600),
    ]


@pytest.mark.asyncio
async def test_post_commit_conversation_task_cleanup_keeps_cancel_when_cache_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_calls: list[tuple[str, str, int]] = []
    queue_released: list[str] = []

    class Redis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            redis_calls.append((key, value, ex))

    async def invalidate_balance_cache(_user_id: str) -> None:
        raise RuntimeError("cache unavailable")

    async def release_generation_queue_state(_redis: Redis, task_id: str) -> None:
        queue_released.append(task_id)

    monkeypatch.setattr(conversations, "get_redis", lambda: Redis())
    monkeypatch.setattr(
        conversations,
        "invalidate_balance_cache",
        invalidate_balance_cache,
    )
    monkeypatch.setattr(
        conversations,
        "_release_conversation_generation_queue_state",
        release_generation_queue_state,
    )

    await conversations._post_commit_conversation_task_cleanup(  # noqa: SLF001
        user_id="user-1",
        cleanup={
            "holds_released": 1,
            "queued_generation_ids": ["gen-queued"],
            "running_generation_ids": ["gen-running"],
            "streaming_completion_ids": ["comp-1"],
        },
    )

    assert queue_released == ["gen-queued"]
    assert redis_calls == [
        ("task:gen-running:cancel", "1", 3600),
        ("task:comp-1:cancel", "1", 3600),
    ]


@pytest.mark.asyncio
async def test_post_commit_conversation_task_cleanup_invalidates_hold_only_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidated: list[str] = []
    redis_requested = False

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append(user_id)

    def get_redis() -> object:
        nonlocal redis_requested
        redis_requested = True
        return object()

    monkeypatch.setattr(conversations, "get_redis", get_redis)
    monkeypatch.setattr(
        conversations,
        "invalidate_balance_cache",
        invalidate_balance_cache,
    )

    await conversations._post_commit_conversation_task_cleanup(  # noqa: SLF001
        user_id="user-1",
        cleanup={
            "holds_released": 1,
            "queued_generation_ids": [],
            "running_generation_ids": [],
            "streaming_completion_ids": [],
        },
    )

    assert invalidated == ["user-1"]
    assert redis_requested is False


@pytest.mark.asyncio
async def test_list_messages_initial_page_returns_latest_messages_chronological(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_owned_conv(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(id="conv-1")

    monkeypatch.setattr(conversations, "_get_owned_conv", fake_get_owned_conv)

    now = datetime.now(timezone.utc)
    rows = [
        # Simulates the DB result for ORDER BY created_at DESC, id DESC.
        _message("msg-3", now + timedelta(seconds=2)),
        _message("msg-2", now + timedelta(seconds=1)),
        _message("msg-1", now),
    ]
    db = _Db(rows)

    out = await conversations.list_messages(
        "conv-1",
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        limit=2,
        include=None,
    )

    assert [m.id for m in out.items] == ["msg-2", "msg-3"]
    assert out.next_cursor is not None
    assert out.next_cursor != "msg-2"
    decoded = conversations._dec_cursor(out.next_cursor)
    assert decoded is not None
    assert decoded["id"] == "msg-2"
    assert decoded["ca"] == (now + timedelta(seconds=1)).isoformat()
    rendered = str(db.statements[0])
    assert "JOIN conversations" in rendered
    assert "conversations.deleted_at IS NULL" in rendered
    assert "messages.created_at DESC" in rendered


@pytest.mark.asyncio
async def test_list_messages_cursor_page_returns_older_messages_chronological(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_owned_conv(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(id="conv-1")

    monkeypatch.setattr(conversations, "_get_owned_conv", fake_get_owned_conv)

    now = datetime.now(timezone.utc)
    cursor = conversations._enc_cursor(
        {"ca": (now + timedelta(seconds=3)).isoformat(), "id": "msg-3"}
    )
    rows = [
        # Simulates the DB result for an older page ordered DESC.
        _message("msg-2", now + timedelta(seconds=2)),
        _message("msg-1", now + timedelta(seconds=1)),
        _message("msg-0", now),
    ]
    db = _Db(rows)

    out = await conversations.list_messages(
        "conv-1",
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        cursor=cursor,
        limit=2,
        include=None,
    )

    assert [m.id for m in out.items] == ["msg-1", "msg-2"]
    decoded = conversations._dec_cursor(out.next_cursor)
    assert decoded is not None
    assert decoded["id"] == "msg-1"
    assert decoded["ca"] == (now + timedelta(seconds=1)).isoformat()
    rendered = str(db.statements[0])
    assert "messages.created_at <" in rendered
    assert "messages.created_at DESC" in rendered


def test_conversation_cursor_ignores_invalid_payload() -> None:
    assert conversations._dec_cursor("not-a-valid-cursor") is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_list_messages_rejects_cursor_with_invalid_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_owned_conv(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(id="conv-1")

    monkeypatch.setattr(conversations, "_get_owned_conv", fake_get_owned_conv)
    cursor = conversations._enc_cursor({"ca": "not-a-date", "id": "msg-1"})

    with pytest.raises(HTTPException) as excinfo:
        await conversations.list_messages(
            "conv-1",
            SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
            _Db([]),  # type: ignore[arg-type]
            cursor=cursor,
            include=None,
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_context_window_estimate_is_token_budgeted_not_20_messages() -> None:
    now = datetime.now(timezone.utc)
    rows = [
        _message(f"msg-{i}", now + timedelta(seconds=i))
        for i in range(25, 0, -1)
    ]
    conv = SimpleNamespace(
        id="conv-1",
        default_system=None,
        default_system_prompt_id=None,
        summary_jsonb={
            "version": 2,
            "kind": "rolling_conversation_summary",
            "up_to_message_id": "msg-4",
            "up_to_created_at": (now + timedelta(seconds=4)).isoformat(),
            "first_user_message_id": "msg-1",
            "text": "Earlier summary",
            "tokens": 12,
            "source_message_count": 3,
            "source_token_estimate": 3000,
            "compressed_at": now.isoformat(),
            "compression_runs": 2,
        },
    )

    out = await conversations._estimate_context_window(
        _ContextDb(rows),  # type: ignore[arg-type]
        conv=conv,  # type: ignore[arg-type]
        user_id="user-1",
        user_default_prompt_id=None,
    )

    assert out.input_budget_tokens == 200_000
    assert out.total_target_tokens == 256_000
    assert out.response_reserve_tokens == 56_000
    assert out.included_messages_count == 22
    assert out.truncated is False
    assert out.compression_enabled is False
    assert out.summary_available is True
    assert out.summary_tokens == 12
    assert out.summary_up_to_message_id == "msg-4"
    assert out.summary_first_user_message_id == "msg-1"
    assert out.summary_compression_runs == 2
    assert out.compressible_messages_count == 4
    assert out.compressible_tokens > 0
    assert out.summary_target_tokens == 1200
    assert out.estimated_tokens_freed < out.compressible_tokens
    assert out.manual_compact_available is False
    assert out.manual_compact_min_input_tokens == 4000
    assert out.manual_compact_unavailable_reason == "below_min_tokens"


@pytest.mark.asyncio
async def test_context_window_estimate_uses_message_id_at_equal_summary_timestamp() -> None:
    boundary_at = datetime.now(timezone.utc)
    messages = [
        _message("msg-c", boundary_at),
        _message("msg-b", boundary_at),
        _message("msg-a", boundary_at),
    ]
    original = _message("msg-0", boundary_at - timedelta(seconds=1))
    by_id = {msg.id: msg for msg in [original, *messages]}
    conv = SimpleNamespace(
        id="conv-1",
        default_system=None,
        default_system_prompt_id=None,
        summary_jsonb={
            "version": 2,
            "kind": "rolling_conversation_summary",
            "up_to_message_id": "msg-b",
            "up_to_created_at": boundary_at.isoformat(),
            "first_user_message_id": original.id,
            "text": "Earlier summary",
            "tokens": 12,
        },
    )

    out = await conversations._estimate_context_window(
        _ContextDb(messages, by_id=by_id),  # type: ignore[arg-type]
        conv=conv,  # type: ignore[arg-type]
        user_id="user-1",
        user_default_prompt_id=None,
    )

    # The original task remains sticky and msg-c is the only message after the
    # msg-b boundary. msg-a/msg-b share its timestamp but are not counted.
    assert out.included_messages_count == 2
    assert out.summary_up_to_message_id == "msg-b"


@pytest.mark.asyncio
async def test_context_window_estimate_uses_summary_instead_of_counting_compacted_history() -> None:
    now = datetime.now(timezone.utc)
    old_blob = "old context " + ("x" * 35_000)
    recent_blob = "recent context " + ("y" * 350)
    messages = [
        _message("msg-5", now + timedelta(seconds=5)),
        _message("msg-4", now + timedelta(seconds=4)),
        _message("msg-3", now + timedelta(seconds=3)),
        _message("msg-2", now + timedelta(seconds=2)),
        _message("msg-1", now + timedelta(seconds=1)),
    ]
    messages[0].content = {"text": "latest question"}
    messages[1].content = {"text": recent_blob}
    messages[2].content = {"text": old_blob}
    messages[3].content = {"text": old_blob}
    messages[4].content = {"text": "original task"}
    by_id = {msg.id: msg for msg in messages}
    raw_history_tokens = sum(
        conversations.estimate_message_tokens(msg.role, msg.content)
        for msg in messages
    )
    summary = {
        "version": 2,
        "kind": "rolling_conversation_summary",
        "up_to_message_id": "msg-3",
        "up_to_created_at": (now + timedelta(seconds=3)).isoformat(),
        "first_user_message_id": "msg-1",
        "text": "compressed old facts",
        "tokens": 100,
        "source_message_count": 3,
        "source_token_estimate": raw_history_tokens,
        "compressed_at": now.isoformat(),
        "compression_runs": 1,
    }
    conv = SimpleNamespace(
        id="conv-1",
        default_system=None,
        default_system_prompt_id=None,
        summary_jsonb=summary,
    )

    out = await conversations._estimate_context_window(
        _ContextDb(messages, by_id=by_id),  # type: ignore[arg-type]
        conv=conv,  # type: ignore[arg-type]
        user_id="user-1",
        user_default_prompt_id=None,
    )

    assert out.summary_available is True
    assert out.estimated_input_tokens < raw_history_tokens // 4
    assert out.summary_tokens == 100
    assert out.compressible_tokens == 0
    assert out.estimated_tokens_freed == 0
