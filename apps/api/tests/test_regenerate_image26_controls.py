"""2.6 formats and independent controls survive a new regeneration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.routes import regenerate


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["png", "svg", "gif"])
async def test_regenerate_preserves_format_complexity_and_animation(monkeypatch, format):
    request = {
        "model": "gpt-image-2.6-sunburst", "render_quality": "high",
        "complexity": "low", "output_format": format, "output_format_source": "request",
        "billing_tier": "1k",
    }
    if format == "gif":
        request.update(frame_count=12, frame_duration_ms=150)
    generation = SimpleNamespace(
        size_requested="512x512" if format == "gif" else "1024x1024",
        aspect_ratio="1:1", upstream_request=request,
    )
    monkeypatch.setattr(regenerate, "_ordered_target_generations", AsyncMock(return_value=[generation]))
    params = await regenerate._image_params_from_target(
        None, user_id="owner", conv_id="conversation", target_msg_id="source",
    )
    assert params.model == "gpt-image-2.6-sunburst"
    assert params.output_format == format
    assert params.complexity == "low"
    assert params.frame_count == (12 if format == "gif" else None)
    assert params.frame_duration_ms == (150 if format == "gif" else None)
    assert request["output_format"] == format
