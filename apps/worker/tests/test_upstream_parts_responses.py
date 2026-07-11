from __future__ import annotations

import inspect
from typing import Any

import pytest

from app import upstream
from app.upstream_parts import responses


@pytest.mark.parametrize(
    ("event", "expected_image", "expected_prompt"),
    [
        (
            {"result": "root-image", "revised_prompt": "root-prompt"},
            "root-image",
            "root-prompt",
        ),
        (
            {
                "item": {
                    "result": "item-image",
                    "revised_prompt": "item-prompt",
                }
            },
            "item-image",
            "item-prompt",
        ),
        ({"item": {"result": 123, "revised_prompt": None}}, None, None),
    ],
)
def test_response_event_extractors(
    event: dict[str, Any],
    expected_image: str | None,
    expected_prompt: str | None,
) -> None:
    assert responses._extract_response_image_b64(event) == expected_image
    assert responses._extract_response_revised_prompt(event) == expected_prompt


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("image-b64", "image-b64"),
        ("", None),
        (None, None),
        (b"image-b64", None),
    ],
)
def test_b64_value_if_str(value: Any, expected: str | None) -> None:
    assert responses._b64_value_if_str(value) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"result": "root-result"}, "root-result"),
        ({"item": {"b64_json": "item-b64"}}, "item-b64"),
        (
            {"data": [{"url": "https://example.test/image"}, {"b64_json": "data-b64"}]},
            "data-b64",
        ),
        ({"output": [{"result": "output-result"}]}, "output-result"),
        (
            {"response": {"output": [{"content": [{"b64_json": "content-b64"}]}]}},
            "content-b64",
        ),
        ({"data": [{"url": "https://example.test/image"}]}, None),
        (["not", "a", "mapping"], None),
    ],
)
def test_extract_image_b64_from_payload(
    payload: Any,
    expected: str | None,
) -> None:
    assert (
        responses._extract_image_b64_from_payload(
            payload,
            b64_value_if_str=responses._b64_value_if_str,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"usage": {"images": 3}}, 3),
        ({"response": {"usage": {"images": 2.0}}}, 2),
        ({"tool_usage": {"image_gen": {"images": 4}}}, 4),
        ({"response": {"tool_usage": {"image_gen": {"images": 0}}}}, 0),
        ({"usage": {"images": True}}, None),
        ({"usage": {"images": -1}}, None),
        ({"usage": {"images": 1.5}}, None),
        (None, None),
    ],
)
def test_extract_image_billable_count(payload: Any, expected: int | None) -> None:
    assert responses._extract_image_billable_count(payload) == expected


def test_upstream_facades_keep_legacy_parameter_signatures() -> None:
    expected_parameters = {
        "_extract_response_image_b64": ("event",),
        "_extract_response_revised_prompt": ("event",),
        "_b64_value_if_str": ("value",),
        "_extract_image_b64_from_payload": ("payload",),
        "_extract_image_billable_count": ("payload",),
    }

    for name, expected in expected_parameters.items():
        signature = inspect.signature(getattr(upstream, name))
        assert tuple(signature.parameters) == expected
        assert all(
            parameter.default is inspect.Parameter.empty
            for parameter in signature.parameters.values()
        )


def test_upstream_payload_facade_uses_current_nested_b64_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def fake_b64_value_if_str(value: Any) -> str | None:
        seen.append(value)
        return "patched-b64" if value == "selected" else None

    monkeypatch.setattr(upstream, "_b64_value_if_str", fake_b64_value_if_str)

    assert (
        upstream._extract_image_b64_from_payload(
            {"result": "skip", "b64_json": "selected"}
        )
        == "patched-b64"
    )
    assert seen == ["skip", "selected"]


def test_upstream_facades_resolve_extracted_helpers_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_extract(event: dict[str, Any]) -> str | None:
        return f"leaf:{event['result']}"

    monkeypatch.setattr(responses, "_extract_response_image_b64", fake_extract)

    assert upstream._extract_response_image_b64({"result": "image"}) == "leaf:image"
