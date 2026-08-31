"""Shared conservative Agent Runtime request-byte planning."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass


DEFAULT_AGENT_RUNTIME_MAX_REQUEST_BYTES = 16 * 1024 * 1024
AGENT_RUNTIME_REQUEST_SAFETY_MARGIN_BYTES = 128 * 1024
AGENT_RUNTIME_CREDENTIAL_HEADROOM_BYTES = 48 * 1024
# Worker accepts previews up to 512 KiB; API admission must budget that maximum
# because the API does not know the Worker's runtime override at submission time.
AGENT_RUNTIME_REFERENCE_PREVIEW_MAX_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class AgentWireBudget:
    estimated_bytes: int
    maximum_bytes: int
    admitted: bool


def encoded_json_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def estimate_agent_runtime_request_bytes(
    *,
    system_prompt: str,
    current_prompt: str,
    history_texts: Iterable[str],
    history_structured_bytes: int,
    current_reference_count: int,
    historical_reference_count: int,
    workspace_files_bytes: int = 0,
    maximum_bytes: int = DEFAULT_AGENT_RUNTIME_MAX_REQUEST_BYTES,
    preview_max_bytes: int = AGENT_RUNTIME_REFERENCE_PREVIEW_MAX_BYTES,
) -> AgentWireBudget:
    text_bytes = encoded_json_bytes(
        {
            "system_prompt": system_prompt,
            "current_prompt": current_prompt,
            "history": list(history_texts),
        }
    )
    base64_preview_bytes = ((max(0, preview_max_bytes) + 2) // 3) * 4
    reference_count = max(0, current_reference_count) + max(
        0, historical_reference_count
    )
    references = reference_count * (base64_preview_bytes + 1024)
    estimate = (
        text_bytes
        + max(0, int(history_structured_bytes))
        + references
        + max(0, int(workspace_files_bytes))
        + AGENT_RUNTIME_CREDENTIAL_HEADROOM_BYTES
        + AGENT_RUNTIME_REQUEST_SAFETY_MARGIN_BYTES
    )
    maximum = max(1, int(maximum_bytes))
    return AgentWireBudget(estimate, maximum, estimate <= maximum)


__all__ = [
    "AGENT_RUNTIME_CREDENTIAL_HEADROOM_BYTES",
    "AGENT_RUNTIME_REFERENCE_PREVIEW_MAX_BYTES",
    "AGENT_RUNTIME_REQUEST_SAFETY_MARGIN_BYTES",
    "DEFAULT_AGENT_RUNTIME_MAX_REQUEST_BYTES",
    "AgentWireBudget",
    "encoded_json_bytes",
    "estimate_agent_runtime_request_bytes",
]
