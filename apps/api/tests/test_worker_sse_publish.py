from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


def _load_worker_sse_publish() -> Any:
    path = (
        Path(__file__).resolve().parents[2]
        / "worker"
        / "app"
        / "sse_publish.py"
    )
    spec = importlib.util.spec_from_file_location("worker_sse_publish_under_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_worker_publish_event_envelope_contains_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sse_publish = _load_worker_sse_publish()

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(sse_publish.asyncio, "sleep", no_sleep)

    class Redis:
        def __init__(self) -> None:
            self.published: list[tuple[str, str]] = []
            self.stream_entries: list[dict[str, str]] = []

        async def eval(
            self,
            _lua: str,
            _num_keys: int,
            _stream_key: str,
            _dedupe_key: str,
            event_id: str,
            event_name: str,
            payload_json: str,
            *_args: str,
        ) -> str:
            self.stream_entries.append(
                {"event": event_name, "event_id": event_id, "data": payload_json}
            )
            return "1710000000000-0"

        async def publish(self, channel: str, payload: str) -> int:
            self.published.append((channel, payload))
            return 1

    redis = Redis()

    await sse_publish.publish_event(
        redis,
        "user-1",
        "task:completion-1",
        "completion.queued",
        {"completion_id": "completion-1"},
    )

    stream_payload = json.loads(redis.stream_entries[0]["data"])
    publish_payload = json.loads(redis.published[0][1])
    assert stream_payload["channel"] == "task:completion-1"
    assert publish_payload["channel"] == "task:completion-1"
