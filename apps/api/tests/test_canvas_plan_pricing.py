from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.canvas_services import plan_pricing


@pytest.mark.asyncio
async def test_image_quote_uses_real_unit_price_and_per_image_rate_rounding(
    monkeypatch,
):
    calls = []

    async def enabled(db):
        return True

    async def estimate(db, **kwargs):
        calls.append(kwargs)
        return 10001, "1k"

    monkeypatch.setattr(plan_pricing, "billing_enabled", enabled)
    monkeypatch.setattr(plan_pricing, "estimate_image_cost_for_tier", estimate)
    quote = await plan_pricing.quote_plan_node(
        object(),
        user=SimpleNamespace(account_mode="wallet", billing_rate_multiplier="1.0009"),
        graph={"nodes": [], "edges": []},
        node={"id": "node", "type": "image_generate", "config": {"count": 3}},
    )
    assert calls == [{"tier": "1k", "n": 1}]
    assert quote.estimated_cost_micro == (10001 * 10009 // 10000) * 3
    assert quote.model == "gpt-image-2"
    assert len(quote.capability_version) == 64


@pytest.mark.asyncio
async def test_unknown_wallet_pricing_does_not_return_fake_zero(monkeypatch):
    async def disabled(db):
        return False

    monkeypatch.setattr(plan_pricing, "billing_enabled", disabled)
    with pytest.raises(HTTPException):
        await plan_pricing.quote_plan_node(
            object(),
            user=SimpleNamespace(account_mode="wallet"),
            graph={},
            node={"id": "node", "type": "image_generate", "config": {}},
        )


def test_video_reference_quote_classifies_image_and_video_sources():
    graph = {
        "nodes": [
            {"id": "i", "type": "image_generate"},
            {"id": "v", "type": "video_asset"},
        ],
        "edges": [
            {
                "source_node_id": "i",
                "source_handle": "image",
                "target_node_id": "target",
                "target_handle": "reference_images",
            },
            {
                "source_node_id": "v",
                "source_handle": "video",
                "target_node_id": "target",
                "target_handle": "reference_videos",
            },
        ],
    }
    assert plan_pricing.reference_kinds(graph, "target") == [
        {"kind": "image"},
        {"kind": "video"},
    ]


@pytest.mark.asyncio
async def test_batch_auto_video_model_requires_explicit_model():
    with pytest.raises(HTTPException) as denied:
        await plan_pricing.quote_video_node(
            object(),
            user=object(),
            graph={},
            node={"id": "v", "config": {}},
        )
    assert denied.value.detail["error"]["code"] == "canvas_plan_model_required"
