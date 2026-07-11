from __future__ import annotations

import pytest

from app.routes import _showcase_template_policy as policy
from app.routes import workflows


MOVED_NAMES = (
    "TEMPLATE_LABELS",
    "SCENE_ENVIRONMENT_TEMPLATES",
    "_scene_environment_outdoor_phrase",
    "_template_requirement",
    "_RENDER_DIRECTIONS",
    "_RENDER_DIRECTIONS_OUTDOOR",
    "_showcase_render_direction",
    "_POSE_DIRECTIONS",
    "_showcase_pose_direction",
    "_LIFESTYLE_TEMPLATES",
    "_showcase_composition_direction",
    "_SQUARE_OR_LANDSCAPE_RATIOS",
    "_showcase_framing_direction",
)


def test_template_requirement_preserves_indoor_background_recommendation() -> None:
    requirement = policy._template_requirement(
        "natural_phone_snapshot",
        {
            "category": "针织开衫",
            "background_recommendation": "落地窗客厅",
        },
        "indoor",
    )

    assert "落地窗客厅" in requirement
    assert "自然光或室内暖光" in requirement
    assert "户外随手拍场景" not in requirement


@pytest.mark.parametrize(
    ("template", "expected_scene"),
    [
        ("daily_snapshot", "户外随拍场景"),
        ("natural_phone_snapshot", "户外随手拍场景"),
        ("social_seed", "户外种草场景"),
    ],
)
def test_template_requirement_uses_outdoor_variant(
    template: str,
    expected_scene: str,
) -> None:
    requirement = policy._template_requirement(
        template,
        {"category": "针织开衫", "background_recommendation": "落地窗客厅"},
        "outdoor",
    )

    assert expected_scene in requirement
    assert "自然日光" in requirement
    assert "针织开衫" in requirement


def test_showcase_render_direction_selects_outdoor_and_fallback_variants() -> None:
    assert (
        policy._showcase_render_direction("daily_snapshot", "outdoor")
        is policy._RENDER_DIRECTIONS_OUTDOOR["daily_snapshot"]
    )
    assert (
        policy._showcase_render_direction("daily_snapshot", "indoor")
        is policy._RENDER_DIRECTIONS["daily_snapshot"]
    )
    assert (
        policy._showcase_render_direction("white_ecommerce", "outdoor")
        is policy._RENDER_DIRECTIONS["white_ecommerce"]
    )
    assert policy._showcase_render_direction("unknown") == (
        "真实摄影质感；皮肤保留真实毛孔细纹和自然光泽；不要塑料感、过度磨皮、AI美颜脸"
    )


def test_showcase_pose_and_composition_directions() -> None:
    assert "平视手持" in policy._showcase_pose_direction("natural_phone_snapshot")
    assert policy._showcase_pose_direction("unknown") == "姿态自然舒展"
    assert "三分法构图" in policy._showcase_composition_direction("lifestyle")
    assert policy._showcase_composition_direction("white_ecommerce") == ""


@pytest.mark.parametrize(
    ("shot_class", "framing", "aspect_ratio", "expected"),
    [
        ("detail_half_body", "tone_first", "16:9", "上半身或胸口以上入镜"),
        ("front_full_body", "tone_first", "16:9", "人物占画面 45-60% 高度"),
        ("front_full_body", "tone_first", "4:5", "人物占画面 55-70% 高度"),
        ("front_full_body", "product_first", "1:1", "人物占画面 60-75% 高度"),
        ("front_full_body", "product_first", "4:5", "人物占画面 70-85% 高度"),
    ],
)
def test_showcase_framing_direction_matrix(
    shot_class: str,
    framing: str,
    aspect_ratio: str,
    expected: str,
) -> None:
    direction = policy._showcase_framing_direction(
        shot_class,
        framing,
        aspect_ratio,
    )

    assert expected in direction


def test_workflows_reexports_showcase_template_policy_names() -> None:
    for name in MOVED_NAMES:
        assert getattr(workflows, name) is getattr(policy, name)
