"""Shared field-to-unit mapping for admin pricing bulk updates."""

from __future__ import annotations


BULK_RATE_UNITS: dict[str, str] = {
    "input": "per_1k_tokens_in",
    "output": "per_1k_tokens_out",
    "cache_read": "per_1k_tokens_cache_read",
    "cache_creation": "per_1k_tokens_cache_creation",
    "cache_creation_5m": "per_1k_tokens_cache_creation_5m",
    "cache_creation_1h": "per_1k_tokens_cache_creation_1h",
    "image_output": "per_1k_tokens_image_output",
    "reasoning": "per_1k_tokens_reasoning",
    "input_priority": "per_1k_tokens_input_priority",
    "output_priority": "per_1k_tokens_output_priority",
    "cache_read_priority": "per_1k_tokens_cache_read_priority",
}


__all__ = ["BULK_RATE_UNITS"]
