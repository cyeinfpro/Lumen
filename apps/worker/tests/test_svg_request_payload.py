"""Network-free contracts for the validated SVG payload across runtime ports."""

from copy import deepcopy
from typing import get_type_hints

import pytest

from lumen_core.svg_options import (
    SvgOptions,
    SvgOptionsPayload,
    normalize_svg_options,
    public_svg_options,
)

from app.tasks.generation_parts.composition_support import build_image_execution_request
from app.tasks.generation_parts.request_options import image_request_options
from app.tasks.generation_parts.services import (
    GenerationProviderContext,
    GenerationProviderRequest,
)
from app.upstream_parts.direct_requests import _apply_generation_controls
from app.upstream_parts.image_execution import ImageExecutionRequest


def test_svg_runtime_ports_share_the_validated_payload_contract():
    expected = SvgOptionsPayload | None
    assert get_type_hints(GenerationProviderRequest)["svg"] == expected
    assert get_type_hints(ImageExecutionRequest)["svg"] == expected
    assert get_type_hints(normalize_svg_options)["return"] == expected
    assert get_type_hints(public_svg_options)["return"] == expected


@pytest.mark.parametrize("model", ["test-svg-author", " test-provider/custom-author "])
@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_svg_payload_preserves_explicit_author_and_all_reasoning_efforts(model, effort):
    author = {"model": model, "reasoning": {"effort": effort}}
    original = deepcopy(author)
    expected = {"model": model.strip(), "reasoning": {"effort": effort}}
    assert normalize_svg_options(author, output_format="svg") == expected
    assert public_svg_options(author, output_format="svg") == expected
    assert SvgOptions.model_validate(author).model_dump(exclude_none=True) == expected

    options = image_request_options(
        {"model": "gpt-image-2.6-sunburst", "output_format": "svg",
         "render_quality": "high", "complexity": "low", "svg": author},
        size="3808x1632",
    )
    provider = GenerationProviderRequest(
        prompt="contract", size="3808x1632", n=1, quality=options["render_quality"],
        output_format=options["output_format"], output_compression=None,
        background="auto", moderation="auto", model=options["responses_model"],
        image_model=options["image_model"], complexity=options["complexity"],
        svg=options["svg"], progress_callback=None, provider_override=None,
        user_id=None, context=GenerationProviderContext("trace", 1, "task", 1),
    )
    execution = build_image_execution_request(
        provider, action="generate", upstream_runtime=None,
    )
    body = {"model": execution.image_model}
    _apply_generation_controls(body, execution, True)
    assert body["svg"] == expected
    assert body["model"] == "gpt-image-2.6-sunburst"
    assert body["complexity"] == "low"
    assert "reasoning" not in body
    assert author == original


@pytest.mark.parametrize("reasoning", [None, {}, {"effort": None}])
def test_svg_payload_omits_unspecified_reasoning(reasoning):
    author = {"model": " test-svg-author ", "reasoning": reasoning}
    expected = {"model": "test-svg-author"}
    assert normalize_svg_options(author, output_format="svg") == expected
    assert public_svg_options(author, output_format="svg") == expected
    assert SvgOptions.model_validate(author).model_dump(exclude_none=True) == expected


def test_svg_payload_preserves_absent_author():
    assert normalize_svg_options(None, output_format="svg") is None
    assert normalize_svg_options(None, output_format="png") is None
    assert public_svg_options(None, output_format="svg") is None
