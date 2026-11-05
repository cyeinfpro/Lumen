import pytest
from pydantic import ValidationError
from lumen_core.schema_models.messaging import ImageParamsIn
from lumen_core.canvas_schemas import ImageGenerateNodeConfig


@pytest.mark.parametrize("schema", [ImageParamsIn, ImageGenerateNodeConfig])
def test_native_output_contract(schema):
    value = schema(output_format="gif", frame_count=9, frame_duration_ms=200)
    assert value.frame_count == 9
    assert value.frame_duration_ms == 200
    assert schema(output_format="svg", output_compression=90).output_compression is None


@pytest.mark.parametrize("schema", [ImageParamsIn, ImageGenerateNodeConfig])
@pytest.mark.parametrize("params", [
    {"output_format": "svg", "count": 2},
    {"output_format": "gif", "frame_count": 1},
    {"output_format": "gif", "frame_count": 17},
    {"output_format": "gif", "frame_count": 64},
    {"output_format": "gif", "frame_count": 65},
    {"output_format": "gif", "frame_duration_ms": 21},
    {"output_format": "gif", "frame_duration_ms": 2010},
    {"output_format": "png", "frame_count": 9},
    {"complexity": "auto"},
])
def test_invalid_native_output_parameters(schema, params):
    with pytest.raises(ValidationError):
        schema(**params)


@pytest.mark.parametrize("schema", [ImageParamsIn, ImageGenerateNodeConfig])
@pytest.mark.parametrize("size,frames", [("3840x2160", 9), ("2048x2048", 16), ("10016x5008", 2)])
def test_gif_admission_rejects_lumen_budget(schema, size, frames):
    with pytest.raises(ValidationError, match="Lumen safety limit"):
        schema(output_format="gif", size_mode="fixed", fixed_size=size, frame_count=frames)


@pytest.mark.parametrize("schema", [ImageParamsIn, ImageGenerateNodeConfig])
def test_gif_admission_accepts_small_and_boundary_budget(schema):
    assert schema(output_format="gif", size_mode="fixed", fixed_size="512x512", frame_count=16)
    assert schema(output_format="gif", size_mode="fixed", fixed_size="4000x8000", frame_count=2)


def test_gif_media_and_admission_share_budget():
    from lumen_core.image_resource_limits import validate_gif_resource_budget
    validate_gif_resource_budget(1000, 1000, 64)
    with pytest.raises(ValueError, match="Lumen safety limit"):
        validate_gif_resource_budget(1001, 1000, 64)


def test_canvas_image_config_reexport_and_strict_contract():
    from lumen_core.canvas_image_config import ImageGenerateNodeConfig as Extracted
    assert ImageGenerateNodeConfig is Extracted
    with pytest.raises(ValidationError, match="Extra inputs"):
        ImageGenerateNodeConfig(unexpected=True)
