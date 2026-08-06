from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app import tracker as tracker_mod  # noqa: E402


class FakeRedis:
    def __init__(self, raw: dict[bytes, bytes]) -> None:
        self.raw = raw
        self.deleted: list[tuple[str, ...]] = []

    async def hgetall(self, _key: str) -> dict[bytes, bytes]:
        return self.raw

    async def delete(self, *keys: str) -> int:
        self.deleted.append(tuple(keys))
        return len(keys)


class RecordingPipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def hset(self, *args: object, **kwargs: object) -> "RecordingPipeline":
        self.calls.append(("hset", *args, kwargs))
        return self

    def expire(self, *args: object) -> "RecordingPipeline":
        self.calls.append(("expire", *args))
        return self

    def zadd(self, *args: object) -> "RecordingPipeline":
        self.calls.append(("zadd", *args))
        return self

    async def execute(self) -> list[bool]:
        return [True] * len(self.calls)


class AddRedis:
    def __init__(self) -> None:
        self.pipe = RecordingPipeline()

    def pipeline(self, *, transaction: bool) -> RecordingPipeline:
        assert transaction is True
        return self.pipe


class LegacyRefreshRedis(FakeRedis):
    def __init__(self, raw: dict[bytes, bytes]) -> None:
        super().__init__(raw)
        self.eval_args: tuple[object, ...] | None = None

    async def eval(
        self,
        _script: str,
        numkeys: int,
        *args: object,
    ) -> int:
        assert numkeys == 2
        self.eval_args = args
        self.raw[b"user_id"] = str(args[2]).encode()
        return 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        {b"chat_id": b"abc", b"status_message_id": b"456"},
    ],
)
async def test_get_removes_dirty_tracker_hashes(raw: dict[bytes, bytes]) -> None:
    redis = FakeRedis(raw)
    tr = tracker_mod.Tracker()
    tr._redis = redis  # type: ignore[assignment]

    result = await tr.get("gen-bad")

    assert result is None
    assert redis.deleted == [
        (
            tracker_mod._key("gen-bad"),
            tracker_mod._notified_key("gen-bad"),
            tracker_mod._delivering_key("gen-bad"),
            tracker_mod._legacy_notified_key("gen-bad"),
            tracker_mod._legacy_delivering_key("gen-bad"),
        )
    ]


@pytest.mark.asyncio
async def test_get_keeps_pending_bonus_track_without_message_id() -> None:
    """bonus「先注册后发消息」的合法中间态：没有 status_message_id 不能当脏数据删。

    缺 chat_id 仍然是脏数据（必须删）；只有 message id 缺失/为 0 时返回
    status_message_id=None 的 track，等 _on_attached 发完消息补写。
    """
    for raw in (
        {b"chat_id": b"123"},
        {b"chat_id": b"123", b"status_message_id": b"0"},
    ):
        redis = FakeRedis(raw)
        tr = tracker_mod.Tracker()
        tr._redis = redis  # type: ignore[assignment]

        result = await tr.get("gen-bonus-pending")

        assert result is not None
        assert result.status_message_id is None
        assert redis.deleted == []


@pytest.mark.asyncio
async def test_get_keeps_track_with_invalid_params_json() -> None:
    redis = FakeRedis(
        {
            b"chat_id": b"123",
            b"status_message_id": b"456",
            b"prompt": b"hello",
            b"params": b"{not-json",
        }
    )
    tr = tracker_mod.Tracker()
    tr._redis = redis  # type: ignore[assignment]

    result = await tr.get("gen-ok")

    assert result == tracker_mod.TaskTrack(
        chat_id=123,
        status_message_id=456,
        prompt="hello",
        params={},
        is_bonus=False,
        batch_id="",
    )
    assert redis.deleted == []


@pytest.mark.asyncio
async def test_get_restores_lumen_user_id_for_stream_subscription() -> None:
    redis = FakeRedis(
        {
            b"user_id": b"user-1",
            b"chat_id": b"123",
            b"status_message_id": b"456",
        }
    )
    tr = tracker_mod.Tracker()
    tr._redis = redis  # type: ignore[assignment]

    result = await tr.get("gen-ok")

    assert result is not None
    assert result.user_id == "user-1"


@pytest.mark.asyncio
async def test_add_uses_fake_clock_and_retains_membership_for_48_hours() -> None:
    redis = AddRedis()
    tr = tracker_mod.Tracker(clock=lambda: 1_000.75)
    tr._redis = redis  # type: ignore[assignment]

    await tr.add(
        "gen-1",
        tracker_mod.TaskTrack(
            chat_id=123,
            status_message_id=456,
            prompt="hello",
            user_id="user-1",
        ),
    )

    assert tracker_mod.ACTIVE_USER_STREAM_TTL_SECONDS >= 48 * 3600
    assert (
        "zadd",
        tracker_mod.ACTIVE_USER_STREAMS_KEY,
        {"user-1": 1_000 + tracker_mod.ACTIVE_USER_STREAM_TTL_SECONDS},
    ) in redis.pipe.calls
    assert (
        "expire",
        tracker_mod._key("gen-1"),
        tracker_mod.TRACK_RETENTION_SECONDS,
    ) in redis.pipe.calls


@pytest.mark.asyncio
async def test_add_stores_empty_message_id_for_pending_bonus() -> None:
    """bonus 先注册后发消息：注册时 status_message_id 为空，落盘成空串。"""
    redis = AddRedis()
    tr = tracker_mod.Tracker(clock=lambda: 1_000.75)
    tr._redis = redis  # type: ignore[assignment]

    await tr.add(
        "gen-bonus-1",
        tracker_mod.TaskTrack(
            chat_id=123,
            status_message_id=None,
            prompt="p",
            user_id="user-1",
        ),
    )

    assert (
        "hset",
        tracker_mod._key("gen-bonus-1"),
        {
            "mapping": {
                "chat_id": "123",
                "tg_user_id": "",
                "user_id": "user-1",
                "status_message_id": "",
                "prompt": "p",
                "params": "{}",
                "is_bonus": "0",
                "batch_id": "",
            }
        },
    ) in redis.pipe.calls


@pytest.mark.asyncio
async def test_tracker_preserves_group_chat_actor_identity() -> None:
    redis = FakeRedis(
        {
            b"user_id": b"user-1",
            b"chat_id": b"-100123",
            b"tg_user_id": b"42",
            b"status_message_id": b"456",
        }
    )
    tr = tracker_mod.Tracker()
    tr._redis = redis  # type: ignore[assignment]

    result = await tr.get("gen-group")

    assert result is not None
    assert result.chat_id == -100123
    assert result.tg_user_id == 42
    assert redis.deleted == []


@pytest.mark.asyncio
async def test_update_status_message_backfills_real_message_id() -> None:
    redis = AddRedis()
    tr = tracker_mod.Tracker()
    tr._redis = redis  # type: ignore[assignment]

    await tr.update_status_message("gen-bonus-1", 42)

    assert (
        "hset",
        tracker_mod._key("gen-bonus-1"),
        "status_message_id",
        "42",
        {},
    ) in redis.pipe.calls
    assert (
        "expire",
        tracker_mod._key("gen-bonus-1"),
        tracker_mod.TRACK_RETENTION_SECONDS,
    ) in redis.pipe.calls


@pytest.mark.asyncio
async def test_update_status_message_ignores_invalid_message_id() -> None:
    redis = AddRedis()
    tr = tracker_mod.Tracker()
    tr._redis = redis  # type: ignore[assignment]

    await tr.update_status_message("gen-bonus-1", 0)
    assert redis.pipe.calls == []


@pytest.mark.asyncio
async def test_refresh_binds_legacy_tracker_and_renews_retention() -> None:
    redis = LegacyRefreshRedis(
        {
            b"chat_id": b"123",
            b"status_message_id": b"456",
        }
    )
    tr = tracker_mod.Tracker(clock=lambda: 2_000.9)
    tr._redis = redis  # type: ignore[assignment]

    legacy = await tr.get("gen-legacy")
    refreshed = await tr.refresh("gen-legacy", "user-legacy")
    restored = await tr.get("gen-legacy")

    assert legacy is not None
    assert legacy.user_id == ""
    assert refreshed is True
    assert restored is not None
    assert restored.user_id == "user-legacy"
    assert redis.eval_args == (
        tracker_mod._key("gen-legacy"),
        tracker_mod.ACTIVE_USER_STREAMS_KEY,
        "user-legacy",
        str(tracker_mod.TRACK_RETENTION_SECONDS),
        str(2_000 + tracker_mod.ACTIVE_USER_STREAM_TTL_SECONDS),
        str(tracker_mod._ACTIVE_USER_STREAMS_KEY_TTL_SECONDS),
    )


@pytest.mark.asyncio
async def test_add_rejects_empty_user_id_before_writing() -> None:
    tr = tracker_mod.Tracker()

    with pytest.raises(ValueError, match="non-empty user_id"):
        await tr.add(
            "gen-legacy-api",
            tracker_mod.TaskTrack(
                chat_id=123,
                status_message_id=456,
                prompt="hello",
            ),
        )


class MarkerRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.set_ex: dict[str, int] = {}

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.store[key] = value.encode()
        self.set_ex[key] = ex

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)


@pytest.mark.asyncio
async def test_retry_marker_roundtrip_with_long_ttl() -> None:
    redis = MarkerRedis()
    tr = tracker_mod.Tracker()
    tr._redis = redis  # type: ignore[assignment]

    assert await tr.retry_source_new_gen("redo", 42, "gen-src") is None

    await tr.mark_retry_submitted("redo", 42, "gen-src", "gen-new")

    assert await tr.retry_source_new_gen("redo", 42, "gen-src") == "gen-new"
    assert await tr.retry_source_new_gen("retry", 42, "gen-src") is None
    assert await tr.retry_source_new_gen("redo", 7, "gen-src") is None
    assert (
        redis.set_ex[f"{tracker_mod._RETRY_SOURCE_PREFIX}redo:42:gen-src"]
        == tracker_mod._RETRY_SOURCE_TTL_SECONDS
    )
    assert tracker_mod._RETRY_SOURCE_TTL_SECONDS > tracker_mod.TRACK_RETENTION_SECONDS


class LuaPipeline:
    def __init__(self, redis: LuaRedis) -> None:
        self.redis = redis
        self.reads: list[tuple[str, str]] = []

    def hget(self, key: str, field: str) -> LuaPipeline:
        self.reads.append((key, field))
        return self

    async def execute(self) -> list[bytes | None]:
        return [
            (
                value.encode()
                if (
                    value := self.redis.hashes.get(key, {}).get(field)
                )
                is not None
                else None
            )
            for key, field in self.reads
        ]


class LuaRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def pipeline(self, *, transaction: bool) -> LuaPipeline:
        assert transaction is False
        return LuaPipeline(self)

    async def set(
        self,
        key: str,
        value: object,
        *,
        nx: bool = False,
        ex: int | None = None,
        px: int | None = None,
    ) -> bool | None:
        del ex, px
        if nx and key in self.strings:
            return None
        self.strings[key] = str(value)
        return True

    async def exists(self, *keys: str) -> int:
        return sum(
            int(key in self.strings or key in self.hashes)
            for key in keys
        )

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        keys = [str(value) for value in args[:numkeys]]
        argv = [str(value) for value in args[numkeys:]]
        if script == tracker_mod._ACQUIRE_DELIVERY_LUA:
            notified_key, delivering_key = keys
            if notified_key in self.strings:
                return -1
            if delivering_key in self.strings:
                return 0
            self.strings[delivering_key] = argv[0]
            return 1
        if script == tracker_mod._RENEW_DELIVERY_LUA:
            return int(self.strings.get(keys[0]) == argv[0])
        if script == tracker_mod._FINALIZE_DELIVERY_LUA:
            notified_key, delivering_key = keys
            if self.strings.get(delivering_key) != argv[0]:
                return 0
            self.strings[notified_key] = argv[1]
            self.strings.pop(delivering_key, None)
            return 1
        if script == tracker_mod._RELEASE_DELIVERY_LUA:
            if self.strings.get(keys[0]) != argv[0]:
                return 0
            self.strings.pop(keys[0], None)
            return 1
        raise AssertionError("unexpected Lua script")

    def force_expire(self, key: str) -> None:
        self.strings.pop(key, None)


@pytest.mark.asyncio
async def test_old_owner_cannot_finalize_release_or_renew_takeover() -> None:
    redis = LuaRedis()
    tr = tracker_mod.Tracker()
    tr._redis = redis  # type: ignore[assignment]

    first = await tr.begin_delivery("gen-1")
    assert first.state == "acquired"
    assert first.owner_token is not None
    redis.force_expire(tracker_mod._delivering_key("gen-1"))
    redis.force_expire(tracker_mod._legacy_delivering_key("gen-1"))

    second = await tr.begin_delivery("gen-1")
    assert second.state == "acquired"
    assert second.owner_token is not None
    assert second.owner_token != first.owner_token

    assert (
        await tr.mark_notified("gen-1", owner_token=first.owner_token) is False
    )
    assert await tr.clear_delivery("gen-1", owner_token=first.owner_token) is False
    assert await tr.renew_delivery("gen-1", first.owner_token) is False
    assert await tr.renew_delivery("gen-1", second.owner_token) is True
    assert await tr.mark_notified("gen-1", owner_token=second.owner_token) is True
    assert (await tr.begin_delivery("gen-1")).state == "already_notified"


def test_delivery_lua_keys_share_cluster_hash_slot() -> None:
    gen_id = "gen-1"
    slot = tracker_mod._delivery_slot(gen_id)

    assert f"{{{slot}}}" in tracker_mod._notified_key(gen_id)
    assert f"{{{slot}}}" in tracker_mod._delivering_key(gen_id)
