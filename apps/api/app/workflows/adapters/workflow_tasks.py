"""Workflow prompting, task construction, and publication helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import Intent, Role
from lumen_core.model_entities import (
    Completion,
    Conversation,
    Generation,
    Message,
    ModelCandidate,
    User,
)
from lumen_core.schema_models import (
    ChatParamsIn,
    ImageParamsIn,
)

from ...redis_client import get_redis
from ...services.message_submission import (
    create_assistant_task,
    publish_assistant_task,
    publish_message_appended,
)
from ..domain.showcase_model_policy import (
    accessory_age_direction,
    accessory_strength_direction,
)
from .paid_idempotency import current_paid_operation_task_metadata
from ..domain.workflow_contracts import PublishBundle
from .output_sync import coerce_string_list
from .serialization import clean_string_list, now

DEFAULT_WORKFLOW_TYPE = "apparel_model_showcase"


def revision_prompt(
    *,
    instruction: str,
    product_analysis: dict[str, Any],
    selected_candidate: ModelCandidate,
) -> str:
    must_preserve = product_analysis.get("must_preserve")
    preserve = (
        ", ".join(str(x) for x in must_preserve)
        if isinstance(must_preserve, list)
        else ""
    )
    return (
        "请根据用户要求返修这张服饰电商模特图。"
        "【商品 1:1 还原】衣服以白底产品图为准，不要改款、改色、改廓形、改领口袖型衣长、改图案/logo、改纽扣拉链口袋缝线。"
        "保持已确认模特的人脸、发型、身材比例和整体身份不变。"
        "需要逐项保留的商品细节："
        f"{preserve or '颜色、版型、领口、袖型、长度、logo/图案、口袋、纽扣、缝线'}。"
        f"返修要求：{instruction}，仅按此改动，不动商品和模特身份。"
        f"参考模特方案：{selected_candidate.id}。"
    )


def accessory_preview_prompt(
    *,
    accessory_plan: dict[str, Any],
    style_prompt: str,
    age_context: str = "",
) -> str:
    items = accessory_plan.get("items")
    item_list = clean_string_list(
        (str(item) for item in items) if isinstance(items, list) else [],
        max_items=8,
        max_len=80,
    )
    item_text = "、".join(item_list)
    strength = str(accessory_plan.get("strength") or "subtle")
    enabled = bool(accessory_plan.get("enabled", True))
    accessory_line = (
        f"只添加这些配饰：{item_text}。不要自动新增未列出的包、帽子、腰带、眼镜、首饰、鞋子或道具。"
        if enabled and item_text
        else "不添加新配饰；保持参考图里的基础造型干净稳定。"
    )
    style = style_prompt.strip() or "干净高级的电商参考图，克制自然"
    age_direction = accessory_age_direction(" ".join([age_context, style]).strip())
    return (
        "请根据上传的已确认模特四宫格参考图，生成一张新的白底模特配饰四宫格参考图。"
        "核心目标是在同一个模特、同一套基础中性服装上预览配饰效果，供后续商品融合图参考；"
        "不要生成最终商品穿搭图。"
        "画面必须保持 2x2 四宫格参考图，不要拆成多张图；"
        "四格内容固定为：正面全身、侧面全身、背面全身、近景头像；"
        "布局顺序为左上正面全身、右上侧面全身、左下背面全身、右下近景头像。"
        "每一格都用白底或近白底、同一摄影棚光线、清晰边界；"
        "不要文字标签、编号、边框标题或水印。"
        "严格保持参考图里的同一张脸、发型、肤色、年龄感、身高、身材比例、肢体长度、"
        "体态和基础服装；不要换人，不要美颜成网红脸，不要改成时装大片造型。"
        "模特只穿原参考图中的简单中性基础服装，不要穿商品图中的衣服，"
        "不要出现任何商品服饰、logo、图案或新衣服细节。"
        f"配饰要求：{accessory_line}"
        f"配饰强度：{accessory_strength_direction(strength)}。"
        "配饰必须真实贴合身体和透视：耳饰在耳垂位置，项链贴合颈部，包带、腰带、鞋帽与姿态一致；"
        "不能漂浮、变形、穿模，不能遮挡脸、手、脚和身体轮廓。"
        "不要让配饰遮挡未来商品展示区域；不要添加多余道具、家具、背景场景或手持物，"
        "除非明确列在配饰里。"
        f"年龄与风格：{age_direction} "
        f"补充方向：{style}。"
        "输出风格：高质量真实商业摄影参考图，清晰、干净、可作为后续服饰电商生成的稳定参考。"
    )


def accessory_plan_from_product_analysis(
    product_analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_items = (product_analysis or {}).get("styling_recommendations")
    items = clean_string_list(coerce_string_list(raw_items), max_items=3, max_len=80)
    return {
        "enabled": True,
        "items": items,
        "strength": "subtle",
    }


def coerce_accessory_plan_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    enabled = bool(value.get("enabled", True))
    strength = str(value.get("strength") or "subtle")
    if strength not in {"subtle", "medium", "strong"}:
        strength = "subtle"
    items = value.get("items")
    return {
        "enabled": enabled,
        "items": clean_string_list(
            (str(item) for item in items) if isinstance(items, list) else [],
            max_items=12,
            max_len=80,
        ),
        "strength": strength,
    }


async def create_workflow_task(
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
) -> tuple[PublishBundle, str | None, list[str]]:
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

    result = await create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=getattr(user, "account_mode", "wallet"),
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
        "workflow_type": DEFAULT_WORKFLOW_TYPE,
        "workflow_step_key": workflow_step_key,
        **(workflow_meta or {}),
        **current_paid_operation_task_metadata(db),
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

    bundle = PublishBundle(
        assistant_msg_id=result.assistant_msg.id,
        message_ids=[user_msg.id, result.assistant_msg.id],
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )
    return bundle, result.completion_id, result.generation_ids


async def publish_bundles(
    db: AsyncSession,
    *,
    user_id: str,
    conv_id: str,
    bundles: list[PublishBundle],
) -> None:
    redis = get_redis()
    for bundle in bundles:
        await publish_message_appended(
            redis=redis,
            user_id=user_id,
            conv_id=conv_id,
            message_ids=bundle.message_ids,
        )
        await publish_assistant_task(
            db=db,
            redis=redis,
            user_id=user_id,
            conv_id=conv_id,
            assistant_msg_id=bundle.assistant_msg_id,
            outbox_payloads=bundle.outbox_payloads,
            outbox_rows=bundle.outbox_rows,
        )


def fixed_size_for_quality(aspect_ratio: str, final_quality: str) -> str | None:
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


def image_params(
    *,
    aspect_ratio: str = "4:5",
    count: int = 1,
    render_quality: str = "high",
    final_quality: str | None = None,
    fast: bool = False,
) -> ImageParamsIn:
    fixed = fixed_size_for_quality(aspect_ratio, final_quality or "high")
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


def candidate_image_params() -> ImageParamsIn:
    params = image_params(
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        fast=False,
    )
    return params.model_copy(
        update={"output_format": "png", "output_compression": None}
    )


def accessory_preview_image_params() -> ImageParamsIn:
    params = image_params(
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        final_quality="high",
        fast=False,
    )
    return params.model_copy(
        update={"output_format": "png", "output_compression": None}
    )


def merge_product_corrections(
    product_output: dict[str, Any],
    corrections: dict[str, Any],
) -> dict[str, Any]:
    final = dict(product_output or {})
    raw_corrections = corrections if isinstance(corrections, dict) else {}
    for key, value in raw_corrections.items():
        if value is not None:
            final[key] = value
    final["user_corrections"] = raw_corrections
    final["confirmed_at"] = now().isoformat()
    return final
