"""Safe Agent public projections."""

from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lumen_core.agent_dispatch import provider_dispatch_evidence_count
from lumen_core.agent_events import (
    AGENT_TOOL_CREATE_IMAGE,
    AGENT_TOOL_LIST_FILES,
    AGENT_TOOL_READ_FILE,
    AGENT_TOOL_SEARCH_FILES,
    AGENT_TOOL_WEB_SEARCH,
)
from lumen_core.agent_errors import (
    agent_error_allows_continuation,
    public_agent_error_code,
    public_agent_error_message,
)
from lumen_core.model_entities import (
    AgentRun,
    AgentRunReference,
    AgentSession,
    AgentToolCall,
    Conversation,
)
from lumen_core.schema_models import (
    AgentImageDefaultsIn,
    AgentReferenceOut,
    AgentRunOut,
    AgentSessionOut,
    AgentToolCallOut,
)


def generation_ids_from_tool(tool_call: AgentToolCall) -> list[str]:
    result = tool_call.result_jsonb if isinstance(tool_call.result_jsonb, dict) else {}
    values = result.get("generation_ids")
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value][:4]


def agent_reference_out(reference: AgentRunReference) -> AgentReferenceOut:
    return AgentReferenceOut(
        id=reference.id,
        image_id=reference.image_id,
        ordinal=reference.ordinal,
        reference_label=reference.reference_label,
        role=reference.role,
        display_label=reference.display_label,
    )


_CONTROL_CHARACTERS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
_MARKUP = re.compile(r"<[^>]{0,512}>")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\n]{0,64}PRIVATE KEY-----.*?"
    r"-----END [^-\n]{0,64}PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_URL = re.compile(r"\b[a-z][a-z0-9+.-]{1,24}://[^\s<>\"']+", re.IGNORECASE)
_AUTH_HEADER = re.compile(
    r"\b(authorization|proxy-authorization|x-api-key|x-auth-token|"
    r"cookie|set-cookie)\b(\s*:\s*)[^\r\n]*",
    re.IGNORECASE,
)
_AUTH_SCHEME_TOKEN = re.compile(
    r"\b(Bearer|Basic)\s+[^\s,;]+",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b((?:[A-Z][A-Z0-9]*[_-])*(?:api[_-]?key|access[_-]?key|"
    r"secret[_-]?access[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"authorization|password|passwd|client[_-]?secret|private[_-]?key|"
    r"credential|credentials|secret|token|cookie|database[_-]?url|db[_-]?url))"
    r"\b(\s*[:=]\s*)"
    r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)
_KNOWN_SECRET_TOKEN = re.compile(
    r"\b(?:sk|rk|pk|ghp|gho|ghu|ghs|github_pat)[-_][A-Za-z0-9_-]{12,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}\b"
    r"|\bglpat-[A-Za-z0-9_-]{12,}\b"
    r"|\bnpm_[A-Za-z0-9]{20,}\b"
    r"|\bpypi-[A-Za-z0-9_-]{20,}\b"
    r"|\bya29\.[A-Za-z0-9_-]{12,}\b"
    r"|\bAKIA[A-Z0-9]{16}\b"
    r"|\bAIza[A-Za-z0-9_-]{35}\b"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    re.IGNORECASE,
)
_SENSITIVE_STRUCTURED_KEY = re.compile(
    r"(?:^|[_-])(?:authorization|cookie|credential|credentials|password|passwd|"
    r"secret|token|api[_-]?key|access[_-]?key|private[_-]?key|database[_-]?url|"
    r"db[_-]?url)(?:$|[_-])",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:^|[_-])(?:auth|authorization|code|cookie|credential|key|password|"
    r"secret|signature|token)(?:$|[_-])",
    re.IGNORECASE,
)


def _sensitive_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_-")
    return bool(_SENSITIVE_STRUCTURED_KEY.search(normalized))


def _scrub_non_url_text(value: str) -> str:
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
    text = _AUTH_HEADER.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    text = _AUTH_SCHEME_TOKEN.sub(
        lambda match: f"{match.group(1)} [REDACTED]",
        text,
    )
    text = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    return _KNOWN_SECRET_TOKEN.sub("[REDACTED]", text)


def _scrub_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;)]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        if not hostname:
            return "[REDACTED URL]" + trailing
        host = f"[{hostname}]" if ":" in hostname else hostname
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            return "[REDACTED URL]" + trailing
        netloc = f"{host}{port}"
        if parsed.username is not None or parsed.password is not None:
            netloc = f"[REDACTED]@{netloc}"
        query = urlencode(
            [
                (
                    query_key,
                    "[REDACTED]"
                    if _SENSITIVE_QUERY_KEY.search(query_key)
                    else _scrub_non_url_text(query_value),
                )
                for query_key, query_value in parse_qsl(
                    parsed.query,
                    keep_blank_values=True,
                    max_num_fields=128,
                )
            ],
            doseq=True,
        )
        fragment = _scrub_non_url_text(parsed.fragment)
        return (
            urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment)) + trailing
        )
    except (TypeError, UnicodeError, ValueError):
        return "[REDACTED URL]" + trailing


def _scrub_public_string(value: str, *, strip_markup: bool = False) -> str:
    text = html.unescape(_MARKUP.sub(" ", value) if strip_markup else value)
    text = _CONTROL_CHARACTERS.sub(" ", text)
    text = _URL.sub(_scrub_url, text)
    return _scrub_non_url_text(text)


def _scrub_public_value(
    value: Any,
    *,
    key: str | None = None,
    depth: int = 0,
) -> Any:
    if key is not None and _sensitive_key(key):
        return "[REDACTED]"
    if depth > 8:
        return None
    if isinstance(value, str):
        return _scrub_public_string(value)
    if isinstance(value, dict):
        return {
            str(item_key): _scrub_public_value(
                item_value,
                key=str(item_key),
                depth=depth + 1,
            )
            for item_key, item_value in list(value.items())[:128]
        }
    if isinstance(value, list):
        return [_scrub_public_value(item, depth=depth + 1) for item in value[:128]]
    return value


def _public_text(
    value: Any,
    *,
    maximum: int,
    strip_markup: bool = False,
) -> str | None:
    if not isinstance(value, str):
        return None
    text = _scrub_public_string(value, strip_markup=strip_markup)
    normalized = "\n".join(
        line for raw_line in text.splitlines() if (line := " ".join(raw_line.split()))
    ).strip()
    return normalized[:maximum] or None


def _public_file_name(value: Any) -> str | None:
    normalized = _public_text(value, maximum=512)
    if not normalized:
        return None
    leaf = re.split(r"[/\\\\]", normalized)[-1].strip()
    if leaf in {"", ".", ".."}:
        return None
    return leaf[:128]


def _public_integer(
    value: Any,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _tool_result_payload(tool_call: AgentToolCall) -> dict[str, Any] | list[Any] | None:
    result = tool_call.result_jsonb if isinstance(tool_call.result_jsonb, dict) else {}
    history_text = result.get("history_text")
    if not isinstance(history_text, str):
        return None
    try:
        payload = json.loads(history_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    scrubbed = _scrub_public_value(payload)
    return scrubbed if isinstance(scrubbed, (dict, list)) else None


def _tool_arguments(tool_call: AgentToolCall) -> dict[str, Any]:
    arguments = (
        tool_call.arguments_jsonb if isinstance(tool_call.arguments_jsonb, dict) else {}
    )
    scrubbed = _scrub_public_value(arguments)
    return scrubbed if isinstance(scrubbed, dict) else {}


def _append_snippet(
    snippets: list[str],
    value: Any,
    *,
    strip_markup: bool = False,
) -> None:
    if len(snippets) >= 6:
        return
    snippet = _public_text(value, maximum=600, strip_markup=strip_markup)
    if snippet and snippet not in snippets:
        snippets.append(snippet)


def _web_search_details(tool_call: AgentToolCall) -> dict[str, Any]:
    arguments = _tool_arguments(tool_call)
    payload = _tool_result_payload(tool_call)
    result = payload if isinstance(payload, dict) else {}
    query = _public_text(arguments.get("query"), maximum=2_000) or _public_text(
        result.get("query"), maximum=2_000
    )
    snippets: list[str] = []
    _append_snippet(snippets, result.get("answer"), strip_markup=True)
    sources = result.get("sources")
    if isinstance(sources, list):
        for raw_source in sources[:6]:
            if not isinstance(raw_source, dict):
                continue
            title = _public_text(
                raw_source.get("title"), maximum=180, strip_markup=True
            )
            snippet = _public_text(
                raw_source.get("snippet"), maximum=480, strip_markup=True
            )
            _append_snippet(
                snippets,
                " - ".join(part for part in (title, snippet) if part),
            )
    return {
        "kind": "web_search",
        "query": query,
        "result_snippets": snippets,
    }


def _file_names(*values: Any) -> list[str]:
    names: list[str] = []
    for value in values:
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            name = _public_file_name(candidate)
            if name and name not in names:
                names.append(name)
            if len(names) >= 8:
                return names
    return names


def _file_tool_details(tool_call: AgentToolCall) -> dict[str, Any]:
    arguments = _tool_arguments(tool_call)
    payload = _tool_result_payload(tool_call)
    result = payload if isinstance(payload, dict) else {}
    kind = {
        AGENT_TOOL_LIST_FILES: "file_list",
        AGENT_TOOL_READ_FILE: "file_read",
        AGENT_TOOL_SEARCH_FILES: "file_search",
    }[tool_call.name]
    result_file_names: list[Any] = []
    if kind == "file_list" and isinstance(payload, list):
        result_file_names = [
            item.get("name") for item in payload[:8] if isinstance(item, dict)
        ]
    searched_files = result.get("searched_files")
    matches = result.get("matches")
    match_rows = matches if isinstance(matches, list) else []
    match_file_names = [
        item.get("name") for item in match_rows[:6] if isinstance(item, dict)
    ]
    names = _file_names(
        arguments.get("name"),
        result.get("name"),
        result_file_names,
        searched_files,
        match_file_names,
    )
    query = _public_text(arguments.get("query"), maximum=256) or _public_text(
        result.get("query"), maximum=256
    )
    line_start = _public_integer(
        result.get("line_start", arguments.get("line_start")), minimum=1
    )
    line_end = _public_integer(result.get("line_end"), minimum=1)
    snippets: list[str] = []
    if kind == "file_read":
        content = result.get("content")
        if isinstance(content, str):
            for line in content.splitlines()[:6]:
                _append_snippet(snippets, line)
    elif kind == "file_search":
        for item in match_rows[:6]:
            if not isinstance(item, dict):
                continue
            name = _public_file_name(item.get("name"))
            line = _public_integer(item.get("line"), minimum=1)
            text = _public_text(item.get("text"), maximum=480)
            location = f"{name}:{line}" if name and line else name
            _append_snippet(
                snippets,
                " - ".join(part for part in (location, text) if part),
            )
    return {
        "kind": kind,
        "file_names": names,
        "query": query,
        "line_start": line_start,
        "line_end": line_end,
        "result_snippets": snippets,
    }


def _image_tool_details(tool_call: AgentToolCall) -> dict[str, Any]:
    arguments = _tool_arguments(tool_call)
    references = arguments.get("reference_labels")
    reference_count = (
        min(
            16,
            len([item for item in references if isinstance(item, str)]),
        )
        if isinstance(references, list)
        else 0
    )
    return {
        "kind": "image",
        "prompt": _public_text(arguments.get("prompt"), maximum=4_000),
        "reference_count": reference_count,
        "count": _public_integer(arguments.get("count"), minimum=1, maximum=4),
        "aspect_ratio": (
            arguments.get("aspect_ratio")
            if arguments.get("aspect_ratio")
            in {
                "1:1",
                "16:9",
                "9:16",
                "21:9",
                "9:21",
                "10:7",
                "7:10",
                "4:5",
                "3:4",
                "4:3",
                "3:2",
                "2:3",
            }
            else None
        ),
        "quality": (
            arguments.get("quality")
            if arguments.get("quality") in {"1k", "2k", "4k"}
            else None
        ),
        "render_quality": (
            arguments.get("render_quality")
            if arguments.get("render_quality") in {"auto", "low", "medium", "high"}
            else None
        ),
        "background": (
            arguments.get("background")
            if arguments.get("background") in {"auto", "opaque", "transparent"}
            else None
        ),
        "output_format": (
            arguments.get("output_format")
            if arguments.get("output_format") in {"png", "jpeg", "webp"}
            else None
        ),
    }


def _tool_details(tool_call: AgentToolCall) -> dict[str, Any] | None:
    if tool_call.name == AGENT_TOOL_WEB_SEARCH:
        return _web_search_details(tool_call)
    if tool_call.name in {
        AGENT_TOOL_LIST_FILES,
        AGENT_TOOL_READ_FILE,
        AGENT_TOOL_SEARCH_FILES,
    }:
        return _file_tool_details(tool_call)
    if tool_call.name == AGENT_TOOL_CREATE_IMAGE:
        return _image_tool_details(tool_call)
    return None


def _tool_duration_ms(tool_call: AgentToolCall) -> int | None:
    if tool_call.started_at is None or tool_call.finished_at is None:
        return None
    try:
        duration = tool_call.finished_at - tool_call.started_at
    except TypeError:
        return None
    return max(0, int(duration.total_seconds() * 1_000))


def agent_tool_call_out(tool_call: AgentToolCall) -> AgentToolCallOut:
    generation_ids = generation_ids_from_tool(tool_call)
    return AgentToolCallOut(
        id=tool_call.id,
        agent_run_id=tool_call.agent_run_id,
        ordinal=tool_call.ordinal,
        name=tool_call.name,
        mode=tool_call.mode,
        status=tool_call.status,
        generation_ids=generation_ids,
        generation_count=len(generation_ids),
        details=_tool_details(tool_call),
        duration_ms=_tool_duration_ms(tool_call),
        error_code=tool_call.error_code,
        started_at=tool_call.started_at,
        finished_at=tool_call.finished_at,
        created_at=tool_call.created_at,
        updated_at=tool_call.updated_at,
    )


def agent_run_out(
    run: AgentRun,
    *,
    references: list[AgentRunReference] | None = None,
    tool_calls: list[AgentToolCall] | None = None,
    is_latest: bool = False,
) -> AgentRunOut:
    usage = run.usage_jsonb if isinstance(run.usage_jsonb, dict) else {}
    dispatch = run.dispatch_jsonb if isinstance(run.dispatch_jsonb, dict) else {}
    memory_state = dispatch.get("memory_state")
    if memory_state not in {"disabled", "empty", "ready", "degraded"}:
        memory_state = None
    unresolved_tool = any(
        tool.status in {"queued", "running", "timed_out"}
        or tool.error_code == "agent_tool_result_unknown"
        for tool in tool_calls or []
    )
    billing = run.billing_jsonb if isinstance(run.billing_jsonb, dict) else {}
    provider_evidence_safe = provider_dispatch_evidence_count(
        dispatch
    ) == 0 or billing.get("knowledge") in {"actual", "proven_absent"}
    transcript = run.transcript_jsonb if isinstance(run.transcript_jsonb, dict) else {}
    transcript_coherent = transcript.get("projection") != "ordered_blocks" or (
        transcript.get("output_revision") == int(run.output_revision or 0)
        and transcript.get("output_runtime_seq") == int(run.output_runtime_seq or 0)
    )
    continuable = (
        is_latest
        and run.status == "partial"
        and agent_error_allows_continuation(run.error_code)
        and not unresolved_tool
        and provider_evidence_safe
        and transcript_coherent
    )
    public_error = public_agent_error_code(run.error_code)
    return AgentRunOut(
        id=run.id,
        agent_session_id=run.agent_session_id,
        user_message_id=run.user_message_id,
        assistant_message_id=run.assistant_message_id,
        status=run.status,
        execution_epoch=run.execution_epoch,
        last_event_seq=run.last_event_seq,
        output_revision=int(run.output_revision or 0),
        output_runtime_seq=int(run.output_runtime_seq or 0),
        idempotency_key=run.idempotency_key,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        memory_state=memory_state,
        continuable=continuable,
        turn_count=run.turn_count,
        tool_call_count=run.tool_call_count,
        usage=usage,
        error_code=public_error,
        error_message=public_agent_error_message(run.error_code),
        started_at=run.started_at,
        finished_at=run.finished_at,
        cancel_requested_at=run.cancel_requested_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
        references=[agent_reference_out(item) for item in references or []],
        tool_calls=[agent_tool_call_out(item) for item in tool_calls or []],
    )


def conversation_agent_defaults(
    conversation: Conversation,
) -> tuple[AgentImageDefaultsIn, bool, bool, bool]:
    params = (
        conversation.default_params
        if isinstance(conversation.default_params, dict)
        else {}
    )
    raw_agent = params.get("agent")
    if not isinstance(raw_agent, dict):
        return AgentImageDefaultsIn(), True, False, True
    try:
        defaults = AgentImageDefaultsIn.model_validate(
            raw_agent.get("image_defaults", {})
        )
    except (TypeError, ValueError):
        defaults = AgentImageDefaultsIn()
    allow_image = raw_agent.get("allow_image")
    allow_web_search = raw_agent.get("allow_web_search")
    allow_file_tools = raw_agent.get("allow_file_tools")
    return (
        defaults,
        allow_image if isinstance(allow_image, bool) else True,
        allow_web_search if isinstance(allow_web_search, bool) else False,
        allow_file_tools if isinstance(allow_file_tools, bool) else True,
    )


def agent_session_out(
    session: AgentSession,
    conversation: Conversation,
    *,
    active_run: AgentRunOut | None = None,
) -> AgentSessionOut:
    (
        image_defaults,
        allow_image,
        allow_web_search,
        allow_file_tools,
    ) = conversation_agent_defaults(conversation)
    return AgentSessionOut(
        id=session.id,
        conversation_id=conversation.id,
        title=conversation.title,
        pinned=conversation.pinned,
        archived=conversation.archived,
        memory_disabled=conversation.memory_disabled,
        active_scope_id=conversation.active_scope_id,
        default_system=conversation.default_system,
        default_system_prompt_id=conversation.default_system_prompt_id,
        image_defaults=image_defaults,
        allow_image=allow_image,
        allow_web_search=allow_web_search,
        allow_file_tools=allow_file_tools,
        runtime_version=session.runtime_version,
        last_activity_at=conversation.last_activity_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
        active_run=active_run,
    )


def agent_default_params(
    *,
    image_defaults: AgentImageDefaultsIn,
    allow_image: bool,
    allow_web_search: bool,
    allow_file_tools: bool,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = dict(existing or {})
    params["agent"] = {
        "image_defaults": image_defaults.model_dump(mode="json"),
        "allow_image": allow_image,
        "allow_web_search": allow_web_search,
        "allow_file_tools": allow_file_tools,
    }
    return params


__all__ = [
    "agent_default_params",
    "agent_reference_out",
    "agent_run_out",
    "agent_session_out",
    "agent_tool_call_out",
    "conversation_agent_defaults",
    "generation_ids_from_tool",
    "public_agent_error_message",
]
