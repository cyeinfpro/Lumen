"""Explicit Images API SVG author selection; no inferred model or paid fallback.

Contract: https://developers.openai.com/api/reference/resources/images/methods/generate
The image model renders a reference; svg.model independently authors the SVG.
Omitting svg retains compatibility with providers without model selection.
"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

SvgReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class SvgReasoningOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effort: SvgReasoningEffort | None = None


class SvgOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model: str = Field(min_length=1)
    reasoning: SvgReasoningOptions | None = None

    @model_validator(mode="after")
    def omit_default_reasoning(self) -> "SvgOptions":
        if self.reasoning is not None and self.reasoning.effort is None:
            self.reasoning = None
        return self


def normalize_svg_options(value: object, *, output_format: str | None) -> dict | None:
    """Validate untrusted persisted input and serialize only explicit options."""
    if value is None:
        return None
    if output_format != "svg":
        raise ValueError("SVG author options require SVG output")
    return SvgOptions.model_validate(value).model_dump(exclude_none=True)


def public_svg_options(value: object, *, output_format: str | None) -> dict | None:
    """Expose only validated author fields, never arbitrary upstream metadata."""
    try:
        return normalize_svg_options(value, output_format=output_format)
    except ValueError:
        return None


def svg_author_recommendation(base_url: object) -> dict | None:
    """Dated provider-specific evidence, never a default or access guarantee."""
    from urllib.parse import urlsplit
    if not isinstance(base_url, str):
        return None
    try:
        url = urlsplit(base_url.strip())
        matches = (
            url.scheme == "https" and url.hostname == "flux.infpro.me"
            and url.port in {None, 443} and url.path.rstrip("/") == "/v1"
            and not url.username and not url.password and not url.query and not url.fragment
        )
    except ValueError:
        return None
    if not matches:
        return None
    return {"model": "gpt-6.1-sol", "tested_at": "2026-11-05",
            "provider_base_url": "https://flux.infpro.me/v1",
            "access_guaranteed": False, "selection_required": True}


def public_svg_usage(value: object) -> dict | None:
    """Retain separate author usage, not a fabricated image-model token charge."""
    if not isinstance(value, dict):
        return None
    result = {}
    model = value.get("model")
    if isinstance(model, str) and 0 < len(model) <= 200 and all(32 <= ord(c) < 127 for c in model):
        result["model"] = model
    for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
        count = value.get(key)
        if type(count) is int and 0 <= count <= 2**63 - 1:
            result[key] = count
    for key, child in (("input_tokens_details", "cached_tokens"),
                       ("output_tokens_details", "reasoning_tokens")):
        detail = value.get(key)
        count = detail.get(child) if isinstance(detail, dict) else None
        if type(count) is int and 0 <= count <= 2**63 - 1:
            result[key] = {child: count}
    return result or None
