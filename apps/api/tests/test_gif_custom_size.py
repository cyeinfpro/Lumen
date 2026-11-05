"""Actual API submission persists GIF size choice without quality presets."""
import json
from unittest.mock import AsyncMock
import pytest
from lumen_core.models import Generation
from lumen_core.schema_models.messaging import ImageParamsIn
from app.routes import messages
from app.canvas_services.execution_image_inputs import image_params as canvas_image_params
from apps.api.tests.test_messages_route import _Db, _Result, _conv, _message, _silent_body, _wallet_user


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ["auto", "768x432"])
async def test_gif_api_submission_preserves_size_requested(monkeypatch, size):
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(messages, "get_spec", lambda _: None)
    monkeypatch.setattr(messages, "_lookup_silent_generation", AsyncMock(return_value=None))
    monkeypatch.setattr(messages, "_lock_idempotency_key", AsyncMock(return_value=False))
    monkeypatch.setattr(messages, "_billing_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(messages, "_publish_message_appended", AsyncMock())
    monkeypatch.setattr(messages, "_publish_assistant_task", AsyncMock())
    db = _Db([_Result(_conv()), _Result(_message(id="parent-1", role="user"))])
    params = ImageParamsIn(model="gpt-image-2.6-sunburst", output_format="gif", quality="4k",
                           aspect_ratio="16:9", frame_count=2, frame_duration_ms=200,
                           size_mode="auto" if size == "auto" else "fixed",
                           fixed_size=None if size == "auto" else size)
    canvas = canvas_image_params(params.model_dump())
    assert canvas.size_mode == params.size_mode and canvas.fixed_size == params.fixed_size
    await messages.create_silent_generation(
        "conv-1", _silent_body(image_params=params), _wallet_user(), db)
    generation = next(value for value in db.added if isinstance(value, Generation))
    assert generation.size_requested == size
    persisted = json.loads(json.dumps(generation.upstream_request))
    assert persisted["frame_count"] == 2
    assert persisted["output_format"] == "gif"
