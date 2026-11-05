"""Dated offline contracts: distinct usage, durable evidence, no paid retries."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import json
import pytest
from app.upstream_parts import direct_generation_responses, native_image_formats
from app.upstream_parts.image_execution import ImageRequestContext
from app.upstream_parts.generated_payload import InlineImageBytes
from app.upstream_parts.upstream_impl import build_image_upstream_runtime
from apps.worker.tests.test_svg_author_options import request

@pytest.mark.asyncio
async def test_author_usage_is_retained_separately_without_price(monkeypatch):
    runtime = build_image_upstream_runtime()
    services = runtime.services
    fixture = json.loads((Path(__file__).parents[3] / "packages/core/tests/fixtures/image26-live/svg-usage-s03.json").read_text())
    usage = fixture["usage"]
    author = fixture["svg_usage"]
    observed = []
    emit = AsyncMock()
    monkeypatch.setattr(services.core, "record_usage", observed.append)
    monkeypatch.setattr(services.transport, "emit_image_progress", emit)
    monkeypatch.setattr(services.core, "extract_image_results", AsyncMock(return_value=[]))
    await direct_generation_responses.complete_direct_generation_response(
        services=services, request=request(runtime),
        response=httpx.Response(200, json={"usage": usage, "svg_usage": {**author, "secret": "drop"}}),
        started=services.infrastructure.time.monotonic(), trace_id="offline",
        url="https://example.invalid/v1/images/generations", context=ImageRequestContext.create(),
        proxy_url=None, http_attempts=1)
    assert observed == [usage, author]
    assert emit.call_args.kwargs["svg_usage"] == author
    assert emit.call_args.kwargs["svg_author_cost_status"] == "unknown"

def test_actual_gif_metadata_reports_differences_without_rerun():
    raw = (Path(__file__).parents[3] / "packages/core/tests/fixtures/image26-live/gif-4frames-100ms.gif").read_bytes()
    item = request(None, output_format="gif", size="512x512", frame_count=9, frame_duration_ms=200)
    metadata = native_image_formats.validate_native_results(
        [(InlineImageBytes(raw), None)], item, url="https://example.invalid", path="images/generations", trace_id="offline")
    assert metadata["actual"][0] == {"width": 256, "height": 512, "frame_count": 4,
        "durations_ms": [100]*4, "total_duration_ms": 400, "loop": 0}
    assert metadata["mismatches"] == ["size", "frame_count", "frame_duration_ms"]
    assert metadata["result_count"] == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [True, False])
async def test_usage_and_actual_metadata_persist_in_public_diagnostics(monkeypatch, stored):
    from app.tasks.generation_parts import runner_dispatch_phase as phase
    from lumen_core.models import Generation
    initial = {"generation_diagnostics": {"existing": True}} if stored else {"requested_params": {"size": "auto"}}
    row = Generation(id="offline", upstream_request=initial)
    previous = dict(row.diagnostics)
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)), commit=AsyncMock())
    class Context:
        async def __aenter__(self): return session
        async def __aexit__(self, *args): return None
    deps = SimpleNamespace(store=SimpleNamespace(session=Context))
    monkeypatch.setattr(phase.DispatchGenerationServices, "from_deps", lambda _: deps)
    monkeypatch.setattr(phase, "generation_execution_epoch", lambda _: 3)
    monkeypatch.setattr(phase, "mark_upstream_response_received", lambda current, **_: dict(current.upstream_request))
    state = SimpleNamespace(services=object(), task_id="offline", attempt=1)
    actual = {"output_format": "gif", "actual": [{"width": 256, "height": 512}]}
    author = {"model": "gpt-6.1-sol", "total_tokens": 4643}
    await phase.record_generation_upstream_marker(state, response_received=True,
        native_output=actual, svg_usage={**author, "secret": "drop"})
    assert row.diagnostics == {**previous, "native_output": actual,
        "svg_usage": author, "svg_author_cost_status": "unknown"}
    assert state.gen_upstream_request_snapshot["generation_diagnostics"] == row.diagnostics
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("error", [EOFError("truncated"), SyntaxError("invalid image")])
def test_malformed_accepted_native_result_is_not_retryable(monkeypatch, error):
    runtime = build_image_upstream_runtime()
    def invalid(*_):
        raise error
    monkeypatch.setattr(native_image_formats, "_validate_native_bytes", invalid)
    with pytest.raises(Exception) as failure:
        native_image_formats.validate_native_results(
            [(InlineImageBytes(b"invalid"), None)], request(runtime, output_format="gif"),
            url="https://example.invalid", path="images/generations", trace_id="offline")
    assert failure.value.error_code == runtime.services.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
    assert failure.value.payload["upstream_result_unknown"] is True


def test_generation_metadata_does_not_apply_editor_byte_limit(monkeypatch):
    # Preserve generated-result policy independently of local-edit upload policy.
    from lumen_core import gif_editing
    monkeypatch.setattr(gif_editing, "MAX_GIF_BYTES", 1)
    raw = (Path(__file__).parents[3] / "packages/core/tests/fixtures/image26-live/gif-4frames-100ms.gif").read_bytes()
    metadata = native_image_formats._validate_native_bytes(raw, "gif")
    assert metadata["frame_count"] == 4
