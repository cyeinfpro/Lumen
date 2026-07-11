from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def parse_response_dict(payload: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return "", {}
    raw_usage = payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip(), usage
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("output_text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks).strip(), usage


def summary_response_body(
    input_text: str,
    *,
    target_tokens: int,
    model: str,
    instructions: str,
    reasoning_effort: str,
) -> dict[str, Any]:
    _ = target_tokens
    return {
        "model": model,
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": input_text}],
            }
        ],
        "stream": False,
        "store": False,
        "reasoning": {"effort": reasoning_effort},
    }


def compose_summary_input(
    previous_summary: str | None,
    lines: Sequence[str],
) -> str:
    parts: list[str] = []
    if previous_summary and previous_summary.strip():
        parts.append("[PREVIOUS_ROLLING_SUMMARY]\n" + previous_summary.strip())
    parts.append("[MESSAGES_TO_COMPRESS]\n" + "\n\n".join(lines))
    return "\n\n".join(parts)


def summary_provider_kwargs(provider: Any, timeout_s: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "route": "text",
        "api_key_override": provider.api_key,
        "base_url_override": provider.base_url,
        "timeout_s": timeout_s,
        "endpoint_label": "responses_summary",
    }
    proxy = getattr(provider, "proxy", None)
    if proxy is not None:
        kwargs["proxy_override"] = proxy
    return kwargs
