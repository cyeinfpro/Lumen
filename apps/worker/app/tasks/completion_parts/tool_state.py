"""Pure normalization and reduction for completion tool-call state."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Any

from lumen_core.chat_tools import (
    TOOL_TERMINAL_STATUSES,
    ToolStatus,
    normalize_tool_status as normalize_upstream_tool_status,
    tool_status_idle_timed_out,
    tool_status_idle_timeout_remaining_seconds,
)

logger = logging.getLogger(__name__)

_WEB_SEARCH_TOOL_TYPE = "web_search"
_FILE_SEARCH_TOOL_TYPE = "file_search"
_CODE_INTERPRETER_TOOL_TYPE = "code_interpreter"
_IMAGE_GENERATION_TOOL_TYPE = "image_generation"


def _tool_display_label(tool_type: str, name: str | None = None) -> str:
    if tool_type == _WEB_SEARCH_TOOL_TYPE:
        return "联网搜索"
    if tool_type == _FILE_SEARCH_TOOL_TYPE:
        return "检索文件"
    if tool_type == _CODE_INTERPRETER_TOOL_TYPE:
        return "运行代码"
    if tool_type == _IMAGE_GENERATION_TOOL_TYPE:
        return "生成图片"
    if name:
        return f"调用{name}"
    return "调用工具"


@dataclass(frozen=True)
class _ToolCallState:
    id: str
    type: str
    status: str
    name: str | None = None
    label: str | None = None
    title: str | None = None
    error: str | None = None

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "label": self.label or _tool_display_label(self.type, self.name),
        }
        if self.name:
            out["name"] = self.name
        if self.title:
            out["title"] = self.title
        if self.error:
            out["error"] = self.error
        return out


def _normalize_tool_type(raw: Any, *, event_type: str = "") -> str | None:
    value = raw if isinstance(raw, str) else ""
    if value.endswith("_call"):
        value = value[: -len("_call")]
    if value in {
        _WEB_SEARCH_TOOL_TYPE,
        _FILE_SEARCH_TOOL_TYPE,
        _CODE_INTERPRETER_TOOL_TYPE,
        _IMAGE_GENERATION_TOOL_TYPE,
        "function",
        "tool",
    }:
        return value
    if value == "function_call":
        return "function"
    if value == "tool_call":
        return "tool"

    # Tool-specific Responses events are shaped like
    # response.web_search_call.searching / response.code_interpreter_call.completed.
    if event_type.startswith("response.") and "_call." in event_type:
        middle = event_type[len("response.") :].split(".", 1)[0]
        return _normalize_tool_type(middle, event_type="")
    return None


def _normalize_tool_status(raw: Any, *, event_type: str = "") -> str | None:
    normalized = normalize_upstream_tool_status(raw, event_type=event_type)
    if normalized is ToolStatus.UNKNOWN:
        if raw is not None and str(raw).strip():
            logger.warning(
                "unknown upstream tool status raw=%r event_type=%s", raw, event_type
            )
        else:
            normalized = ToolStatus.RUNNING
    return normalized.value


def _tool_status_rank(status: str | None) -> int:
    return {
        "unknown": 0,
        "queued": 0,
        "running": 1,
        "succeeded": 2,
        "failed": 3,
        "cancelled": 3,
        "timed_out": 3,
    }.get(status or "", -1)


def _summarize_tool_error(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:500]
    if isinstance(value, dict):
        code = value.get("code") or value.get("type")
        message = value.get("message") or value.get("reason")
        if code and message:
            return f"{code}: {message}"[:500]
        if message:
            return str(message)[:500]
        if code:
            return str(code)[:500]
    return None


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_tool_call_update(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    event_type = event_type if isinstance(event_type, str) else ""
    item = event.get("item")
    call = event.get("tool_call") or event.get("call")
    source: dict[str, Any]
    if isinstance(item, dict):
        source = item
    elif isinstance(call, dict):
        source = call
    else:
        source = event

    tool_type = _normalize_tool_type(source.get("type"), event_type=event_type)
    if tool_type is None:
        tool_type = _normalize_tool_type(event.get("tool_type"), event_type=event_type)
    if tool_type is None:
        return None

    call_id = _first_str(
        source.get("id"),
        source.get("call_id"),
        source.get("tool_call_id"),
        event.get("item_id"),
        event.get("output_item_id"),
        event.get("call_id"),
        event.get("tool_call_id"),
        event.get("id"),
    )
    if call_id is None:
        return None

    name = _first_str(source.get("name"), event.get("name"))
    title = _first_str(
        source.get("query"),
        event.get("query"),
        source.get("title"),
        event.get("title"),
    )
    status = _normalize_tool_status(source.get("status"), event_type=event_type)
    error = _summarize_tool_error(
        source.get("error")
        or source.get("incomplete_details")
        or event.get("error")
        or event.get("incomplete_details")
    )
    if error and status in (None, ToolStatus.UNKNOWN.value):
        status = ToolStatus.FAILED.value
    status = status or ToolStatus.RUNNING.value

    return {
        "id": call_id,
        "type": tool_type,
        "status": status,
        "name": name,
        "title": title,
        "label": _tool_display_label(tool_type, name),
        "error": error,
    }


def _merge_tool_call_state(
    previous: _ToolCallState | None,
    update: dict[str, Any],
) -> _ToolCallState:
    next_status = update["status"]
    if previous is not None:
        if (
            previous.status in TOOL_TERMINAL_STATUSES
            and next_status in TOOL_TERMINAL_STATUSES
        ) or _tool_status_rank(previous.status) > _tool_status_rank(next_status):
            next_status = previous.status
    next_type = update["type"] or (previous.type if previous is not None else "tool")
    next_name = update.get("name") or (previous.name if previous is not None else None)
    next_label = update.get("label") or (
        previous.label
        if previous is not None
        else _tool_display_label(next_type, next_name)
    )
    return _ToolCallState(
        id=update["id"],
        type=next_type,
        status=next_status,
        name=next_name,
        label=next_label,
        title=update.get("title") or (previous.title if previous is not None else None),
        error=update.get("error") or (previous.error if previous is not None else None),
    )


class _CompletionToolTracker:
    """Normalize Responses tool-call events and suppress duplicate progress."""

    def __init__(self) -> None:
        self._calls: dict[str, _ToolCallState] = {}
        self._last_published: dict[str, tuple[Any, ...]] = {}
        self.last_update_ts: float | None = None

    def _publish_if_changed(self, state: _ToolCallState) -> dict[str, Any] | None:
        signature = (
            state.type,
            state.status,
            state.name,
            state.label,
            state.title,
            state.error,
        )
        if self._last_published.get(state.id) == signature:
            return None
        self._last_published[state.id] = signature
        self.last_update_ts = time.monotonic()
        return state.payload()

    def update(self, event: dict[str, Any]) -> dict[str, Any] | None:
        update = _extract_tool_call_update(event)
        if update is None:
            return None
        call_id = update["id"]
        previous = self._calls.get(call_id)
        next_state = _merge_tool_call_state(previous, update)
        self._calls[call_id] = next_state
        return self._publish_if_changed(next_state)

    def update_from_response(
        self, response: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        if not isinstance(response, dict):
            return []
        output = response.get("output")
        if not isinstance(output, list):
            return []
        published: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            event = {"type": "response.output_item.done", "item": item}
            payload = self.update(event)
            if payload is not None:
                published.append(payload)
        return published

    def finalize_active(
        self,
        status: str,
        *,
        error: str | None = None,
    ) -> list[dict[str, Any]]:
        published: list[dict[str, Any]] = []
        for call_id, state in list(self._calls.items()):
            if state.status in TOOL_TERMINAL_STATUSES:
                continue
            next_state = replace(
                state,
                status=status,
                error=error or state.error,
            )
            self._calls[call_id] = next_state
            payload = self._publish_if_changed(next_state)
            if payload is not None:
                published.append(payload)
        return published

    @property
    def invocation_count(self) -> int:
        return len(self._calls)

    @property
    def has_active(self) -> bool:
        return any(
            state.status not in TOOL_TERMINAL_STATUSES for state in self._calls.values()
        )

    def idle_timeout_remaining(self, timeout_s: float) -> float | None:
        if not self.has_active:
            return None
        now = time.monotonic()
        if tool_status_idle_timed_out(
            self.last_update_ts,
            now=now,
            timeout_s=timeout_s,
        ):
            return 0.0
        remaining = tool_status_idle_timeout_remaining_seconds(
            self.last_update_ts,
            now=now,
            timeout_s=timeout_s,
        )
        return None if remaining is None else max(0.001, remaining)

    def content(self) -> list[dict[str, Any]]:
        return [state.payload() for state in self._calls.values()]
