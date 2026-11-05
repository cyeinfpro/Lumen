"""Offline SVG workflow security, exact usage, durable receipt and local repair tests."""

from __future__ import annotations
import asyncio
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import pytest
from PIL import Image as PILImage
from app import vision_svg_artifact as svg
from app import completion_checkpoint as checkpoint
from app.completion_checkpoint_takeover import completion_checkpoint_claim_action
from app.tasks.completion_parts import runner
from app.tasks.completion_parts.vision_svg_repair import repair_terminal_svg
from app.tasks.completion_parts.request_metadata import (
    merge_completion_upstream_metadata,
)
from lumen_core.models import Image, ImageVariant, Message
from apps.worker.tests.test_completion_checkpoint import (
    _checkpoint_fixture,
    _terminal_checkpoint,
)

FIXTURE = Path(__file__).parent / "fixtures/vision-svg-s11.json"
SAFE = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="orange"/></svg>'


def receipt(text=SAFE):
    c = _terminal_checkpoint(usage_exact=True, text=text, version=2)
    c.user_id = "user-1"
    c.message_id = "message-1"
    c.model = "gpt-6.1-sol"
    c.status = "streaming"
    c.cancel_requested_at = None
    c.upstream_request.update(
        artifact_format="svg",
        artifact_status="pending",
        source_image_id="source",
        source_image_sha256="a" * 64,
    )
    return c


class Session:
    def __init__(self, completion):
        self.completion = completion
        self.message = NS(id=completion.message_id, status="pending", content={})
        self.images = {}
        self.added = []
        self.commits = 0

    async def get(self, model, key):
        if model is Message:
            return self.message
        if model is Image:
            return self.images.get(key)
        return self.completion

    def add(self, row):
        self.added.append(row)
        if isinstance(row, Image):
            self.images[row.id] = row

    async def commit(self):
        self.commits += 1


@pytest.fixture
def renderer(monkeypatch):
    variant = NS(bytes=b"offline-raster-preview", size=(20, 20))
    render = Mock(
        return_value=NS(
            width=20, height=20, display=variant, preview=variant, thumb=variant
        )
    )
    monkeypatch.setattr(svg.image_artifacts, "_make_image_variants_sync", render)
    files = {}

    def put(key, data):
        if key in files:
            assert files[key] == data
        files[key] = data
        return NS(created=True, size=len(data))

    monkeypatch.setattr(svg.storage, "put_bytes_result", put)
    return render, files


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "<svg>",
        "<html/>",
        SAFE.replace("<rect", "<script>alert(1)</script><rect"),
        SAFE.replace("<rect", '<image href="data:image/png;base64,AA=="/><rect'),
        SAFE.replace("<rect", '<image href="https://example.com/a.png"/><rect'),
        SAFE.replace("<rect", "<style>path{fill:red}</style><rect"),
        SAFE.replace("<rect", "<foreignObject/><rect"),
        SAFE.replace("<rect", "<animate/><rect"),
        SAFE.replace("<rect", '<rect onclick="alert(1)"/><rect'),
        SAFE.replace('fill="orange"', 'fill="url(https://example.com/x)"'),
        '<!DOCTYPE svg [<!ENTITY x "evil">]>' + SAFE,
        '<?xml-stylesheet href="https://example.com/x"?>' + SAFE,
        SAFE.replace('fill="orange"', 'style="fill:red"'),
        "x" * (svg.MAX_SVG_BYTES + 1),
        '<svg width="1" height="1">' + "<g/>" * 10001 + "</svg>",
    ],
)
def test_strict_output_rejections(text):
    with pytest.raises((ValueError, UnicodeError)):
        svg.validate_svg_text(text)


def png():
    out = io.BytesIO()
    PILImage.new("RGB", (20, 20), "orange").save(out, format="PNG")
    return out.getvalue()


def test_source_bytes_verified():
    raw = png()
    assert svg.source_data_url(raw, hashlib.sha256(raw).hexdigest()).startswith(
        "data:image/png;base64,"
    )
    for raw, sha in [
        (raw, "b" * 64),
        (b"GIF89a", None),
        (b"x" * (svg.MAX_SOURCE_BYTES + 1), None),
    ]:
        with pytest.raises(Exception):
            svg.source_data_url(raw, sha)


def test_animation_is_not_static():
    out = io.BytesIO()
    PILImage.new("RGB", (2, 2), "red").save(
        out,
        format="PNG",
        save_all=True,
        append_images=[PILImage.new("RGB", (2, 2), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(ValueError):
        svg.source_data_url(out.getvalue())


def test_s11_fixture_provenance_and_svg():
    f = json.loads(FIXTURE.read_text())
    assert (
        f["source_sha256"]
        == "b1cb9e5fc0f36c07ad78db8220e286d5599726e602a935c3a2620d027236542b"
    )
    text = f["response"]["output"][0]["content"][0]["text"]
    assert (
        hashlib.sha256(svg.validate_svg_text(text)).hexdigest()
        == "d1848e048420be55f9aa882f2c5afec703be44fae68c59b2207a06b51c18a677"
    )
    assert f["response"]["usage"]["input_tokens"] == 1307
    assert f["response"]["usage"]["output_tokens"] == 214


@pytest.mark.asyncio
async def test_valid_svg_private_non_tool_artifact_and_stable_identity(renderer):
    c = receipt()
    s = Session(c)
    result = await svg.stage_vision_svg_artifact(s, c)
    assert result["artifact_status"] == "ready"
    image = next(row for row in s.added if isinstance(row, Image))
    assert image.owner_generation_id is None and image.visibility == "private"
    assert image.metadata_jsonb["source"] == "vision_svg"
    assert image.metadata_jsonb["completion_id"] == c.id
    assert image.metadata_jsonb["upstream_response_id"] == "resp-1"
    assert len([row for row in s.added if isinstance(row, ImageVariant)]) == 3
    assert c.image_output_tokens == 0
    before = len(s.added)
    c.execution_epoch = 8
    again = await svg.stage_vision_svg_artifact(s, c)
    assert again["artifact_image_id"] == result["artifact_image_id"]
    assert len(s.added) == before and renderer[0].call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["", "<svg><script/></svg>", "x" * (svg.MAX_SVG_BYTES + 1)]
)
async def test_invalid_is_private_text_not_media(bad, renderer):
    c = receipt(bad)
    s = Session(c)
    result = await svg.stage_vision_svg_artifact(s, c)
    assert result["artifact_status"] == "invalid"
    assert "<svg" not in result["text"] and "images" not in result
    assert result["artifact_source_text"] == bad[: svg.MAX_SVG_BYTES]
    assert not s.added and renderer[0].call_count == 0


@pytest.mark.asyncio
async def test_render_failure_local_recovery_uses_receipt_not_model(
    renderer, monkeypatch
):
    c = receipt()
    s = Session(c)
    render, files = renderer
    render.side_effect = RuntimeError("native renderer unavailable")
    result = await svg.stage_vision_svg_artifact(s, c)
    assert result["artifact_status"] == "processing_failed" and c.text == SAFE
    assert not s.added and not files
    render.side_effect = None
    result = await svg.stage_vision_svg_artifact(s, c)
    assert result["artifact_status"] == "ready"
    assert c.image_output_tokens == 0 and c.tokens_out == 45


@pytest.mark.asyncio
async def test_storage_failure_has_no_media_adoption_and_retries_same_keys(
    renderer, monkeypatch
):
    c = receipt()
    s = Session(c)
    files = renderer[1]
    real = svg.storage.put_bytes_result
    count = 0

    def failing(key, data):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("disk full")
        return real(key, data)

    monkeypatch.setattr(svg.storage, "put_bytes_result", failing)
    assert (await svg.stage_vision_svg_artifact(s, c))[
        "artifact_status"
    ] == "processing_failed"
    assert not s.added
    first = set(files)
    monkeypatch.setattr(svg.storage, "put_bytes_result", real)
    assert (await svg.stage_vision_svg_artifact(s, c))["artifact_status"] == "ready"
    assert first <= set(files) and len(files) == 4


@pytest.mark.asyncio
async def test_checkpoint_committed_before_postprocessing_and_s11_tokens(monkeypatch):
    state, c, s = _checkpoint_fixture()
    c.model = "gpt-6.1-sol"
    c.upstream_request = {"artifact_format": "svg"}
    state.preparation.artifact_format = "svg"
    state.ports.upstream._merge_completion_upstream_metadata = (
        merge_completion_upstream_metadata
    )
    response = json.loads(FIXTURE.read_text())["response"]
    response["output_text"] = response["output"][0]["content"][0]["text"]
    await runner._handle_completed(
        state, {"response": response}, append_completed_text=False, finalize_tools=True
    )
    assert s.commits == 1
    assert c.tokens_in == 1307 and c.tokens_out == 214 and c.image_output_tokens == 0
    assert c.text.startswith("<svg")
    assert checkpoint.completion_has_completed_checkpoint(c)
    assert not state.streaming.tool_images
    assert c.upstream_request["vision_svg_actual_model"] == "gpt-6.1-sol"


@pytest.mark.asyncio
async def test_artifact_settlement_uses_only_completion_charge(renderer):
    c = receipt()
    s = Session(c)
    billing = NS(charge_completion=AsyncMock())
    staged = []

    def stage(*args, **kwargs):
        staged.append(kwargs["payload"])
        return ("event", "sse", kwargs["payload"])

    context = NS(
        session=s, billing=billing, now=datetime.now(timezone.utc), stage_event=stage
    )
    await checkpoint.apply_completed_checkpoint(context, c)
    billing.charge_completion.assert_awaited_once_with(s, c)
    assert c.status == "succeeded" and c.image_output_tokens == 0
    assert staged[0]["data"]["artifact_status"] == "ready"
    assert staged[0]["data"]["text"] == s.message.content["text"]
    assert not staged[0]["data"]["text"].startswith("<svg")


@pytest.mark.parametrize("text", ["", " ", "<svg><script/></svg>"])
def test_completed_invalid_or_empty_is_recoverable_without_redispatch(text):
    c = receipt(text)
    assert checkpoint.completion_has_completed_checkpoint(c)
    assert not checkpoint.completion_checkpoint_has_no_usable_output(c)
    assert completion_checkpoint_claim_action(c).required


def test_unknown_usage_cannot_redispatch():
    c = receipt()
    c.upstream_request["completion_checkpoint_usage_exact"] = False
    c.upstream_request["completion_checkpoint_usage_complete"] = False
    c.upstream_request["completion_checkpoint_state"] = "artifacts_committed"
    assert completion_checkpoint_claim_action(c).required
    assert not checkpoint.completion_has_completed_checkpoint(c)


@pytest.mark.asyncio
async def test_svg_delta_never_published_or_flushed():
    state = NS(
        preparation=NS(artifact_format="svg"),
        streaming=NS(has_partial=False, accumulated_text=""),
        usage=NS(delta_counter=0),
        ports=NS(retry=NS(_CANCEL_CHECK_EVERY_DELTAS=99)),
    )
    await runner._handle_delta(state, {"delta": SAFE}, phase="initial")
    assert state.streaming.accumulated_text == SAFE
    await runner._publish_thinking(state, "<svg/>")
    await runner._store_image_event(
        state, {"image_b64": "unexpected"}, mark_partial=True
    )
    assert not await runner.handle_completion_tool_call(
        state, {}, allow_tool_limit=True
    )


@pytest.mark.asyncio
async def test_terminal_repair_has_no_charge_or_upstream(renderer):
    c = receipt()
    c.status = "succeeded"
    c.upstream_request["artifact_status"] = "processing_failed"
    s = Session(c)
    state = NS(
        request=NS(redis=object(), channel="task:c"),
        settlement=NS(lease_lost=asyncio.Event()),
        ports=NS(
            retry=NS(
                _raise_if_completion_cancelled=AsyncMock(), _LeaseLost=RuntimeError
            ),
            events=NS(
                _stage_completion_event=Mock(return_value=("e", "s", {})),
                _deliver_completion_event=AsyncMock(),
            ),
        ),
    )
    assert await repair_terminal_svg(state, s, c)
    assert s.commits == 1 and c.upstream_request["artifact_status"] == "ready"
    assert c.tokens_out == 45 and c.image_output_tokens == 0
    assert not await repair_terminal_svg(state, s, c)
    assert s.commits == 1


@pytest.mark.asyncio
async def test_terminal_repair_cancel_and_unknown_rejected(renderer):
    c = receipt()
    c.status = "succeeded"
    c.upstream_request["artifact_status"] = "processing_failed"
    s = Session(c)
    c.cancel_requested_at = "cancelled"
    assert not await repair_terminal_svg(NS(), s, c)
    c.cancel_requested_at = None
    c.upstream_request["completion_checkpoint_usage_exact"] = False
    assert not await repair_terminal_svg(NS(), s, c)
    assert not s.added


@pytest.mark.asyncio
async def test_terminal_repair_lease_loss_cannot_commit(renderer):
    c = receipt()
    c.status = "succeeded"
    c.upstream_request["artifact_status"] = "processing_failed"
    s = Session(c)
    lost = asyncio.Event()
    lost.set()
    state = NS(
        settlement=NS(lease_lost=lost), ports=NS(retry=NS(_LeaseLost=RuntimeError))
    )
    with pytest.raises(RuntimeError):
        await repair_terminal_svg(state, s, c)
    assert s.commits == 0 and not s.added


@pytest.mark.asyncio
async def test_context_is_only_current_owned_image_and_prompt(monkeypatch):
    c = receipt()
    c.input_image_ids = ["source"]
    raw = png()
    c.upstream_request["source_image_sha256"] = hashlib.sha256(raw).hexdigest()
    image = NS(user_id=c.user_id, deleted_at=None, storage_key="source.png")
    target = NS(parent_message_id="parent")
    parent = NS(
        content={"text": "draw orange", "web_search": True, "memory": "must not inject"}
    )

    async def get(model, key):
        return image if model is Image else target if key == c.message_id else parent

    state = NS(preparation=NS(), streaming=NS())
    monkeypatch.setattr(svg.image_artifacts, "require_svg_renderer", Mock())
    monkeypatch.setattr(svg.storage, "get_bytes", lambda key: raw)
    await svg.load_vision_svg_context(state, c, NS(get=get))
    assert (
        state.streaming.chat_tools == [] and state.preparation.artifact_format == "svg"
    )
    assert len(state.streaming.input_list) == 1
    parts = state.streaming.input_list[0]["content"]
    assert len(parts) == 2 and parts[0] == {"type": "input_text", "text": "draw orange"}
    assert parts[1]["type"] == "input_image"
    assert "memory" not in state.streaming.instructions
    image.user_id = "other"
    with pytest.raises(ValueError):
        await svg.load_vision_svg_context(state, c, NS(get=get))
    image.user_id = c.user_id
    image.deleted_at = "deleted"
    with pytest.raises(ValueError):
        await svg.load_vision_svg_context(state, c, NS(get=get))


@pytest.mark.asyncio
async def test_wire_body_tools_empty_store_false_no_local_fields(monkeypatch):
    state = NS(
        preparation=NS(
            user_api_credential_id=None,
            chat_model="gpt-6.1-sol",
            reasoning_effort=None,
            fast_mode=False,
            artifact_format="svg",
            artifact_max_output_tokens=4096,
        ),
        streaming=NS(
            input_list=[{"role": "user", "content": []}],
            instructions=svg.SVG_INSTRUCTIONS,
            chat_tools=[],
        ),
        ports=NS(
            upstream=NS(_normalize_reasoning_effort_for_upstream=lambda x: x),
            tools=NS(
                _configure_chat_tools=lambda body, tools: body.update(tools=tools)
            ),
            context=NS(
                runtime_settings=NS(
                    resolve_int=AsyncMock(side_effect=lambda key, default: default)
                )
            ),
            retry=NS(
                _MAX_TOOL_INVOCATIONS_DEFAULT=8,
                _CANCEL_POLL_INTERVAL_S=0.1,
                _TOOL_IDLE_TIMEOUT_S_DEFAULT=30,
            ),
        ),
    )
    monkeypatch.setattr(runner, "load_request_context", AsyncMock())
    await runner.prepare_completion_request(state)
    assert (
        state.streaming.body["tools"] == [] and state.streaming.body["store"] is False
    )
    assert state.streaming.body["max_output_tokens"] == 4096
    assert (
        "artifact_format" not in state.streaming.body
        and "source_image_id" not in state.streaming.body
    )


@pytest.mark.asyncio
async def test_storage_unknown_receipt_is_not_ready(renderer, monkeypatch):
    c = receipt()
    s = Session(c)
    monkeypatch.setattr(
        svg.storage, "put_bytes_result", lambda key, data: NS(state="unknown")
    )
    assert (await svg.stage_vision_svg_artifact(s, c))[
        "artifact_status"
    ] == "processing_failed"
    assert not s.added


def test_endpoint_never_contains_credentials():
    assert (
        svg.safe_endpoint("https://name:secret@host/v1/responses?key=secret#token")
        == "/v1/responses"
    )
    assert svg.safe_endpoint("/responses?api_key=secret") == "/responses"


@pytest.mark.asyncio
async def test_empty_completed_text_still_charges_exact_text_usage(renderer):
    c = receipt("")
    s = Session(c)
    billing = NS(charge_completion=AsyncMock())
    context = NS(
        session=s,
        billing=billing,
        now=datetime.now(timezone.utc),
        stage_event=lambda *a, **k: ("e", "s", k),
    )
    await checkpoint.apply_completed_checkpoint(context, c)
    billing.charge_completion.assert_awaited_once_with(s, c)
    assert (
        c.status == "succeeded" and c.upstream_request["artifact_status"] == "invalid"
    )
    assert c.image_output_tokens == 0 and not s.added


def test_svg_render_pixel_bounds():
    for dims in [(8193, 1), (4001, 4000)]:
        with pytest.raises(ValueError):
            svg.validate_svg_text(f'<svg width="{dims[0]}" height="{dims[1]}"/>')


@pytest.mark.asyncio
async def test_unready_source_rejected_before_loading(monkeypatch):
    c = receipt()
    c.input_image_ids = ["source"]
    session = NS(
        get=AsyncMock(
            return_value=NS(
                user_id=c.user_id, deleted_at=None, artifact_status="processing"
            )
        )
    )
    with pytest.raises(ValueError):
        await svg.load_vision_svg_context(NS(), c, session)


@pytest.mark.asyncio
async def test_inexact_positive_usage_is_unknown_not_actual_charge():
    from app.completion_execution_settlement import settle_completion_actual_or_unknown

    c = receipt()
    c.upstream_request.update(
        completion_checkpoint_usage_exact=False,
        completion_checkpoint_usage_complete=False,
        completion_checkpoint_state="artifacts_committed",
    )
    assert c.tokens_in > 0 and c.tokens_out > 0
    billing = NS(
        charge_completion=AsyncMock(),
        release_completion=AsyncMock(),
        settle_completion_unknown_upstream=AsyncMock(),
    )
    session = object()
    await settle_completion_actual_or_unknown(
        billing, session, c, reason="vision_svg_usage_unknown", knowledge="unknown"
    )
    billing.charge_completion.assert_not_awaited()
    billing.release_completion.assert_not_awaited()
    billing.settle_completion_unknown_upstream.assert_awaited_once_with(
        session, c, reason="vision_svg_usage_unknown", knowledge="unknown"
    )


@pytest.mark.asyncio
async def test_exact_cache_usage_preserved_without_image_tool_tokens():
    state, c, s = _checkpoint_fixture()
    c.model = "gpt-6.1-sol"
    c.upstream_request = {"artifact_format": "svg"}
    state.preparation.artifact_format = "svg"
    state.ports.upstream._merge_completion_upstream_metadata = (
        merge_completion_upstream_metadata
    )
    response = json.loads(FIXTURE.read_text())["response"]
    response["output_text"] = response["output"][0]["content"][0]["text"]
    response["usage"]["input_tokens_details"] = {"cached_tokens": 100}
    await runner._handle_completed(
        state, {"response": response}, append_completed_text=False, finalize_tools=True
    )
    # Core stores uncached input separately to avoid charging cached input twice.
    assert c.tokens_in == 1207 and c.tokens_out == 214
    assert c.tokens_in + c.cache_read_tokens == 1307
    assert c.cache_read_tokens == 100 and c.image_output_tokens == 0
    assert (
        c.upstream_request["completion_checkpoint_usage"]["input_tokens_details"][
            "cached_tokens"
        ]
        == 100
    )
