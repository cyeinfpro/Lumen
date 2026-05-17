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


@pytest.mark.asyncio
async def test_call_gpt55_json_retries_text_only_when_reference_image_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_images = [{"label": "商品图", "image_url": "data:image/jpeg;base64,x"}]
    calls: list[list[dict[str, str]] | None] = []

    async def fake_call(*args: Any, **kwargs: Any) -> str:
        calls.append(kwargs.get("reference_images"))
        if len(calls) == 1:
            raise scene_planner._UpstreamHTTPError(
                400, "unsupported input_image data URL"
            )
        return '{"ok": true}'

    monkeypatch.setattr(scene_planner, "_call_responses_text", fake_call)

    result = await scene_planner._call_gpt55_json(
        SimpleNamespace(),  # type: ignore[arg-type]
        purpose="test",
        instructions="return json",
        payload={},
        max_output_tokens=200,
        provider_order=[fake_provider("p1")],
        reference_images=reference_images,
    )

    assert calls == [reference_images, None]
    assert result["ok"] is True
    assert "reference_image_fallback_reason" in result


@pytest.mark.asyncio
async def test_call_gpt55_json_continues_attempts_after_text_only_retry_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_images = [{"label": "商品图", "image_url": "data:image/jpeg;base64,x"}]
    calls: list[tuple[str, list[dict[str, str]] | None]] = []

    async def fake_call(*args: Any, **kwargs: Any) -> str:
        attempt_name = kwargs["attempt"]["name"]
        refs = kwargs.get("reference_images")
        calls.append((attempt_name, refs))
        if len(calls) == 1:
            raise scene_planner._UpstreamHTTPError(
                400, "unsupported input_image data URL"
            )
        if len(calls) == 2:
            raise scene_planner._UpstreamHTTPError(500, "temporary upstream error")
        return '{"ok": true}'

    monkeypatch.setattr(scene_planner, "_call_responses_text", fake_call)

    result = await scene_planner._call_gpt55_json(
        SimpleNamespace(),  # type: ignore[arg-type]
        purpose="test",
        instructions="return json",
        payload={},
        max_output_tokens=200,
        provider_order=[fake_provider("p1")],
        reference_images=reference_images,
    )

    assert calls == [
        ("gpt55-priority", reference_images),
        ("gpt55-priority", None),
        ("gpt55-standard", None),
    ]
    assert result["ok"] is True
    assert "reference_image_fallback_reason" in result


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


def test_normalize_scene_cards_forces_non_side_shots_front_facing() -> None:
    raw_cards = [
        {
            "id": "natural_pose-1",
            "scene_family": "street",
            "location": "树影步道",
            "micro_event": "背向前走半步后回望",
            "camera": {"distance": "full_body", "angle": "side_or_back"},
            "pose": "背向镜头回头",
            "motion": "侧后转身带出后背轮廓",
            "lighting": "侧光",
            "composition": "后背作为主视角",
            "product_visibility": "side_or_back_silhouette",
            "camera_detail": "侧后角度，背面结构清楚",
        }
    ]
    fallback_cards = [
        {
            "id": "fallback-natural",
            "scene_family": "street",
            "location": "树影步道",
            "micro_event": "沿着场景向前走时被自然抓拍",
            "camera": {"distance": "full_body", "angle": "front_three_quarter"},
            "pose": "身体三分之二正面，手部保持低位",
            "motion": "正面微侧移动带出衣服褶皱",
            "lighting": "侧光",
            "composition": "脸部和商品主体清楚",
            "product_visibility": "front_full_body",
            "camera_detail": "平视三分之二正面机位",
        }
    ]

    cards = scene_planner._normalize_scene_cards(
        raw_cards,
        fallback_cards,
        [("natural_pose", {"label": "自然动作", "framing": "tone_first"})],
    )

    card = cards[0]
    assert card["camera"]["angle"] == "front_three_quarter"
    assert card["product_visibility"] == "front_full_body"
    assert "背向" not in card["pose"]
    assert "侧后" not in card["motion"]
    assert any("不要背影" in item for item in card["negative"])


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
async def test_scene_director_receives_compact_product_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "series_concept": "有活力的儿童环境肖像",
            "continuity_anchors": [],
            "scene_cards": [
                {
                    "id": "front_full_body-1",
                    "scene_family": "outdoor_daily",
                    "location": "树影步道边",
                    "micro_event": "向镜头方向小步跑近后自然停住",
                    "camera": {
                        "distance": "full_body",
                        "angle": "front_three_quarter",
                        "lens_feel": "handheld_standard",
                        "orientation": "vertical",
                    },
                    "pose": "身体三分之二正面，双手自然低位",
                    "motion": "刚停住的轻微惯性带出衣摆褶皱",
                    "props": ["白色短袜"],
                    "lighting": "树影下的自然侧逆光",
                    "composition": "人物完整入镜，商品主体清楚",
                    "product_visibility": "front_full_body",
                    "environment_detail": "远处绿植和步道形成前后景层次",
                    "lighting_detail": "侧逆光勾出肩线，脸部有真实明暗",
                    "camera_detail": "平视标准镜头，轻微手持抓拍感",
                    "composition_detail": "人物落在右侧三分线，左侧留出行进空间",
                    "creative_intent": "用小步停住的决定性瞬间制造童装活力",
                    "natural_detail": "表情松弛，手指自然弯曲，衣料褶皱可信",
                    "negative": ["不要遮挡商品主体"],
                }
            ],
            "risk_notes": [],
        }

    monkeypatch.setattr(scene_planner, "_call_gpt55_json", fake_call)

    result = await scene_planner.plan_scene_cards_with_gpt55(
        SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={
            "category": "女童短袖假两件背带连衣裙",
            "color": "白色上衣和浅蓝色牛仔裙",
            "material_guess": "牛仔布",
            "silhouette": "A 字裙身",
            "key_details": [
                "异色背带",
                "毛绒小熊贴布",
                "雏菊刺绣",
                "白色花形扣饰",
            ],
            "must_preserve": [
                "白色圆领短袖上衣",
                "浅蓝色牛仔A字裙身",
                "一红一浅黄的异色背带",
                "前片立体毛绒小熊贴布与小口袋",
                "背后交叉背带和牛仔蝴蝶结",
                "裙摆彩色波浪缝线",
            ],
            "risks": ["背带颜色容易被改错"],
            "background_recommendation": "明亮、有童趣但干净的生活化氛围",
        },
        garment_lock={
            "core_identity": "女童短袖假两件背带连衣裙、白色圆领短袖上衣、浅蓝色牛仔A字裙身",
            "must_preserve": ["白色圆领短袖上衣", "浅蓝色牛仔A字裙身"],
            "occlusion_policy": "手、头发、包带、宠物不得遮挡商品主体",
            "mutation_bans": ["改颜色", "改廓形"],
        },
        model_summary="独立生成 · 儿童",
        template="lifestyle",
        scene_environment="outdoor",
        shot_picks=[
            ("front_full_body", {"label": "正面全身", "framing": "product_first"})
        ],
        aspect_ratio="4:5",
        output_count=1,
        user_prompt="要活力街拍感",
        accessory_plan={"items": ["白色短袜"]},
        scene_strategy="editorial_campaign",
        scene_variety="rich",
        continuity_anchor="none",
        allow_pet=False,
        allow_background_people=False,
        reference_images=[
            {"label": "商品图", "image_url": "data:image/jpeg;base64,product"},
            {"label": "已确认模特图", "image_url": "data:image/jpeg;base64,model"},
        ],
    )

    payload = captured["payload"]
    product = payload["product"]
    assert product["category"] == "女童短袖假两件背带连衣裙"
    assert product["visual_keywords"] == [
        "白色上衣和浅蓝色牛仔裙",
        "牛仔布",
        "A 字裙身",
        "异色背带",
        "毛绒小熊贴布",
    ]
    assert "analysis" not in product
    assert "garment_lock" not in product
    payload_text = json.dumps(payload, ensure_ascii=False)
    assert "must_preserve" not in payload_text
    assert "occlusion_policy" not in payload_text
    assert "mutation_bans" not in payload_text
    assert "背带颜色容易被改错" not in payload_text
    assert "少量服装关键词" in captured["instructions"]
    assert "已确认模特图" in captured["instructions"]
    assert captured["reference_images"] == [
        {"label": "商品图", "image_url": "data:image/jpeg;base64,product"},
        {"label": "已确认模特图", "image_url": "data:image/jpeg;base64,model"},
    ]
    assert "最高优先级" not in captured["instructions"]
    assert result["planner_status"] == "ok"


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
        reference_images=[
            {"label": "商品图", "image_url": "data:image/jpeg;base64,product"},
            {"label": "已确认模特图", "image_url": "data:image/jpeg;base64,model"},
        ],
    )

    payload = captured["payload"]
    assert "base_prompt" not in payload
    assert payload["request"]["system_will_append_product_lock"] is True
    assert payload["request"]["candidate_count"] == 3
    assert "core_identity" not in payload["product_context"]
    assert payload["product_context"]["visual_keywords"] == ["蓝色牛仔", "异色背带"]
    assert "GPT Image 2" in captured["instructions"]
    assert captured["reference_images"] == [
        {"label": "商品图", "image_url": "data:image/jpeg;base64,product"},
        {"label": "已确认模特图", "image_url": "data:image/jpeg;base64,model"},
    ]
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
