"""GPT Image request-contract regressions for the async image sidecar."""

from __future__ import annotations

from copy import deepcopy

import pytest

from image_job.contracts import (
    ALLOWED_FIXED_ENDPOINTS,
    ALLOWED_PREFIX_ENDPOINTS,
    DEFAULT_IMAGE_OUTPUT_COMPRESSION,
    DEFAULT_IMAGE_OUTPUT_FORMAT,
    IMAGE_OUTPUT_FORMATS,
)
from image_job.payloads import PayloadPolicy, validate_payload


@pytest.fixture
def policy() -> PayloadPolicy:
    return PayloadPolicy(
        allowed_fixed_endpoints=ALLOWED_FIXED_ENDPOINTS,
        allowed_prefix_endpoints=ALLOWED_PREFIX_ENDPOINTS,
        image_output_formats=frozenset(IMAGE_OUTPUT_FORMATS),
        default_image_output_format=DEFAULT_IMAGE_OUTPUT_FORMAT,
        default_image_output_compression=DEFAULT_IMAGE_OUTPUT_COMPRESSION,
        responses_strip_partial_images=True,
        max_endpoint_chars=2048,
        max_request_type_chars=100,
        default_retention_days=7,
        max_retention_days=30,
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "/v1/images/generations",
        "/v1/images/edits",
    ],
)
@pytest.mark.parametrize(
    "model",
    [
        "gpt-image-2",
        "gpt-image-2-2026-04-21",
        "gpt-image-2.5-flare",
        "gpt-image-2.5-sunburst",
        "chatgpt-image-latest",
    ],
)
@pytest.mark.parametrize("response_format", ["b64_json", "url"])
def test_gpt_image_payload_omits_legacy_response_format(
    policy: PayloadPolicy, endpoint: str, model: str, response_format: str
) -> None:
    body = {
        "model": model,
        "prompt": "A bicycle by the sea",
        "response_format": response_format,
        "output_format": "webp",
        "output_compression": 82,
        "stream": True,
        "partial_images": 0,
    }
    before = deepcopy(body)

    normalized = validate_payload({"endpoint": endpoint, "body": body}, policy)

    assert "response_format" not in normalized["body"]
    assert normalized["body"]["output_format"] == "webp"
    assert normalized["body"]["output_compression"] == 82
    assert normalized["body"]["stream"] is True
    assert normalized["body"]["partial_images"] == 0
    assert body == before


@pytest.mark.parametrize("model", ["dall-e-3", "provider-image-model"])
def test_other_image_models_keep_response_format(
    policy: PayloadPolicy, model: str
) -> None:
    normalized = validate_payload(
        {
            "endpoint": "/v1/images/generations",
            "body": {"model": model, "response_format": "url"},
        },
        policy,
    )

    assert normalized["body"]["response_format"] == "url"


def test_responses_image_tool_omits_legacy_response_format(
    policy: PayloadPolicy,
) -> None:
    function = {
        "type": "function",
        "name": "format_report",
        "parameters": {
            "type": "object",
            "properties": {
                "response_format": {"type": "string"},
            },
        },
    }
    body = {
        "model": "reasoning-model",
        "text": {"format": {"type": "json_object"}},
        "tools": [
            {
                "type": "image_generation",
                "model": "gpt-image-2",
                "response_format": "b64_json",
                "output_format": "webp",
            },
            function,
        ],
    }
    before = deepcopy(body)

    normalized = validate_payload(
        {
            "endpoint": "/v1/responses",
            "body": body,
        },
        policy,
    )["body"]

    assert "response_format" not in normalized["tools"][0]
    assert normalized["tools"][0]["output_format"] == "webp"
    assert normalized["tools"][1] == function
    assert normalized["text"] == body["text"]
    assert body == before


@pytest.mark.parametrize(
    "endpoint",
    [
        "/v1/images/generations",
        "/v1/images/edits",
        "/v1/responses",
    ],
)
@pytest.mark.parametrize(
    "output_format, expected_format",
    [
        ("webp", "webp"),
        ("png", "png"),
        ("jpeg", "png"),
    ],
)
def test_transparent_output_keeps_supported_format_and_normalizes_options(
    policy: PayloadPolicy, endpoint: str, output_format: str, expected_format: str
) -> None:
    image = {
        "model": "gpt-image-2",
        "background": "transparent",
        "output_format": output_format,
        "output_compression": 82,
        "moderation": "invalid",
    }
    body = (
        {"tools": [{"type": "image_generation", **image}]}
        if endpoint == "/v1/responses"
        else image
    )
    normalized = validate_payload({"endpoint": endpoint, "body": body}, policy)["body"]
    target = normalized["tools"][0] if endpoint == "/v1/responses" else normalized

    assert target["background"] == "transparent"
    assert target["output_format"] == expected_format
    assert target["moderation"] == "low"
    if expected_format == "webp":
        assert target["output_compression"] == 82
    else:
        assert "output_compression" not in target


def test_transparent_webp_gets_missing_compression_default(
    policy: PayloadPolicy,
) -> None:
    normalized = validate_payload(
        {
            "endpoint": "/v1/images/generations",
            "body": {
                "model": "gpt-image-2",
                "background": "transparent",
                "output_format": "webp",
            },
        },
        policy,
    )["body"]

    assert normalized["output_format"] == "webp"
    assert normalized["output_compression"] == DEFAULT_IMAGE_OUTPUT_COMPRESSION


@pytest.mark.parametrize(
    "endpoint",
    [
        "/v1/images/generations",
        "/v1/images/edits",
        "/v1/responses",
    ],
)
@pytest.mark.parametrize(
    "options",
    [
        {"output_format": []},
        {"background": []},
        {"moderation": {}},
        {"background": "transparent", "output_format": {}, "moderation": []},
    ],
)
def test_malformed_option_containers_use_defaults_without_server_error(
    policy: PayloadPolicy,
    endpoint: str,
    options: dict,
) -> None:
    image = {"model": "gpt-image-2", **options}
    body = (
        {"tools": [{"type": "image_generation", **image}]}
        if endpoint == "/v1/responses"
        else image
    )
    normalized = validate_payload({"endpoint": endpoint, "body": body}, policy)["body"]
    target = normalized["tools"][0] if endpoint == "/v1/responses" else normalized

    assert target["output_format"] in {"png", "jpeg", "webp"}
    assert target["background"] in {"auto", "transparent"}
    assert target["moderation"] == "low"
