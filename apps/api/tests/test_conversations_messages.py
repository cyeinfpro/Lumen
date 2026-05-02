from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.routes import conversations


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
