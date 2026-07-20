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


def _first_b64_field(
    candidate: dict[str, Any],
    fields: tuple[str, ...],
    *,
    b64_value_if_str: B64ValueIfStr,
) -> str | None:
    for field in fields:
        found = b64_value_if_str(candidate.get(field))
        if found:
            return found
    return None


def _payload_containers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [payload]
    nested_response = payload.get("response")
    if isinstance(nested_response, dict):
        containers.append(nested_response)
    return containers


def _mapping_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _extract_data_image_b64(
    container: dict[str, Any],
    *,
    b64_value_if_str: B64ValueIfStr,
) -> str | None:
    for entry in _mapping_entries(container.get("data")):
        found = _first_b64_field(
            entry,
            ("b64_json", "result"),
            b64_value_if_str=b64_value_if_str,
        )
        if found:
            return found
    return None


def _extract_output_entry_b64(
    entry: dict[str, Any],
    *,
    b64_value_if_str: B64ValueIfStr,
) -> str | None:
    direct = _first_b64_field(
        entry,
        ("result",),
        b64_value_if_str=b64_value_if_str,
    )
    if direct:
        return direct
    for piece in _mapping_entries(entry.get("content")):
        found = _first_b64_field(
            piece,
            ("result", "b64_json"),
            b64_value_if_str=b64_value_if_str,
        )
        if found:
            return found
    return None


def _extract_output_image_b64(
    container: dict[str, Any],
    *,
    b64_value_if_str: B64ValueIfStr,
) -> str | None:
    for entry in _mapping_entries(container.get("output")):
        found = _extract_output_entry_b64(
            entry,
            b64_value_if_str=b64_value_if_str,
        )
        if found:
            return found
    return None


def _extract_container_image_b64(
    container: dict[str, Any],
    *,
    b64_value_if_str: B64ValueIfStr,
) -> str | None:
    return _extract_data_image_b64(
        container,
        b64_value_if_str=b64_value_if_str,
    ) or _extract_output_image_b64(
        container,
        b64_value_if_str=b64_value_if_str,
    )


def _extract_image_b64_from_payload(
    payload: Any,
    *,
    b64_value_if_str: B64ValueIfStr,
) -> str | None:
    """Extract a base64 image from common OpenAI-compatible payload shapes."""
    if not isinstance(payload, dict):
        return None

    direct = _first_b64_field(
        payload,
        ("result", "b64_json"),
        b64_value_if_str=b64_value_if_str,
    )
    if direct:
        return direct

    item = payload.get("item")
    if isinstance(item, dict):
        nested = _first_b64_field(
            item,
            ("result", "b64_json"),
            b64_value_if_str=b64_value_if_str,
        )
        if nested:
            return nested

    for container in _payload_containers(payload):
        found = _extract_container_image_b64(
            container,
            b64_value_if_str=b64_value_if_str,
        )
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
