"""Canonical Agent provider SDK base URLs and streamed probe payloads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit


AgentApi = Literal[
    "openai-responses",
    "openai-completions",
    "anthropic-messages",
]

AGENT_APIS = frozenset({"openai-responses", "openai-completions", "anthropic-messages"})
AGENT_PROBE_EXPECTED_TEXT = "9801"
AGENT_PROBE_MAX_RESPONSE_BYTES = 64 * 1024
_COMPLETE_ENDPOINT_SUFFIXES = (
    "/responses",
    "/chat/completions",
    "/v1/messages",
    "/models",
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class AgentEndpointContract:
    api: AgentApi
    sdk_base_url: str
    request_url: str
    models_url: str


@dataclass(frozen=True, slots=True)
class AgentProbeResult:
    text: str
    terminal: bool
    usage_present: bool
    stop_reason: str | None


@dataclass(slots=True)
class _AgentProbeState:
    text: list[str]
    terminal: bool = False
    usage_present: bool = False
    stop_reason: str | None = None


def _normalized_http_url(raw: str) -> tuple[SplitResult, str]:
    value = raw.strip().rstrip("/")
    if not value or _CONTROL_RE.search(value):
        raise ValueError("Agent base URL contains control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Agent base URL must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("Agent base URL must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Agent base URL must not include a query or fragment")
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    if any(lowered.endswith(suffix) for suffix in _COMPLETE_ENDPOINT_SUFFIXES):
        raise ValueError("Agent base URL must be an SDK base, not a request endpoint")
    normalized = urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, path, "", "")
    ).rstrip("/")
    return parsed, normalized


def effective_agent_base_url(
    base_url: str,
    api: str,
    *,
    agent_base_url: str | None = None,
) -> str:
    """Return the exact base handed to the selected provider SDK.

    Existing OpenAI providers historically treated ``base_url`` as a service
    root and probes appended ``/v1``. Preserve that behavior only when the
    Agent-specific base is absent. Anthropic's SDK appends ``/v1/messages``;
    an inherited base ending in ``/v1`` is ambiguous and must be made explicit.
    """

    if api not in AGENT_APIS:
        raise ValueError("Agent API is unsupported")
    explicit = bool(agent_base_url and agent_base_url.strip())
    _parsed, normalized = _normalized_http_url(agent_base_url if explicit else base_url)
    path = urlsplit(normalized).path.rstrip("/").lower()
    if api == "anthropic-messages":
        if path.endswith("/v1"):
            raise ValueError(
                "Anthropic Agent SDK base must not end in /v1; configure the service root"
            )
        return normalized
    if explicit or path.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def agent_endpoint_contract(
    base_url: str,
    api: str,
    *,
    agent_base_url: str | None = None,
) -> AgentEndpointContract:
    sdk_base = effective_agent_base_url(
        base_url,
        api,
        agent_base_url=agent_base_url,
    )
    if api == "openai-responses":
        request_suffix = "/responses"
        models_suffix = "/models"
    elif api == "openai-completions":
        request_suffix = "/chat/completions"
        models_suffix = "/models"
    else:
        request_suffix = "/v1/messages"
        models_suffix = "/v1/models"
    return AgentEndpointContract(
        api=cast(AgentApi, api),
        sdk_base_url=sdk_base,
        request_url=f"{sdk_base}{request_suffix}",
        models_url=f"{sdk_base}{models_suffix}",
    )


def agent_probe_headers(api: str, api_key: str) -> dict[str, str]:
    if api == "anthropic-messages":
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    if api not in AGENT_APIS:
        raise ValueError("Agent API is unsupported")
    return {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }


def build_agent_probe_request(api: str, model: str) -> dict[str, Any]:
    if not model.strip():
        raise ValueError("Agent probe model is required")
    system = "Return only the final integer. No words or punctuation."
    prompt = "Calculate 99 * 99."
    if api == "openai-responses":
        return {
            "model": model,
            "instructions": system,
            "input": prompt,
            "stream": True,
            "store": False,
        }
    if api == "openai-completions":
        # Keep the canary portable across legacy Chat Completions and
        # reasoning/o-series compatibility profiles. Production compatibility
        # tests cover developer-role and token-field details separately.
        return {
            "model": model,
            "messages": [{"role": "user", "content": f"{system}\n\n{prompt}"}],
            "stream": True,
        }
    if api == "anthropic-messages":
        return {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "max_tokens": 16,
        }
    raise ValueError("Agent API is unsupported")


def _sse_data(raw: str) -> list[str]:
    return [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]


def _parse_responses_probe_event(
    payload: dict[str, Any],
    state: _AgentProbeState,
) -> None:
    event_type = payload.get("type")
    if event_type == "response.output_text.delta" and isinstance(
        payload.get("delta"), str
    ):
        state.text.append(payload["delta"])
    if event_type == "response.completed":
        state.terminal = True
        response = payload.get("response")
        if isinstance(response, dict):
            state.usage_present = isinstance(response.get("usage"), dict)
            status = response.get("status")
            state.stop_reason = status if isinstance(status, str) else "completed"
    if event_type in {"response.failed", "response.incomplete"}:
        state.terminal = True
        state.stop_reason = str(event_type)


def _parse_completions_probe_event(
    payload: dict[str, Any],
    state: _AgentProbeState,
) -> None:
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                state.text.append(delta["content"])
            finish = choice.get("finish_reason")
            if isinstance(finish, str):
                state.terminal = True
                state.stop_reason = finish
    state.usage_present = state.usage_present or isinstance(payload.get("usage"), dict)


def _parse_anthropic_probe_event(
    payload: dict[str, Any],
    state: _AgentProbeState,
) -> None:
    event_type = payload.get("type")
    if event_type == "message_start":
        message = payload.get("message")
        state.usage_present = state.usage_present or (
            isinstance(message, dict) and isinstance(message.get("usage"), dict)
        )
    delta = payload.get("delta")
    if event_type == "content_block_delta" and isinstance(delta, dict):
        value = delta.get("text")
        if isinstance(value, str):
            state.text.append(value)
    if event_type == "message_delta" and isinstance(delta, dict):
        stop = delta.get("stop_reason")
        if isinstance(stop, str):
            state.stop_reason = stop
        state.usage_present = state.usage_present or isinstance(
            payload.get("usage"), dict
        )
    if event_type == "message_stop":
        state.terminal = True


def parse_agent_probe_sse(api: str, raw: str) -> AgentProbeResult:
    """Parse a complete bounded SSE response and require adapter terminal proof."""

    parsers = {
        "openai-responses": _parse_responses_probe_event,
        "openai-completions": _parse_completions_probe_event,
        "anthropic-messages": _parse_anthropic_probe_event,
    }
    parser = parsers.get(api)
    if parser is None:
        raise ValueError("Agent API is unsupported")
    state = _AgentProbeState(text=[])
    saw_done = False
    for item in _sse_data(raw):
        if item == "[DONE]":
            saw_done = True
            continue
        try:
            payload = json.loads(item)
        except json.JSONDecodeError as exc:
            raise ValueError("Agent probe SSE contains invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Agent probe SSE event must be an object")
        parser(payload, state)
    terminal = state.terminal
    if api == "openai-completions":
        terminal = terminal and saw_done
    return AgentProbeResult(
        text="".join(state.text),
        terminal=terminal,
        usage_present=state.usage_present,
        stop_reason=state.stop_reason,
    )


__all__ = [
    "AGENT_APIS",
    "AGENT_PROBE_EXPECTED_TEXT",
    "AGENT_PROBE_MAX_RESPONSE_BYTES",
    "AgentApi",
    "AgentEndpointContract",
    "AgentProbeResult",
    "agent_endpoint_contract",
    "agent_probe_headers",
    "build_agent_probe_request",
    "effective_agent_base_url",
    "parse_agent_probe_sse",
]
