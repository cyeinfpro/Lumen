import pytest
from pydantic import ValidationError

from lumen_core.schema_models.messaging import ImageParamsIn


@pytest.mark.parametrize("model", ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst"])
@pytest.mark.parametrize("quality", ["auto", "low", "medium", "high", "xhigh", "max"])
def test_new_image_model_accepts_quality_and_round_trips(model, quality):
    params = ImageParamsIn(model=model, render_quality=quality, background="transparent", output_format="jpeg")
    restored = ImageParamsIn.model_validate(params.model_dump())
    assert restored.model == model
    assert restored.render_quality == quality
    assert restored.output_format == "png"


@pytest.mark.parametrize("model,quality", [("gpt-image-2", "max"), ("gpt-image-2", "xhigh"), ("unknown", "high"), ("gpt-image-2.5-flare", "ultra")])
def test_invalid_model_quality_is_rejected_before_submission(model, quality):
    with pytest.raises(ValidationError):
        ImageParamsIn(model=model, render_quality=quality)


def test_legacy_request_defaults_to_original_model():
    assert ImageParamsIn().model == "gpt-image-2"
