from __future__ import annotations

import json
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
            "environment_detail": "玻璃门和木桌形成前后景层次",
            "lighting_detail": "窗光从左侧进入，袖口有轻微高光",
            "camera_detail": "半身平视镜头，透视自然",
            "composition_detail": "人物在右侧三分线，左侧留白",
            "creative_intent": "用近距离观察感呈现衣料和手部微动作",
            "natural_detail": "手指轻触袖口，不遮挡胸前",
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
    assert cards[1]["environment_detail"] == "玻璃门和木桌形成前后景层次"
    assert cards[1]["lighting_detail"] == "窗光从左侧进入，袖口有轻微高光"
    assert cards[1]["camera_detail"] == "半身平视镜头，透视自然"
    assert cards[1]["composition_detail"] == "人物在右侧三分线，左侧留白"
    assert cards[1]["creative_intent"] == "用近距离观察感呈现衣料和手部微动作"
    assert cards[1]["natural_detail"] == "手指轻触袖口，不遮挡胸前"
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
    assert all(card["environment_detail"] for card in cards)
    assert all(card["lighting_detail"] for card in cards)
    assert all(card["camera_detail"] for card in cards)
    assert all(card["composition_detail"] for card in cards)
    assert all(card["creative_intent"] for card in cards)
    assert all(card["natural_detail"] for card in cards)


@pytest.mark.asyncio
async def test_prompt_composer_expands_only_shooting_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "candidate_briefs": [
                "候选一：窗边自然侧光下的生活抓拍，人物停在灰白墙面与木地板之间，动作安静。",
                "候选二：用更强的空间留白处理人物和墙角关系，手指只轻触衣摆边缘。",
                "候选三：抓住小步停住的决定性瞬间，侧光落在肩线和衣料纹理上。",
            ],
            "selected_candidate_index": 3,
            "selection_scores": [
                {
                    "candidate": 1,
                    "product_visibility": 8,
                    "naturalness": 8,
                    "photographic_quality": 7,
                    "variety": 7,
                    "risk_control": 8,
                    "total": 38,
                    "reason": "清楚但摄影意图偏常规",
                },
                {
                    "candidate": 2,
                    "product_visibility": 8,
                    "naturalness": 8,
                    "photographic_quality": 8,
                    "variety": 8,
                    "risk_control": 8,
                    "total": 40,
                    "reason": "空间关系更好",
                },
                {
                    "candidate": 3,
                    "product_visibility": 9,
                    "naturalness": 9,
                    "photographic_quality": 9,
                    "variety": 8,
                    "risk_control": 9,
                    "total": 44,
                    "reason": "商品可见性和作品感最均衡",
                },
            ],
            "shooting_brief": (
                "窗边自然侧光下的真实生活抓拍，人物在灰白墙面和木地板之间小步停住，"
                "身体轻微侧向镜头，手指只在衣摆边缘做很小的整理动作。镜头保持平视全身距离，"
                "头脚完整不切断，背景保留墙角和地面交界的空间层次，商品主体在当前角度清楚。"
            ),
            "scene_keywords": ["窗边", "灰白墙面", "木地板"],
            "composition_keywords": ["全身", "留白"],
            "lighting_keywords": ["侧光", "柔和阴影"],
            "action_keywords": ["小步停住", "整理衣摆"],
            "photographic_idea_keywords": ["决定性瞬间", "空间张力"],
            "product_visibility_checklist": ["当前角度商品主体清楚"],
            "negative_prompt_notes": ["手不要遮挡主体"],
            "regenerate_if": ["动作僵硬"],
        }

    monkeypatch.setattr(scene_planner, "_call_gpt55_json", fake_call)

    result = await scene_planner.compose_image_prompt_with_gpt55(
        SimpleNamespace(),  # type: ignore[arg-type]
        base_prompt="【最高优先级：商品 1:1 还原】完整系统 prompt",
        product_analysis={
            "category": "女童背带裙",
            "must_preserve": ["蓝色牛仔", "异色背带"],
        },
        garment_lock={
            "core_identity": "女童背带裙",
            "must_preserve": ["蓝色牛仔", "异色背带"],
        },
        model_summary="独立生成 · 儿童",
        scene_card={
            "id": "scene-1",
            "scene_family": "premium_studio",
            "location": "灰白墙面和木地板的空间",
            "micro_event": "小步停住后整理衣摆",
            "camera": {"distance": "full_body", "angle": "eye_level"},
            "pose": "轻微侧身",
            "motion": "手指整理衣摆边缘",
            "props": ["白色短袜"],
            "lighting": "窗边自然侧光",
            "composition": "人物完整入镜",
            "creative_intent": "用决定性停步瞬间和克制空间关系呈现服装",
            "product_visibility": "front_full_body",
            "negative": ["不要遮挡主体"],
        },
        shot_class="front_full_body",
        template="premium_studio",
        aspect_ratio="4:5",
        final_quality="high",
    )

    payload = captured["payload"]
    assert "base_prompt" not in payload
    assert payload["request"]["system_will_append_product_lock"] is True
    assert payload["request"]["candidate_count"] == 3
    assert "must_preserve" not in json.dumps(payload, ensure_ascii=False)
    assert "完整系统 prompt" not in json.dumps(payload, ensure_ascii=False)
    assert result["shooting_brief"].startswith("窗边自然侧光")
    assert result["final_prompt"] == result["shooting_brief"]
    assert len(result["candidate_briefs"]) == 3
    assert result["selected_candidate_index"] == "3"
    assert result["selection_scores"][2]["total"] == 44
    assert result["scene_keywords"] == ["窗边", "灰白墙面", "木地板"]
    assert result["photographic_idea_keywords"] == ["决定性瞬间", "空间张力"]


def test_fallback_prompt_composition_does_not_reinject_base_prompt() -> None:
    result = scene_planner.fallback_prompt_composition(
        base_prompt="【最高优先级：商品 1:1 还原】完整系统 prompt",
        scene_card={"id": "scene-1", "negative": ["不要遮挡主体"]},
        reason="composer failed",
    )

    assert result["status"] == "fallback"
    assert result["shooting_brief"] == ""
    assert result["final_prompt"] == ""
    assert result["candidate_briefs"] == []
    assert result["selected_candidate_index"] is None
    assert result["selection_scores"] == []
    assert result["photographic_idea_keywords"] == []
    assert result["negative_prompt_notes"] == ["不要遮挡主体"]
