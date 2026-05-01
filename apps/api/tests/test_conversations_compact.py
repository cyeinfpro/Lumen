"""Manual context compaction endpoint tests (P0-3).

These cover the four contract scenarios the design spec calls out for
``POST /api/conversations/{conversation_id}/compact``: owner success path,
cross-user 404, empty-conversation 409, and the lock-busy 503 fallback.

The tests use lightweight fakes (no real Postgres / Redis) following the
pattern in ``test_conversations_messages.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.dialects import postgresql

from app.routes import conversations


# ---------- fakes ----------


class _Result:
    """Mimics the subset of SQLAlchemy Result we need (scalars + scalar_one_or_none)."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> "_Result":
        return self

    def __iter__(self):  # noqa: D401 — iter protocol
        return iter(self.rows)

    def all(self) -> list[Any]:
        return self.rows

    def scalar_one_or_none(self) -> Any:
        return self.rows[0] if self.rows else None


class _Db:
    """Routes only ``select(Conversation)`` and ``select(Message)`` queries here."""

    def __init__(
        self,
        *,
        conv: Any | None,
        latest_message: Any | None,
        settings: dict[str, str] | None = None,
    ) -> None:
        self.conv = conv
        self.latest_message = latest_message
        self.settings = settings or {}
        self.statements: list[Any] = []
        self.committed = 0

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        rendered = str(statement)
        if "FROM conversations" in rendered:
            return _Result([self.conv] if self.conv is not None else [])
        if "FROM messages" in rendered:
            return _Result([self.latest_message] if self.latest_message is not None else [])
        if "FROM system_settings" in rendered:
            try:
                params = statement.compile().params
            except Exception:
                params = {}
            requested = next(
                (value for value in params.values() if value in self.settings),
                None,
            )
            if requested is not None:
                return _Result([self.settings[requested]])
            for key, value in self.settings.items():
                if key in rendered:
                    return _Result([value])
            return _Result([])
        return _Result([])

    def add(self, value: Any) -> None:
        return None

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, _value: Any) -> None:
        return None

    async def flush(self) -> None:
        return None


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def _conv(user_id: str = "user-1") -> SimpleNamespace:
    return SimpleNamespace(
        id="conv-1",
        user_id=user_id,
        deleted_at=None,
        default_system=None,
        default_system_prompt_id=None,
        summary_jsonb={
            # ensure_context_summary writes compressed_at into summary_jsonb;
            # the endpoint reads it from there for the response payload.
            "compressed_at": "2026-04-26T12:00:00+00:00",
        },
        last_activity_at=datetime(2026, 4, 26, tzinfo=timezone.utc),
    )


def _message(message_id: str = "msg-latest") -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        role="user",
        content={"text": "hi"},
        created_at=datetime(2026, 4, 26, tzinfo=timezone.utc) + timedelta(seconds=1),
        deleted_at=None,
    )


def _user(user_id: str = "user-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        email="user@example.com",
        default_system_prompt_id=None,
    )


# ---------- 1. owner success path ----------


@pytest.mark.asyncio
async def test_compact_returns_summary_for_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _Db(conv=_conv(), latest_message=_message())
    captured: dict[str, Any] = {}

    async def fake_ensure(
        session: Any,
        conv: Any,
        boundary: Any,
        settings: Any,
        *,
        force: bool = False,
        extra_instruction: str | None = None,
        dry_run: bool = False,
        trigger: str = "auto",
    ) -> dict[str, Any]:
        captured["force"] = force
        captured["trigger"] = trigger
        captured["extra_instruction"] = extra_instruction
        captured["boundary_id"] = getattr(boundary, "id", None)
        return {
            "status": "created",
            "summary_created": True,
            "summary_used": True,
            "summary_up_to_message_id": "msg-latest",
            "summary_up_to_created_at": "2026-04-26T00:00:01+00:00",
            "summary_tokens": 1234,
            "source_message_count": 45,
            "source_token_estimate": 99000,
            "image_caption_count": 0,
            "tokens_freed": 8000,
            "extra_instruction_hash": None,
        }

    monkeypatch.setattr(conversations, "_import_ensure_context_summary", lambda: fake_ensure)
    monkeypatch.setattr(conversations, "get_redis", lambda: object())

    out = await conversations.compact_conversation(
        "conv-1",
        _request(),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        conversations.ManualCompactIn(
            extra_instruction="keep file paths", force=True
        ),
    )

    assert out["status"] == "ok"
    assert out.get("compacted") is True
    summary = out["summary"]
    assert summary["summary_created"] is True
    assert summary["summary_used"] is True
    assert summary["summary_up_to_message_id"] == "msg-latest"
    assert summary["summary_up_to_created_at"] == "2026-04-26T00:00:01+00:00"
    assert summary["tokens"] == 1234
    assert summary["source_message_count"] == 45
    assert summary["compressed_at"] == "2026-04-26T12:00:00+00:00"
    assert summary["status"] == "created"
    # ensure_context_summary must be called with force=True and trigger="manual"
    assert captured["force"] is True
    assert captured["trigger"] == "manual"
    assert captured["extra_instruction"] == "keep file paths"
    assert captured["boundary_id"] == "msg-latest"
    assert "FOR UPDATE" in str(db.statements[0].compile(dialect=postgresql.dialect())).upper()


# ---------- 2. cross-user 404 ----------


@pytest.mark.asyncio
async def test_compact_404_for_other_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The DB filter clause includes user_id, so a cross-user query yields no
    # row at all (mirrors how _get_owned_conv hides existence from outsiders).
    db = _Db(conv=None, latest_message=None)

    async def fake_ensure(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("ensure_context_summary must not run for non-owners")

    monkeypatch.setattr(conversations, "_import_ensure_context_summary", lambda: fake_ensure)
    monkeypatch.setattr(conversations, "get_redis", lambda: object())

    with pytest.raises(HTTPException) as excinfo:
        await conversations.compact_conversation(
            "conv-1",
            _request(),
            _user(user_id="someone-else"),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
            conversations.ManualCompactIn(force=True),
        )

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == {
        "error": {
            "code": "not_found",
            "message": "conversation not found",
        }
    }


# ---------- 3. empty conversation → 409 ----------


@pytest.mark.asyncio
async def test_compact_409_when_no_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _Db(conv=_conv(), latest_message=None)

    async def fake_ensure(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("ensure_context_summary must not run without messages")

    monkeypatch.setattr(conversations, "_import_ensure_context_summary", lambda: fake_ensure)
    monkeypatch.setattr(conversations, "get_redis", lambda: object())

    with pytest.raises(HTTPException) as excinfo:
        await conversations.compact_conversation(
            "conv-1",
            _request(),
            _user(),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
            conversations.ManualCompactIn(force=True),
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "no messages to compact"


# ---------- 4. lock busy → 503 ----------


@pytest.mark.asyncio
async def test_compact_503_on_lock_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _Db(conv=_conv(), latest_message=_message())

    async def fake_ensure(*_args: Any, **_kwargs: Any) -> None:
        # ensure_context_summary returns None when the Redis summary lock is
        # already held and the post-wait re-read still misses.
        return None

    monkeypatch.setattr(conversations, "_import_ensure_context_summary", lambda: fake_ensure)
    monkeypatch.setattr(conversations, "get_redis", lambda: object())

    with pytest.raises(HTTPException) as excinfo:
        await conversations.compact_conversation(
            "conv-1",
            _request(),
            _user(),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
            conversations.ManualCompactIn(force=True),
        )

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == {
        "error": {
            "code": "compression_unavailable",
            "message": "compression unavailable",
            "reason": "lock_busy",
        }
    }


# ---------- 5. force=False + 历史未超预算 → compacted=false（不打上游） ----------


@pytest.mark.asyncio
async def test_compact_returns_false_when_below_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 历史只有一条短消息，远低于 200k 预算；force=False 时应直接返回 compacted=false。
    db = _Db(conv=_conv(), latest_message=_message())

    async def fake_ensure(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("budget gate must short-circuit before ensure")

    monkeypatch.setattr(conversations, "_import_ensure_context_summary", lambda: fake_ensure)
    monkeypatch.setattr(conversations, "get_redis", lambda: object())

    out = await conversations.compact_conversation(
        "conv-1",
        _request(),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        conversations.ManualCompactIn(force=False),
    )

    assert out["status"] == "ok"
    assert out["compacted"] is False
    assert out["reason"] == "below_budget"
    assert out["input_budget_tokens"] == conversations.CONTEXT_INPUT_TOKEN_BUDGET
    assert "estimated_input_tokens" in out


# ---------- 6. force=False + 历史超预算 → 走完整 compact 流程 ----------


@pytest.mark.asyncio
async def test_compact_force_false_with_huge_safety_margin_invokes_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 用 safety_margin 故意压到比预算还大，强制 would_exceed_budget 为 True，
    # 这样既能验证 force=False 时的预算门也能跑通，又能复用现有的 fake_ensure。
    db = _Db(conv=_conv(), latest_message=_message())
    invoked: dict[str, Any] = {}

    async def fake_ensure(
        session: Any,
        conv: Any,
        boundary: Any,
        settings: Any,
        *,
        force: bool = False,
        extra_instruction: str | None = None,
        dry_run: bool = False,
        trigger: str = "auto",
    ) -> dict[str, Any]:
        invoked["called"] = True
        invoked["force"] = force
        return {
            "status": "created",
            "summary_created": True,
            "summary_used": True,
            "summary_up_to_message_id": "msg-latest",
            "summary_up_to_created_at": "2026-04-26T00:00:01+00:00",
            "summary_tokens": 100,
            "source_message_count": 2,
            "source_token_estimate": 800,
            "image_caption_count": 0,
            "tokens_freed": 700,
            "extra_instruction_hash": None,
        }

    monkeypatch.setattr(conversations, "_import_ensure_context_summary", lambda: fake_ensure)
    monkeypatch.setattr(conversations, "get_redis", lambda: object())

    out = await conversations.compact_conversation(
        "conv-1",
        _request(),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        conversations.ManualCompactIn(
            force=False,
            safety_margin=conversations.CONTEXT_INPUT_TOKEN_BUDGET + 1,
        ),
    )

    assert out["status"] == "ok"
    assert out.get("compacted") is True
    assert invoked.get("called") is True
    # ensure 入参恒为 force=True：用户已显式触发 compact，应当强制重新生成摘要。
    assert invoked.get("force") is True


@pytest.mark.asyncio
async def test_manual_compact_cooldown_uses_single_redis_eval() -> None:
    class Redis:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def eval(self, *args: Any) -> list[int]:
            self.calls.append(args)
            return [1, 600]

        async def incr(self, *_args: Any) -> int:
            raise AssertionError("cooldown must not use non-atomic INCR")

        async def expire(self, *_args: Any) -> bool:
            raise AssertionError("cooldown must not use separate EXPIRE")

    redis = Redis()

    remaining, reset = await conversations._check_manual_compact_cooldown(
        redis,
        user_id="user-1",
        conv_id="conv-1",
        cooldown_seconds=600,
    )

    assert remaining == 0
    assert reset == 600
    assert len(redis.calls) == 1
    assert redis.calls[0][1:] == (
        1,
        "context:manual_compact:user-1:conv-1:cooldown",
        "600",
    )


@pytest.mark.asyncio
async def test_load_messages_for_compaction_has_limit() -> None:
    db = _Db(conv=None, latest_message=_message())

    await conversations._load_messages_for_compaction(
        db, "conv-1"  # type: ignore[arg-type]
    )

    rendered = str(db.statements[-1].compile(dialect=postgresql.dialect())).upper()
    assert " LIMIT " in rendered
    assert conversations.COMPACTION_MESSAGE_LOAD_LIMIT in (
        db.statements[-1].compile().params.values()
    )
