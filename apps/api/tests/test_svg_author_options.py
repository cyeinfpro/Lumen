"""SVG nested controls survive API admission, storage and regeneration."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import hashlib
import json

import pytest
from lumen_core.schema_models.messaging import ImageParamsIn
from lumen_core.schemas import PostMessageIn
from lumen_core.sizing import ResolvedSize
from app.services.message_submission import image_upstream_request
from app.services.message_idempotency import message_request_fingerprint
from app.routes.messages_parts.silent import silent_generation_request_hash
from app.routes import regenerate
from app.canvas_services.execution_image_inputs import image_params as canvas_image_params
from apps.api.tests.test_messages_route import _silent_body

AUTHOR = {"model": "test-svg-author", "reasoning": {"effort": "high"}}


def params(svg=AUTHOR):
    return ImageParamsIn(model="gpt-image-2.6-sunburst", output_format="svg",
                         svg=svg, complexity="low", aspect_ratio="21:9",
                         size_mode="fixed", fixed_size="3808x1632")


@pytest.mark.parametrize("svg", [AUTHOR, {"model": "test-svg-author"}, None])
def test_admitted_parameters_roundtrip_to_persistent_request(svg):
    settings = params(svg)
    request = image_upstream_request(settings, ResolvedSize("3808x1632", 3808, 1632, ""))
    persisted = json.loads(json.dumps(request))
    assert persisted.get("svg") == svg
    assert ("svg" in persisted) == (svg is not None)
    assert persisted["model"] == settings.model
    assert persisted["complexity"] == "low"
    assert canvas_image_params(settings.model_dump()).svg == settings.svg


def test_author_and_effort_are_semantic_request_identity():
    variants = [AUTHOR, {"model": "other-test-author"},
                {"model": "test-svg-author", "reasoning": {"effort": "low"}}, None]
    hashes = [message_request_fingerprint(PostMessageIn(
        text="test", idempotency_key="svg-test", intent="text_to_image", image_params=params(value))) for value in variants]
    silent_hashes = [silent_generation_request_hash(_silent_body(image_params=params(value)))
                     for value in variants]
    assert len(set(hashes)) == len(variants)
    assert len(set(silent_hashes)) == len(variants)


def test_omitted_author_preserves_184_silent_identity():
    body = _silent_body(image_params=params(None))
    image = body.image_params.model_dump(mode="json")
    for key in ("svg", "complexity", "frame_count", "frame_duration_ms"):
        if image.get(key) is None:
            image.pop(key, None)
    payload = {"parent_message_id": body.parent_message_id, "intent": body.intent,
               "prompt": body.prompt, "attachment_image_ids": body.attachment_image_ids,
               "image_params": image}
    expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
    assert silent_generation_request_hash(body) == expected


@pytest.mark.asyncio
async def test_regenerate_keeps_author_and_independent_complexity(monkeypatch):
    original = image_upstream_request(params(), ResolvedSize("3808x1632", 3808, 1632, ""))
    generation = SimpleNamespace(size_requested="3808x1632", aspect_ratio="21:9",
                                 upstream_request=original)
    monkeypatch.setattr(regenerate, "_ordered_target_generations", AsyncMock(return_value=[generation]))
    replay = await regenerate._image_params_from_target(
        None, user_id="owner", conv_id="conversation", target_msg_id="source")
    assert replay.svg.model_dump(exclude_none=True) == AUTHOR
    assert replay.complexity == "low"
    assert replay.fixed_size == "3808x1632"
    assert original["svg"] == AUTHOR

@pytest.mark.parametrize("reasoning", [None, {}, {"effort": None}])
def test_default_author_effort_does_not_change_fingerprint(reasoning):
    def body(svg):
        return PostMessageIn(text="test", idempotency_key="svg-test", intent="text_to_image", image_params=params(svg))
    assert message_request_fingerprint(body({"model": "author", "reasoning": reasoning})) == (
        message_request_fingerprint(body({"model": "author"})))


@pytest.mark.asyncio
async def test_real_silent_submission_persists_json_native_svg(monkeypatch):
    from app.routes import messages
    from lumen_core.models import Generation
    from apps.api.tests.test_messages_route import _Db, _Result, _conv, _message, _wallet_user
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(messages, "get_spec", lambda _: None)
    monkeypatch.setattr(messages, "_lookup_silent_generation", AsyncMock(return_value=None))
    monkeypatch.setattr(messages, "_lock_idempotency_key", AsyncMock(return_value=False))
    monkeypatch.setattr(messages, "_billing_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(messages, "_publish_message_appended", AsyncMock())
    monkeypatch.setattr(messages, "_publish_assistant_task", AsyncMock())
    db = _Db([_Result(_conv()), _Result(_message(id="parent-1", role="user"))])
    await messages.create_silent_generation(
        "conv-1", _silent_body(image_params=params()), _wallet_user(), db)
    generation = next(value for value in db.added if isinstance(value, Generation))
    persisted = json.loads(json.dumps(generation.upstream_request))
    assert persisted["svg"] == AUTHOR
    assert isinstance(generation.upstream_request["svg"], dict)
    assert persisted["model"] == "gpt-image-2.6-sunburst"
    assert persisted["complexity"] == "low"
    assert generation.size_requested == "3808x1632"
