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
