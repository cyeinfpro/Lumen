"""Projection helpers for persisted message content."""

from __future__ import annotations

from typing import Any


_AGENT_PUBLIC_KEYS = frozenset(
    {
        "text",
        "source",
        "agent_run_id",
        "blocks",
        "output_revision",
        "output_runtime_seq",
        "attachments",
        "input_images",
        "images",
        "tool_calls",
        "generation_ids",
        "memory_writes",
        "used_memory_summary",
        "reasoning_summary",
        "thinking_summary",
    }
)
_AGENT_ATTACHMENT_KEYS = frozenset(
    {"image_id", "role", "label", "reference_label", "weight"}
)
_AGENT_TOOL_CALL_KEYS = frozenset(
    {
        "id",
        "name",
        "label",
        "mode",
        "status",
        "generation_ids",
        "generation_count",
        "error_code",
    }
)
_AGENT_BLOCK_KEYS = frozenset(
    {
        "kind",
        "turn",
        "text",
        "tool_call_id",
        "ordinal",
        "name",
        "status",
        "generation_ids",
    }
)
_AGENT_IMAGE_KEYS = frozenset(
    {
        "image_id",
        "generation_id",
        "mime",
        "width",
        "height",
        "blurhash",
    }
)


def _public_dict_list(value: Any, allowed: frozenset[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {key: item[key] for key in allowed if key in item}
        for item in value
        if isinstance(item, dict)
    ]


def _public_agent_content(content: dict[str, Any]) -> dict[str, Any]:
    projected = {key: content[key] for key in _AGENT_PUBLIC_KEYS if key in content}
    for key in ("attachments", "input_images"):
        if key in projected:
            projected[key] = _public_dict_list(
                projected[key],
                _AGENT_ATTACHMENT_KEYS,
            )
    if "blocks" in projected:
        projected["blocks"] = _public_dict_list(projected["blocks"], _AGENT_BLOCK_KEYS)
    if "tool_calls" in projected:
        projected["tool_calls"] = _public_dict_list(
            projected["tool_calls"],
            _AGENT_TOOL_CALL_KEYS,
        )
    if "images" in projected:
        projected["images"] = _public_dict_list(
            projected["images"],
            _AGENT_IMAGE_KEYS,
        )
    if "generation_ids" in projected:
        raw_generation_ids = projected["generation_ids"]
        projected["generation_ids"] = (
            [value for value in raw_generation_ids if isinstance(value, str) and value][
                :16
            ]
            if isinstance(raw_generation_ids, list)
            else []
        )
    return projected


def public_message_content(content: Any) -> dict[str, Any]:
    """Project persisted message JSON onto the user-visible content contract."""

    if not isinstance(content, dict):
        return {}
    if content.get("source") == "agent" or isinstance(content.get("agent_run_id"), str):
        return _public_agent_content(content)
    return {
        key: value
        for key, value in content.items()
        if not (isinstance(key, str) and key.startswith("_"))
    }


__all__ = ["public_message_content"]
