"""Structured workflow routes.

The apparel model showcase workflow is a project-style layer on top of the
existing durable image/text task system. Endpoints here own stage state and
approvals; generations/completions still run through the same worker queues so
refreshing or closing the browser does not lose progress.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    CompletionStatus,
    GenerationStatus,
    Intent,
    MessageStatus,
    Role,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    ModelCandidate,
    new_uuid7,
    OutboxEvent,
    QualityReport,
    User,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.schemas import (
    AccessoryPlanIn,
    AccessoryPreviewCreateIn,
    AccessorySelectionIn,
    ApparelWorkflowCreateIn,
    ApparelWorkflowCreateOut,
    ChatParamsIn,
    GenerationOut,
    ImageOut,
    ImageParamsIn,
    ImageRevisionIn,
    ModelCandidateApproveIn,
    ModelCandidatesCreateIn,
    ModelCandidateOut,
    ProductAnalysisApproveIn,
    QualityReportOut,
    ShowcaseImagesCreateIn,
    WorkflowRunListItemOut,
    WorkflowRunListOut,
    WorkflowRunOut,
    WorkflowStepOut,
)

from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..redis_client import get_redis
from .messages import (
    _create_assistant_task,
    _publish_assistant_task,
    _publish_message_appended,
)


router = APIRouter(prefix="/workflows", tags=["workflows"])
logger = logging.getLogger(__name__)


WORKFLOW_TYPE = "apparel_model_showcase"
WORKFLOW_STEPS = [
    "upload_product",
    "product_analysis",
    "model_settings",
    "model_candidates",
    "model_approval",
    "showcase_generation",
    "quality_review",
    "delivery",
]
MODEL_CANDIDATE_COUNT = 3
DEFAULT_SHOT_PLAN = [
    "front_full_body",
    "natural_pose",
    "detail_half_body",
    "side_or_back",
]

STEP_LABELS = {
    "upload_product": "上传商品",
    "product_analysis": "商品理解",
    "model_settings": "模特设定",
    "model_candidates": "模特候选",
    "model_approval": "方案确认",
    "showcase_generation": "商品融合",
    "quality_review": "质检返修",
    "delivery": "交付",
}

SHOT_LABELS = {
    "front_full_body": "主图：干净背景，全身正面，突出商品轮廓",
    "natural_pose": "姿态图：自然站姿或轻微动态，展示穿着氛围",
    "detail_half_body": "细节图：半身或局部，突出面料和结构细节",
    "side_or_back": "侧背图：侧面或背面角度，展示版型和长度",
}

SHOT_COMPOSITION_REQUIREMENTS = {
    "front_full_body": (
        "Composition requirement: full-body, head-to-toe framing, the complete "
        "model must be visible from top of head to shoes/feet, no cropping at "
        "head, hands, legs, knees, ankles, or feet, enough margin around the body."
    ),
    "natural_pose": (
        "Composition requirement: full-body or near full-body natural pose, keep "
        "head-to-toe readability whenever possible, do not crop feet or head, "
        "show the garment on the complete body silhouette."
    ),
    "detail_half_body": (
        "Composition requirement: half-body or detail framing is allowed, but the "
        "garment structure must remain readable and not be obscured."
    ),
    "side_or_back": (
        "Composition requirement: side or back angle with full outfit readability; "
        "prefer full-body framing and avoid cropping feet/head unless absolutely "
        "necessary for the angle."
    ),
}

SHOT_ACTION_REQUIREMENTS = {
    "front_full_body": (
        "Action requirement: standing front-facing with a relaxed ecommerce pose, "
        "arms naturally at the sides or lightly angled, clearly showing the garment front."
    ),
    "natural_pose": (
        "Action requirement: natural walking or turning pose with gentle movement, "
        "different from the front-facing main image, while keeping the garment readable."
    ),
    "detail_half_body": (
        "Action requirement: half-body detail pose, one hand may lightly adjust cuff, "
        "collar, pocket, or hem without covering key product details."
    ),
    "side_or_back": (
        "Action requirement: side or back three-quarter pose, looking slightly away "
        "or over shoulder, clearly showing side/back structure and length."
    ),
}

TEMPLATE_LABELS = {
    "white_ecommerce": "白底电商图",
    "premium_studio": "高级灰棚拍",
    "urban_commute": "城市通勤场景",
    "lifestyle": "自然生活场景",
    "social_seed": "社媒种草图",
}

def _template_requirement(template: str, product_analysis: dict[str, Any]) -> str:
    _ = product_analysis
    return TEMPLATE_LABELS.get(template, template)


def _showcase_prompt_brief(
    *,
    user_direction: str,
    template_direction: str,
    product_preserve: str,
    accessory_direction: str,
    model_consistency: str,
    shot_direction: str,
    quality_direction: str,
) -> str:
    direction_parts = [part for part in (user_direction.strip(), template_direction.strip()) if part]
    direction = "，".join(direction_parts) or "高级自然电商场景，动作自然"
    return (
        "请根据白底产品图和已确认模特参考图，生成真实自然的真人模特穿搭电商图。"
        "核心要求："
        "1. 模特必须穿着产品图中的同一件衣服，严格还原商品服饰，不要改款、不要改变颜色、"
        f"不要添加不存在的服装细节。必须保留：{product_preserve}。"
        f"2. {model_consistency}"
        f"3. {accessory_direction}"
        f"4. 场景选择与衣服风格和用户方向匹配的干净商业摄影背景。参考方向：{direction}。"
        "背景简洁高级，不杂乱，不抢主体，整体适合亚马逊/电商主图。"
        f"5. {shot_direction}"
        f"6. 画质：{quality_direction}，超写实，自然商业摄影风格，细节清晰，光线真实，干净高级。"
        "7. 画面中不要出现文字、水印、logo 水印、畸形手脚、多余人物、衣架、假人或不自然背景物体。"
    )


def _infer_age(text: str) -> int | None:
    lowered = (text or "").lower()
    age_match = re.search(r"(\d{1,2})\s*(?:岁|-year-old|year old|yo)", lowered)
    if age_match:
        try:
            return int(age_match.group(1))
        except ValueError:
            return None
    return None


def _infer_model_height_cm(text: str) -> int:
    age = _infer_age(text)
    if age is None:
        lowered = (text or "").lower()
        if any(word in lowered for word in ("儿童", "童装", "小朋友", "孩子", "kid", "kids", "child")):
            return 128
        return 168
    if age <= 2:
        return 90
    if age <= 12:
        return 80 + age * 6
    if age <= 17:
        return min(168, 128 + (age - 12) * 7)
    return 168


def _height_requirement(text: str) -> str:
    height_cm = _infer_model_height_cm(text)
    return (
        f"Keep the model's perceived height around {height_cm}cm, with consistent "
        "head-to-body ratio, limb length, and scale across all reference views and later images."
    )


def _age_direction(text: str) -> str:
    lowered = (text or "").lower()
    age = _infer_age(text)
    if age is not None and age <= 12 or any(
        word in lowered for word in ("儿童", "童装", "小朋友", "孩子", "kid", "kids", "child")
    ):
        age_text = f"around {age} years old" if age is not None else "child age range"
        return (
            f"The model should be {age_text}, with age-appropriate body proportion, face, "
            "posture, expression, and styling. Keep the look natural and non-adultized."
        )
    if age is not None and age < 18:
        return (
            f"The model should be around {age} years old, with teen-appropriate proportions, "
            "expression, pose, and styling."
        )
    if age is not None:
        return (
            f"The model should look around {age} years old, with expression, pose, styling, "
            "and scene adjusted to that age."
        )
    return (
        "Follow the user's requested model type, age impression, expression, pose, and styling; "
        "if unspecified, choose a natural ecommerce model that fits the garment."
    )


def _accessory_age_direction(text: str) -> str:
    lowered = (text or "").lower()
    age = _infer_age(text)
    if age is not None and age <= 12 or any(
        word in lowered for word in ("儿童", "童装", "小朋友", "孩子", "kid", "kids", "child")
    ):
        return (
            "Accessory styling must be child-appropriate: simple, safe-looking, playful but restrained, "
            "with no adult jewelry styling, glamour accessories, mature handbags, heels, or adult fashion cues."
        )
    if age is not None and age < 18 or any(word in lowered for word in ("青少年", "teen", "teenager")):
        return (
            "Accessory styling must fit a teenager: casual, age-appropriate, not childish, and not adult glamour."
        )
    if age is not None:
        return (
            f"Accessory styling must match an adult around {age} years old: commercially polished, natural, "
            "and appropriate for that age and product category."
        )
    return (
        "Infer the target age from the user's direction and product context, then choose accessories that match "
        "that age group instead of assuming the model is always a child."
    )


class _PublishBundle:
    def __init__(
        self,
        *,
        assistant_msg_id: str,
        message_ids: list[str],
        outbox_payloads: list[dict[str, Any]],
        outbox_rows: list[OutboxEvent],
    ) -> None:
        self.assistant_msg_id = assistant_msg_id
        self.message_ids = message_ids
        self.outbox_payloads = outbox_payloads
        self.outbox_rows = outbox_rows


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dedupe_nonempty(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        v = value.strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _showcase_reference_image_ids(
    *,
    product_image_ids: Iterable[str],
    model_image_id: str | None,
    selected_accessory_image_id: str | None,
) -> list[str]:
    product_or_accessory_ids = (
        [selected_accessory_image_id] if selected_accessory_image_id else product_image_ids
    )
    return _dedupe_nonempty([*product_or_accessory_ids, model_image_id or ""])


def _showcase_target_image_count(
    *,
    existing_image_ids: Iterable[str],
    output_count: int,
) -> int:
    return len(_dedupe_nonempty(existing_image_ids)) + output_count


def _showcase_expected_image_count(
    *,
    showcase_input: dict[str, Any],
    fallback_task_count: int,
) -> int:
    return int(
        showcase_input.get("target_image_count")
        or showcase_input.get("output_count")
        or fallback_task_count
    )


async def _validate_owned_images(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
    min_count: int = 1,
    max_count: int | None = None,
) -> list[str]:
    ids = _dedupe_nonempty(image_ids)
    if len(ids) < min_count:
        raise _http("missing_image", f"at least {min_count} image required", 422)
    if max_count is not None and len(ids) > max_count:
        raise _http("too_many_images", f"at most {max_count} images allowed", 422)
    rows = (
        await db.execute(
            select(Image.id).where(
                Image.id.in_(ids),
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    if set(rows) != set(ids):
        raise _http(
            "invalid_image",
            "one or more images are not owned by the current user or were deleted",
            400,
        )
    return ids


async def _get_owned_conversation(
    db: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
) -> Conversation:
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise _http("not_found", "conversation not found", 404)
    return conv


async def _get_or_create_workflow_conversation(
    db: AsyncSession,
    *,
    user: User,
    conversation_id: str | None,
    title: str,
) -> Conversation:
    if conversation_id:
        return await _get_owned_conversation(db, user_id=user.id, conversation_id=conversation_id)
    conv = Conversation(
        user_id=user.id,
        title=title,
        archived=True,
        default_params={"workflow_type": WORKFLOW_TYPE},
    )
    db.add(conv)
    await db.flush()
    return conv


async def _get_run(
    db: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    lock: bool = False,
) -> WorkflowRun:
    stmt = select(WorkflowRun).where(
        WorkflowRun.id == run_id,
        WorkflowRun.user_id == user_id,
        WorkflowRun.deleted_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    run = (await db.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise _http("not_found", "workflow not found", 404)
    return run


async def _load_steps(db: AsyncSession, run_id: str) -> list[WorkflowStep]:
    rows = (
        await db.execute(
            select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id)
        )
    ).scalars().all()
    order = {key: idx for idx, key in enumerate(WORKFLOW_STEPS)}
    return sorted(rows, key=lambda s: order.get(s.step_key, 999))


async def _step(db: AsyncSession, run_id: str, step_key: str) -> WorkflowStep:
    row = (
        await db.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run_id,
                WorkflowStep.step_key == step_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("workflow_corrupt", f"missing workflow step: {step_key}", 500)
    return row


def _seed_steps(run: WorkflowRun, *, user_prompt: str) -> list[WorkflowStep]:
    steps: list[WorkflowStep] = []
    for key in WORKFLOW_STEPS:
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
        "请分析上传的服饰商品图，只描述图片中可见信息，不确定填 unknown。"
        "返回严格 JSON，字段固定为：category, color, material_guess, silhouette, "
        "key_details, must_preserve, risks, styling_recommendations。"
        "除 unknown 和字段名外，内容用简体中文。must_preserve 列出后续生成必须还原的"
        "颜色、廓形、领口、袖型、长度、面料观感、图案/logo、纽扣、口袋、拉链、缝线/拼接等可见细节。"
        "styling_recommendations 只给 1-3 个低存在感、适合商品和用户方向的搭配建议，"
        "不需要覆盖所有饰品类别，并根据用户指定的人群/年龄保持搭配合适。"
        f"用户方向：{user_prompt or '高级电商服饰模特展示图'}"
    )


def _candidate_prompt(
    *,
    style_prompt: str,
    product_analysis: dict[str, Any],
    candidate_index: int,
    avoid: list[str],
) -> str:
    product_category = str(product_analysis.get("category") or "adult apparel")
    base_styling = "uniform warm ivory sleeveless top and warm ivory shorts, barefoot"
    style = style_prompt.strip() or "clean premium ecommerce model, refined, natural"
    age_requirement = _age_direction(style)
    height_requirement = _height_requirement(style)
    avoid_text = ", ".join(avoid) if avoid else "celebrity likeness, influencer likeness, dramatic makeup"
    return (
        "Create one clean ecommerce model reference sheet with exactly four views: "
        "front full body, side full body, back full body, and close-up headshot. "
        "Use the same synthetic model in every view, keeping face, hairstyle, body proportion, "
        "skin tone, and lighting consistent. The model is not wearing the user's product yet. "
        f"Use simple neutral base clothing: {base_styling}. Do not add any product-specific garment details. "
        f"{age_requirement} {height_requirement} "
        "No text labels, no height labels, no watermark, no logo, no celebrity or real-person likeness. "
        f"Style direction: {style}. Product category context: {product_category}. "
        f"Candidate variation number: {candidate_index}. Avoid: {avoid_text}."
    )


def _showcase_prompt(
    *,
    product_analysis: dict[str, Any],
    selected_candidate: ModelCandidate,
    accessory_plan: dict[str, Any],
    template: str,
    shot_type: str,
    final_quality: str,
    user_prompt: str = "",
) -> str:
    brief = selected_candidate.model_brief_json or {}
    height_cm = brief.get("height_cm")
    height_text = f"身高约 {height_cm}cm，" if isinstance(height_cm, int) else ""
    summary = str(brief.get("summary") or user_prompt or "自然电商模特")
    must_preserve = product_analysis.get("must_preserve")
    product_preserve = (
        "、".join(str(item) for item in must_preserve if str(item).strip())
        if isinstance(must_preserve, list)
        else "颜色、版型、领口、袖型、衣长、面料质感、图案/logo、纽扣、口袋、拉链、缝线和所有可见结构"
    )
    model_consistency = (
        "严格保持已确认模特参考图中的同一张脸、发型、肤色、年龄感、"
        f"{height_text}身材比例、肢体长度、肩宽、腿长和整体体态一致，不要换人。"
        f"模特方向：{summary}。若为儿童或青少年，必须保持年龄合适、自然活泼，不能成人化、不能性感化。"
    )
    _ = accessory_plan
    accessory_direction = (
        "配饰只参考已提供的商品/饰品搭配参考图；如果参考图中已有配饰，则保持其风格和位置关系。"
        "不要额外新增、替换或强化配饰，不要让配饰遮挡衣服主体，不要改变商品本身。"
    )
    shot_label = SHOT_LABELS.get(shot_type, shot_type)
    shot_action = SHOT_ACTION_REQUIREMENTS.get(shot_type, "")
    shot_composition = SHOT_COMPOSITION_REQUIREMENTS.get(shot_type, "")
    shot_direction = (
        f"本张镜头：{shot_label}。{shot_action} {shot_composition} "
        "This shot must use a distinct pose/action from the other generated showcase images. "
    )
    quality_direction = "4K 终稿" if final_quality == "4k" else "2K 高质量"
    return _showcase_prompt_brief(
        user_direction=user_prompt,
        template_direction=_template_requirement(template, product_analysis),
        product_preserve=product_preserve,
        accessory_direction=accessory_direction,
        model_consistency=model_consistency,
        shot_direction=shot_direction,
        quality_direction=quality_direction,
    )


def _revision_prompt(
    *,
    instruction: str,
    product_analysis: dict[str, Any],
    selected_candidate: ModelCandidate,
) -> str:
    must_preserve = product_analysis.get("must_preserve")
    preserve = ", ".join(str(x) for x in must_preserve) if isinstance(must_preserve, list) else ""
    return (
        "请根据用户要求返修这张服饰电商模特图。保持已确认模特的人脸、发型、身材比例和整体身份不变；"
        "保持商品为同一件衣服，不要改款。需要保留的商品细节："
        f"{preserve or '颜色、版型、领口、袖型、长度、logo/图案、口袋、纽扣、缝线'}。"
        f"返修要求：{instruction}。参考模特方案：{selected_candidate.id}。"
    )


def _accessory_preview_prompt(
    *,
    accessory_plan: dict[str, Any],
    style_prompt: str,
) -> str:
    items = accessory_plan.get("items")
    item_text = ", ".join(str(item) for item in items) if isinstance(items, list) else ""
    strength = str(accessory_plan.get("strength") or "subtle")
    return (
        "请根据上传的原商品图，生成一张白底平面商品搭配预览图。画面中只能出现原商品和所选饰品，"
        "不要出现模特、人体、衣架、假人、场景、家具或多余道具。保持原商品的颜色、版型、材质观感和可见细节，"
        "不要改款，不要让饰品遮挡商品关键结构。饰品应与原商品自然摆放在同一张白底商品图中，"
        "像电商商品搭配图或平铺图，构图干净，主体完整，适合用户确认搭配。"
        f"Accessories: {item_text or 'no accessories'}. Styling strength: {strength}. "
        f"Additional direction: {style_prompt or 'clean ecommerce styling'}."
    )


def _quality_review_prompt(
    *,
    product_analysis: dict[str, Any],
    selected_candidate: ModelCandidate,
    shot_type: str | None,
) -> str:
    must_preserve = product_analysis.get("must_preserve")
    preserve = (
        ", ".join(str(x) for x in must_preserve)
        if isinstance(must_preserve, list)
        else "garment color, silhouette, neckline, sleeve shape, length, logo, pattern, buttons, pockets, zippers, seams"
    )
    brief = selected_candidate.model_brief_json or {}
    return (
        "请对生成的服饰电商模特图做自动质检。对比商品参考图、已确认模特参考图和最终图，检查："
        "1. 是否还是同一件商品，颜色、版型和关键细节是否保留；"
        "2. 模特人脸、发型、身材比例和年龄感是否接近确认方案；"
        "3. 手、脚、脸、衣服边缘、背景是否有明显瑕疵；"
        "4. 是否适合作为电商主图使用。"
        "只返回严格 JSON，字段：overall_score, product_fidelity_score, model_consistency_score, "
        "aesthetic_score, artifact_score, issues, recommendation。分数 0-100，recommendation 只能是 approve 或 revise。"
        f"必须保留：{preserve}。镜头类型：{shot_type or 'unknown'}。"
        f"已确认模特摘要：{brief.get('summary') or 'synthetic ecommerce model'}。"
    )


def _try_parse_json_text(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"summary": value}
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(raw[start : end + 1])
                return value if isinstance(value, dict) else {"summary": value}
            except json.JSONDecodeError:
                pass
    return {
        "category": "unknown",
        "color": "unknown",
        "material_guess": "unknown",
        "silhouette": "unknown",
        "key_details": [],
        "must_preserve": ["颜色", "廓形", "可见商品细节"],
        "risks": ["模型没有返回结构化 JSON，请人工复核文本摘要"],
        "summary_text": raw,
    }


def _clamp_score(value: Any, default: int = 0) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        score = default
    return max(0, min(100, score))


def _quality_payload_from_text(text: str) -> dict[str, Any]:
    parsed = _try_parse_json_text(text)
    issues = parsed.get("issues")
    if not isinstance(issues, list):
        issues = [
            {
                "severity": "medium",
                "type": "quality_review",
                "message": str(parsed.get("summary_text") or text or "QC review did not return issue details."),
            }
        ]
    recommendation = str(parsed.get("recommendation") or "review").strip().lower()
    if recommendation not in {"approve", "revise"}:
        recommendation = "revise"
    return {
        "overall_score": _clamp_score(parsed.get("overall_score"), 70),
        "product_fidelity_score": _clamp_score(parsed.get("product_fidelity_score"), 70),
        "model_consistency_score": _clamp_score(parsed.get("model_consistency_score"), 70),
        "aesthetic_score": _clamp_score(parsed.get("aesthetic_score"), 70),
        "artifact_score": _clamp_score(parsed.get("artifact_score"), 70),
        "issues_json": [item for item in issues if isinstance(item, dict)] or [
            {
                "severity": "medium",
                "type": "quality_review",
                "message": "QC review returned no structured issues.",
            }
        ],
        "recommendation": recommendation,
    }


async def _create_workflow_task(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    intent: Intent,
    text: str,
    attachment_ids: list[str],
    idempotency_key: str,
    workflow_run_id: str,
    workflow_step_key: str,
    image_params: ImageParamsIn | None = None,
    chat_params: ChatParamsIn | None = None,
    workflow_meta: dict[str, Any] | None = None,
) -> tuple[_PublishBundle, str | None, list[str]]:
    user_msg = Message(
        conversation_id=conv.id,
        role=Role.USER.value,
        content={
            "text": text,
            "attachments": [{"image_id": image_id} for image_id in attachment_ids],
            "workflow_run_id": workflow_run_id,
            "workflow_step_key": workflow_step_key,
        },
        intent=None,
        status=None,
    )
    db.add(user_msg)
    await db.flush()

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        conv=conv,
        user_msg=user_msg,
        intent=intent,
        idempotency_key=idempotency_key[:64],
        image_params=image_params or ImageParamsIn(),
        chat_params=chat_params or ChatParamsIn(),
        system_prompt=None,
        attachment_ids=attachment_ids,
        text=text,
    )

    meta = {
        "workflow_run_id": workflow_run_id,
        "workflow_type": WORKFLOW_TYPE,
        "workflow_step_key": workflow_step_key,
        **(workflow_meta or {}),
    }
    if result.completion_id:
        comp = await db.get(Completion, result.completion_id)
        if comp is not None:
            req = dict(comp.upstream_request or {})
            req.update(meta)
            comp.upstream_request = req
    for generation_id in result.generation_ids:
        gen = await db.get(Generation, generation_id)
        if gen is not None:
            req = dict(gen.upstream_request or {})
            req.update(meta)
            gen.upstream_request = req

    bundle = _PublishBundle(
        assistant_msg_id=result.assistant_msg.id,
        message_ids=[user_msg.id, result.assistant_msg.id],
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )
    return bundle, result.completion_id, result.generation_ids


async def _publish_bundles(
    db: AsyncSession,
    *,
    user_id: str,
    conv_id: str,
    bundles: list[_PublishBundle],
) -> None:
    redis = get_redis()
    for bundle in bundles:
        await _publish_message_appended(
            redis=redis,
            user_id=user_id,
            conv_id=conv_id,
            message_ids=bundle.message_ids,
        )
        await _publish_assistant_task(
            db=db,
            redis=redis,
            user_id=user_id,
            conv_id=conv_id,
            assistant_msg_id=bundle.assistant_msg_id,
            outbox_payloads=bundle.outbox_payloads,
            outbox_rows=bundle.outbox_rows,
        )


def _fixed_size_for_quality(aspect_ratio: str, final_quality: str) -> str | None:
    if final_quality == "standard":
        return None
    high: dict[str, str] = {
        "1:1": "2048x2048",
        "4:5": "1600x2000",
        "3:4": "1536x2048",
        "4:3": "2048x1536",
        "16:9": "2560x1440",
        "9:16": "1440x2560",
        "3:2": "2016x1344",
        "2:3": "1344x2016",
        "21:9": "2688x1152",
        "9:21": "1152x2688",
    }
    four_k: dict[str, str] = {
        "1:1": "2880x2880",
        "4:5": "2560x3200",
        "3:4": "2448x3264",
        "4:3": "3264x2448",
        "16:9": "3840x2160",
        "9:16": "2160x3840",
        "3:2": "3504x2336",
        "2:3": "2336x3504",
        "21:9": "3808x1632",
        "9:21": "1632x3808",
    }
    return (four_k if final_quality == "4k" else high).get(aspect_ratio, high["4:5"])


def _image_params(
    *,
    aspect_ratio: str = "4:5",
    count: int = 1,
    render_quality: str = "high",
    final_quality: str | None = None,
    fast: bool = False,
) -> ImageParamsIn:
    fixed = _fixed_size_for_quality(aspect_ratio, final_quality or "high")
    return ImageParamsIn(
        aspect_ratio=aspect_ratio,  # type: ignore[arg-type]
        size_mode="fixed" if fixed else "auto",
        fixed_size=fixed,
        count=count,
        fast=fast,
        render_quality=render_quality,  # type: ignore[arg-type]
        output_format="jpeg",
        output_compression=100,
        background="opaque",
        moderation="low",
    )


def _merge_product_corrections(
    product_output: dict[str, Any],
    corrections: dict[str, Any],
) -> dict[str, Any]:
    final = dict(product_output or {})
    raw_corrections = corrections if isinstance(corrections, dict) else {}
    for key, value in raw_corrections.items():
        if value is not None:
            final[key] = value
    final["user_corrections"] = raw_corrections
    final["confirmed_at"] = _now().isoformat()
    return final


async def _sync_workflow_outputs(
    db: AsyncSession,
    run: WorkflowRun,
) -> None:
    steps = {step.step_key: step for step in await _load_steps(db, run.id)}

    product_step = steps.get("product_analysis")
    if product_step and product_step.status == "running" and product_step.task_ids:
        completion = (
            await db.execute(
                select(Completion)
                .where(Completion.id.in_(product_step.task_ids))
                .order_by(desc(Completion.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if completion is not None:
            if completion.status == CompletionStatus.SUCCEEDED.value:
                parsed = _try_parse_json_text(completion.text)
                product_step.output_json = parsed
                product_step.status = "needs_review"
                run.status = "needs_review"
                run.current_step = "product_analysis"
            elif completion.status == CompletionStatus.FAILED.value:
                product_step.status = "failed"
                product_step.output_json = {
                    "error_code": completion.error_code,
                    "error_message": completion.error_message,
                }
                run.status = "failed"

    candidates = list(
        (
            await db.execute(
                select(ModelCandidate)
                .where(ModelCandidate.workflow_run_id == run.id)
                .order_by(ModelCandidate.candidate_index.asc())
            )
        ).scalars().all()
    )
    if candidates:
        all_candidate_task_ids = [
            task_id for candidate in candidates for task_id in (candidate.task_ids or [])
        ]
        images_by_gen: dict[str, Image] = {}
        gens_by_id: dict[str, Generation] = {}
        bonus_gen_ids_by_parent: dict[str, list[str]] = {}
        bonus_parent_by_gen: dict[str, str] = {}
        if all_candidate_task_ids:
            base_generations = (
                await db.execute(
                    select(Generation).where(Generation.id.in_(all_candidate_task_ids))
                )
            ).scalars().all()
            bonus_generations = (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.user_id == run.user_id,
                        Generation.upstream_request["parent_generation_id"].astext.in_(
                            all_candidate_task_ids
                        ),
                        Generation.upstream_request["is_dual_race_bonus"].as_boolean()
                        == True,  # noqa: E712
                    )
                    .order_by(Generation.created_at.asc(), Generation.id.asc())
                )
            ).scalars().all()
            generations = [*base_generations, *bonus_generations]
            gens_by_id = {g.id: g for g in generations}
            for generation in bonus_generations:
                req = generation.upstream_request or {}
                parent_id = req.get("parent_generation_id") if isinstance(req, dict) else None
                if isinstance(parent_id, str) and parent_id:
                    bonus_gen_ids_by_parent.setdefault(parent_id, []).append(generation.id)
                    bonus_parent_by_gen[generation.id] = parent_id
            images = (
                await db.execute(
                    select(Image)
                    .where(
                        Image.owner_generation_id.in_([g.id for g in generations]),
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            ).scalars().all()
            for image in images:
                if (
                    image.owner_generation_id
                    and image.owner_generation_id not in images_by_gen
                ):
                    images_by_gen[image.owner_generation_id] = image
        for candidate in candidates:
            candidate_image_ids: list[str] = []
            for task_id in candidate.task_ids or []:
                image = images_by_gen.get(task_id)
                if image is not None:
                    candidate_image_ids.append(image.id)
            candidate_image_ids = _dedupe_nonempty(candidate_image_ids)
            if candidate_image_ids:
                brief = dict(candidate.model_brief_json or {})
                brief["candidate_image_ids"] = candidate_image_ids
                candidate.model_brief_json = brief
            if candidate.contact_sheet_image_id is None:
                if candidate_image_ids:
                    candidate.contact_sheet_image_id = candidate_image_ids[0]
            if candidate.contact_sheet_image_id and candidate.status == "generating":
                candidate.status = "ready"
            elif (
                candidate.status == "generating"
                and candidate.task_ids
                and all(
                    gens_by_id.get(task_id) is not None
                    and gens_by_id[task_id].status == GenerationStatus.FAILED.value
                    for task_id in candidate.task_ids
                )
            ):
                candidate.status = "failed"

        existing_bonus_gen_ids = {
            task_id
            for candidate in candidates
            for task_id in (candidate.task_ids or [])
            if task_id in bonus_parent_by_gen
        }
        next_index = max((c.candidate_index for c in candidates), default=0) + 1
        for parent_task_id, bonus_gen_ids in bonus_gen_ids_by_parent.items():
            parent_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if parent_task_id in (candidate.task_ids or [])
                ),
                None,
            )
            if parent_candidate is None:
                continue
            for bonus_gen_id in bonus_gen_ids:
                if bonus_gen_id in existing_bonus_gen_ids:
                    continue
                bonus_image = images_by_gen.get(bonus_gen_id)
                if bonus_image is None:
                    continue
                brief = dict(parent_candidate.model_brief_json or {})
                brief["candidate_image_ids"] = [bonus_image.id]
                brief["source_candidate_id"] = parent_candidate.id
                brief["source_generation_id"] = parent_task_id
                brief["is_dual_race_bonus"] = True
                bonus_candidate = ModelCandidate(
                    workflow_run_id=run.id,
                    candidate_index=next_index,
                    status="ready",
                    contact_sheet_image_id=bonus_image.id,
                    model_brief_json=brief,
                    task_ids=[bonus_gen_id],
                )
                db.add(bonus_candidate)
                candidates.append(bonus_candidate)
                existing_bonus_gen_ids.add(bonus_gen_id)
                next_index += 1

        candidate_step = steps.get("model_candidates")
        if candidate_step and candidate_step.status == "running":
            ready_count = sum(1 for c in candidates if c.status == "ready")
            failed_count = sum(1 for c in candidates if c.status == "failed")
            if ready_count >= MODEL_CANDIDATE_COUNT:
                candidate_step.status = "needs_review"
                candidate_step.image_ids = _dedupe_nonempty(
                    image_id
                    for c in candidates
                    for image_id in (
                        (c.model_brief_json or {}).get("candidate_image_ids")
                        if isinstance(
                            (c.model_brief_json or {}).get("candidate_image_ids"), list
                        )
                        else [c.contact_sheet_image_id]
                    )
                    if isinstance(image_id, str)
                )
                run.current_step = "model_approval"
                run.status = "needs_review"
                approval_step = steps.get("model_approval")
                if approval_step and approval_step.status == "waiting_input":
                    approval_step.status = "needs_review"
            elif failed_count and failed_count == len(candidates):
                candidate_step.status = "failed"
                run.status = "failed"

    showcase_step = steps.get("showcase_generation")
    quality_step = steps.get("quality_review")
    approval_step = steps.get("model_approval")
    if approval_step and approval_step.task_ids:
        accessory_base_generations = (
            await db.execute(
                select(Generation).where(Generation.id.in_(approval_step.task_ids))
            )
        ).scalars().all()
        accessory_bonus_generations = (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == run.user_id,
                    Generation.upstream_request["parent_generation_id"].astext.in_(
                        approval_step.task_ids
                    ),
                    Generation.upstream_request["is_dual_race_bonus"].as_boolean()
                    == True,  # noqa: E712
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        ).scalars().all()
        accessory_generations = [
            *accessory_base_generations,
            *accessory_bonus_generations,
        ]
        accessory_images = (
            await db.execute(
                select(Image)
                .where(
                    Image.owner_generation_id.in_(
                        [generation.id for generation in accessory_generations]
                    ),
                    Image.deleted_at.is_(None),
                )
                .order_by(Image.created_at.asc(), Image.id.asc())
            )
        ).scalars().all()
        if accessory_images:
            approval_step.image_ids = _dedupe_nonempty(image.id for image in accessory_images)
            if approval_step.status == "running":
                approval_step.status = "needs_review"
                run.status = "needs_review"
                run.current_step = "model_approval"
    if showcase_step and showcase_step.task_ids:
        base_generations = (
            await db.execute(
                select(Generation).where(Generation.id.in_(showcase_step.task_ids))
            )
        ).scalars().all()
        bonus_generations = (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == run.user_id,
                    Generation.upstream_request["parent_generation_id"].astext.in_(
                        showcase_step.task_ids
                    ),
                    Generation.upstream_request["is_dual_race_bonus"].as_boolean()
                    == True,  # noqa: E712
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        ).scalars().all()
        generations = [*base_generations, *bonus_generations]
        images = (
            await db.execute(
                select(Image)
                .where(
                    Image.owner_generation_id.in_([generation.id for generation in generations]),
                    Image.deleted_at.is_(None),
                )
                .order_by(Image.created_at.asc(), Image.id.asc())
            )
        ).scalars().all()
        image_ids = _dedupe_nonempty(image.id for image in images)
        if image_ids:
            showcase_step.image_ids = image_ids
        expected = _showcase_expected_image_count(
            showcase_input=showcase_step.input_json or {},
            fallback_task_count=len(showcase_step.task_ids),
        )
        succeeded = [
            generation
            for generation in generations
            if generation.status == GenerationStatus.SUCCEEDED.value
        ]
        active = [
            generation
            for generation in generations
            if generation.status
            in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
        ]
        failed = [
            generation
            for generation in generations
            if generation.status == GenerationStatus.FAILED.value
        ]
        has_enough_output_images = len(image_ids) >= expected
        if showcase_step.status in {"running", "failed"} and has_enough_output_images:
            showcase_step.status = "completed"
            if failed:
                output_json = dict(showcase_step.output_json or {})
                output_json["failed_generation_ids"] = [g.id for g in failed]
                output_json["succeeded_generation_ids"] = [g.id for g in succeeded]
                output_json["recovered_by_bonus_images"] = True
                showcase_step.output_json = output_json
            if quality_step:
                quality_step.status = "waiting_input"
                quality_step.image_ids = image_ids
            run.current_step = "showcase_generation"
            run.status = "needs_review"
        elif showcase_step.status == "running" and failed and not active:
            showcase_step.status = "failed"
            showcase_step.output_json = {
                "failed_generation_ids": [g.id for g in failed],
                "succeeded_generation_ids": [g.id for g in succeeded],
            }
            run.status = "failed"
        elif showcase_step.status == "completed" and quality_step:
            quality_step.image_ids = image_ids
            await _sync_quality_reports_from_tasks(
                db,
                run=run,
                quality_step=quality_step,
            )
            reports = await _load_quality_reports(db, run.id)
            if (
                image_ids
                and len(reports) >= len(image_ids)
                and quality_step.status == "running"
            ):
                quality_step.status = "needs_review"
                run.status = "needs_review"
            quality_step.output_json = _merge_quality_summary_payload(
                quality_step.output_json,
                reports,
            )


def _quality_summary_payload(reports: list[QualityReport]) -> dict[str, Any]:
    if not reports:
        return {"overall": "pending", "image_count": 0}
    revise_count = sum(1 for report in reports if report.recommendation == "revise")
    return {
        "overall": "revise" if revise_count else "approve",
        "image_count": len(reports),
        "revise_count": revise_count,
        "average_score": round(
            sum(report.overall_score for report in reports) / max(1, len(reports)),
            1,
        ),
    }


def _merge_quality_summary_payload(
    current: dict[str, Any] | None,
    reports: list[QualityReport],
) -> dict[str, Any]:
    payload = dict(current or {})
    payload.update(_quality_summary_payload(reports))
    review_tasks = (current or {}).get("review_tasks")
    if isinstance(review_tasks, dict):
        payload["review_tasks"] = review_tasks
        payload["review_task_count"] = len(review_tasks)
    return payload


async def _load_quality_reports(db: AsyncSession, run_id: str) -> list[QualityReport]:
    return list(
        (
            await db.execute(
                select(QualityReport)
                .where(QualityReport.workflow_run_id == run_id)
                .order_by(QualityReport.created_at.asc(), QualityReport.id.asc())
            )
        ).scalars().all()
    )


async def _sync_quality_reports_from_tasks(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    quality_step: WorkflowStep,
) -> None:
    output_json = dict(quality_step.output_json or {})
    review_map = output_json.get("review_tasks")
    if not isinstance(review_map, dict) or not review_map:
        return
    existing_by_image = {
        image_id: report
        for image_id, report in (
            (
                report.image_id,
                report,
            )
            for report in await _load_quality_reports(db, run.id)
        )
    }
    task_ids = [
        task_id
        for task_id in review_map.values()
        if isinstance(task_id, str) and task_id
    ]
    if not task_ids:
        return
    completions = (
        await db.execute(
            select(Completion).where(
                Completion.id.in_(task_ids),
                Completion.user_id == run.user_id,
            )
        )
    ).scalars().all()
    completion_by_id = {completion.id: completion for completion in completions}
    for image_id, raw_task_id in review_map.items():
        if not isinstance(image_id, str) or not isinstance(raw_task_id, str):
            continue
        completion = completion_by_id.get(raw_task_id)
        if completion is None:
            continue
        if completion.status == CompletionStatus.SUCCEEDED.value:
            payload = _quality_payload_from_text(completion.text)
        elif completion.status == CompletionStatus.FAILED.value:
            payload = {
                "overall_score": 0,
                "product_fidelity_score": 0,
                "model_consistency_score": 0,
                "aesthetic_score": 0,
                "artifact_score": 0,
                "issues_json": [
                    {
                        "severity": "high",
                        "type": "quality_review_failed",
                        "message": completion.error_message
                        or "Automatic quality review failed; revise or rerun before delivery.",
                    }
                ],
                "recommendation": "revise",
            }
        else:
            continue
        existing = existing_by_image.get(image_id)
        if existing is None:
            db.add(
                QualityReport(
                    workflow_run_id=run.id,
                    image_id=image_id,
                    **payload,
                )
            )
        else:
            existing.overall_score = payload["overall_score"]
            existing.product_fidelity_score = payload["product_fidelity_score"]
            existing.model_consistency_score = payload["model_consistency_score"]
            existing.aesthetic_score = payload["aesthetic_score"]
            existing.artifact_score = payload["artifact_score"]
            existing.issues_json = payload["issues_json"]
            existing.recommendation = payload["recommendation"]


async def _ensure_quality_review_tasks(
    db: AsyncSession,
    *,
    user: User,
    conv: Conversation,
    run: WorkflowRun,
    showcase_step: WorkflowStep,
    quality_step: WorkflowStep,
) -> list[_PublishBundle]:
    image_ids = _dedupe_nonempty(showcase_step.image_ids or [])
    if not image_ids:
        return []
    output_json = dict(quality_step.output_json or {})
    review_map = output_json.get("review_tasks")
    if not isinstance(review_map, dict):
        review_map = {}
    missing_image_ids = [image_id for image_id in image_ids if image_id not in review_map]
    if not missing_image_ids:
        return []

    product_step = await _step(db, run.id, "product_analysis")
    candidate = await _selected_candidate(db, run.id)
    showcase_input = showcase_step.input_json or {}
    shot_plan = showcase_input.get("shot_plan")
    shot_by_image: dict[str, str | None] = {}
    if isinstance(shot_plan, list):
        for image_id, shot_type in zip(image_ids, shot_plan, strict=False):
            shot_by_image[image_id] = str(shot_type) if shot_type is not None else None

    bundles: list[_PublishBundle] = []
    for image_id in missing_image_ids:
        existing_completion = (
            await db.execute(
                select(Completion.id).where(
                    Completion.user_id == user.id,
                    Completion.upstream_request["workflow_run_id"].astext == run.id,
                    Completion.upstream_request["workflow_step_key"].astext
                    == "quality_review",
                    Completion.upstream_request["workflow_review_image_id"].astext
                    == image_id,
                )
            )
        ).scalar_one_or_none()
        if existing_completion:
            review_map[image_id] = existing_completion
            quality_step.task_ids = _dedupe_nonempty(
                [*(quality_step.task_ids or []), existing_completion]
            )
            continue
        attachment_ids = _dedupe_nonempty(
            [
                *(run.product_image_ids or []),
                candidate.contact_sheet_image_id or "",
                image_id,
            ]
        )
        bundle, completion_id, _ = await _create_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.VISION_QA,
            text=_quality_review_prompt(
                product_analysis=product_step.output_json or {},
                selected_candidate=candidate,
                shot_type=shot_by_image.get(image_id),
            ),
            attachment_ids=attachment_ids,
            idempotency_key=f"wf:{run.id[:18]}:qc:{image_id[:20]}",
            workflow_run_id=run.id,
            workflow_step_key="quality_review",
            chat_params=ChatParamsIn(reasoning_effort="low", stream=True),
            workflow_meta={
                "workflow_action": "quality_review",
                "workflow_review_image_id": image_id,
            },
        )
        if completion_id:
            review_map[image_id] = completion_id
            quality_step.task_ids = _dedupe_nonempty(
                [*(quality_step.task_ids or []), completion_id]
            )
            bundles.append(bundle)
    output_json["review_tasks"] = review_map
    output_json["review_task_count"] = len(review_map)
    quality_step.output_json = output_json
    if bundles:
        quality_step.status = "running"
        run.status = "running"
        run.current_step = "quality_review"
    return bundles


async def _ensure_legacy_quality_reports(
    db: AsyncSession,
    *,
    run_id: str,
    images: list[Image],
) -> None:
    """Retained for migrations/manual recovery; normal path uses review tasks."""
    if not images:
        return
    existing = {
        image_id
        for image_id in (
            await db.execute(
                select(QualityReport.image_id).where(
                    QualityReport.workflow_run_id == run_id,
                    QualityReport.image_id.in_([image.id for image in images]),
                )
            )
        ).scalars().all()
    }
    for image in images:
        if image.id in existing:
            continue
        report = QualityReport(
            workflow_run_id=run_id,
            image_id=image.id,
            overall_score=86,
            product_fidelity_score=84,
            model_consistency_score=86,
            aesthetic_score=88,
            artifact_score=86,
            issues_json=[
                {
                    "severity": "low",
                    "type": "automatic_quality_review",
                    "message": "Automatic QC completed. Review garment color, structure, model identity, and artifacts before final delivery.",
                }
            ],
            recommendation="approve",
        )
        db.add(report)


def _next_action_for(run: WorkflowRun) -> str:
    if run.status == "completed":
        return "查看交付"
    return {
        "product_analysis": "确认商品信息",
        "model_settings": "生成模特候选",
        "model_candidates": "等待模特候选",
        "model_approval": "确认模特",
        "showcase_generation": "开始生成展示图",
        "quality_review": "查看质检",
        "delivery": "下载最终图",
    }.get(run.current_step, "继续项目")


async def _image_out_map(db: AsyncSession, images: list[Image]) -> dict[str, ImageOut]:
    if not images:
        return {}
    variant_rows = (
        await db.execute(
            select(ImageVariant.image_id, ImageVariant.kind).where(
                ImageVariant.image_id.in_([image.id for image in images])
            )
        )
    ).all()
    variant_map: dict[str, set[str]] = {}
    for image_id, kind in variant_rows:
        variant_map.setdefault(image_id, set()).add(kind)
    return {image.id: _image_to_out(image, variant_map.get(image.id)) for image in images}


def _image_to_out(img: Image, variant_kinds: set[str] | None = None) -> ImageOut:
    variant_kinds = variant_kinds or set()
    return ImageOut(
        id=img.id,
        source=img.source,
        parent_image_id=img.parent_image_id,
        owner_generation_id=img.owner_generation_id,
        width=img.width,
        height=img.height,
        mime=img.mime,
        blurhash=img.blurhash,
        url=f"/api/images/{img.id}/binary",
        display_url=f"/api/images/{img.id}/variants/display2048",
        preview_url=(
            f"/api/images/{img.id}/variants/preview1024"
            if "preview1024" in variant_kinds
            else None
        ),
        thumb_url=(
            f"/api/images/{img.id}/variants/thumb256"
            if "thumb256" in variant_kinds
            else None
        ),
        metadata_jsonb=img.metadata_jsonb or {},
    )


async def _build_run_out(db: AsyncSession, run: WorkflowRun) -> WorkflowRunOut:
    await _sync_workflow_outputs(db, run)
    await db.flush()
    await db.refresh(run)

    steps = await _load_steps(db, run.id)
    candidates = list(
        (
            await db.execute(
                select(ModelCandidate)
                .where(ModelCandidate.workflow_run_id == run.id)
                .order_by(ModelCandidate.candidate_index.asc())
            )
        ).scalars().all()
    )
    reports = await _load_quality_reports(db, run.id)
    for row in [*steps, *candidates, *reports]:
        await db.refresh(row)

    all_task_ids: set[str] = set()
    image_ids: set[str] = set(run.product_image_ids or [])
    for step in steps:
        all_task_ids.update(step.task_ids or [])
        image_ids.update(step.image_ids or [])
    for candidate in candidates:
        all_task_ids.update(candidate.task_ids or [])
        candidate_image_ids = (candidate.model_brief_json or {}).get("candidate_image_ids")
        if isinstance(candidate_image_ids, list):
            image_ids.update(
                image_id for image_id in candidate_image_ids if isinstance(image_id, str)
            )
        for iid in (
            candidate.contact_sheet_image_id,
            candidate.front_image_id,
            candidate.side_image_id,
            candidate.back_image_id,
            candidate.portrait_image_id,
        ):
            if iid:
                image_ids.add(iid)
    for report in reports:
        image_ids.add(report.image_id)

    generations: list[Generation] = []
    if all_task_ids:
        generations = list(
            (
                await db.execute(
                    select(Generation)
                    .where(Generation.id.in_(all_task_ids), Generation.user_id == run.user_id)
                    .order_by(Generation.created_at.asc(), Generation.id.asc())
                )
            ).scalars().all()
        )
    if all_task_ids:
        owned_images = list(
            (
                await db.execute(
                    select(Image)
                    .where(
                        or_(
                            Image.id.in_(image_ids) if image_ids else Image.id == "__none__",
                            Image.owner_generation_id.in_(all_task_ids),
                        ),
                        Image.user_id == run.user_id,
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            ).scalars().all()
        )
    elif image_ids:
        owned_images = list(
            (
                await db.execute(
                    select(Image)
                    .where(
                        Image.id.in_(image_ids),
                        Image.user_id == run.user_id,
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            ).scalars().all()
        )
    else:
        owned_images = []
    for row in [*generations, *owned_images]:
        await db.refresh(row)

    image_map = await _image_out_map(db, owned_images)
    product_images = [image_map[iid] for iid in (run.product_image_ids or []) if iid in image_map]
    generated_images = [
        image_map[image.id]
        for image in owned_images
        if image.source == "generated" and image.id in image_map
    ]

    return WorkflowRunOut(
        id=run.id,
        conversation_id=run.conversation_id,
        user_id=run.user_id,
        type=run.type,
        status=run.status,
        title=run.title,
        user_prompt=run.user_prompt,
        product_image_ids=run.product_image_ids or [],
        current_step=run.current_step,
        quality_mode=run.quality_mode,
        metadata_jsonb=run.metadata_jsonb or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
        steps=[WorkflowStepOut.model_validate(step) for step in steps],
        model_candidates=[ModelCandidateOut.model_validate(c) for c in candidates],
        quality_reports=[QualityReportOut.model_validate(r) for r in reports],
        product_images=product_images,
        generated_images=generated_images,
        generations=[GenerationOut.model_validate(g) for g in generations],
    )


def _list_item_from_run(run: WorkflowRun, output_count: int = 0) -> WorkflowRunListItemOut:
    return WorkflowRunListItemOut(
        id=run.id,
        conversation_id=run.conversation_id,
        type=run.type,
        status=run.status,
        title=run.title,
        user_prompt=run.user_prompt,
        product_image_ids=run.product_image_ids or [],
        current_step=run.current_step,
        quality_mode=run.quality_mode,
        metadata_jsonb=run.metadata_jsonb or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
        output_count=output_count,
        next_action=_next_action_for(run),
    )


@router.post(
    "/apparel-model-showcase",
    response_model=ApparelWorkflowCreateOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_apparel_model_showcase(
    body: ApparelWorkflowCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelWorkflowCreateOut:
    image_ids = await _validate_owned_images(
        db,
        user_id=user.id,
        image_ids=body.product_image_ids,
        min_count=1,
        max_count=3,
    )
    title = (body.title or "").strip() or "服饰模特展示图"
    conv = await _get_or_create_workflow_conversation(
        db,
        user=user,
        conversation_id=body.conversation_id,
        title=title,
    )
    conv.title = title
    conv.archived = True
    run = WorkflowRun(
        conversation_id=conv.id,
        user_id=user.id,
        type=WORKFLOW_TYPE,
        status="running",
        title=title,
        user_prompt=body.user_prompt,
        product_image_ids=image_ids,
        current_step="product_analysis",
        quality_mode=body.quality_mode,
        metadata_jsonb={
            "template": WORKFLOW_TYPE,
            "mvp_scope": "adult_daily_apparel",
            "priority": ["model_consistency", "product_fidelity", "premium_aesthetic"],
        },
    )
    db.add(run)
    await db.flush()
    for step in _seed_steps(run, user_prompt=body.user_prompt):
        db.add(step)
    product_step = await _step(db, run.id, "product_analysis")

    bundle, completion_id, _ = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.VISION_QA,
        text=_product_analysis_prompt(body.user_prompt),
        attachment_ids=image_ids,
        idempotency_key=f"wf:{run.id}:analysis",
        workflow_run_id=run.id,
        workflow_step_key="product_analysis",
        chat_params=ChatParamsIn(reasoning_effort="low", stream=True),
        workflow_meta={"workflow_action": "product_analysis"},
    )
    product_step.task_ids = [completion_id] if completion_id else []
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    return ApparelWorkflowCreateOut(
        workflow_run_id=run.id,
        status=run.status,
        current_step=run.current_step,
    )


@router.get("", response_model=WorkflowRunListOut)
async def list_workflows(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    type: str | None = Query(default=None),  # noqa: A002 - API field name
    limit: int = Query(default=50, ge=1, le=100),
) -> WorkflowRunListOut:
    stmt = select(WorkflowRun).where(
        WorkflowRun.user_id == user.id,
        WorkflowRun.deleted_at.is_(None),
    )
    if type:
        stmt = stmt.where(WorkflowRun.type == type)
    runs = list(
        (
            await db.execute(
                stmt.order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id)).limit(limit)
            )
        ).scalars().all()
    )
    output_counts: dict[str, int] = {}
    if runs:
        rows = (
            await db.execute(
                select(WorkflowStep.workflow_run_id, WorkflowStep.image_ids)
                .where(
                    WorkflowStep.workflow_run_id.in_([run.id for run in runs]),
                    WorkflowStep.step_key == "showcase_generation",
                )
            )
        ).all()
        output_counts = {run_id: len(image_ids or []) for run_id, image_ids in rows}
    return WorkflowRunListOut(
        items=[_list_item_from_run(run, output_counts.get(run.id, 0)) for run in runs],
        next_cursor=None,
    )


@router.get("/{workflow_run_id}", response_model=WorkflowRunOut)
async def get_workflow(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/steps/product-analysis/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_product_analysis(
    workflow_run_id: str,
    body: ProductAnalysisApproveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status not in {"needs_review", "approved"}:
        raise _http("step_not_ready", "product analysis is not ready to approve", 409)
    product_step.output_json = _merge_product_corrections(
        product_step.output_json or {},
        body.corrections or {},
    )
    product_step.status = "approved"
    product_step.approved_at = _now()
    product_step.approved_by = user.id
    model_settings = await _step(db, run.id, "model_settings")
    if model_settings.status == "waiting_input":
        model_settings.status = "needs_review"
        model_settings.input_json = {
            "style_prompt": run.user_prompt,
            "avoid": ["过度网红感", "夸张姿势", "强烈妆容"],
        }
    run.current_step = "model_settings"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_model_candidates(
    workflow_run_id: str,
    body: ModelCandidatesCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status != "approved":
        raise _http("product_not_approved", "approve product analysis first", 409)
    existing = (
        await db.execute(
            select(ModelCandidate.id).where(ModelCandidate.workflow_run_id == run.id).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _http("already_created", "model candidates already exist for this workflow", 409)

    model_settings = await _step(db, run.id, "model_settings")
    model_settings.status = "approved"
    model_settings.approved_at = _now()
    model_settings.approved_by = user.id
    model_settings.output_json = {
        "style_prompt": body.style_prompt or run.user_prompt,
        "avoid": body.avoid,
        "candidate_count": body.candidate_count,
        "accessory_plan": body.accessory_plan.model_dump(),
    }
    candidate_step = await _step(db, run.id, "model_candidates")
    candidate_step.status = "running"
    candidate_step.input_json = model_settings.output_json
    run.current_step = "model_candidates"
    run.status = "running"

    conv = await _get_owned_conversation(db, user_id=user.id, conversation_id=run.conversation_id or "")
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    model_direction = body.style_prompt or run.user_prompt or "premium ecommerce synthetic model"
    height_cm = _infer_model_height_cm(model_direction)
    height_requirement = _height_requirement(model_direction)
    for idx in range(1, body.candidate_count + 1):
        candidate = ModelCandidate(
            workflow_run_id=run.id,
            candidate_index=idx,
            status="generating",
            model_brief_json={
                "summary": model_direction,
                "candidate_index": idx,
                "height_cm": height_cm,
                "height_label": f"身高 {height_cm}cm",
                "height_requirement": height_requirement,
                "product_context": product_step.output_json,
                "note": "未试穿商品，仅用于确认模特形象",
            },
        )
        db.add(candidate)
        await db.flush()
        bundle, _, gen_ids = await _create_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.TEXT_TO_IMAGE,
            text=_candidate_prompt(
                style_prompt=body.style_prompt or run.user_prompt,
                product_analysis=product_step.output_json or {},
                candidate_index=idx,
                avoid=body.avoid,
            ),
            attachment_ids=[],
            idempotency_key=f"wf:{run.id[:24]}:cand:{idx}",
            workflow_run_id=run.id,
            workflow_step_key="model_candidates",
            image_params=_image_params(aspect_ratio="4:5", count=1, render_quality="high"),
            workflow_meta={
                "workflow_action": "model_candidate",
                "workflow_candidate_id": candidate.id,
                "workflow_candidate_index": idx,
                "workflow_candidate_view": "concept_sheet",
            },
        )
        candidate.task_ids = gen_ids
        task_ids.extend(gen_ids)
        bundles.append(bundle)
    candidate_step.task_ids = task_ids
    approval = await _step(db, run.id, "model_approval")
    approval.input_json = {
        **(approval.input_json or {}),
        "accessory_plan": body.accessory_plan.model_dump(),
        "style_prompt": body.style_prompt or run.user_prompt,
    }
    if body.accessory_plan.enabled:
        accessory_bundle, _, accessory_gen_ids = await _create_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.IMAGE_TO_IMAGE,
            text=_accessory_preview_prompt(
                accessory_plan=body.accessory_plan.model_dump(),
                style_prompt=body.style_prompt or run.user_prompt,
            ),
            attachment_ids=run.product_image_ids or [],
            idempotency_key=f"wf:{run.id[:24]}:acc:init",
            workflow_run_id=run.id,
            workflow_step_key="model_approval",
            image_params=_image_params(aspect_ratio="4:5", count=1, render_quality="high"),
            workflow_meta={
                "workflow_action": "accessory_preview",
                "workflow_origin": "model_candidates",
            },
        )
        approval.status = "running"
        approval.task_ids = _dedupe_nonempty([*(approval.task_ids or []), *accessory_gen_ids])
        bundles.append(accessory_bundle)
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/{candidate_id}/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_model_candidate(
    workflow_run_id: str,
    candidate_id: str,
    body: ModelCandidateApproveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    if candidate.status != "ready" or not candidate.contact_sheet_image_id:
        raise _http("candidate_not_ready", "model candidate is not ready to approve", 409)
    selected_accessory_image_id = body.selected_accessory_image_id
    approval = await _step(db, run.id, "model_approval")
    if selected_accessory_image_id:
        valid_accessory_image_id = (
            await db.execute(
                select(Image.id).where(
                    Image.id == selected_accessory_image_id,
                    Image.user_id == user.id,
                    Image.deleted_at.is_(None),
                    Image.id.in_(approval.image_ids or []),
                )
            )
        ).scalar_one_or_none()
        if valid_accessory_image_id is None:
            raise _http("invalid_accessory_image", "selected accessory preview is invalid", 400)
    all_candidates = (
        await db.execute(
            select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
        )
    ).scalars().all()
    now = _now()
    for row in all_candidates:
        if row.id == candidate.id:
            row.status = "selected"
            row.selected_at = now
            brief = dict(row.model_brief_json or {})
            brief["adjustments"] = body.adjustments
            brief["accessory_plan"] = body.accessory_plan.model_dump()
            brief["selected_accessory_image_id"] = selected_accessory_image_id
            row.model_brief_json = brief
        elif row.status != "failed":
            row.status = "rejected"
    approval.status = "approved"
    approval.approved_at = now
    approval.approved_by = user.id
    approval.input_json = {
        "candidate_id": candidate.id,
        "adjustments": body.adjustments,
        "accessory_plan": body.accessory_plan.model_dump(),
        "selected_accessory_image_id": selected_accessory_image_id,
    }
    approval.output_json = {
        "selected_candidate_id": candidate.id,
        "contact_sheet_image_id": candidate.contact_sheet_image_id,
        "selected_accessory_image_id": selected_accessory_image_id,
    }
    showcase = await _step(db, run.id, "showcase_generation")
    if showcase.status == "waiting_input":
        showcase.status = "needs_review"
    run.current_step = "model_approval"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/reopen",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def reopen_model_selection(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidates = (
        await db.execute(
            select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
        )
    ).scalars().all()
    for candidate in candidates:
        if candidate.status in {"selected", "rejected"}:
            candidate.status = "ready" if candidate.contact_sheet_image_id else "generating"
            candidate.selected_at = None
    approval = await _step(db, run.id, "model_approval")
    approval.status = "needs_review"
    approval.approved_at = None
    approval.approved_by = None
    approval.input_json = {}
    approval.output_json = {}
    showcase = await _step(db, run.id, "showcase_generation")
    showcase.status = "waiting_input"
    showcase.input_json = {}
    showcase.output_json = {}
    showcase.task_ids = []
    showcase.image_ids = []
    quality = await _step(db, run.id, "quality_review")
    quality.status = "waiting_input"
    quality.input_json = {}
    quality.output_json = {}
    quality.task_ids = []
    quality.image_ids = []
    delivery = await _step(db, run.id, "delivery")
    delivery.status = "waiting_input"
    delivery.input_json = {}
    delivery.output_json = {}
    delivery.task_ids = []
    delivery.image_ids = []
    run.current_step = "model_approval"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/accessory-previews",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_accessory_previews(
    workflow_run_id: str,
    body: AccessoryPreviewCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == body.candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    if candidate.status != "selected" or not candidate.contact_sheet_image_id:
        raise _http(
            "model_not_selected",
            "select and approve a model candidate before generating accessory previews",
            409,
        )
    approval = await _step(db, run.id, "model_approval")
    conv = await _get_owned_conversation(db, user_id=user.id, conversation_id=run.conversation_id or "")
    bundle, _, gen_ids = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=_accessory_preview_prompt(
            accessory_plan=body.accessory_plan.model_dump(),
            style_prompt=body.style_prompt,
        ),
        attachment_ids=run.product_image_ids or [],
        idempotency_key=f"wf:{run.id[:12]}:acc:{candidate.id[:8]}:{new_uuid7()[:8]}",
        workflow_run_id=run.id,
        workflow_step_key="model_approval",
        image_params=_image_params(aspect_ratio="4:5", count=1, render_quality="high"),
        workflow_meta={
            "workflow_action": "accessory_preview",
            "workflow_candidate_id": candidate.id,
        },
    )
    approval.status = "running"
    approval.task_ids = _dedupe_nonempty([*(approval.task_ids or []), *gen_ids])
    approval.input_json = {
        **(approval.input_json or {}),
        "candidate_id": candidate.id,
        "accessory_plan": body.accessory_plan.model_dump(),
        "style_prompt": body.style_prompt,
    }
    run.current_step = "model_approval"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/accessory-selection",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def save_accessory_selection(
    workflow_run_id: str,
    body: AccessorySelectionIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    approval = await _step(db, run.id, "model_approval")
    selected_image_id = body.selected_accessory_image_id
    if selected_image_id:
        valid_image_id = (
            await db.execute(
                select(Image.id).where(
                    Image.id == selected_image_id,
                    Image.user_id == user.id,
                    Image.deleted_at.is_(None),
                    Image.id.in_(approval.image_ids or []),
                )
            )
        ).scalar_one_or_none()
        if valid_image_id is None:
            raise _http("invalid_accessory_image", "selected accessory preview is invalid", 400)
    approval.input_json = {
        **(approval.input_json or {}),
        "selected_accessory_image_id": selected_image_id,
    }
    approval.output_json = {
        **(approval.output_json or {}),
        "selected_accessory_image_id": selected_image_id,
    }
    run.current_step = "model_approval"
    if run.status not in {"running", "failed"}:
        run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def _selected_candidate(db: AsyncSession, run_id: str) -> ModelCandidate:
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.workflow_run_id == run_id,
                ModelCandidate.status == "selected",
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("model_not_approved", "approve a model candidate first", 409)
    return candidate


@router.post(
    "/{workflow_run_id}/showcase-images",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_showcase_images(
    workflow_run_id: str,
    body: ShowcaseImagesCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status != "approved":
        raise _http("product_not_approved", "approve product analysis first", 409)
    candidate = await _selected_candidate(db, run.id)
    if not candidate.contact_sheet_image_id:
        raise _http("missing_model_reference", "selected model has no reference image", 409)
    showcase = await _step(db, run.id, "showcase_generation")
    conv = await _get_owned_conversation(db, user_id=user.id, conversation_id=run.conversation_id or "")
    shot_plan = (body.shot_plan or DEFAULT_SHOT_PLAN)[: body.output_count]
    while len(shot_plan) < body.output_count:
        shot_plan.append(DEFAULT_SHOT_PLAN[len(shot_plan) % len(DEFAULT_SHOT_PLAN)])

    approval = await _step(db, run.id, "model_approval")
    accessory_plan = (approval.input_json or {}).get("accessory_plan")
    if not isinstance(accessory_plan, dict):
        accessory_plan = AccessoryPlanIn().model_dump()
    selected_accessory_image_id = (approval.input_json or {}).get("selected_accessory_image_id")
    ref_ids = _showcase_reference_image_ids(
        product_image_ids=run.product_image_ids,
        model_image_id=candidate.contact_sheet_image_id,
        selected_accessory_image_id=(
            selected_accessory_image_id if isinstance(selected_accessory_image_id, str) else None
        ),
    )
    existing_task_ids = _dedupe_nonempty(showcase.task_ids or [])
    existing_image_ids = _dedupe_nonempty(showcase.image_ids or [])
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    for idx, shot_type in enumerate(shot_plan, start=1):
        bundle, _, gen_ids = await _create_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.IMAGE_TO_IMAGE,
            text=_showcase_prompt(
                product_analysis=product_step.output_json or {},
                selected_candidate=candidate,
                accessory_plan=accessory_plan,
                template=body.template,
                shot_type=shot_type,
                final_quality=body.final_quality,
                user_prompt=run.user_prompt,
            ),
            attachment_ids=ref_ids,
            idempotency_key=f"wf:{run.id[:12]}:shot:{idx}:{new_uuid7()[:8]}",
            workflow_run_id=run.id,
            workflow_step_key="showcase_generation",
            image_params=_image_params(
                aspect_ratio=body.aspect_ratio,
                count=1,
                render_quality="high" if body.final_quality != "standard" else "medium",
                final_quality=body.final_quality,
                fast=True,
            ),
            workflow_meta={
                "workflow_action": "showcase_image",
                "workflow_candidate_id": candidate.id,
                "workflow_shot_type": shot_type,
                "workflow_template": body.template,
                "workflow_final_quality": body.final_quality,
            },
        )
        task_ids.extend(gen_ids)
        bundles.append(bundle)
    showcase.status = "running"
    showcase.task_ids = _dedupe_nonempty([*existing_task_ids, *task_ids])
    showcase.image_ids = existing_image_ids
    showcase.input_json = {
        "template": body.template,
        "shot_plan": shot_plan,
        "aspect_ratio": body.aspect_ratio,
        "final_quality": body.final_quality,
        "output_count": body.output_count,
        "target_image_count": _showcase_target_image_count(
            existing_image_ids=existing_image_ids,
            output_count=body.output_count,
        ),
        "reference_image_ids": ref_ids,
    }
    quality = await _step(db, run.id, "quality_review")
    quality.status = "waiting_input"
    quality.input_json = {}
    quality.output_json = {}
    quality.task_ids = []
    quality.image_ids = []
    run.current_step = "showcase_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/images/{image_id}/revise",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def revise_showcase_image(
    workflow_run_id: str,
    image_id: str,
    body: ImageRevisionIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    showcase = await _step(db, run.id, "showcase_generation")
    if image_id not in set(showcase.image_ids or []):
        raise _http("invalid_image", "image is not a showcase output for this workflow", 404)
    image = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if image is None:
        raise _http("not_found", "image not found", 404)
    product_step = await _step(db, run.id, "product_analysis")
    candidate = await _selected_candidate(db, run.id)
    refs = _dedupe_nonempty([*run.product_image_ids, candidate.contact_sheet_image_id or "", image_id])
    conv = await _get_owned_conversation(db, user_id=user.id, conversation_id=run.conversation_id or "")
    revision_index = len(showcase.task_ids or []) + 1
    bundle, _, gen_ids = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=_revision_prompt(
            instruction=body.instruction,
            product_analysis=product_step.output_json or {},
            selected_candidate=candidate,
        ),
        attachment_ids=refs,
        idempotency_key=f"wf:{run.id[:22]}:rev:{revision_index}",
        workflow_run_id=run.id,
        workflow_step_key="showcase_generation",
        image_params=_image_params(aspect_ratio="4:5", count=1, render_quality="high"),
        workflow_meta={
            "workflow_action": "revision",
            "workflow_revision_source_image_id": image_id,
            "workflow_revision_scope": body.scope,
        },
    )
    showcase.task_ids = [*(showcase.task_ids or []), *gen_ids]
    showcase.status = "running"
    quality = await _step(db, run.id, "quality_review")
    quality.status = "running"
    quality.input_json = {
        **(quality.input_json or {}),
        "latest_revision": {
            "source_image_id": image_id,
            "instruction": body.instruction,
            "scope": body.scope,
        },
    }
    run.current_step = "quality_review"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/delivery/complete",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def complete_delivery(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    showcase = await _step(db, run.id, "showcase_generation")
    if not showcase.image_ids:
        raise _http("no_outputs", "generate showcase images before delivery", 409)
    quality = await _step(db, run.id, "quality_review")
    delivery = await _step(db, run.id, "delivery")
    now = _now()
    quality.status = "approved"
    quality.approved_at = now
    quality.approved_by = user.id
    delivery.status = "completed"
    delivery.approved_at = now
    delivery.approved_by = user.id
    delivery.input_json = {"final_image_ids": showcase.image_ids}
    delivery.output_json = {
        "download_image_ids": showcase.image_ids,
        "completed_at": now.isoformat(),
    }
    run.status = "completed"
    run.current_step = "delivery"
    out = await _build_run_out(db, run)
    await db.commit()
    return out
