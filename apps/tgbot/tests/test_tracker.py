from __future__ import annotations

import sys
from pathlib import Path

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
        {b"chat_id": b"123"},
        {b"chat_id": b"abc", b"status_message_id": b"456"},
        {b"chat_id": b"123", b"status_message_id": b"0"},
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
        )
    ]


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
        {
            "user-1": 1_000
            + tracker_mod.ACTIVE_USER_STREAM_TTL_SECONDS
        },
    ) in redis.pipe.calls
    assert (
        "expire",
        tracker_mod._key("gen-1"),
        tracker_mod.TRACK_RETENTION_SECONDS,
    ) in redis.pipe.calls


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
