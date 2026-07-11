"""Pure Responses payload extraction helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

B64ValueIfStr = Callable[[Any], str | None]


def _extract_response_image_b64(event: dict[str, Any]) -> str | None:
    if isinstance(event.get("result"), str):
        return event["result"]
    item = event.get("item")
    if isinstance(item, dict) and isinstance(item.get("result"), str):
        return item["result"]
    return None


def _extract_response_revised_prompt(event: dict[str, Any]) -> str | None:
    if isinstance(event.get("revised_prompt"), str):
        return event["revised_prompt"]
    item = event.get("item")
    if isinstance(item, dict) and isinstance(item.get("revised_prompt"), str):
        return item["revised_prompt"]
    return None


def _b64_value_if_str(value: Any) -> str | None:
    """Return non-empty string image fields as valid base64 values."""
    if isinstance(value, str) and value:
        return value
    return None


def _extract_image_b64_from_payload(
    payload: Any,
    *,
    b64_value_if_str: B64ValueIfStr,
) -> str | None:
    """Extract a base64 image from common OpenAI-compatible payload shapes."""
    if not isinstance(payload, dict):
        return None

    direct = b64_value_if_str(payload.get("result")) or b64_value_if_str(
        payload.get("b64_json")
    )
    if direct:
        return direct

    item = payload.get("item")
    if isinstance(item, dict):
        nested = b64_value_if_str(item.get("result")) or b64_value_if_str(
            item.get("b64_json")
        )
        if nested:
            return nested

    candidates: list[dict[str, Any]] = [payload]
    nested_response = payload.get("response")
    if isinstance(nested_response, dict):
        candidates.append(nested_response)

    for container in candidates:
        data = container.get("data")
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                found = b64_value_if_str(entry.get("b64_json")) or b64_value_if_str(
                    entry.get("result")
                )
                if found:
                    return found

        outputs = container.get("output")
        if isinstance(outputs, list):
            for entry in outputs:
                if not isinstance(entry, dict):
                    continue
                found = b64_value_if_str(entry.get("result"))
                if found:
                    return found
                content = entry.get("content")
                if isinstance(content, list):
                    for piece in content:
                        if isinstance(piece, dict):
                            found = b64_value_if_str(
                                piece.get("result")
                            ) or b64_value_if_str(piece.get("b64_json"))
                            if found:
                                return found
    return None


def _extract_image_billable_count(payload: Any) -> int | None:
    """Extract a non-negative image count from compatible usage payloads."""
    if not isinstance(payload, dict):
        return None

    def _coerce_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, float) and value.is_integer():
            return int(value) if value >= 0 else None
        return None

    containers: list[dict[str, Any]] = [payload]
    nested_response = payload.get("response")
    if isinstance(nested_response, dict):
        containers.append(nested_response)

    for container in containers:
        usage = container.get("usage")
        if isinstance(usage, dict):
            count = _coerce_int(usage.get("images"))
            if count is not None:
                return count
        tool_usage = container.get("tool_usage")
        if isinstance(tool_usage, dict):
            image_gen = tool_usage.get("image_gen")
            if isinstance(image_gen, dict):
                count = _coerce_int(image_gen.get("images"))
                if count is not None:
                    return count
    return None
