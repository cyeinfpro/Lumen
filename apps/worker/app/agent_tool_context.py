"""Closed first-party tool policy and virtual-file projection for Agent runs."""

from __future__ import annotations

from typing import Any

from lumen_core.agent_events import (
    AGENT_FILE_TOOLS,
    AGENT_FIRST_PARTY_TOOLS,
    AGENT_TOOL_CREATE_IMAGE,
    AGENT_TOOL_LIST_FILES,
    AGENT_TOOL_READ_FILE,
    AGENT_TOOL_SEARCH_FILES,
    AGENT_TOOL_WEB_SEARCH,
)
from lumen_core.model_entities import AgentRun, Message

from .agent_context_errors import AgentContextError
from .agent_runtime_client import AgentRuntimeToolPolicy, AgentRuntimeWorkspaceFile


def _snapshot_dict(run: AgentRun, key: str) -> dict[str, Any]:
    snapshot = run.request_snapshot_jsonb
    if not isinstance(snapshot, dict):
        return {}
    value = snapshot.get(key)
    return value if isinstance(value, dict) else {}


def _snapshot_list(run: AgentRun, key: str) -> list[Any]:
    snapshot = run.request_snapshot_jsonb
    if not isinstance(snapshot, dict):
        return []
    value = snapshot.get(key)
    return list(value) if isinstance(value, list) else []


def _positive_int(value: Any, fallback: int, *, maximum: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, maximum)
    return fallback


def _nonnegative_int(value: Any, fallback: int, *, maximum: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return min(value, maximum)
    return fallback


def runtime_tool_policy(run: AgentRun) -> AgentRuntimeToolPolicy:
    policy = _snapshot_dict(run, "tool_policy") or _snapshot_dict(run, "limits")
    return AgentRuntimeToolPolicy(
        max_image_tool_calls=_nonnegative_int(
            policy.get("max_image_tool_calls"), 2, maximum=8
        ),
        max_images_per_run=_positive_int(
            policy.get("max_images_per_run"), 4, maximum=16
        ),
        max_web_search_calls=_nonnegative_int(
            policy.get("max_web_search_calls"), 0, maximum=8
        ),
        max_file_tool_calls=_nonnegative_int(
            policy.get("max_file_tool_calls"), 0, maximum=32
        ),
        max_tool_calls=_nonnegative_int(policy.get("max_tool_calls"), 8, maximum=48),
    )


def workspace_files(message: Message) -> list[AgentRuntimeWorkspaceFile]:
    content = message.content if isinstance(message.content, dict) else {}
    raw_files = content.get("files")
    if not isinstance(raw_files, list):
        return []
    files: list[AgentRuntimeWorkspaceFile] = []
    for raw in raw_files[:8]:
        try:
            files.append(AgentRuntimeWorkspaceFile.model_validate(raw))
        except ValueError as exc:
            raise AgentContextError("agent_workspace_file_invalid") from exc
    if (
        sum(item.size for item in files) > 1024 * 1024
        or sum(len(item.content) for item in files) > 800_000
    ):
        raise AgentContextError("agent_workspace_file_limit_reached")
    return files


def allowed_tools(
    run: AgentRun,
    *,
    workspace_files: list[AgentRuntimeWorkspaceFile],
) -> list[str]:
    policy = runtime_tool_policy(run)
    configured = {
        value
        for value in _snapshot_list(run, "allowed_tools")
        if isinstance(value, str) and value in AGENT_FIRST_PARTY_TOOLS
    }
    ordered: list[str] = []
    if AGENT_TOOL_CREATE_IMAGE in configured and policy.max_image_tool_calls > 0:
        ordered.append(AGENT_TOOL_CREATE_IMAGE)
    if AGENT_TOOL_WEB_SEARCH in configured and policy.max_web_search_calls > 0:
        ordered.append(AGENT_TOOL_WEB_SEARCH)
    if workspace_files and policy.max_file_tool_calls > 0:
        ordered.extend(
            tool
            for tool in (
                AGENT_TOOL_LIST_FILES,
                AGENT_TOOL_READ_FILE,
                AGENT_TOOL_SEARCH_FILES,
            )
            if tool in configured and tool in AGENT_FILE_TOOLS
        )
    return ordered


__all__ = ["allowed_tools", "runtime_tool_policy", "workspace_files"]
