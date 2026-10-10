from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app import canvas_run_notifications as notifications


ARGS = dict(
    user_id="owner",
    canvas_id="canvas",
    run_id="run",
    seq=2,
    execution_id="execution",
    event_type="execution.running",
)


@pytest.mark.asyncio
async def test_notice_uses_only_existing_owner_stream_and_stable_identity(monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr(notifications, "publish_event", publish)
    redis = object()
    assert await notifications.publish_canvas_run_notification(redis, **ARGS)
    assert await notifications.publish_canvas_run_notification(redis, **ARGS)
    first, second = publish.call_args_list
    assert first == second
    assert first.args == (redis,)
    assert first.kwargs["user_id"] == "owner"
    assert first.kwargs["channel"] == "user:owner"
    assert first.kwargs["event_name"] == "canvas.run.updated"
    assert first.kwargs["data"]["event_id"] == "canvas-run:run:2"
    assert "user_id" not in first.kwargs["data"]


@pytest.mark.asyncio
async def test_notification_failure_never_escapes_to_resubmit_committed_work(
    monkeypatch, caplog
):
    publish = AsyncMock(side_effect=RuntimeError("secret-provider-url"))
    monkeypatch.setattr(notifications, "publish_event", publish)
    assert not await notifications.publish_canvas_run_notification(object(), **ARGS)
    assert publish.await_count == 1
    assert "secret-provider-url" not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed(monkeypatch):
    publish = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(notifications, "publish_event", publish)
    with pytest.raises(asyncio.CancelledError):
        await notifications.publish_canvas_run_notification(object(), **ARGS)


@pytest.mark.asyncio
async def test_invalid_owner_or_sequence_cannot_publish(monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr(notifications, "publish_event", publish)
    assert not await notifications.publish_canvas_run_notification(
        object(), **{**ARGS, "user_id": ""}
    )
    assert not await notifications.publish_canvas_run_notification(
        object(), **{**ARGS, "seq": True}
    )
    publish.assert_not_awaited()
