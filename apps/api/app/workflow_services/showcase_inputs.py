"""Showcase request validation, workflow seeding, and analysis prompts."""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.models import Generation, Image, WorkflowRun, WorkflowStep

from .showcase_runtime import runtime as _runtime


def _showcase_reference_image_ids(
    *,
    product_image_ids: Iterable[str],
    model_image_id: str | None,
    selected_accessory_image_id: str | None,
) -> list[str]:
    runtime = _runtime()
    model_reference_id = selected_accessory_image_id or model_image_id
    return runtime._dedupe_nonempty(
        [
            *product_image_ids,
            model_reference_id or "",
        ]
    )


async def _validate_accessory_preview_image(
    db: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    approval_step: WorkflowStep,
    image_id: str,
) -> str:
    runtime = _runtime()
    if image_id not in set(runtime._dedupe_nonempty(approval_step.image_ids or [])):
        raise runtime._http(
            "invalid_accessory_image", "selected accessory preview is invalid", 400
        )
    valid_image_id = (
        await db.execute(
            select(Image.id)
            .join(Generation, Generation.id == Image.owner_generation_id)
            .where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
                Generation.user_id == user_id,
                Generation.upstream_request["workflow_run_id"].astext == run_id,
                Generation.upstream_request["workflow_step_key"].astext
                == "model_approval",
                or_(
                    Generation.upstream_request["workflow_action"].astext
                    == "accessory_preview",
                    Generation.upstream_request["workflow_action"].astext.is_(None),
                ),
            )
        )
    ).scalar_one_or_none()
    if valid_image_id is None:
        raise runtime._http(
            "invalid_accessory_image", "selected accessory preview is invalid", 400
        )
    return str(valid_image_id)


def _showcase_target_image_count(
    *,
    existing_image_ids: Iterable[str],
    output_count: int,
) -> int:
    return len(_runtime()._dedupe_nonempty(existing_image_ids)) + output_count


async def _validate_owned_images(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
    min_count: int = 1,
    max_count: int | None = None,
) -> list[str]:
    runtime = _runtime()
    ids = runtime._dedupe_nonempty(image_ids)
    if len(ids) < min_count:
        raise runtime._http(
            "missing_image",
            f"at least {min_count} image required",
            422,
        )
    if max_count is not None and len(ids) > max_count:
        raise runtime._http(
            "too_many_images",
            f"at most {max_count} images allowed",
            422,
        )
    rows = (
        (
            await db.execute(
                select(Image.id).where(
                    Image.id.in_(ids),
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if set(rows) != set(ids):
        raise runtime._http(
            "invalid_image",
            "one or more images are not owned by the current user or were deleted",
            400,
        )
    return ids


def _seed_steps(run: WorkflowRun, *, user_prompt: str) -> list[WorkflowStep]:
    runtime = _runtime()
    steps: list[WorkflowStep] = []
    for key in runtime.WORKFLOW_STEPS:
        status = "waiting_input"
        input_json: dict[str, Any] = {}
        output_json: dict[str, Any] = {}
        if key == "upload_product":
            status = "approved"
            input_json = {
                "product_image_ids": run.product_image_ids,
                "user_prompt": user_prompt,
            }
            output_json = {"confirmed": True}
        elif key == "product_analysis":
            status = "running"
            input_json = {
                "product_image_ids": run.product_image_ids,
                "user_prompt": user_prompt,
                "prompt_contract": "extract visible apparel constraints as structured JSON",
            }
        steps.append(
            WorkflowStep(
                workflow_run_id=run.id,
                step_key=key,
                status=status,
                input_json=input_json,
                output_json=output_json,
            )
        )
    return steps


def _product_analysis_prompt(user_prompt: str) -> str:
    return (
        "请分析上传的服饰白底商品图。这个步骤只服务后续生成真人模特穿搭图，"
        "不要写复杂营销文案，只提取最终提示词真正需要的信息。只描述图片中可见信息，"
        "不确定填 unknown。"
        "必须只返回一个 JSON object，不要 Markdown，不要代码块，不要解释文字。"
        "字段固定为：category, color, material_guess, silhouette, "
        "key_details, must_preserve, styling_recommendations, background_recommendation, risks。"
        "除 unknown 和字段名外，内容用简体中文。must_preserve 只列 3-8 个后续生成必须完全还原的"
        "视觉点，例如颜色、版型、领口、袖型、衣长、面料观感、图案/logo、纽扣、口袋、拉链、缝线/拼接。"
        "styling_recommendations 只给 1-3 个低存在感、适合商品和用户方向的配饰/搭配建议，"
        "用来让整体更搭配，不要遮挡衣服主体。background_recommendation 给 1 句与衣服风格匹配的"
        "开放式背景氛围建议，不要列具体地点或具体空间名，适合亚马逊/电商主图。risks 只列会影响商品还原的风险。"
        f"用户方向：{user_prompt or '高级电商服饰模特展示图'}"
    )


def _candidate_prompt(
    *,
    style_prompt: str,
    product_analysis: dict[str, Any],
    candidate_index: int,
    avoid: list[str],
) -> str:
    runtime = _runtime()
    product_category = str(product_analysis.get("category") or "adult apparel")
    base_styling = "warm ivory sleeveless top and warm ivory shorts, barefoot"
    style = style_prompt.strip() or "clean premium ecommerce model, refined, natural"
    age_requirement = runtime._age_direction(style)
    height_requirement = runtime._height_requirement(style)
    avoid_text = ", ".join(item.strip() for item in avoid if item and item.strip())
    avoid_line = f"Avoid: {avoid_text}." if avoid_text else ""
    diversity = runtime._model_diversity_anchor(
        candidate_index=candidate_index,
        gender=runtime._infer_candidate_gender(style_prompt, product_analysis),
    )
    return " ".join(
        part
        for part in [
            "Create one clean 2x2 ecommerce model reference contact sheet, exactly four panels: "
            "top-left front full body, top-right left 90-degree profile full body, "
            "bottom-left straight back full body, bottom-right close-up headshot.",
            "Same model in all four panels, consistent framing, "
            "same camera height and distance for the three full-body views.",
            "Side panel must be a true left profile (only one eye visible, "
            "body fully sideways, not a three-quarter pose).",
            "Back panel must hide the face. Headshot must be straight frontal with both eyes visible.",
            "Plain seamless white or light gray studio background, soft even lighting, "
            "no props, no text labels.",
            "Real commercially photographed person, not an AI beauty render.",
            "The model is not wearing the user's product yet.",
            f"Use simple neutral base clothing: {base_styling}.",
            "Every candidate must wear this exact same outfit; "
            "only face, hair, and body type may differ between candidates.",
            f"{age_requirement} {height_requirement}".strip(),
            diversity,
            "No text labels, no height labels, no watermark, no logo, no celebrity likeness.",
            f"Style direction: {style}.",
            f"Product category context: {product_category}.",
            f"Candidate variation number: {candidate_index}.",
            avoid_line,
        ]
        if part
    ).strip()
