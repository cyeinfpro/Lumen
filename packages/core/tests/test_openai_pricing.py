from __future__ import annotations

from lumen_core.openai_pricing import (
    OPENAI_DEFAULT_CHAT_MODEL,
    openai_standard_price_rows,
)
from lumen_core.pricing_fallback import fallback_pricing_for


def test_openai_defaults_use_gpt_56_sol_and_include_current_family() -> None:
    rows = {
        row["model"]: row
        for row in openai_standard_price_rows()
    }

    assert OPENAI_DEFAULT_CHAT_MODEL == "gpt-5.6-sol"
    assert rows["gpt-5.6-sol"] == {
        "model": "gpt-5.6-sol",
        "input_usd_per_1m": "5.00",
        "output_usd_per_1m": "30.00",
    }
    assert rows["gpt-5.6-terra"]["input_usd_per_1m"] == "2.00"
    assert rows["gpt-5.6-luna"]["output_usd_per_1m"] == "1.20"


def test_fallback_pricing_covers_canonical_and_alias_models() -> None:
    canonical = fallback_pricing_for("gpt-5.6-sol")
    alias = fallback_pricing_for("gpt-5.6")

    assert canonical is not None
    assert alias is not None
    assert canonical.input_per_1k_micro == alias.input_per_1k_micro
    assert canonical.output_per_1k_micro == alias.output_per_1k_micro
