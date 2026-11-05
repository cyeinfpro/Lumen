"""Regression coverage for the composition request adapter boundary."""
from dataclasses import fields

import pytest

from app.tasks.generation_parts import composition_ports
from app.tasks.generation_parts.services import (
    GenerationProviderContext,
    GenerationProviderEditRequest,
    GenerationProviderRequest,
)


@pytest.mark.parametrize("action", ["generate", "edit"])
@pytest.mark.parametrize("model,fmt,complexity,frames", [
    ("gpt-image-2", "png", None, None),
    ("gpt-image-2.6-flare", "gif", "low", 9),
    ("gpt-image-2.6-sunburst", "svg", "high", None),
])
def test_provider_adapter_preserves_request_contract(
    monkeypatch, action, model, fmt, complexity, frames,
):
    seen = []
    marker = object()
    monkeypatch.setattr(composition_ports, action + "_image", lambda item: seen.append(item))
    provider = composition_ports.DefaultGenerationProvider(None, None, None)
    common = GenerationProviderRequest(
        prompt="preserve", size="1024x1024", n=1, quality="medium",
        output_format=fmt, output_compression=80, background="transparent",
        moderation="auto", model="upstream-model", progress_callback=marker,
        provider_override=marker, user_id="user",
        context=GenerationProviderContext("trace", 2, "task", 3, marker),
        image_model=model, complexity=complexity, frame_count=frames,
        frame_duration_ms=200 if frames else None,
    )
    images = [b"first", b"second"]
    if action == "edit":
        provider.edit(GenerationProviderEditRequest(common, images, b"mask", b"markup"))
    else:
        provider.generate(common)
    actual = seen[0]
    for field in fields(common):
        if field.name != "context":
            assert getattr(actual, field.name) == getattr(common, field.name)
    assert actual.action == action
    assert actual.images == (images if action == "edit" else None)
    assert actual.images is not images
    assert actual.mask == (b"mask" if action == "edit" else None)
    assert actual.markup == (b"markup" if action == "edit" else None)
    assert actual.request_context.trace_id == common.context.trace_id
    assert actual.request_context.retry_attempt == common.context.retry_attempt
    assert actual.request_context.sidecar_execution is marker
    assert actual.request_context.quota_scope.task_id == common.context.quota_task_id
    assert actual.request_context.quota_scope.attempt_epoch == common.context.quota_attempt_epoch
    assert actual.request_context.upstream_runtime is None
    assert actual.upstream_runtime is None
