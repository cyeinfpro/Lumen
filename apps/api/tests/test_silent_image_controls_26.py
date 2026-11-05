from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.routes import messages
from app.routes.messages_parts import silent
from lumen_core.models import Generation
from apps.api.tests.test_messages_route import _Db, _Result, _conv, _message, _silent_body, _wallet_user


def test_silent_markup_and_controls_are_part_of_durable_hash():
    plain = _silent_body()
    payload = {
        "parent_message_id": plain.parent_message_id,
        "intent": plain.intent,
        "prompt": plain.prompt,
        "attachment_image_ids": plain.attachment_image_ids,
        "image_params": plain.image_params.model_dump(mode="json"),
    }
    for key in ("complexity", "frame_count", "frame_duration_ms"):
        payload["image_params"].pop(key, None)
    expected = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert silent.silent_generation_request_hash(plain) == expected
    assert silent.silent_generation_request_hash(
        plain.model_copy(update={"markup_image_id": "layer"})
    ) != expected
    assert silent.silent_generation_request_hash(
        plain.model_copy(update={"image_params": plain.image_params.model_copy(update={"complexity": "low"})})
    ) != expected


def test_public_requested_controls_do_not_expose_credentials():
    generation = Generation(upstream_request={
        "model": "gpt-image-2.6-flare", "complexity": "low",
        "frame_count": 12, "frame_duration_ms": 300, "markup_image_id": "layer",
        "output_format": "png", "api_key": "not-public", "auth_headers": {"Authorization": "not-public"},
    })
    assert generation.requested_params == {
        "model": "gpt-image-2.6-flare", "complexity": "low", "frame_count": 12,
        "frame_duration_ms": 300, "markup_image_id": "layer", "output_format": "png",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [True, False])
async def test_silent_markup_rechecks_ownership_and_persists_separate_metadata(monkeypatch, owned):
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(messages, "get_spec", lambda _: None)
    monkeypatch.setattr(messages, "_lookup_silent_generation", AsyncMock(return_value=None))
    monkeypatch.setattr(messages, "_lock_idempotency_key", AsyncMock(return_value=False))
    monkeypatch.setattr(messages, "_billing_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(messages, "_publish_message_appended", AsyncMock())
    monkeypatch.setattr(messages, "_publish_assistant_task", AsyncMock())
    db = _Db([
        _Result(_conv()),
        _Result(_message(id="parent-1", role="user")),
        _Result(all_values=["source", "reference"]),
        _Result("layer" if owned else None),
    ])
    body = _silent_body(
        intent="image_to_image", attachment_image_ids=["source", "reference"], markup_image_id="layer",
        image_params=_silent_body().image_params.model_copy(update={"complexity": "low"}),
    )
    if not owned:
        with pytest.raises(HTTPException) as error:
            await messages.create_silent_generation("conv-1", body, _wallet_user(), db)
        assert error.value.status_code == 404
        assert not any(isinstance(item, Generation) for item in db.added)
        return
    await messages.create_silent_generation("conv-1", body, _wallet_user(), db)
    generation = next(item for item in db.added if isinstance(item, Generation))
    assert generation.input_image_ids == ["source", "reference"]
    assert generation.primary_input_image_id == "source"
    assert generation.mask_image_id is None
    assert generation.upstream_request["markup_image_id"] == "layer"
    assert generation.upstream_request["complexity"] == "low"
    assert generation.upstream_request["input_images"][-1] == {"image_id": "layer", "role": "markup"}
    assert "images.user_id" in str(db.statements[-1]) or any(
        "images.user_id" in str(statement) and "images.deleted_at IS NULL" in str(statement)
        for statement in db.statements
    )
