"""Reserved Agent pseudo-protocol detection for non-streamed text boundaries."""

from __future__ import annotations

import re

from .agent_content_safety import agent_content_safety_decision


_RESERVED_FUNCTIONS = frozenset(
    {
        "lumen_create_image",
        "exec_command",
        "apply_patch",
        "bash",
        "read",
        "write",
        "edit",
        "grep",
        "find",
        "ls",
        "store_put",
        "store_get",
    }
)
_FUNCTION_RE = re.compile(r"<function=\s*([^>\s]+)\s*>", re.IGNORECASE)


def contains_reserved_agent_protocol(value: str) -> bool:
    """Return true for reserved frames outside Markdown literal contexts."""

    fence_character: str | None = None
    fence_length = 0
    for raw_line in value.splitlines(keepends=True):
        line = raw_line.removesuffix("\n").removesuffix("\r")
        leading = len(line) - len(line.lstrip(" "))
        body = line[min(leading, 3) :]
        fence = re.match(r"(`{3,}|~{3,})", body)
        if fence_character is not None:
            if (
                fence is not None
                and fence.group(1)[0] == fence_character
                and len(fence.group(1)) >= fence_length
            ):
                fence_character = None
                fence_length = 0
            continue
        if fence is not None:
            fence_character = fence.group(1)[0]
            fence_length = len(fence.group(1))
            continue
        if body.startswith(">") or line.startswith("\t") or leading >= 4:
            continue
        inline_ticks = 0
        index = 0
        while index < len(line):
            if line[index] == "`":
                end = index
                while end < len(line) and line[end] == "`":
                    end += 1
                run = end - index
                if inline_ticks == 0:
                    inline_ticks = run
                elif inline_ticks == run:
                    inline_ticks = 0
                index = end
                continue
            if inline_ticks == 0 and line.startswith("<tool_call>", index):
                return True
            if inline_ticks == 0 and line.startswith("<function=", index):
                match = _FUNCTION_RE.match(line, index)
                if match is not None and match.group(1).lower() in _RESERVED_FUNCTIONS:
                    return True
            index += 1
    return False


def agent_text_boundary_error(value: str) -> str | None:
    if agent_content_safety_decision(value).blocked:
        return "content_policy_violation"
    if contains_reserved_agent_protocol(value):
        return "agent_provider_protocol_error"
    return None


__all__ = ["agent_text_boundary_error", "contains_reserved_agent_protocol"]
