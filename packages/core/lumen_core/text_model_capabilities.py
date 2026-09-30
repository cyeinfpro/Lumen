"""Model-specific wire compatibility, independent of provider availability.

Contract: https://developers.openai.com/api/docs/guides/latest-model
Only documented IDs and dated snapshots match; gateway prefixes are preserved
on the wire. These helpers never enable a provider or change the default model.
"""

from __future__ import annotations

import re
from typing import Any

from .agent_model_profiles import canonical_model_id

_GPT6_MODEL = re.compile(r"gpt-6-(astra|sol|luna)(?:-\d{4}-\d{2}-\d{2})?\Z")


def gpt6_model_family(model_id: str | None) -> str | None:
    match = _GPT6_MODEL.fullmatch(canonical_model_id(model_id or ""))
    return match.group(1) if match else None


def normalize_model_reasoning(
    model_id: str | None, effort: str | None
) -> str | None:
    """Preserve Auto (omission) and migrate legacy explicit effort settings."""
    family = gpt6_model_family(model_id)
    if family is None or effort is None:
        return effort
    if effort == "minimal" or (family == "astra" and effort in {"none", "off"}):
        return "low"
    return "none" if effort == "off" else effort


def agent_model_api_supported(model_id: str, api: str) -> bool:
    # Lumen's Agent may call tools at any effort, including Auto. GPT-6 Sol/Luna
    # completions tools are only supported at none; Astra tools require Responses.
    # Do not silently rewrite an operator's endpoint or send a billable bad run.
    return gpt6_model_family(model_id) is None or api == "openai-responses"


def normalize_responses_model_body(body: dict[str, Any]) -> dict[str, Any]:
    """Return a non-mutating GPT-6 Responses payload compatibility projection."""
    model = body.get("model")
    if not isinstance(model, str) or gpt6_model_family(model) is None:
        return body
    result = dict(body)
    reasoning = body.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    effective = normalize_model_reasoning(model, effort)
    if isinstance(reasoning, dict) and effective != effort:
        result["reasoning"] = {**reasoning, "effort": effective}
    # Omission means the model's reasoning default, not reasoning disabled.
    if effective != "none":
        for key in ("temperature", "top_p", "top_logprobs", "logprobs"):
            result.pop(key, None)
        include = result.get("include")
        if isinstance(include, list):
            result["include"] = [
                value for value in include if value != "message.output_text.logprobs"
            ]
    return result
