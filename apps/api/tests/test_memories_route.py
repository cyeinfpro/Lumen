from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import memories


class _Redis:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.values = {
            "memory:undo:undo-1": json.dumps(
                {"user_id": "user-1", "action": "added", "memory_id": "mem-1"}
            )
        }
        self.claims: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        if key in self.claims:
            return self.claims[key]
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> bool:
        if nx and key in self.claims:
            return False
        self.claims[key] = value
        return True

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        self.values.pop(key, None)
        self.claims.pop(key, None)
        return 1


class _Db:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.fail_commit = fail_commit
        self.added: list[Any] = []
        self.committed = False

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("deadlock")
        self.committed = True


class _ConsumedDb(_Db):
    def __init__(self, consumed: bool) -> None:
        super().__init__()
        self.consumed = consumed


@pytest.mark.asyncio
async def test_undo_memory_write_keeps_token_when_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    memory = SimpleNamespace(id="mem-1", disabled=False)

    async def fake_owned_memory(_db: Any, _user_id: str, _memory_id: str) -> Any:
        return memory

    async def undo_token_consumed(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(memories, "get_redis", lambda: redis)
    monkeypatch.setattr(memories, "_owned_memory", fake_owned_memory)
    monkeypatch.setattr(memories, "_undo_token_consumed", undo_token_consumed)
    monkeypatch.setattr(
        memories,
        "_audit",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    with pytest.raises(RuntimeError, match="deadlock"):
        await memories.undo_memory_write(
            memories.MemoryUndoIn(undo_token="undo-1"),
            SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
            _Db(fail_commit=True),  # type: ignore[arg-type]
        )

    assert memory.disabled is True
    assert redis.deleted == [memories._undo_token_claim_key("undo-1")]  # noqa: SLF001
    assert "memory:undo:undo-1" in redis.values
    assert memories._undo_token_claim_key("undo-1") not in redis.claims  # noqa: SLF001


@pytest.mark.asyncio
async def test_undo_memory_write_deletes_token_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    memory = SimpleNamespace(id="mem-1", disabled=False)

    async def fake_owned_memory(_db: Any, _user_id: str, _memory_id: str) -> Any:
        return memory

    async def undo_token_consumed(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(memories, "get_redis", lambda: redis)
    monkeypatch.setattr(memories, "_owned_memory", fake_owned_memory)
    monkeypatch.setattr(memories, "_undo_token_consumed", undo_token_consumed)
    monkeypatch.setattr(
        memories,
        "_audit",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    out = await memories.undo_memory_write(
        memories.MemoryUndoIn(undo_token="undo-1"),
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        _Db(),  # type: ignore[arg-type]
    )

    assert out == {"ok": True}
    assert memory.disabled is True
    assert redis.deleted == [
        "memory:undo:undo-1",
        memories._undo_token_claim_key("undo-1"),
    ]  # noqa: SLF001
    assert "memory:undo:undo-1" not in redis.values
    assert memories._undo_token_claim_key("undo-1") not in redis.claims  # noqa: SLF001


@pytest.mark.asyncio
async def test_undo_memory_write_is_db_idempotent_when_token_delete_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    memory = SimpleNamespace(id="mem-1", disabled=False)
    db = _ConsumedDb(consumed=True)
    owned_calls: list[str] = []

    async def fake_owned_memory(_db: Any, _user_id: str, memory_id: str) -> Any:
        owned_calls.append(memory_id)
        return memory

    async def undo_token_consumed(
        _db: Any,
        *,
        user_id: str,
        undo_token: str,
    ) -> bool:
        assert user_id == "user-1"
        assert undo_token == "undo-1"
        return db.consumed

    monkeypatch.setattr(memories, "get_redis", lambda: redis)
    monkeypatch.setattr(memories, "_owned_memory", fake_owned_memory)
    monkeypatch.setattr(memories, "_undo_token_consumed", undo_token_consumed)

    out = await memories.undo_memory_write(
        memories.MemoryUndoIn(undo_token="undo-1"),
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"ok": True}
    assert memory.disabled is False
    assert owned_calls == []
    assert db.added == []
    assert db.committed is False
    assert redis.deleted == ["memory:undo:undo-1"]
    assert memories._undo_token_claim_key("undo-1") not in redis.claims  # noqa: SLF001


@pytest.mark.asyncio
async def test_undo_memory_write_second_call_is_noop_after_consumed_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    memory = SimpleNamespace(id="mem-1", disabled=False)
    db = _ConsumedDb(consumed=True)
    owned_calls: list[str] = []

    async def fake_owned_memory(_db: Any, _user_id: str, memory_id: str) -> Any:
        owned_calls.append(memory_id)
        return memory

    async def undo_token_consumed(
        _db: Any,
        *,
        user_id: str,
        undo_token: str,
    ) -> bool:
        return True

    monkeypatch.setattr(memories, "get_redis", lambda: redis)
    monkeypatch.setattr(memories, "_owned_memory", fake_owned_memory)
    monkeypatch.setattr(memories, "_undo_token_consumed", undo_token_consumed)

    out = await memories.undo_memory_write(
        memories.MemoryUndoIn(undo_token="undo-1"),
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"ok": True}
    assert memory.disabled is False
    assert owned_calls == []
    assert db.added == []
    assert db.committed is False
