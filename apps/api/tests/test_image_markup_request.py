"""Authorization and idempotency regressions for separate image markup layers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from lumen_core.constants import Intent
from lumen_core.schemas import PostMessageIn

from app.services.message_idempotency import (
    MESSAGE_CREATE_IDEMPOTENCY_OPERATION,
    canonical_request_fingerprint,
    message_request_fingerprint,
)
from app.services.message_request import validate_markup_image


def _http(code: str, message: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _db(value: str | None):
    return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: value,
    )))


@pytest.mark.asyncio
async def test_markup_owned_separate_layer_accepts_multiple_ordered_references():
    db = _db("markup")
    references = ["primary", "reference"]
    await validate_markup_image(
        db, user_id="owner", intent=Intent.IMAGE_TO_IMAGE,
        attachment_ids=references, markup_image_id="markup", mask_image_id=None,
        visibility_filter=None, http_error=_http,
    )
    assert references == ["primary", "reference"]
    statement = db.execute.call_args.args[0]
    parameters = statement.compile().params
    assert "owner" in parameters.values()
    assert "markup" in parameters.values()
    assert "deleted_at IS NULL" in str(statement)


@pytest.mark.asyncio
@pytest.mark.parametrize(("intent", "references", "markup", "mask", "code"), [
    (Intent.TEXT_TO_IMAGE, [], "markup", None, "markup_requires_image_to_image"),
    (Intent.IMAGE_TO_IMAGE, [], "markup", None, "markup_requires_image_to_image"),
    (Intent.IMAGE_TO_IMAGE, ["source"], "source", None, "markup_requires_separate_layer"),
    (Intent.IMAGE_TO_IMAGE, ["source"], "markup", "mask", "markup_mask_conflict"),
])
async def test_markup_invalid_combinations_fail_before_lookup(intent, references, markup, mask, code):
    db = _db("markup")
    with pytest.raises(HTTPException) as failure:
        await validate_markup_image(
            db, user_id="owner", intent=intent, attachment_ids=references,
            markup_image_id=markup, mask_image_id=mask,
            visibility_filter=None, http_error=_http,
        )
    assert failure.value.status_code == 422
    assert failure.value.detail["code"] == code
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_markup_inaccessible_or_deleted_layer_is_rejected():
    db = _db(None)
    with pytest.raises(HTTPException) as failure:
        await validate_markup_image(
            db, user_id="owner", intent=Intent.IMAGE_TO_IMAGE,
            attachment_ids=["source"], markup_image_id="other-owner-layer",
            mask_image_id=None, visibility_filter=None, http_error=_http,
        )
    assert failure.value.status_code == 404
    assert failure.value.detail["code"] == "markup_not_found"


def _body(**overrides):
    return PostMessageIn.model_validate({
        "text": "Draw a bird", "idempotency_key": "markup-regression",
        "intent": "text_to_image", **overrides,
    })


def test_absent_controls_preserve_existing_request_fingerprint():
    body = _body()
    legacy_payload = body.model_dump(mode="json", exclude={"idempotency_key"})
    legacy_payload.pop("markup_image_id", None)
    for key in ("complexity", "frame_count", "frame_duration_ms"):
        legacy_payload["image_params"].pop(key, None)
    assert message_request_fingerprint(body) == canonical_request_fingerprint(
        MESSAGE_CREATE_IDEMPOTENCY_OPERATION, legacy_payload,
    )


def test_markup_and_complexity_are_part_of_request_identity():
    ordinary = _body()
    assert message_request_fingerprint(_body(markup_image_id="layer-a")) != message_request_fingerprint(ordinary)
    assert message_request_fingerprint(_body(image_params={"complexity": "low"})) != message_request_fingerprint(ordinary)


def test_markup_upload_policy_preserves_original_png_even_with_mask_filename(tmp_path):
    import hashlib
    from PIL import Image
    from app.images.application.http_route_parts.upload import build_upload_policy
    from app.images.processing.service import ImageProcessor

    policy = build_upload_policy(
        purpose="image_markup", filename="mask_overlay.png", reference_size=(512, 512),
    )
    assert policy.allowed_mime == {"image/png"}
    assert not policy.normalizable_mime
    assert not policy.mask_requested
    assert policy.max_pixels == 16_777_216
    source = tmp_path / "markup.png"
    Image.new("RGBA", (512, 512), (255, 0, 0, 80)).save(source)
    original = source.read_bytes()
    stage = SimpleNamespace(
        path=source, size_bytes=len(original), sha256=hashlib.sha256(original).hexdigest(),
        new_temp_path=lambda suffix: tmp_path / ("derived" + suffix),
    )
    prepared = ImageProcessor().process(
        stage, "mask_overlay.png", allowed_mime=policy.allowed_mime,
        normalizable_mime=policy.normalizable_mime, max_bytes=policy.max_bytes,
        max_pixels=policy.max_pixels, max_long_side=policy.max_long_side,
        mask_requested=policy.mask_requested,
    )
    assert prepared.original_path == source
    assert prepared.original_path.read_bytes() == original
    assert prepared.mime == "image/png"
    assert (prepared.width, prepared.height) == (512, 512)


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", [Intent.CHAT, Intent.VISION_QA])
async def test_saved_gif_setting_does_not_block_non_image_messages(intent):
    from app.routes.messages_parts.submission import _validate_edit_input_ids

    await _validate_edit_input_ids(
        None, user=SimpleNamespace(id="owner"),
        body=_body(image_params={"output_format": "gif"}), intent=intent,
        attachment_ids=["reference"] if intent == Intent.VISION_QA else [],
        mask_image_id=None, runtime=SimpleNamespace(http_error=_http),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["gif", "svg"])
async def test_native_edit_with_static_references_is_allowed(output_format):
    from app.routes.messages_parts.submission import _validate_edit_input_ids

    references = ["source", "reference"]
    await _validate_edit_input_ids(
        None, user=SimpleNamespace(id="owner"),
        body=_body(image_params={"output_format": output_format}), intent=Intent.IMAGE_TO_IMAGE,
        attachment_ids=references, mask_image_id=None,
        runtime=SimpleNamespace(http_error=_http),
    )
    assert references == ["source", "reference"]
