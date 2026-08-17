"""Built-in OpenAI standard chat-model pricing defaults.

The admin pricing importer accepts USD per million tokens and converts it to
the configured RMB rate.  Keeping the source rows here gives the API, CLI, and
database migration one deterministic default catalog.
"""

from __future__ import annotations

from typing import Final

OPENAI_DEFAULT_CHAT_MODEL: Final[str] = "gpt-5.6-sol"

# Values are the current OpenAI standard-tier USD prices per 1M tokens.  The
# model IDs intentionally omit presentation-only context-length suffixes.
OPENAI_STANDARD_CHAT_PRICES: Final[
    tuple[tuple[str, str, str], ...]
] = (
    ("gpt-5.6-sol", "5.00", "30.00"),
    ("gpt-5.6-terra", "2.00", "12.00"),
    ("gpt-5.6-luna", "0.20", "1.20"),
    ("gpt-5.6", "5.00", "30.00"),
    ("gpt-5.5", "5.00", "30.00"),
    ("gpt-5.5-pro", "30.00", "180.00"),
    ("gpt-5.4", "2.50", "15.00"),
    ("gpt-5.4-mini", "0.75", "4.50"),
    ("gpt-5.4-nano", "0.20", "1.25"),
    ("gpt-5.4-pro", "30.00", "180.00"),
    ("gpt-5.2", "1.75", "14.00"),
    ("gpt-5.2-pro", "21.00", "168.00"),
    ("gpt-5.1", "1.25", "10.00"),
    ("gpt-5", "1.25", "10.00"),
    ("gpt-5-mini", "0.25", "2.00"),
    ("gpt-5-nano", "0.05", "0.40"),
    ("gpt-5-pro", "15.00", "120.00"),
    ("gpt-4.1", "2.00", "8.00"),
    ("gpt-4.1-mini", "0.40", "1.60"),
    ("gpt-4.1-nano", "0.10", "0.40"),
    ("gpt-4o", "2.50", "10.00"),
    ("gpt-4o-2024-05-13", "5.00", "15.00"),
    ("gpt-4o-mini", "0.15", "0.60"),
    ("o1", "15.00", "60.00"),
    ("o1-pro", "150.00", "600.00"),
    ("o3-pro", "20.00", "80.00"),
    ("o3", "2.00", "8.00"),
    ("o4-mini", "1.10", "4.40"),
    ("o3-mini", "1.10", "4.40"),
    ("gpt-4-turbo-2024-04-09", "10.00", "30.00"),
    ("gpt-4-0613", "30.00", "60.00"),
    ("gpt-3.5-turbo", "0.50", "1.50"),
    ("gpt-3.5-turbo-0125", "0.50", "1.50"),
    ("gpt-3.5-turbo-1106", "1.00", "2.00"),
    ("gpt-3.5-turbo-instruct", "1.50", "2.00"),
    ("davinci-002", "2.00", "2.00"),
    ("babbage-002", "0.40", "0.40"),
)


def openai_standard_price_rows() -> list[dict[str, str]]:
    """Return importer-compatible rows without exposing mutable source state."""
    return [
        {
            "model": model,
            "input_usd_per_1m": input_price,
            "output_usd_per_1m": output_price,
        }
        for model, input_price, output_price in OPENAI_STANDARD_CHAT_PRICES
    ]


__all__ = [
    "OPENAI_DEFAULT_CHAT_MODEL",
    "OPENAI_STANDARD_CHAT_PRICES",
    "openai_standard_price_rows",
]
