from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request

from app.routes import events


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/events",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_replay_payload_filter_matches_requested_channels() -> None:
    user_channel = "user:user-1"

    assert events._replay_payload_matches_channels(
        {"conversation_id": "conv-1"},
        requested_channels={"conv:conv-1"},
        include_user_channel=False,
        user_channel=user_channel,
    )
    assert not events._replay_payload_matches_channels(
        {"conversation_id": "conv-2"},
        requested_channels={"conv:conv-1"},
        include_user_channel=False,
        user_channel=user_channel,
    )
    assert events._replay_payload_matches_channels(
        {"generation_id": "gen-1"},
        requested_channels={"task:gen-1"},
        include_user_channel=False,
        user_channel=user_channel,
    )
    assert not events._replay_payload_matches_channels(
        {"conversation_id": "conv-2"},
        requested_channels={user_channel},
        include_user_channel=True,
        user_channel=user_channel,
    )
    assert events._replay_payload_matches_channels(
        {"notice": "ok"},
        requested_channels={user_channel},
        include_user_channel=True,
        user_channel=user_channel,
    )


@pytest.mark.asyncio
async def test_pubsub_event_without_sse_id_is_persisted_for_live_id() -> None:
    class Redis:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def xadd(
            self,
            stream_key: str,
            fields: dict[str, str],
            **kwargs: Any,
        ) -> str:
            self.calls.append(
                {"stream_key": stream_key, "fields": fields, "kwargs": kwargs}
            )
            return "1710000000001-0"

    redis = Redis()

    stream_id = await events._stream_id_for_pubsub_event(  # noqa: SLF001
        redis,
        stream_key="events:user:user-1",
        event_name="generation.completed",
        envelope_event_id="event-1",
        payload={"generation_id": "gen-1"},
    )

    assert stream_id == "1710000000001-0"
    assert redis.calls[0]["stream_key"] == "events:user:user-1"
    assert redis.calls[0]["fields"]["event"] == "generation.completed"
    payload = json.loads(redis.calls[0]["fields"]["data"])
    assert payload["event_id"] == "event-1"
    assert payload["data"]["generation_id"] == "gen-1"


@pytest.mark.asyncio
async def test_events_rejects_too_many_channels_before_subscribing() -> None:
    channels = ",".join(f"task:{i}" for i in range(events.MAX_SSE_CHANNELS + 1))

    with pytest.raises(Exception) as excinfo:
        await events.events(
            _request(),
            SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            channels=channels,
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "too_many_channels"
    assert excinfo.value.detail["error"]["max_channels"] == events.MAX_SSE_CHANNELS
    assert (
        excinfo.value.detail["error"]["requested_count"] == events.MAX_SSE_CHANNELS + 1
    )
    assert (
        excinfo.value.detail["error"]["effective_count"] == events.MAX_SSE_CHANNELS + 2
    )


@pytest.mark.asyncio
async def test_events_logs_replay_failure_and_acloses_pubsub(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class PubSub:
        def __init__(self) -> None:
            self.aclose_called = False
            self.close_called = False

        async def subscribe(self, *_channels: str) -> None:
            return None

        async def unsubscribe(self, *_channels: str) -> None:
            return None

        async def aclose(self) -> None:
            self.aclose_called = True

        async def close(self) -> None:
            self.close_called = True

        async def get_message(self, **_kwargs: Any) -> None:
            return None

    class Redis:
        def __init__(self) -> None:
            self.pubsub_obj = PubSub()

        async def xread(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("stream unavailable")

        def pubsub(self) -> PubSub:
            return self.pubsub_obj

    # 用当前 ms 构造 Last-Event-ID，否则会被 _sanitize_last_event_id 以
    # "超出 24h 重放窗口" 拒绝，replay 分支被跳过，logger 不会 emit。
    sane_event_id = f"{int(time.time() * 1000)}-0"

    class DisconnectedRequest:
        headers = {"Last-Event-ID": sane_event_id}

        async def is_disconnected(self) -> bool:
            return True

    redis = Redis()
    monkeypatch.setattr(events, "get_redis", lambda: redis)

    response = await events.events(
        DisconnectedRequest(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        channels="",
    )

    with caplog.at_level("WARNING"):
        async for _chunk in response.body_iterator:
            pass

    assert "sse replay failed" in caplog.text
    assert redis.pubsub_obj.aclose_called is True
    assert redis.pubsub_obj.close_called is False


@pytest.mark.asyncio
async def test_sse_replay_does_not_block_when_cursor_is_latest() -> None:
    class Redis:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] | None = None

        async def xread(self, *_args: Any, **kwargs: Any) -> list[Any]:
            self.kwargs = kwargs
            return []

    redis = Redis()
    events_seen = [
        item
        async for item in events._iter_replay_events(
            redis,
            stream_key="events:user:user-1",
            last_event_id="1710000000000-0",
            requested_channels=set(),
            include_user_channel=False,
            user_channel="user:user-1",
        )
    ]

    assert events_seen == []
    assert redis.kwargs is not None
    assert "block" not in redis.kwargs


@pytest.mark.asyncio
async def test_sse_replay_pages_until_stream_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events, "_REPLAY_BATCH_SIZE", 2)

    class Redis:
        def __init__(self) -> None:
            self.calls = 0

        async def xread(self, cursor: dict[str, str], **_kwargs: Any) -> list[Any]:
            self.calls += 1
            stream_key, last_id = next(iter(cursor.items()))
            if last_id == "0-0":
                return [
                    (
                        stream_key,
                        [
                            (
                                "1-0",
                                {
                                    "event": "completion.delta",
                                    "data": '{"data":{"completion_id":"c1","text_delta":"a"}}',
                                },
                            ),
                            (
                                "2-0",
                                {
                                    "event": "completion.delta",
                                    "data": '{"data":{"completion_id":"c1","text_delta":"b"}}',
                                },
                            ),
                        ],
                    )
                ]
            if last_id == "2-0":
                return [
                    (
                        stream_key,
                        [
                            (
                                "3-0",
                                {
                                    "event": "completion.delta",
                                    "data": '{"data":{"completion_id":"c1","text_delta":"c"}}',
                                },
                            )
                        ],
                    )
                ]
            return []

    redis = Redis()
    events_seen = [
        item
        async for item in events._iter_replay_events(
            redis,
            stream_key="events:user:user-1",
            last_event_id="0-0",
            requested_channels={"task:c1"},
            include_user_channel=False,
            user_channel="user:user-1",
        )
    ]

    assert [item["id"] for item in events_seen] == ["1-0", "2-0", "3-0"]
    assert redis.calls == 2


@pytest.mark.asyncio
async def test_sse_replay_truncated_advances_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events, "_REPLAY_BATCH_SIZE", 2)
    monkeypatch.setattr(events, "_REPLAY_MAX_EVENTS", 2)

    class Redis:
        async def xread(self, cursor: dict[str, str], **_kwargs: Any) -> list[Any]:
            stream_key, last_id = next(iter(cursor.items()))
            assert last_id == "0-0"
            return [
                (
                    stream_key,
                    [
                        (
                            "1-0",
                            {
                                "event": "completion.delta",
                                "data": '{"data":{"completion_id":"c1","text_delta":"a"}}',
                            },
                        ),
                        (
                            "2-0",
                            {
                                "event": "completion.delta",
                                "data": '{"data":{"completion_id":"c1","text_delta":"b"}}',
                            },
                        ),
                    ],
                )
            ]

    events_seen = [
        item
        async for item in events._iter_replay_events(
            Redis(),
            stream_key="events:user:user-1",
            last_event_id="0-0",
            requested_channels={"task:c1"},
            include_user_channel=False,
            user_channel="user:user-1",
        )
    ]

    truncated = events_seen[-1]
    assert truncated["event"] == "replay_truncated"
    assert truncated["id"] == "2-0"
    assert json.loads(truncated["data"])["cursor"] == "2-0"


@pytest.mark.asyncio
async def test_events_replay_includes_auto_user_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_validate_channels(
        channels: list[str],
        _user_id: str,
        _db: Any,
    ) -> list[str]:
        return channels

    async def fake_iter_replay_events(*_args: Any, **kwargs: Any):
        captured.update(kwargs)
        if False:
            yield {}

    class PubSub:
        async def subscribe(self, *_channels: str) -> None:
            return None

        async def unsubscribe(self, *_channels: str) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def get_message(self, **_kwargs: Any) -> None:
            return None

    class Redis:
        def pubsub(self) -> PubSub:
            return PubSub()

    class DisconnectedRequest:
        headers = {"Last-Event-ID": f"{int(time.time() * 1000)}-0"}

        async def is_disconnected(self) -> bool:
            return True

    monkeypatch.setattr(events, "_validate_channels", fake_validate_channels)
    monkeypatch.setattr(events, "_iter_replay_events", fake_iter_replay_events)
    monkeypatch.setattr(events, "get_redis", lambda: Redis())

    response = await events.events(
        DisconnectedRequest(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        channels="conv:conv-1",
    )
    async for _chunk in response.body_iterator:
        pass

    assert captured["requested_channels"] == {"conv:conv-1", "user:user-1"}
    assert captured["include_user_channel"] is True
