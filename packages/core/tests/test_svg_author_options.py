import pytest

from lumen_core.canvas_image_config import ImageGenerateNodeConfig
from lumen_core.schema_models.messaging import ImageParamsIn
from lumen_core.svg_options import normalize_svg_options
from lumen_core.models import Generation


@pytest.mark.parametrize("schema", [ImageParamsIn, ImageGenerateNodeConfig])
@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_explicit_author_is_independent_of_complexity(schema, effort):
    value = schema(output_format="svg", complexity="low",
                   svg={"model": " test-svg-author ", "reasoning": {"effort": effort}})
    assert value.svg.model == "test-svg-author"
    assert value.complexity == "low"
    assert value.svg.reasoning.effort == effort


@pytest.mark.parametrize("schema", [ImageParamsIn, ImageGenerateNodeConfig])
@pytest.mark.parametrize("svg", [{}, {"model": ""}, {"model": "  "}, {"model": 123},
                                  {"model": "author", "reasoning": {"effort": "auto"}},
                                  {"model": "author", "api_key": "private"},
                                  {"model": "author", "reasoning": {"secret": "private"}}])
def test_invalid_author_rejected(schema, svg):
    with pytest.raises(ValueError):
        schema(output_format="svg", svg=svg)


@pytest.mark.parametrize("schema", [ImageParamsIn, ImageGenerateNodeConfig])
@pytest.mark.parametrize("fmt", ["png", "jpeg", "webp", "gif", None])
def test_author_only_for_svg(schema, fmt):
    with pytest.raises(ValueError, match="SVG author"):
        schema(output_format=fmt, svg={"model": "test-author"})


def test_omission_null_and_no_inferred_model():
    assert normalize_svg_options(None, output_format="svg") is None
    assert normalize_svg_options({"model": "test-author", "reasoning": None},
                                 output_format="svg") == {"model": "test-author"}
    assert ImageParamsIn(output_format="svg").svg is None


def test_public_history_only_exposes_validated_author_fields():
    gen = Generation(upstream_request={"model": "gpt-image-2.6-sunburst",
        "output_format": "svg", "svg": {"model": "author", "reasoning": {"effort": "high"}},
        "api_key": "never-public"})
    assert gen.requested_params["svg"] == {"model": "author", "reasoning": {"effort": "high"}}
    assert "api_key" not in gen.requested_params
    gen.upstream_request = {"output_format": "svg", "svg": {"model": "author", "api_key": "private"}}
    assert "svg" not in gen.requested_params

@pytest.mark.parametrize("reasoning", [None, {}, {"effort": None}])
def test_default_reasoning_is_canonical_omission(reasoning):
    value = {"model": " test-author ", "reasoning": reasoning}
    assert normalize_svg_options(value, output_format="svg") == {"model": "test-author"}
    assert ImageParamsIn(output_format="svg", svg=value).svg.reasoning is None
