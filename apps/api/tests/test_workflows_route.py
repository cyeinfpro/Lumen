from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.routes import workflows
from lumen_core.schemas import ModelCandidatesCreateIn


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self.rows


class _Db:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)


def test_model_candidates_mvp_requires_three_candidates() -> None:
    body = ModelCandidatesCreateIn(
        candidate_count=3,
        style_prompt="premium",
        accessory_plan={"enabled": True, "items": ["white sneakers"], "strength": "subtle"},
    )

    assert body.accessory_plan.items == ["white sneakers"]

    with pytest.raises(ValidationError):
        ModelCandidatesCreateIn(candidate_count=2, style_prompt="premium")


@pytest.mark.asyncio
async def test_validate_owned_images_accepts_one_to_three_owned_images() -> None:
    db = _Db(["img-1", "img-2"])

    out = await workflows._validate_owned_images(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        user_id="user-1",
        image_ids=["img-1", "img-2", "img-1"],
        min_count=1,
        max_count=3,
    )

    assert out == ["img-1", "img-2"]
    rendered = str(db.statements[0])
    assert "images.user_id" in rendered
    assert "images.deleted_at IS NULL" in rendered


@pytest.mark.asyncio
async def test_validate_owned_images_rejects_missing_or_foreign_image() -> None:
    db = _Db(["img-1"])

    with pytest.raises(Exception) as excinfo:
        await workflows._validate_owned_images(  # noqa: SLF001
            db,  # type: ignore[arg-type]
            user_id="user-1",
            image_ids=["img-1", "img-foreign"],
            min_count=1,
            max_count=3,
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_image"


def test_product_analysis_json_fallback_keeps_reviewable_constraints() -> None:
    out = workflows._try_parse_json_text("This looks like an ivory blazer.")  # noqa: SLF001

    assert out["category"] == "unknown"
    assert "可见商品细节" in out["must_preserve"]
    assert out["summary_text"] == "This looks like an ivory blazer."


def test_product_analysis_prompt_requests_styling_recommendations() -> None:
    prompt = workflows._product_analysis_prompt("8岁童装")  # noqa: SLF001

    assert "styling_recommendations" in prompt
    assert "1-3 个" in prompt
    assert "不需要覆盖所有饰品类别" in prompt
    assert "人群/年龄" in prompt
    assert "must_preserve" in prompt


def test_workflow_image_params_use_high_quality_jpeg() -> None:
    params = workflows._image_params(aspect_ratio="4:5", count=1, render_quality="high")  # noqa: SLF001

    assert params.output_format == "jpeg"
    assert params.output_compression == 100
    assert params.fast is False


def test_workflow_image_params_default_to_non_fast_high_quality_for_showcase() -> None:
    params = workflows._image_params(  # noqa: SLF001
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        final_quality="high",
    )

    assert params.fast is False
    assert params.render_quality == "high"
    assert params.fixed_size == "1600x2000"


def test_showcase_refs_use_product_images_when_no_accessory_preview() -> None:
    refs = workflows._showcase_reference_image_ids(  # noqa: SLF001
        product_image_ids=["product-1", "product-2"],
        model_image_id="model-1",
        selected_accessory_image_id=None,
    )

    assert refs == ["product-1", "product-2", "model-1"]


def test_showcase_refs_use_accessory_preview_instead_of_product_images() -> None:
    refs = workflows._showcase_reference_image_ids(  # noqa: SLF001
        product_image_ids=["product-1", "product-2"],
        model_image_id="model-1",
        selected_accessory_image_id="accessory-preview-1",
    )

    assert refs == ["product-1", "product-2", "accessory-preview-1"]
    assert "model-1" not in refs


def test_showcase_regeneration_target_keeps_existing_outputs() -> None:
    assert (
        workflows._showcase_target_image_count(  # noqa: SLF001
            existing_image_ids=["old-1", "old-2", "old-2"],
            output_count=4,
        )
        == 6
    )
    assert (
        workflows._showcase_expected_image_count(  # noqa: SLF001
            showcase_input={"target_image_count": 8, "output_count": 4},
            fallback_task_count=12,
        )
        == 8
    )


def test_candidate_prompt_uses_clean_four_view_reference_without_text_labels() -> None:
    prompt = workflows._candidate_prompt(  # noqa: SLF001
        style_prompt="premium natural model",
        product_analysis={"category": "连衣裙"},
        candidate_index=2,
        avoid=[],
    )

    assert "warm ivory sleeveless top" in prompt
    assert "warm ivory shorts" in prompt
    assert "exactly four views" in prompt
    assert "front full body" in prompt
    assert "side full body" in prompt
    assert "back full body" in prompt
    assert "close-up headshot" in prompt
    assert "No text labels" in prompt
    assert "no height labels" in prompt


def test_age_direction_adapts_child_model_pose_and_expression() -> None:
    candidate_prompt = workflows._candidate_prompt(  # noqa: SLF001
        style_prompt="8岁儿童，活泼自然",
        product_analysis={"category": "童装"},
        candidate_index=1,
        avoid=[],
    )
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "8岁儿童，活泼自然"},
    )
    showcase_prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["颜色", "版型"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="lifestyle",
        shot_type="natural_pose",
        final_quality="high",
    )

    assert "around 8 years old" in candidate_prompt
    assert "around 128cm" in candidate_prompt
    assert "age-appropriate" in candidate_prompt
    assert "non-adultized" in candidate_prompt
    assert "已确认模特参考图" in showcase_prompt
    assert "智能生活场景" in showcase_prompt
    assert "不要纯灰棚拍" in showcase_prompt
    assert "同一张脸" in showcase_prompt
    assert "身材比例" in showcase_prompt
    assert "肢体长度" in showcase_prompt


def test_showcase_prompt_uses_user_direction_for_scene_and_action() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean cold commute model"},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={
            "must_preserve": ["lapel shape", "button position", "pocket placement"],
        },
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={
            "enabled": True,
            "items": ["small earrings"],
            "strength": "subtle",
        },
        template="premium_studio",
        shot_type="front_full_body",
        final_quality="high",
        user_prompt="咖啡馆窗边，自然走动回头",
    )

    assert "请根据白底产品图和已确认模特参考图" in prompt
    assert "真实自然的真人模特穿搭电商图" in prompt
    assert "REFERENCE USE:" in prompt
    assert "SCENE:" in prompt
    assert "CAMERA / PHOTO STYLE:" in prompt
    assert "SHOT:" in prompt
    assert "OUTPUT:" in prompt
    assert "咖啡馆窗边，自然走动回头" in prompt
    assert "模板强约束：高级灰棚拍" in prompt
    assert "必须保留：lapel shape、button position、pocket placement" in prompt
    assert "配饰只参考已提供的商品/饰品搭配参考图" in prompt
    assert "不要额外新增、替换或强化配饰" in prompt
    assert "不要让配饰遮挡衣服主体" in prompt
    assert "超写实" in prompt
    assert "真实 Canon 相机商业摄影风格" in prompt
    assert "Real Canon full-frame commercial fashion photography" in prompt
    assert "realistic lens rendering" in prompt
    assert "true-to-life skin texture" in prompt
    assert "自然商业摄影风格" in prompt
    assert "细节清晰" in prompt
    assert "适合亚马逊/电商主图" in prompt
    assert "透视、地面接触、脚下阴影、反射、环境光和色温一致" in prompt
    assert "不要像抠图贴到背景上" in prompt
    assert "中等景深" in prompt
    assert "不要大光圈虚化、强 bokeh 或背景过度模糊" in prompt
    assert "主图" in prompt
    assert "standing front-facing" in prompt
    assert "文字、水印" in prompt
    assert len(prompt) < 1800


def test_showcase_prompt_preserves_model_identity_height_and_limb_proportions() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "8岁儿童，活泼自然", "height_cm": 128},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["颜色", "版型"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="lifestyle",
        shot_type="natural_pose",
        final_quality="high",
    )

    assert "同一张脸" in prompt
    assert "发型" in prompt
    assert "身高约 128cm" in prompt
    assert "身材比例" in prompt
    assert "肢体长度" in prompt
    assert "腿长" in prompt
    assert "不要换人" in prompt
    assert "不能成人化" in prompt
    assert "不能性感化" in prompt


def test_showcase_prompt_uses_quality_mode_variable() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean ecommerce model", "height_cm": 168},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="white_ecommerce",
        shot_type="front_full_body",
        final_quality="4k",
    )

    assert "画质：4K 终稿" in prompt
    assert "不要额外新增、替换或强化配饰" in prompt


def test_showcase_prompts_assign_distinct_actions_per_shot() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model", "height_cm": 168},
    )
    prompts = {
        shot: workflows._showcase_prompt(  # noqa: SLF001
            product_analysis={},
            selected_candidate=candidate,  # type: ignore[arg-type]
            accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
            template="premium_studio",
            shot_type=shot,
            final_quality="high",
        )
        for shot in workflows.DEFAULT_SHOT_PLAN
    }

    assert "standing front-facing" in prompts["front_full_body"]
    assert "natural walking or turning" in prompts["natural_pose"]
    assert "half-body detail pose" in prompts["detail_half_body"]
    assert "side or back three-quarter pose" in prompts["side_or_back"]
    assert len(set(prompts.values())) == len(workflows.DEFAULT_SHOT_PLAN)


def test_accessory_preview_prompt_is_model_quad_with_accessories_only() -> None:
    prompt = workflows._accessory_preview_prompt(  # noqa: SLF001
        accessory_plan={"items": ["small earrings", "white sneakers"], "strength": "subtle"},
        style_prompt="natural clean styling",
    )

    assert "已确认模特四宫格参考图" in prompt
    assert "白底模特配饰四宫格参考图" in prompt
    assert "正面全身、侧面全身、背面全身、近景头像" in prompt
    assert "不要穿商品图中的衣服" in prompt
    assert "不要出现任何商品服饰" in prompt
    assert "只在模特身上加入所选配饰" in prompt


def test_lifestyle_template_uses_product_matched_scene_and_integration() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model"},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={
            "category": "西装外套",
            "color": "深灰色",
            "material_guess": "羊毛混纺",
            "silhouette": "修身通勤",
            "must_preserve": ["深灰色", "西装驳领"],
        },
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="lifestyle",
        shot_type="front_full_body",
        final_quality="high",
        user_prompt="美术馆长廊，轻松侧身",
    )

    assert "美术馆长廊，轻松侧身" in prompt
    assert "智能生活场景" in prompt
    assert "不要纯灰棚拍" in prompt
    assert "不要纯色背景" in prompt
    assert "不要白底" in prompt
    assert "真实环境线索" in prompt
    assert "西装外套" in prompt
    assert "羊毛混纺" not in prompt
    assert "boutique hotel lobby" not in prompt


def test_revision_prompt_is_short_repair_brief() -> None:
    candidate = SimpleNamespace(id="cand-1")

    prompt = workflows._revision_prompt(  # noqa: SLF001
        instruction="衣服颜色更接近商品图",
        product_analysis={"must_preserve": ["米白色", "宽松版型"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
    )

    assert "返修" in prompt
    assert "保持已确认模特" in prompt
    assert "不要改款" in prompt
    assert "米白色" in prompt
    assert "衣服颜色更接近商品图" in prompt


def test_quality_review_prompt_focuses_on_core_ecommerce_checks() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model"},
    )

    prompt = workflows._quality_review_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["领口", "纽扣"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        shot_type="front_full_body",
    )

    assert "自动质检" in prompt
    assert "是否还是同一件商品" in prompt
    assert "模特人脸" in prompt
    assert "电商主图" in prompt
    assert "只返回严格 JSON" in prompt
    assert "approve 或 revise" in prompt


@pytest.mark.asyncio
async def test_quality_review_tasks_are_not_auto_created() -> None:
    bundles = await workflows._ensure_quality_review_tasks(  # noqa: SLF001
        None,  # type: ignore[arg-type]
        user=None,  # type: ignore[arg-type]
        conv=None,  # type: ignore[arg-type]
        run=None,  # type: ignore[arg-type]
        showcase_step=SimpleNamespace(image_ids=["image-1"]),
        quality_step=SimpleNamespace(output_json={}, task_ids=[]),
    )

    assert bundles == []


def test_quality_summary_merge_preserves_review_task_map() -> None:
    report = SimpleNamespace(overall_score=88, recommendation="approve")

    payload = workflows._merge_quality_summary_payload(  # noqa: SLF001
        {
            "review_tasks": {"image-1": "completion-1"},
            "review_task_count": 1,
        },
        [report],  # type: ignore[list-item]
    )

    assert payload["overall"] == "approve"
    assert payload["image_count"] == 1
    assert payload["average_score"] == 88.0
    assert payload["review_tasks"] == {"image-1": "completion-1"}
    assert payload["review_task_count"] == 1
