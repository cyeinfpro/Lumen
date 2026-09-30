from __future__ import annotations

import pytest

from lumen_core.openai_pricing import OPENAI_DEFAULT_CHAT_MODEL, openai_standard_price_rows
from lumen_core.pricing_fallback import fallback_pricing_for


@pytest.mark.parametrize(
    "model,input_usd,output_usd,input_micro,output_micro",
    [
        ("gpt-6-astra", "10.00", "50.00", 72_000, 360_000),
        ("gpt-6-sol", "2.00", "10.00", 14_400, 72_000),
        ("gpt-6-luna", "0.10", "0.50", 720, 3_600),
    ],
)
def test_gpt6_standard_catalog_and_fallback(model, input_usd, output_usd, input_micro, output_micro):
    rows = {row["model"]: row for row in openai_standard_price_rows()}
    assert rows[model]["input_usd_per_1m"] == input_usd
    assert rows[model]["output_usd_per_1m"] == output_usd
    pricing = fallback_pricing_for(model)
    assert pricing is not None
    assert pricing.input_per_1k_micro == input_micro
    assert pricing.output_per_1k_micro == output_micro
    assert pricing.long_context_threshold_tokens == 272_000
    assert pricing.long_context_input_multiplier_x10000 == 20_000
    assert pricing.long_context_output_multiplier_x10000 == 15_000
    assert fallback_pricing_for(f"gateway:openai/{model}-2026-09-29") == pricing


def test_adding_gpt6_does_not_change_default_or_guess_unverified_variants():
    assert OPENAI_DEFAULT_CHAT_MODEL == "gpt-5.6-sol"
    assert fallback_pricing_for("gpt-6-astra-pro") is None
    rows = openai_standard_price_rows()
    rows[0]["input_usd_per_1m"] = "0"
    assert openai_standard_price_rows()[0]["input_usd_per_1m"] == "5.00"
