"""Whitelisted Responses API JSON and SSE output-text extraction."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


_OUTPUT_TEXT_DELTA_EVENTS = frozenset({"response.output_text.delta"})
_OUTPUT_TEXT_DONE_EVENTS = frozenset({"response.output_text.done"})
_CONTENT_PART_EVENTS = frozenset(
    {"response.content_part.added", "response.content_part.done"}
)
_OUTPUT_ITEM_EVENTS = frozenset(
    {"response.output_item.added", "response.output_item.done"}
)
_RESPONSE_DONE_EVENTS = frozenset({"response.completed", "response.done"})
_LEGACY_DELTA_KEYS = frozenset(
    {"delta", "item_id", "output_index", "content_index", "logprobs"}
)


def _extract_output_text_part(part: object) -> str:
    if not isinstance(part, dict) or part.get("type") != "output_text":
        return ""
    for key in ("text", "output_text"):
        text = part.get(key)
        if isinstance(text, str) and text:
            return text
    return ""


def _extract_output_item_text(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        text for part in content if (text := _extract_output_text_part(part))
    )


def extract_response_output_text(payload: object) -> str:
    """Extract text only from whitelisted Responses API output paths."""

    if not isinstance(payload, dict):
        return ""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    return "".join(text for item in output if (text := _extract_output_item_text(item)))


def _extract_legacy_sse_output_text(payload: dict[str, Any]) -> str:
    """Handle known data-only gateway shapes without searching arbitrary fields."""

    for candidate in (payload, payload.get("response")):
        text = extract_response_output_text(candidate)
        if text:
            return text
    part_text = _extract_output_text_part(payload.get("part"))
    if part_text:
        return part_text
    item_text = _extract_output_item_text(payload.get("item"))
    if item_text:
        return item_text
    delta = payload.get("delta")
    if isinstance(delta, str) and delta and set(payload).issubset(_LEGACY_DELTA_KEYS):
        return delta
    return ""


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = payload.get(key)
        if isinstance(text, str) and text:
            return text
    return ""


@dataclass(frozen=True)
class _ParsedSseEvent:
    event_type: str | None
    payload: dict[str, Any]


def _parse_sse_event(raw_event: str) -> _ParsedSseEvent | None:
    event_name: str | None = None
    data_lines: list[str] = []
    for line in raw_event.splitlines():
        line = line.strip()
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip() or None
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    payload_event = payload.get("type")
    if not isinstance(payload_event, str) or not payload_event:
        payload_event = None
    if (
        event_name
        and event_name.startswith("response.")
        and payload_event
        and event_name != payload_event
    ):
        return None
    return _ParsedSseEvent(
        event_type=payload_event or event_name,
        payload=payload,
    )


@dataclass
class _OutputTextAccumulator:
    fallback: list[str] = field(default_factory=list)
    output_text_done: list[str] = field(default_factory=list)
    content_part_added: list[str] = field(default_factory=list)
    content_part_done: list[str] = field(default_factory=list)
    output_item_added: list[str] = field(default_factory=list)
    output_item_done: list[str] = field(default_factory=list)
    response_done: str = ""

    def add(self, parsed: _ParsedSseEvent) -> None:
        event_type = parsed.event_type
        payload = parsed.payload
        if event_type in _OUTPUT_TEXT_DELTA_EVENTS:
            self._append(
                self.fallback, _first_text(payload, ("delta", "text", "output_text"))
            )
        elif event_type in _OUTPUT_TEXT_DONE_EVENTS:
            self._append(
                self.output_text_done,
                _first_text(payload, ("text", "output_text")),
            )
        elif event_type in _CONTENT_PART_EVENTS:
            target = (
                self.content_part_done
                if event_type.endswith(".done")
                else self.content_part_added
            )
            self._append(target, _extract_output_text_part(payload.get("part")))
        elif event_type in _OUTPUT_ITEM_EVENTS:
            target = (
                self.output_item_done
                if event_type.endswith(".done")
                else self.output_item_added
            )
            self._append(target, _extract_output_item_text(payload.get("item")))
        elif event_type in _RESPONSE_DONE_EVENTS:
            self.response_done = self._response_done_text(payload)
        elif event_type is None:
            self._append(self.fallback, _extract_legacy_sse_output_text(payload))

    @staticmethod
    def _append(target: list[str], text: str) -> None:
        if text:
            target.append(text)

    @staticmethod
    def _response_done_text(payload: dict[str, Any]) -> str:
        text = extract_response_output_text(payload.get("response"))
        return text or extract_response_output_text(payload)

    def result(self) -> str:
        if self.response_done:
            return self.response_done
        for chunks in (
            self.output_text_done,
            self.content_part_done,
            self.output_item_done,
            self.content_part_added,
            self.output_item_added,
            self.fallback,
        ):
            if chunks:
                return "".join(chunks)
        return ""


def extract_sse_output_text(raw: str) -> str:
    """Extract text from explicitly supported Responses output-text SSE events."""

    accumulator = _OutputTextAccumulator()
    for raw_event in raw.replace("\r\n", "\n").split("\n\n"):
        parsed = _parse_sse_event(raw_event)
        if parsed is not None:
            accumulator.add(parsed)
    return accumulator.result()


__all__ = ["extract_response_output_text", "extract_sse_output_text"]
