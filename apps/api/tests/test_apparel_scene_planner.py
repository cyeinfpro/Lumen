from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import _apparel_scene_planner as scene_planner
from lumen_core.providers import ProviderDefinition


def fake_provider(name: str) -> ProviderDefinition:
    return ProviderDefinition(
        name=name,
        base_url="https://upstream.example/v1",
        api_key="sk-test",
    )


@pytest.mark.asyncio
async def test_call_gpt55_json_skips_attempts_on_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_call(*args: Any, **kwargs: Any) -> str:
        calls.append(kwargs["attempt"]["name"])
        raise scene_planner._UpstreamHTTPError(401, "unauthorized")

    monkeypatch.setattr(scene_planner, "_call_responses_text", fake_call)

    with pytest.raises(RuntimeError):
        await scene_planner._call_gpt55_json(
            SimpleNamespace(),  # type: ignore[arg-type]
            purpose="test",
            instructions="return json",
            payload={},
            max_output_tokens=200,
            provider_order=[fake_provider("p1"), fake_provider("p2")],
        )

    assert calls == ["gpt55-priority", "gpt55-priority"]


def test_normalize_scene_cards_aligns_by_product_visibility_and_dedupes() -> None:
    shot_picks = [
        ("front_full_body", {"label": "正面全身"}),
        ("detail_half_body", {"label": "半身细节"}),
        ("natural_pose", {"label": "自然动作"}),
    ]
    duplicate = {
        "id": "same",
        "scene_family": "street",
        "location": "街角",
        "micro_event": "自然站立",
        "camera": {"distance": "full_body", "angle": "eye_level"},
        "lighting": "侧光",
        "product_visibility": "front_full_body",
    }
    raw_cards = [
        {
            "id": "detail-first",
            "scene_family": "street",
            "location": "咖啡店门口",
            "micro_event": "整理袖口",
            "camera": {"distance": "half_body", "angle": "eye_level"},
            "lighting": "窗光",
            "product_visibility": "upper_body_detail",
        },
        duplicate,
        duplicate,
    ]
    fallback_cards = [
        duplicate,
        {
            "id": "fallback-detail",
            "scene_family": "studio",
            "location": "白墙",
            "micro_event": "半身细节",
            "camera": {"distance": "half_body", "angle": "eye_level"},
            "lighting": "柔光",
            "product_visibility": "upper_body_detail",
        },
        duplicate,
    ]

    cards = scene_planner._normalize_scene_cards(raw_cards, fallback_cards, shot_picks)
    fingerprints = [card["fingerprint"] for card in cards]

    assert cards[0]["product_visibility"] == "front_full_body"
    assert cards[1]["product_visibility"] == "upper_body_detail"
    assert len(fingerprints) == len(set(fingerprints))
    assert "变体 3" in cards[2]["micro_event"]


def test_fallback_scene_cards_use_real_events_not_shot_labels() -> None:
    shot_picks = [
        ("front_full_body", {"label": "正面全身", "framing": "product_first"}),
        ("natural_pose", {"label": "自然动作", "framing": "tone_first"}),
        ("detail_half_body", {"label": "半身细节", "framing": "product_first"}),
        ("side_or_back", {"label": "侧面背面", "framing": "tone_first"}),
    ]

    cards = scene_planner.fallback_scene_cards_from_pool(
        product_analysis={"category": "衬衫"},
        template="premium_studio",
        scene_environment="indoor",
        shot_picks=shot_picks,
        aspect_ratio="4:5",
        user_prompt="自然真实",
        accessory_plan={"items": ["细项链"]},
        allow_pet=False,
        continuity_anchor="accessory",
        scene_strategy="natural_series",
        scene_variety="rich",
    )

    labels = {variant["label"] for _shot, variant in shot_picks}
    assert len(cards) == 4
    assert len({card["fingerprint"] for card in cards}) == 4
    assert len({card["location"] for card in cards}) > 1
    assert len({card["pose"] for card in cards}) > 1
    assert all(card["micro_event"] not in labels for card in cards)
    assert all(card["pose"] not in labels for card in cards)
