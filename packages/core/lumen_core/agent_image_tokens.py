"""Conservative, versioned Agent image-input token estimates."""

from __future__ import annotations

import math
from dataclasses import dataclass


AGENT_IMAGE_TOKEN_POLICY_VERSION = "agent-image-v1"


@dataclass(frozen=True, slots=True)
class AgentImageTokenEstimate:
    upper: int
    policy_version: str = AGENT_IMAGE_TOKEN_POLICY_VERSION


def agent_preview_dimensions(
    width: int,
    height: int,
    *,
    maximum_side: int = 1024,
) -> tuple[int, int]:
    bounded_width = max(1, int(width))
    bounded_height = max(1, int(height))
    scale = min(1.0, maximum_side / max(bounded_width, bounded_height))
    return (
        max(1, math.ceil(bounded_width * scale)),
        max(1, math.ceil(bounded_height * scale)),
    )


def estimate_agent_image_tokens(
    provider_api: str,
    width: int,
    height: int,
) -> AgentImageTokenEstimate:
    """Estimate transformed <=1024px previews without claiming exact billing."""
    bounded_width = max(1, min(8192, int(width)))
    bounded_height = max(1, min(8192, int(height)))
    pixels = bounded_width * bounded_height
    anthropic_upper = math.ceil(pixels / 750) + 256
    openai_tiles = math.ceil(bounded_width / 512) * math.ceil(bounded_height / 512)
    openai_upper = 85 + 170 * openai_tiles + 256
    if provider_api == "anthropic-messages":
        upper = anthropic_upper
    elif provider_api in {"openai-responses", "openai-completions"}:
        upper = openai_upper
    else:
        upper = max(anthropic_upper, openai_upper, 2048)
    return AgentImageTokenEstimate(upper=max(1, min(1_000_000, upper)))


__all__ = [
    "AGENT_IMAGE_TOKEN_POLICY_VERSION",
    "AgentImageTokenEstimate",
    "agent_preview_dimensions",
    "estimate_agent_image_tokens",
]
