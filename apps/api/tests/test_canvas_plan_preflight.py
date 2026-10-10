from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.canvas_services.plan_preflight import (
    validate_plan_inputs,
    validate_media_shape,
)
from app.canvas_services.plan_pricing import cached_plan_quote, quote_plan_node


def prompt_graph(text="draw", with_edge=True):
    return {
        "nodes": [
            {"id": "p", "type": "prompt", "config": {"text": text}},
            {"id": "a", "type": "image_generate", "config": {}},
        ],
        "edges": [
            {
                "id": "pa",
                "source_node_id": "p",
                "source_handle": "text",
                "target_node_id": "a",
                "target_handle": "prompt",
            }
        ]
        if with_edge
        else [],
    }


def test_static_required_prompt_is_checked_before_admission():
    plan = SimpleNamespace(steps=[SimpleNamespace(node_id="a", reuse=None)])
    validate_plan_inputs(prompt_graph(), plan)
    for graph in (prompt_graph(with_edge=False), prompt_graph("   ")):
        with pytest.raises(HTTPException):
            validate_plan_inputs(graph, plan)
    plan.steps[0].reuse = object()
    validate_plan_inputs(prompt_graph(with_edge=False), plan)


@pytest.mark.parametrize(
    "kind,config,counts",
    [
        ("image_edit", {}, {"prompt": 1}),
        ("image_generate", {}, {"prompt": 1, "mask": 1}),
        ("video_generate", {"mode": "i2v"}, {"prompt": 1}),
        ("video_generate", {"mode": "t2v"}, {"prompt": 1, "reference_images": 1}),
        ("video_generate", {"mode": "reference"}, {"prompt": 1}),
    ],
)
def test_static_media_shape_blocks_invalid_independent_branches(kind, config, counts):
    with pytest.raises(HTTPException):
        validate_media_shape({"id": "a", "type": kind, "config": config}, counts)


@pytest.mark.asyncio
async def test_equal_configs_share_only_request_local_quotes():
    calls = []

    async def quote(*args, **kwargs):
        calls.append(kwargs["node"]["id"])
        return object()

    graph = prompt_graph()
    a = graph["nodes"][1]
    b = {**a, "id": "b"}
    cache = {}
    first = await cached_plan_quote(
        None, user="u", graph=graph, node=a, cache=cache, quote_fn=quote
    )
    assert (
        await cached_plan_quote(
            None, user="u", graph=graph, node=b, cache=cache, quote_fn=quote
        )
        is first
    )
    assert calls == ["a"]
    await cached_plan_quote(
        None, user="u", graph=graph, node=b, cache={}, quote_fn=quote
    )
    assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_missing_pricing_is_controlled_and_never_zero(monkeypatch):
    from lumen_core.billing_values import BillingError

    async def unavailable(*args, **kwargs):
        raise BillingError("pricing_missing", "private configuration")

    monkeypatch.setattr(
        "app.canvas_services.plan_pricing.calculate_plan_quote", unavailable
    )
    with pytest.raises(HTTPException) as denied:
        await quote_plan_node(None, user="u", graph={}, node={})
    assert denied.value.detail["error"]["code"] == "canvas_plan_pricing_unavailable"
    assert "private configuration" not in str(denied.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["pending", "preparing", "failed", "unavailable", "ready"]
)
async def test_plan_assets_require_authoritative_ready(state, monkeypatch):
    from app.canvas_services.plan_asset_preflight import validate_plan_assets
    from lumen_core.canvas_run_plan import OutputReference

    reference = OutputReference("a", "old", 0, "image", "asset", "a" * 64)
    plan = SimpleNamespace(
        steps=[SimpleNamespace(node_id="a", reuse=reference)], bindings=[]
    )

    class Session:
        async def execute(self, statement):
            assert "user_id" in str(statement) and "deleted_at" in str(statement)
            return SimpleNamespace(scalars=lambda: [SimpleNamespace(id="asset")])

    monkeypatch.setattr(
        "app.canvas_services.plan_asset_preflight.asset_descriptor",
        lambda row: {
            "preparation_state": state,
            "source_sha256": "a" * 64,
        },
    )
    if state == "ready":
        await validate_plan_assets(
            Session(), user_id="u", graph=prompt_graph(), plan=plan
        )
    else:
        with pytest.raises(HTTPException) as denied:
            await validate_plan_assets(
                Session(), user_id="u", graph=prompt_graph(), plan=plan
            )
        assert denied.value.detail["error"]["code"] == "canvas_plan_asset_not_ready"
