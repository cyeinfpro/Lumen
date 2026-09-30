from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.routes import prompts
from app.routes.prompt_parts import billing as billing_module
from app.task_billing import EnhanceBillingContext, EnhanceUsageCapture
from lumen_core.pricing import UsageTokens


def frozen_billing(model: str) -> EnhanceBillingContext:
    return EnhanceBillingContext(
        db=SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock()),
        user_id="user", user_email=None, request_id="request",
        rate_multiplier_x10000=10_000, cache_aware=True, allow_negative=False,
        hold_amount_micro=10_000,
        pricing_snapshots={f"{model}::standard": {}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"])
async def test_missing_usage_settlement_keeps_frozen_model_and_hold(monkeypatch, model):
    billing = frozen_billing(model)
    settle = AsyncMock(return_value=None)
    audit = AsyncMock()
    monkeypatch.setattr(billing_module, "_settle_or_charge", settle)
    monkeypatch.setattr(billing_module, "_audit_default_settlement", audit)
    runtime = replace(prompts._prompt_billing_runtime(), invalidate_balance_cache=AsyncMock())
    assert await billing_module.charge_prompt_enhance(
        billing, EnhanceUsageCapture(), runtime=runtime
    )
    assert settle.await_args.kwargs["transaction_meta"]["model"] == model
    assert settle.await_args.kwargs["cost"] == billing.hold_amount_micro
    assert audit.await_args.kwargs["model"] == model
    assert billing.settle_outcome.attempted is True
    billing.db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"])
async def test_usage_without_response_model_uses_frozen_policy(monkeypatch, model):
    parser = Mock(return_value=UsageTokens(input_tokens=1, output_tokens=1))
    monkeypatch.setattr(billing_module, "parse_usage", parser)
    monkeypatch.setattr(billing_module, "_resolve_breakdown", AsyncMock(return_value=None))
    assert await billing_module.charge_prompt_enhance(
        frozen_billing(model), EnhanceUsageCapture(usage={"input_tokens": 1}),
        runtime=prompts._prompt_billing_runtime(),
    )
    assert parser.call_args.args[0] == model


def test_legacy_and_multi_model_policies_are_not_inferred_as_pinned():
    runtime = prompts._prompt_billing_runtime()
    assert billing_module._pinned_enhancement_model(frozen_billing("legacy"), runtime) is None
    billing = frozen_billing("gpt-6-astra")
    billing.pricing_snapshots["gpt-6-sol::standard"] = {}
    assert billing_module._pinned_enhancement_model(billing, runtime) is None
