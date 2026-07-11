from __future__ import annotations

import pytest

from app.routes import workflows
from app.routes import _showcase_model_policy as policy


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8岁童装模特", 8),
        ("12-year-old model", 12),
        ("16 yo teen", 16),
        ("adult model", None),
    ],
)
def test_infer_age(text: str, expected: int | None) -> None:
    assert policy._infer_age(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2岁", 90),
        ("8岁", 128),
        ("15岁", 149),
        ("adult", 168),
        ("儿童模特", 128),
    ],
)
def test_infer_model_height(text: str, expected: int) -> None:
    assert policy._infer_model_height_cm(text) == expected
    assert f"{expected}cm" in policy._height_requirement(text)


def test_age_and_accessory_directions_cover_child_teen_and_adult() -> None:
    assert "non-adultized" in policy._age_direction("8岁")
    assert "teen-appropriate" in policy._age_direction("16-year-old")
    assert "around 30 years old" in policy._age_direction("30岁")
    assert "child-appropriate" in policy._accessory_age_direction("童装")
    assert "fit a teenager" in policy._accessory_age_direction("teen")
    assert "adult around 30" in policy._accessory_age_direction("30岁")


def test_candidate_gender_uses_word_boundaries_and_category_fallback() -> None:
    assert policy._infer_candidate_gender("female editorial", {}) == "female"
    assert policy._infer_candidate_gender("male editorial", {}) == "male"
    assert policy._infer_candidate_gender("", {"category": "男装夹克"}) == "male"
    assert policy._infer_candidate_gender("", {}) == "female"


def test_model_diversity_anchor_rotates_and_protects_child_prompts() -> None:
    first = policy._model_diversity_anchor(
        candidate_index=1,
        gender="female",
    )
    second = policy._model_diversity_anchor(
        candidate_index=2,
        gender="female",
    )
    wrapped = policy._model_diversity_anchor(
        candidate_index=9,
        gender="female",
    )
    child = policy._model_diversity_anchor(
        candidate_index=1,
        gender="female",
        age_segment="child",
    )
    assert first != second
    assert first == wrapped
    assert "visibly different" in child
    assert "Look anchor" not in child


def test_style_region_and_user_direction_compaction() -> None:
    assert policy._style_region_from_text("韩系日常") == "亚洲"
    assert policy._style_region_from_text("拉美街头") == "拉美"
    assert policy._style_region_from_text("minimal") == "自然商业摄影"
    assert (
        policy._compact_showcase_user_direction(
            "外貌方向：亚洲，全身照，自然走动回头，保留红色围巾",
            "亚洲",
        )
        == "保留红色围巾"
    )


def test_workflows_keeps_private_policy_compatibility() -> None:
    assert workflows._infer_age is policy._infer_age
    assert workflows._infer_model_height_cm is policy._infer_model_height_cm
    assert workflows._infer_candidate_gender is policy._infer_candidate_gender
    assert workflows._model_diversity_anchor is policy._model_diversity_anchor
    assert (
        workflows._compact_showcase_user_direction
        is policy._compact_showcase_user_direction
    )
