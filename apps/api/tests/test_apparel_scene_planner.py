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


def test_gpt55_call_timeout_warns_on_unknown_purpose(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="app.routes._apparel_scene_planner")

    assert scene_planner._gpt55_call_timeout_seconds("new_unmapped_purpose") == 75.0
    assert "unknown GPT-5.5 call purpose" in caplog.text
    assert "new_unmapped_purpose" in caplog.text


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


def _complete_scene_card(**overrides: Any) -> dict[str, Any]:
    card = {
        "id": "front_full_body-1",
        "scene_family": "gpt_scene",
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
        "props": [],
        "lighting": "树影下的自然侧逆光",
        "composition": "人物完整入镜，商品主体清楚",
        "product_visibility": "front_full_body",
        "environment_detail": "远处绿植和步道形成前后景层次",
        "lighting_detail": "侧逆光勾出肩线，脸部有真实明暗",
        "camera_detail": "平视标准镜头，轻微手持抓拍感",
        "composition_detail": "人物落在右侧三分线，左侧留出行进空间",
        "creative_intent": "用小步停住的决定性瞬间制造童装活力",
        "natural_detail": "表情松弛，手指自然弯曲，衣料褶皱可信",
        "shooting_brief": "树影步道边的自然抓拍，孩子向镜头小步跑近后停住，商品主体清楚。",
        "negative": ["不要遮挡商品主体"],
    }
    card.update(overrides)
    return card


def test_normalize_scene_cards_aligns_by_product_visibility_without_fallback_fill() -> (
    None
):
    shot_picks = [
        ("front_full_body", {"label": "正面全身"}),
        ("detail_half_body", {"label": "半身细节"}),
        ("natural_pose", {"label": "自然动作"}),
    ]
    raw_cards = [
        _complete_scene_card(
            id="detail-first",
            location="咖啡店门口",
            micro_event="整理袖口",
            camera={
                "distance": "half_body",
                "angle": "eye_level",
                "lens_feel": "natural_standard",
                "orientation": "vertical",
            },
            pose="半身微侧，手指只触碰袖口边缘",
            motion="手指从袖口外侧掠过，胸前保持清楚",
            lighting="窗光",
            composition="半身居中，胸前和袖口清楚",
            product_visibility="upper_body_detail",
            environment_detail="玻璃门和木桌形成前后景层次",
            lighting_detail="窗光从左侧进入，袖口有轻微高光",
            camera_detail="半身平视镜头，透视自然",
            composition_detail="人物在右侧三分线，左侧留白",
            creative_intent="用近距离观察感呈现衣料和手部微动作",
            natural_detail="手指轻触袖口，不遮挡胸前",
            shooting_brief="咖啡店门口窗光下的半身近景，手指轻触袖口，胸前商品主体清楚。",
        ),
        _complete_scene_card(id="front_full_body-1"),
        _complete_scene_card(
            id="natural_pose-3",
            product_visibility="front_full_body",
            location="街边玻璃门外",
            micro_event="绕过门框半步转身看向镜头",
            pose="身体三分之二正面移动中回头",
            motion="脚步方向和视线形成抓拍张力",
            shooting_brief="街边玻璃门外，孩子绕过门框半步转身看向镜头，商品主体清楚。",
        ),
    ]

    cards = scene_planner._normalize_scene_cards(raw_cards, shot_picks)
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
    assert cards[2]["location"] == "街边玻璃门外"


def test_normalize_scene_cards_rejects_incomplete_gpt_card_instead_of_pool_fill() -> (
    None
):
    with pytest.raises(ValueError, match="incomplete GPT scene_card"):
        scene_planner._normalize_scene_cards(
            [
                {
                    "id": "front_full_body-1",
                    "location": "街角",
                    "micro_event": "自然站立",
                    "camera": {"distance": "full_body", "angle": "eye_level"},
                    "lighting": "侧光",
                    "product_visibility": "front_full_body",
                }
            ],
            [("front_full_body", {"label": "正面全身", "framing": "product_first"})],
        )


def test_normalize_scene_cards_rejects_non_side_back_view_instead_of_rewriting() -> (
    None
):
    raw_cards = [
        _complete_scene_card(
            id="natural_pose-1",
            location="树影步道",
            micro_event="背向前走半步后回望",
            camera={
                "distance": "full_body",
                "angle": "side_or_back",
                "lens_feel": "natural_standard",
                "orientation": "vertical",
            },
            pose="背向镜头回头",
            motion="侧后转身带出后背轮廓",
            lighting="侧光",
            composition="后背作为主视角",
            product_visibility="side_or_back_silhouette",
            camera_detail="侧后角度，背面结构清楚",
        )
    ]

    with pytest.raises(ValueError, match=r"back/side (view|camera angle)"):
        scene_planner._normalize_scene_cards(
            raw_cards,
            [("natural_pose", {"label": "自然动作", "framing": "tone_first"})],
        )


def test_normalize_scene_cards_rejects_side_profile_camera_for_front_shot() -> None:
    raw_cards = [
        _complete_scene_card(
            id="front_full_body-1",
            camera={
                "distance": "full_body",
                "angle": "side_profile",
                "lens_feel": "natural_standard",
                "orientation": "vertical",
            },
            pose="身体正面站位，双手自然放低",
            motion="向镜头前走半步后停住",
            composition="人物完整入镜，服装正面清楚",
        )
    ]

    with pytest.raises(ValueError, match=r"back/side (view|camera angle)"):
        scene_planner._normalize_scene_cards(
            raw_cards,
            [("front_full_body", {"label": "正面全身", "framing": "product_first"})],
        )


def test_normalize_scene_cards_requires_front_angle_for_natural_pose() -> None:
    raw_cards = [
        _complete_scene_card(
            id="natural_pose-1",
            camera={
                "distance": "full_body",
                "angle": "low_angle",
                "lens_feel": "natural_standard",
                "orientation": "vertical",
            },
            pose="正面微侧的小幅移动姿态",
            motion="向镜头前走半步后自然停住",
        )
    ]

    with pytest.raises(ValueError, match="front-facing"):
        scene_planner._normalize_scene_cards(
            raw_cards,
            [("natural_pose", {"label": "自然动作", "framing": "tone_first"})],
        )


def test_normalize_scene_cards_accepts_side_profile_for_side_shot() -> None:
    raw_cards = [
        _complete_scene_card(
            id="side_or_back-1",
            camera={
                "distance": "full_body",
                "angle": "side_profile",
                "lens_feel": "natural_standard",
                "orientation": "vertical",
            },
            pose="侧身站位，肩背轮廓完整",
            motion="小步转身，衣摆随动作轻微移动",
            product_visibility="side_or_back_silhouette",
            composition="侧面廓形清楚，人物完整入镜",
        )
    ]

    cards = scene_planner._normalize_scene_cards(
        raw_cards,
        [("side_or_back", {"label": "侧面背面", "framing": "tone_first"})],
    )

    assert cards[0]["camera"]["angle"] == "side_profile"


def test_normalize_scene_cards_rejects_duplicate_gpt_fingerprint() -> None:
    card = _complete_scene_card(id="front_full_body-1")
    with pytest.raises(ValueError, match="duplicate GPT scene fingerprint"):
        scene_planner._normalize_scene_cards(
            [card, {**card, "id": "natural_pose-2"}],
            [
                ("front_full_body", {"label": "正面全身", "framing": "product_first"}),
                ("natural_pose", {"label": "自然动作", "framing": "tone_first"}),
            ],
        )


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


def test_fallback_scene_card_shooting_brief_skips_empty_sentence_pairs() -> None:
    brief = scene_planner._fallback_scene_card_shooting_brief(  # noqa: SLF001
        {
            "location": None,
            "micro_event": None,
            "pose": "",
            "motion": None,
            "camera": {},
            "camera_detail": None,
            "lighting_detail": "",
            "composition_detail": "",
            "creative_intent": "",
            "natural_detail": "",
        },
        shot_class="front_full_body",
    )

    assert "，。" not in brief
    assert "；。" not in brief
    assert not brief.startswith(("，", "。", "；"))
    assert "自然标准镜头" in brief
    assert "保持正面或三分之二正面" in brief


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
                    "shooting_brief": (
                        "树影步道边的自然抓拍，孩子向镜头方向小步跑近后停住，"
                        "侧逆光勾出肩线，商品主体清楚。"
                    ),
                    "negative": ["不要遮挡商品主体"],
                }
            ],
            "risk_notes": [],
        }

    def fail_fallback_pool(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("fallback pool should be lazy on GPT success")

    monkeypatch.setattr(scene_planner, "_call_gpt55_json", fake_call)
    monkeypatch.setattr(
        scene_planner, "fallback_scene_cards_from_pool", fail_fallback_pool
    )

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
async def test_scene_director_wild_mode_is_gpt_directed_not_pool_driven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "series_concept": "大胆但商品清楚的儿童环境肖像",
            "continuity_anchors": [],
            "scene_cards": [_complete_scene_card()],
            "risk_notes": [],
        }

    monkeypatch.setattr(scene_planner, "_call_gpt55_json", fake_call)

    result = await scene_planner.plan_scene_cards_with_gpt55(
        SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={"category": "女童连衣裙"},
        garment_lock={"core_identity": "女童连衣裙", "must_preserve": ["正面主体"]},
        model_summary="独立生成 · 儿童",
        template="lifestyle",
        scene_environment="outdoor",
        shot_picks=[
            ("front_full_body", {"label": "正面全身", "framing": "product_first"})
        ],
        aspect_ratio="4:5",
        output_count=1,
        user_prompt="要大胆独特",
        accessory_plan={"items": []},
        scene_strategy="editorial_campaign",
        scene_variety="wild",
        continuity_anchor="none",
        allow_pet=False,
        allow_background_people=False,
    )

    assert captured["payload"]["request"]["variety"] == "wild"
    assert captured["payload"]["request"]["creativity_mode"] == "bold_distinctive"
    assert "不要从模板或固定地点池选场景" in captured["instructions"]
    assert "每张至少有一个清楚的视觉钩子" in captured["instructions"]
    assert result["planner"] == "gpt55_preflight"


@pytest.mark.asyncio
async def test_scene_director_retries_invalid_gpt_output_before_rules_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if len(calls) == 1:
            return {
                "series_concept": "不完整输出",
                "scene_cards": [
                    {
                        "id": "front_full_body-1",
                        "location": "普通窗边",
                        "micro_event": "自然站立",
                    }
                ],
            }
        return {
            "series_concept": "重试后完整输出",
            "continuity_anchors": [],
            "scene_cards": [_complete_scene_card()],
            "risk_notes": [],
        }

    monkeypatch.setattr(scene_planner, "_call_gpt55_json", fake_call)

    result = await scene_planner.plan_scene_cards_with_gpt55(
        SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={"category": "女童连衣裙"},
        garment_lock={"core_identity": "女童连衣裙", "must_preserve": ["正面主体"]},
        model_summary="独立生成 · 儿童",
        template="lifestyle",
        scene_environment="outdoor",
        shot_picks=[
            ("front_full_body", {"label": "正面全身", "framing": "product_first"})
        ],
        aspect_ratio="4:5",
        output_count=1,
        user_prompt="要大胆独特",
        accessory_plan={"items": []},
        scene_strategy="editorial_campaign",
        scene_variety="wild",
        continuity_anchor="none",
        allow_pet=False,
        allow_background_people=False,
    )

    assert len(calls) == 2
    assert "retry_context" not in calls[0]["payload"]
    assert calls[1]["payload"]["retry_context"]["attempt"] == 2
    previous_failure = calls[1]["payload"]["retry_context"]["previous_failure"]
    assert "字段不完整" in previous_failure
    assert "camera.lens_feel" not in previous_failure
    assert "【重试修正】" in calls[1]["instructions"]
    assert "camera.lens_feel" not in calls[1]["instructions"]
    assert result["planner"] == "gpt55_preflight"
    assert result["planner_status"] == "ok"
    assert result["director_attempts_made"] == 2
    assert result["director_retry_count"] == 1
    assert len(result["director_retry_errors"]) == 1


@pytest.mark.asyncio
async def test_scene_director_falls_back_only_after_retry_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        raise RuntimeError("temporary upstream failure")

    monkeypatch.setenv("LUMEN_SHOWCASE_GPT_DIRECTOR_RETRIES", "1")
    monkeypatch.setattr(scene_planner, "_call_gpt55_json", fake_call)

    result = await scene_planner.plan_scene_cards_with_gpt55(
        SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={"category": "女童连衣裙"},
        garment_lock={"core_identity": "女童连衣裙", "must_preserve": ["正面主体"]},
        model_summary="独立生成 · 儿童",
        template="lifestyle",
        scene_environment="outdoor",
        shot_picks=[
            ("front_full_body", {"label": "正面全身", "framing": "product_first"})
        ],
        aspect_ratio="4:5",
        output_count=1,
        user_prompt="要大胆独特",
        accessory_plan={"items": []},
        scene_strategy="editorial_campaign",
        scene_variety="wild",
        continuity_anchor="none",
        allow_pet=False,
        allow_background_people=False,
    )

    assert len(calls) == 2
    assert result["planner"] == "rules_fallback"
    assert result["planner_status"] == "fallback"
    assert result["fallback_reason"].startswith("gpt55_director_retry_exhausted")
    assert result["director_attempts_made"] == 2
    assert result["director_retry_count"] == 2
    assert result["director_retry_errors"] == [
        "temporary upstream failure",
        "temporary upstream failure",
    ]


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
    assert payload["request"]["candidate_count"] == 1
    assert "core_identity" not in payload["product_context"]
    assert payload["product_context"]["visual_keywords"] == ["蓝色牛仔", "异色背带"]
    assert "GPT Image 2" in captured["instructions"]
    assert "只输出 1 条最终 shooting_brief" in captured["instructions"]
    assert (
        "不得把原本的行走、落步、半转、回头等动态改成静态站姿"
        in captured["instructions"]
    )
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
