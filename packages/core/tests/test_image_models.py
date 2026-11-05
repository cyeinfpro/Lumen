import pytest
from pydantic import ValidationError

from lumen_core.image_models import (
    DEFAULT_IMAGE_MODEL,
    IMAGE_MODELS,
    LEGACY_IMAGE_MODELS,
    upgrade_legacy_image_model,
    validate_image_model_quality,
)
from lumen_core.schema_models.messaging import ImageParamsIn
from lumen_core.canvas_schemas import ImageGenerateNodeConfig


@pytest.mark.parametrize("model", sorted(IMAGE_MODELS))
@pytest.mark.parametrize("quality", ["auto", "low", "medium", "high", "xhigh", "max"])
def test_active_image_model_accepts_quality_and_round_trips(model, quality):
    params = ImageParamsIn(
        model=model, render_quality=quality, background="transparent", output_format="jpeg"
    )
    restored = ImageParamsIn.model_validate(params.model_dump())
    assert restored.model == model
    assert restored.render_quality == quality
    assert restored.output_format == "png"


@pytest.mark.parametrize("model", [*sorted(LEGACY_IMAGE_MODELS), "unknown"])
def test_retired_and_unknown_models_cannot_create_new_requests(model):
    with pytest.raises(ValidationError):
        ImageParamsIn(model=model)


@pytest.mark.parametrize("quality", ["ultra", "", "HIGH"])
def test_invalid_quality_is_rejected(quality):
    with pytest.raises(ValidationError):
        ImageParamsIn(model=DEFAULT_IMAGE_MODEL, render_quality=quality)


def test_only_26_models_are_active_and_default_supports_max():
    assert IMAGE_MODELS == {"gpt-image-2.6-flare", "gpt-image-2.6-sunburst"}
    assert ImageParamsIn().model == "gpt-image-2.6-flare"
    assert ImageParamsIn(render_quality="max").render_quality == "max"


@pytest.mark.parametrize("old,new", [
    ("gpt-image-2", "gpt-image-2.6-flare"),
    ("gpt-image-2.5-flare", "gpt-image-2.6-flare"),
    ("gpt-image-2.5-sunburst", "gpt-image-2.6-sunburst"),
    (None, "gpt-image-2.6-flare"),
])
def test_legacy_requests_upgrade_without_mutating_history(old, new):
    original = {"model": old, "render_quality": "high"}
    assert upgrade_legacy_image_model(original["model"]) == new
    assert original["model"] == old
    config = ImageGenerateNodeConfig.model_validate(original)
    assert config.model == old


@pytest.mark.parametrize("model", sorted(LEGACY_IMAGE_MODELS))
def test_admitted_legacy_tasks_remain_valid(model):
    validate_image_model_quality(model, "high", allow_legacy=True)
    with pytest.raises(ValueError):
        validate_image_model_quality(model, "high")
    if model == "gpt-image-2":
        with pytest.raises(ValueError):
            validate_image_model_quality(model, "max", allow_legacy=True)
