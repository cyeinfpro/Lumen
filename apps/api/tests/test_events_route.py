from __future__ import annotations

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
    assert excinfo.value.detail["error"]["requested_count"] == events.MAX_SSE_CHANNELS + 1
    assert excinfo.value.detail["error"]["effective_count"] == events.MAX_SSE_CHANNELS + 2


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
