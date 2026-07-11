"""Poster workflow helpers, state synchronization, and endpoints."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.constants import (
    Intent,
    Role,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Message,
    PosterMaster,
    PosterRender,
    PosterStyleItem,
    User,
    WorkflowRun,
    WorkflowStep,
    new_uuid7,
)
from lumen_core.schemas import (
    ChatParamsIn,
    CopyAnalysisApproveIn,
    ImageParamsIn,
    PosterDesignWorkflowCreateIn,
    PosterDesignWorkflowCreateOut,
    PosterInpaintIn,
    PosterMasterApproveIn,
    PosterMastersCreateIn,
    PosterRendersCreateIn,
    PosterReviseIn,
    WorkflowRunOut,
)

from ...db import get_db
from ...deps import CurrentUser, verify_csrf
from ._facade import RouteFacade, _PublishBundle
from .poster_sync import PosterSyncHooks, sync_poster_workflow_outputs


router = APIRouter()
logger = logging.getLogger("app.routes.workflows")
_ROUTE_FACADE = RouteFacade(__name__)
FACADE_RUNTIME = _ROUTE_FACADE.runtime
facade_entry = _ROUTE_FACADE.entry

_clean_string_list = _ROUTE_FACADE.sync_hook("_clean_string_list")
_dedupe_nonempty = _ROUTE_FACADE.sync_hook("_dedupe_nonempty")
_failed_generation_output = _ROUTE_FACADE.sync_hook("_failed_generation_output")
_generation_batch_outcome = _ROUTE_FACADE.sync_hook("_generation_batch_outcome")
_http = _ROUTE_FACADE.sync_hook("_http")
_image_params = _ROUTE_FACADE.sync_hook("_image_params")
_now = _ROUTE_FACADE.sync_hook("_now")

_build_run_out = _ROUTE_FACADE.async_hook("_build_run_out")
_create_assistant_task = _ROUTE_FACADE.async_hook("_create_assistant_task")
_get_or_create_workflow_conversation = _ROUTE_FACADE.async_hook(
    "_get_or_create_workflow_conversation"
)
_get_owned_conversation = _ROUTE_FACADE.async_hook("_get_owned_conversation")
_get_run = _ROUTE_FACADE.async_hook("_get_run")
_load_steps = _ROUTE_FACADE.async_hook("_load_steps")
_publish_bundles = _ROUTE_FACADE.async_hook("_publish_bundles")
_step = _ROUTE_FACADE.async_hook("_step")
_validate_owned_images = _ROUTE_FACADE.async_hook("_validate_owned_images")

# ===========================================================================
# Poster Design Workflow（2026-05-12 起）
#
# 设计要点（与 apparel_model_showcase 同源蓝本）：
# 1. workflow_runs.type = "poster_design"；7 个 step：
#    copy_input → style_selection → copy_analysis → master_generation
#    → master_approval → multi_size_generation → delivery
#    （V1 删去 text_layer_editing + quality_review，全 AI 出图 + 文字直塞 prompt）
# 2. 文案分析走 Intent.VISION_QA（纯文本结构化，输出固定 schema JSON）
# 3. 母版生成无品牌图时走 TEXT_TO_IMAGE，有 logo/product 时走 IMAGE_TO_IMAGE；
#    N 个 candidate = N 个独立 Generation 任务，输出 1:1 母版
# 4. 多尺寸成品走 Intent.IMAGE_TO_IMAGE，把母版作为 reference，
#    每个 aspect = 独立 Generation 任务（不在单任务串行多尺寸，遵守 4K timeout 分层）
# 5. inpaint 返修走 Intent.IMAGE_TO_IMAGE + mask_image_id（用户传 mask），
#    prompt 在 worker 侧用 _wrap_inpaint_prompt 包装（OpenAI invariant 模板）
# 6. 风格 prompt 注入：从 PosterStyleItem.prompt_template 读，前缀化拼到母版 prompt
# 7. prompt cache friendly：所有 prompt 前缀稳定（风格 + 信息密度 + 母版指令固定），
#    用户具体文案在末尾
# ===========================================================================


POSTER_WORKFLOW_TYPE = "poster_design"
POSTER_WORKFLOW_STEPS = [
    "copy_input",
    "style_selection",
    "copy_analysis",
    "master_generation",
    "master_approval",
    "multi_size_generation",
    "delivery",
]
POSTER_DEFAULT_TARGET_ASPECTS: tuple[str, ...] = ("1:1", "9:16", "16:9", "3:4")
# 母版固定 1:1。premium 走 4K preset（2880x2880）；standard 走 size=auto。
POSTER_MASTER_ASPECT = "1:1"


# ---- size helpers ----------------------------------------------------------

# 用 _fixed_size_for_quality 已经覆盖了所有比例的 4K preset，
# 多尺寸成品按 quality_mode 选 4k / high。我们对接 apparel 的同一函数。


@facade_entry
def _poster_image_params(
    *,
    aspect_ratio: str,
    quality_mode: str,
    count: int = 1,
) -> ImageParamsIn:
    """统一构造海报 ImageParamsIn。premium → final_quality='4k'。"""
    final_quality = "4k" if quality_mode == "premium" else "high"
    return _image_params(
        aspect_ratio=aspect_ratio,
        count=count,
        render_quality="high",
        final_quality=final_quality,
        fast=False,
    )


@facade_entry
def _poster_master_image_params(quality_mode: str) -> ImageParamsIn:
    return _poster_image_params(
        aspect_ratio=POSTER_MASTER_ASPECT,
        quality_mode=quality_mode,
        count=1,
    )


@facade_entry
async def _poster_find_preset_item(
    db: AsyncSession, *, user_id: str, style_id: str
) -> dict[str, Any] | None:
    from ..poster_styles import _bootstrap_local_presets_if_empty, _find_preset_item

    await _bootstrap_local_presets_if_empty()
    return await _find_preset_item(db, user_id=user_id, item_id=style_id)


@facade_entry
def _poster_style_from_preset(raw: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        mood=str(raw.get("mood") or ""),
        prompt_template=str(raw.get("prompt_template") or ""),
        palette=[str(v) for v in (raw.get("palette") or []) if str(v).strip()],
        recommended_aspects=[
            str(v) for v in (raw.get("recommended_aspects") or []) if str(v).strip()
        ],
        style_tags=[str(v) for v in (raw.get("style_tags") or []) if str(v).strip()],
        category=str(raw.get("category") or ""),
    )


@facade_entry
async def _poster_load_style(
    db: AsyncSession,
    *,
    user_id: str,
    style_id: str,
) -> Any:
    """Load a poster style for workflow creation.

    User-created styles are private DB rows and must match ``user_id``. Presets
    live in the poster-style JSON index rather than ``poster_style_items``.
    """
    if style_id.startswith("preset:"):
        preset = await _poster_find_preset_item(db, user_id=user_id, style_id=style_id)
        if preset is not None:
            return _poster_style_from_preset(preset)
        raise _http("style_not_found", "poster style not found", 404)
    row = (
        await db.execute(
            select(PosterStyleItem).where(
                PosterStyleItem.id == style_id,
                PosterStyleItem.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("style_not_found", "poster style not found", 404)
    return row


# ---- prompt helpers --------------------------------------------------------


@facade_entry
def _poster_copy_analysis_prompt(copy_text: str) -> str:
    """文案语义切分 prompt（docs §8.1）。

    前缀（指令 + JSON schema 描述）稳定，用户输入只在末尾追加 —— 这样上游
    prompt cache 在多次重生间能命中前缀。
    """
    return (
        "你是海报文案结构化助手。请把下面一段海报营销文案切分成固定 JSON schema："
        "main_title（主标题，3-12 字）、subtitle（副标，可空）、selling_points（卖点数组，最多 4 条）、"
        "cta（行动号召，可空）、price（价格，可空）、tone（语气，1 句话）、"
        "info_density（信息密度，取值 high/medium/low）。"
        "必须只返回一个 JSON object，不要 Markdown、不要代码块、不要解释文字。"
        "如果某字段在原文里没有，填 null。info_density 的判定："
        "卖点+CTA+价格总条数 ≥ 4 → high；2-3 → medium；≤ 1 → low。"
        "保留原文措辞，不要改写或扩写。"
        f"\n\n原文案：\n{copy_text}"
    )


@facade_entry
def _poster_style_summary(style: PosterStyleItem) -> dict[str, Any]:
    """把 PosterStyleItem 抽成稳定的 style_summary JSON，作为后续 prompt 注入字段。"""
    return {
        "style_id": style.id,
        "title": style.title or "",
        "mood": style.mood or "",
        "prompt_template": (style.prompt_template or "").strip(),
        "palette": list(style.palette or []),
        "recommended_aspects": list(style.recommended_aspects or []),
        "style_tags": list(style.style_tags or []),
        "category": style.category or "",
    }


@facade_entry
def _poster_layout_safe_area(info_density: str) -> str:
    """按信息密度决定 safe area 位置；全 AI 出图也用这个信号控制构图留白。"""
    mapping = {
        "high": "下半区或左侧 1/3 区为主信息密集区，画面上半区留呼吸感",
        "medium": "中部水平带为主信息区，上下各留 25% 空间",
        "low": "中心 1/3 区为主信息区，四周大留白",
    }
    return mapping.get(info_density, mapping["medium"])


@facade_entry
def _poster_text_fields_block(copy_analysis: dict[str, Any]) -> str:
    """把文案字段拼成稳定的 prompt 段（用于母版/多尺寸 prompt）。"""

    def _val(key: str) -> str:
        v = copy_analysis.get(key)
        if v is None:
            return ""
        if isinstance(v, list):
            return "、".join(str(x).strip() for x in v if str(x).strip())
        return str(v).strip()

    main_title = _val("main_title")
    subtitle = _val("subtitle")
    selling_points = _val("selling_points")
    cta = _val("cta")
    price = _val("price")
    lines = []
    if main_title:
        lines.append(f"- main_title: {main_title}")
    if subtitle:
        lines.append(f"- subtitle: {subtitle}")
    if selling_points:
        lines.append(f"- selling_points: {selling_points}")
    if cta:
        lines.append(f"- cta: {cta}")
    if price:
        lines.append(f"- price: {price}")
    return "\n".join(lines) if lines else "- main_title: (无)"


@facade_entry
def _poster_brand_assets_block(brand_assets: dict[str, Any]) -> str:
    """品牌资产 prompt 段；空字段直接跳过，保持 prompt 前缀稳定。"""
    primary_color = str(brand_assets.get("primary_color") or "").strip()
    font_family = str(brand_assets.get("font_family") or "").strip()
    bits: list[str] = []
    if primary_color:
        bits.append(f"primary brand color: {primary_color}")
    if font_family:
        bits.append(f"preferred font family: {font_family}")
    if brand_assets.get("logo_image_id"):
        bits.append(
            "a brand logo image is provided as reference; integrate it tastefully if appropriate"
        )
    if brand_assets.get("product_image_id"):
        bits.append(
            "a product image is provided as reference; place it as the visual focal point"
        )
    return "; ".join(bits) if bits else "no extra brand asset constraints"


@facade_entry
def _poster_brand_attachment_ids(run: WorkflowRun) -> list[str]:
    """Return logo/product references in their semantic order, with legacy fallback."""
    metadata = run.metadata_jsonb if isinstance(run.metadata_jsonb, dict) else {}
    raw_brand_assets = metadata.get("brand_assets")
    brand_assets = raw_brand_assets if isinstance(raw_brand_assets, dict) else {}
    image_ids: list[str] = []
    for key in ("logo_image_id", "product_image_id"):
        value = brand_assets.get(key)
        if isinstance(value, str):
            image_ids.append(value)
    for value in getattr(run, "product_image_ids", None) or []:
        if isinstance(value, str):
            image_ids.append(value)
    return _dedupe_nonempty(image_ids)


@facade_entry
def _poster_master_prompt(
    *,
    style_summary: dict[str, Any],
    copy_analysis: dict[str, Any],
    brand_assets: dict[str, Any],
    candidate_index: int,
) -> str:
    """母版 prompt（docs §8.2 改良）。

    决策：全 AI 出图——main_title / subtitle / cta / price 当字段塞进 prompt，
    让 gpt-image-2 直接画带文字的成品。短中文（3-8 字）实测可用，长文走 inpaint 兜底。
    prompt 前缀稳定（指令 + 风格段），candidate_index / 用户文案在末尾。
    """
    info_density = str(copy_analysis.get("info_density") or "medium")
    palette = style_summary.get("palette") or []
    palette_text = (
        ", ".join(str(p) for p in palette if str(p).strip()) or "balanced palette"
    )
    style_prompt_template = (style_summary.get("prompt_template") or "").strip()
    style_mood = (style_summary.get("mood") or "").strip()
    safe_area = _poster_layout_safe_area(info_density)
    text_block = _poster_text_fields_block(copy_analysis)
    brand_block = _poster_brand_assets_block(brand_assets)
    style_block = style_prompt_template or "clean modern poster design"
    return (
        "Create one high-quality marketing poster master, square 1:1 composition, "
        "print-ready visual.\n"
        "This is a master candidate used to confirm the visual style before "
        "rendering other aspect ratios; keep composition logic clean.\n"
        "Render the marketing text fields directly inside the image (do NOT leave "
        "them as placeholders): main_title is the largest, subtitle smaller below, "
        "selling_points as short bullets, cta as a small accent badge, price as a "
        "highlighted callout if present. Keep all text short, sharp, and legible.\n"
        f"Style direction: {style_block}.\n"
        f"Color palette priority: {palette_text}.\n"
        f"Mood: {style_mood or 'aligned with the style direction above'}.\n"
        f"Information density: {info_density}; layout safe area: {safe_area}.\n"
        f"Brand assets: {brand_block}.\n"
        "Avoid: watermark, signature, busy textures over text, unreadable glyphs, "
        "duplicated headlines, English filler text when source copy is Chinese.\n"
        f"Text fields to render:\n{text_block}\n"
        f"Candidate variation number: {candidate_index}."
    )


@facade_entry
def _poster_render_prompt(
    *,
    style_summary: dict[str, Any],
    copy_analysis: dict[str, Any],
    target_aspect: str,
    adjustments: str = "",
) -> str:
    """多尺寸 prompt（docs §8.3）。母版作为 reference 重出目标比例。"""
    palette = style_summary.get("palette") or []
    palette_text = (
        ", ".join(str(p) for p in palette if str(p).strip()) or "balanced palette"
    )
    info_density = str(copy_analysis.get("info_density") or "medium")
    safe_area = _poster_layout_safe_area(info_density)
    text_block = _poster_text_fields_block(copy_analysis)
    extra = (adjustments or "").strip()
    extra_line = f"\nAdditional direction: {extra}" if extra else ""
    return (
        f"Re-render the reference poster master into a {target_aspect} composition.\n"
        "Match the visual style, color palette, mood, decoration logic, and text "
        "rendering style of the reference image exactly.\n"
        "Adapt the composition naturally to the new aspect ratio without distortion; "
        "reposition text fields to keep them clearly legible in the new frame.\n"
        f"Reference palette: {palette_text}.\n"
        f"Information density: {info_density}; layout safe area: {safe_area}.\n"
        f"Text fields to keep visible:\n{text_block}\n"
        "Do not change the wording of any text field; only adjust position, size, "
        "and orientation to fit the new aspect ratio."
        f"{extra_line}"
    )


@facade_entry
def _poster_revision_prompt(
    *,
    style_summary: dict[str, Any],
    copy_analysis: dict[str, Any],
    target_aspect: str,
    instruction: str,
    scope: str,
) -> str:
    """整张返修 prompt（scope=background 或 style）。inpaint 走单独的路径。"""
    if scope == "style":
        return (
            f"{_poster_render_prompt(style_summary=style_summary, copy_analysis=copy_analysis, target_aspect=target_aspect)}"
            f"\nUser revision (style change): {instruction.strip()}."
        )
    # background: 默认保留风格+文案，只改背景/构图
    return (
        f"Revise this poster background while keeping the {target_aspect} composition.\n"
        "Preserve the visual style, color palette, mood, and decoration logic of the reference exactly.\n"
        "Do not change the wording of any text field; only adjust the background, "
        "layout, or composition based on the user's instruction.\n"
        f"Text fields to keep visible:\n{_poster_text_fields_block(copy_analysis)}\n"
        f"User revision: {instruction.strip()}."
    )


# ---- step / state helpers --------------------------------------------------


@facade_entry
def _poster_seed_steps(run: WorkflowRun) -> list[WorkflowStep]:
    """初始化 7 个 step：copy_input/style_selection 在创建时即 approved（用户已选定），
    copy_analysis 进入 running，其它 step 处于 waiting_input。"""
    steps: list[WorkflowStep] = []
    for key in POSTER_WORKFLOW_STEPS:
        status = "waiting_input"
        input_json: dict[str, Any] = {}
        output_json: dict[str, Any] = {}
        if key == "copy_input":
            status = "approved"
            input_json = {"copy_text": run.user_prompt}
            output_json = {"confirmed": True}
        elif key == "style_selection":
            status = "approved"
            input_json = {
                "style_id": (run.metadata_jsonb or {}).get("style_id"),
                "target_aspects": (run.metadata_jsonb or {}).get("target_aspects")
                or list(POSTER_DEFAULT_TARGET_ASPECTS),
            }
            output_json = {"confirmed": True}
        elif key == "copy_analysis":
            status = "running"
            input_json = {
                "copy_text": run.user_prompt,
                "prompt_contract": "extract poster copy into structured JSON",
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


@facade_entry
async def _create_poster_workflow_task(
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
    mask_image_id: str | None = None,
) -> tuple[_PublishBundle, str | None, list[str]]:
    """与 _create_workflow_task 同源；额外支持 mask_image_id（inpaint）+
    workflow_type=poster_design 标记。"""
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
        mask_image_id=mask_image_id,
    )

    meta = {
        "workflow_run_id": workflow_run_id,
        "workflow_type": POSTER_WORKFLOW_TYPE,
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


@facade_entry
def _poster_parse_copy_analysis_text(text: str) -> dict[str, Any]:
    """解析文案分析 completion 的返回 JSON，规整字段；解析失败时 graceful 降级。

    与 apparel _try_parse_json_text 不同——后者会走 _normalize_product_analysis_payload
    把字段规整到 apparel schema，把海报字段全丢掉。这里只做原始 JSON 提取 + 海报字段规整。
    """
    raw = (text or "").strip()
    parsed: Any = None
    if raw:
        body = raw
        if body.startswith("```"):
            body = body.strip("`")
            body = body.removeprefix("json").strip()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            start = body.find("{")
            end = body.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(body[start : end + 1])
                except json.JSONDecodeError:
                    parsed = None
    if not isinstance(parsed, dict):
        parsed = {}
    main_title = parsed.get("main_title")
    subtitle = parsed.get("subtitle")
    selling_points = parsed.get("selling_points")
    cta = parsed.get("cta")
    price = parsed.get("price")
    tone = parsed.get("tone")
    info_density = parsed.get("info_density")
    if info_density not in {"high", "medium", "low"}:
        info_density = "medium"
    return {
        "main_title": str(main_title).strip() if main_title else None,
        "subtitle": str(subtitle).strip() if subtitle else None,
        "selling_points": (
            _clean_string_list(
                (str(item) for item in selling_points)
                if isinstance(selling_points, list)
                else [],
                max_items=4,
                max_len=60,
            )
            if isinstance(selling_points, list)
            else []
        ),
        "cta": str(cta).strip() if cta else None,
        "price": str(price).strip() if price else None,
        "tone": str(tone).strip() if tone else None,
        "info_density": info_density,
        "raw_text": text or "",
    }


@facade_entry
def _poster_merge_copy_corrections(
    base: dict[str, Any],
    corrections: dict[str, Any],
) -> dict[str, Any]:
    """用户对文案分析的手工修正——None 表示沿用 AI 输出，非 None 覆盖。"""
    final = dict(base or {})
    raw = corrections if isinstance(corrections, dict) else {}
    for key, value in raw.items():
        if value is not None:
            final[key] = value
    final["user_corrections"] = raw
    final["confirmed_at"] = _now().isoformat()
    return final


@facade_entry
async def _sync_poster_workflow_outputs(
    db: AsyncSession,
    run: WorkflowRun,
) -> None:
    await sync_poster_workflow_outputs(
        db,
        run,
        workflow_type=POSTER_WORKFLOW_TYPE,
        hooks=PosterSyncHooks(
            load_steps=_load_steps,
            parse_copy_analysis_text=_poster_parse_copy_analysis_text,
            generation_batch_outcome=_generation_batch_outcome,
            failed_generation_output=_failed_generation_output,
            dedupe_nonempty=_dedupe_nonempty,
        ),
    )


# ---- endpoints -------------------------------------------------------------


@router.post(
    "/poster-design",
    response_model=PosterDesignWorkflowCreateOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def create_poster_design_workflow(
    body: PosterDesignWorkflowCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterDesignWorkflowCreateOut:
    """创建海报工作流 + 触发文案分析。

    流程：
    1. 校验 copy_text 非空（pydantic 已校验 min_length=1）
    2. 校验 style_id 存在（_poster_load_style）
    3. 可选校验 brand_assets 中 logo/product image_id 归属当前用户
    4. 创建 WorkflowRun + 7 个 step（copy_input/style_selection 直接 approved）
    5. 入队 vision_qa 任务做文案切分
    """
    copy_text = (body.copy_text or "").strip()
    if not copy_text:
        raise _http("missing_copy_text", "copy_text is required", 422)
    style = await _poster_load_style(db, user_id=user.id, style_id=body.style_id)
    brand_image_ids: list[str] = []
    if body.brand_assets.logo_image_id:
        brand_image_ids.append(body.brand_assets.logo_image_id)
    if body.brand_assets.product_image_id:
        brand_image_ids.append(body.brand_assets.product_image_id)
    if brand_image_ids:
        brand_image_ids = await _validate_owned_images(
            db,
            user_id=user.id,
            image_ids=brand_image_ids,
            min_count=1,
            max_count=8,
        )

    title = (body.title or "").strip() or (copy_text[:24] or "海报设计")
    conv = await _get_or_create_workflow_conversation(
        db,
        user=user,
        conversation_id=body.conversation_id,
        title=title,
        workflow_type=POSTER_WORKFLOW_TYPE,
    )
    conv.title = title
    conv.archived = True
    run = WorkflowRun(
        conversation_id=conv.id,
        user_id=user.id,
        type=POSTER_WORKFLOW_TYPE,
        status="running",
        title=title,
        user_prompt=copy_text,
        product_image_ids=brand_image_ids,  # 复用字段承载品牌资产图（前端按 type 解释）
        current_step="copy_analysis",
        quality_mode=body.quality_mode,
        metadata_jsonb={
            "template": POSTER_WORKFLOW_TYPE,
            "style_id": style.id,
            "style_summary": _poster_style_summary(style),
            "target_aspects": list(body.target_aspects),
            "brand_assets": body.brand_assets.model_dump(),
        },
    )
    db.add(run)
    await db.flush()
    for step in _poster_seed_steps(run):
        db.add(step)
    copy_step = await _step(db, run.id, "copy_analysis")
    bundle, completion_id, _ = await _create_poster_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.VISION_QA,
        text=_poster_copy_analysis_prompt(copy_text),
        attachment_ids=[],  # vision_qa 纯文本结构化也走 vision route，无图 attachment
        idempotency_key=f"wf:{run.id}:copy",
        workflow_run_id=run.id,
        workflow_step_key="copy_analysis",
        chat_params=ChatParamsIn(reasoning_effort="low", stream=True),
        workflow_meta={"workflow_action": "poster_copy_analysis"},
    )
    copy_step.task_ids = [completion_id] if completion_id else []
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    return PosterDesignWorkflowCreateOut(
        workflow_run_id=run.id,
        status=run.status,
        current_step=run.current_step,
    )


@router.post(
    "/{workflow_run_id}/steps/copy-analysis/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def approve_copy_analysis(
    workflow_run_id: str,
    body: CopyAnalysisApproveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """用户确认（含手工修正）文案分析输出，推进到 master_generation 等待入参。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    copy_step = await _step(db, run.id, "copy_analysis")
    if copy_step.status not in {"needs_review", "approved"}:
        raise _http("step_not_ready", "copy analysis is not ready to approve", 409)
    copy_step.output_json = _poster_merge_copy_corrections(
        copy_step.output_json or {},
        body.corrections or {},
    )
    copy_step.status = "approved"
    copy_step.approved_at = _now()
    copy_step.approved_by = user.id
    master_step = await _step(db, run.id, "master_generation")
    if master_step.status == "waiting_input":
        master_step.input_json = {
            "copy_analysis": copy_step.output_json,
            "style_summary": (run.metadata_jsonb or {}).get("style_summary") or {},
        }
    run.current_step = "master_generation"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/masters",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def create_poster_masters(
    workflow_run_id: str,
    body: PosterMastersCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """生成 N 张母版候选（默认 4），每张 = 独立 Generation 任务。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    copy_step = await _step(db, run.id, "copy_analysis")
    if copy_step.status != "approved":
        raise _http("copy_not_approved", "approve copy analysis first", 409)
    master_step = await _step(db, run.id, "master_generation")
    if master_step.status == "running":
        raise _http("already_running", "master generation already running", 409)

    style_summary = (run.metadata_jsonb or {}).get("style_summary") or {}
    brand_assets = (run.metadata_jsonb or {}).get("brand_assets") or {}
    brand_attachment_ids = _poster_brand_attachment_ids(run)
    copy_analysis = copy_step.output_json or {}
    candidate_count = max(1, min(8, body.candidate_count))

    # 已有 master 行：累加 candidate_index 避免唯一冲突。
    existing_masters = (
        (
            await db.execute(
                select(PosterMaster)
                .where(PosterMaster.workflow_run_id == run.id)
                .order_by(PosterMaster.candidate_index.asc())
            )
        )
        .scalars()
        .all()
    )
    existing_count = len(existing_masters)

    master_step.status = "running"
    master_step.task_ids = []
    master_step.image_ids = []
    master_step.output_json = {}
    master_step.input_json = {
        "candidate_count": candidate_count,
        "size_mode": body.size_mode,
        "size": body.size,
        "copy_analysis": copy_analysis,
        "style_summary": style_summary,
        "reference_image_ids": brand_attachment_ids,
    }
    run.current_step = "master_generation"
    run.status = "running"

    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    image_params = _poster_master_image_params(run.quality_mode)
    if body.size_mode == "fixed" and body.size:
        image_params = image_params.model_copy(
            update={"size_mode": "fixed", "fixed_size": body.size}
        )

    for idx in range(1, candidate_count + 1):
        candidate_index = existing_count + idx
        master = PosterMaster(
            workflow_run_id=run.id,
            candidate_index=candidate_index,
            status="generating",
            style_summary_json={
                "style_summary": style_summary,
                "copy_analysis": copy_analysis,
                "candidate_index": candidate_index,
            },
        )
        db.add(master)
        await db.flush()
        bundle, _, gen_ids = await _create_poster_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=(
                Intent.IMAGE_TO_IMAGE if brand_attachment_ids else Intent.TEXT_TO_IMAGE
            ),
            text=_poster_master_prompt(
                style_summary=style_summary,
                copy_analysis=copy_analysis,
                brand_assets=brand_assets,
                candidate_index=candidate_index,
            ),
            attachment_ids=brand_attachment_ids,
            idempotency_key=f"wf:{run.id[:22]}:m:{candidate_index}",
            workflow_run_id=run.id,
            workflow_step_key="master_generation",
            image_params=image_params,
            workflow_meta={
                "workflow_action": "poster_master",
                "workflow_master_id": master.id,
                "workflow_master_index": candidate_index,
            },
        )
        master.task_ids = gen_ids
        task_ids.extend(gen_ids)
        bundles.append(bundle)
    master_step.task_ids = _dedupe_nonempty(task_ids)
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/masters/{master_id}/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def approve_poster_master(
    workflow_run_id: str,
    master_id: str,
    body: PosterMasterApproveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """用户选定 1 张母版。其它候选 status 保留 ready，但 selected 字段只有 1 张。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    master = (
        await db.execute(
            select(PosterMaster).where(
                PosterMaster.id == master_id,
                PosterMaster.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if master is None:
        raise _http("not_found", "poster master not found", 404)
    if master.status != "ready" or not master.image_id:
        raise _http("master_not_ready", "poster master is not ready to approve", 409)
    # 把其它已选的 master 切回 ready，保证只有 1 张 selected
    other_selected = (
        (
            await db.execute(
                select(PosterMaster).where(
                    PosterMaster.workflow_run_id == run.id,
                    PosterMaster.status == "selected",
                    PosterMaster.id != master.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in other_selected:
        row.status = "ready"
        row.selected_at = None
    adjustments = body.adjustments or ""
    master.status = "selected"
    master.selected_at = _now()
    master_step = await _step(db, run.id, "master_generation")
    if master_step.status == "needs_review":
        master_step.status = "approved"
        master_step.approved_at = _now()
        master_step.approved_by = user.id
        master_step.output_json = {
            **(master_step.output_json or {}),
            "selected_master_id": master.id,
            "selected_master_image_id": master.image_id,
            "adjustments": adjustments,
        }
    approval_step = await _step(db, run.id, "master_approval")
    approval_step.status = "approved"
    approval_step.approved_at = _now()
    approval_step.approved_by = user.id
    approval_step.input_json = {
        **(approval_step.input_json or {}),
        "selected_master_id": master.id,
        "selected_master_image_id": master.image_id,
        "adjustments": adjustments,
    }
    approval_step.output_json = {
        "selected_master_id": master.id,
        "selected_master_image_id": master.image_id,
        "adjustments": adjustments,
    }
    multi_step = await _step(db, run.id, "multi_size_generation")
    if multi_step.status == "waiting_input":
        multi_step.input_json = {
            **(multi_step.input_json or {}),
            "selected_master_id": master.id,
            "selected_master_image_id": master.image_id,
            "target_aspects": (run.metadata_jsonb or {}).get("target_aspects")
            or list(POSTER_DEFAULT_TARGET_ASPECTS),
            "adjustments": adjustments,
        }
    run.current_step = "multi_size_generation"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@facade_entry
async def _poster_selected_master(db: AsyncSession, run_id: str) -> PosterMaster:
    master = (
        await db.execute(
            select(PosterMaster).where(
                PosterMaster.workflow_run_id == run_id,
                PosterMaster.status == "selected",
            )
        )
    ).scalar_one_or_none()
    if master is None:
        raise _http("master_not_selected", "select a poster master first", 409)
    if not master.image_id:
        raise _http("master_missing_image", "selected master has no image", 409)
    return master


@router.post(
    "/{workflow_run_id}/renders",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def create_poster_renders(
    workflow_run_id: str,
    body: PosterRendersCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """按 aspect 批量生成多尺寸成品。每个 aspect = 独立 Generation 任务（stagger 入队）。

    复用现有 _create_assistant_task 内部的 stagger（i*5s, cap 30s），
    但因为每次都 count=1，stagger 跨调用不会触发——这与 apparel showcase 同。
    """
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    master = await _poster_selected_master(db, run.id)
    master_image_id = master.image_id
    if not master_image_id:
        raise _http("master_missing_image", "selected master has no image", 409)
    multi_step = await _step(db, run.id, "multi_size_generation")
    if multi_step.status == "running":
        raise _http("already_running", "multi-size generation already running", 409)
    aspects = list(dict.fromkeys(body.aspects))
    if not aspects:
        raise _http("missing_aspects", "at least one aspect ratio required", 422)
    style_summary = (run.metadata_jsonb or {}).get("style_summary") or {}
    copy_step = await _step(db, run.id, "copy_analysis")
    copy_analysis = copy_step.output_json or {}
    approval_step = await _step(db, run.id, "master_approval")
    approval_output = (
        approval_step.output_json if isinstance(approval_step.output_json, dict) else {}
    )
    approval_input = (
        approval_step.input_json if isinstance(approval_step.input_json, dict) else {}
    )
    adjustments = str(
        approval_output.get("adjustments") or approval_input.get("adjustments") or ""
    ).strip()
    brand_attachment_ids = _poster_brand_attachment_ids(run)
    reference_image_ids = _dedupe_nonempty(
        [
            master_image_id if body.use_master_as_reference else "",
            *brand_attachment_ids,
        ]
    )
    quality_mode = (
        body.quality_mode
        if body.quality_mode in {"standard", "premium"}
        else run.quality_mode
    )

    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    # 已有 render 行（同 aspect 已生成过则跳过，避免唯一冲突）
    existing_renders = (
        (
            await db.execute(
                select(PosterRender).where(PosterRender.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    existing_aspects = {r.aspect_ratio for r in existing_renders}
    pending_aspects = [aspect for aspect in aspects if aspect not in existing_aspects]

    multi_step.status = "running"
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "aspects": aspects,
        "use_master_as_reference": body.use_master_as_reference,
        "quality_mode": quality_mode,
        "expected_render_count": len(pending_aspects),
        "active_aspects": pending_aspects,
        "active_task_ids": [],
        "reference_image_ids": reference_image_ids,
        "adjustments": adjustments,
    }
    run.current_step = "multi_size_generation"
    run.status = "running"

    if not pending_aspects:
        requested_image_ids = _dedupe_nonempty(
            r.image_id
            for r in existing_renders
            if r.aspect_ratio in aspects and isinstance(r.image_id, str)
        )
        if not requested_image_ids:
            raise _http(
                "renders_already_exist",
                "requested renders already exist but are not ready",
                409,
            )
        multi_step.status = "needs_review"
        multi_step.image_ids = requested_image_ids
        run.status = "needs_review"
        await db.commit()
        run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
        out = await _build_run_out(db, run)
        await db.commit()
        return out

    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    for idx, aspect in enumerate(pending_aspects, start=1):
        image_params = _poster_image_params(
            aspect_ratio=aspect, quality_mode=quality_mode, count=1
        )
        size_str = image_params.fixed_size or "auto"
        render = PosterRender(
            workflow_run_id=run.id,
            master_id=master.id,
            aspect_ratio=aspect,
            size=size_str,
            status="generating",
            metadata_jsonb={
                "quality_mode": quality_mode,
                "use_master_as_reference": body.use_master_as_reference,
            },
        )
        db.add(render)
        await db.flush()
        bundle, _, gen_ids = await _create_poster_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=(
                Intent.IMAGE_TO_IMAGE if reference_image_ids else Intent.TEXT_TO_IMAGE
            ),
            text=_poster_render_prompt(
                style_summary=style_summary,
                copy_analysis=copy_analysis,
                target_aspect=aspect,
                adjustments=adjustments,
            ),
            attachment_ids=reference_image_ids,
            idempotency_key=f"wf:{run.id[:18]}:r:{idx}:{aspect}",
            workflow_run_id=run.id,
            workflow_step_key="multi_size_generation",
            image_params=image_params,
            workflow_meta={
                "workflow_action": "poster_render",
                "workflow_render_id": render.id,
                "workflow_master_id": master.id,
                "workflow_target_aspect": aspect,
                "workflow_quality_mode": quality_mode,
            },
        )
        render.task_ids = gen_ids
        task_ids.extend(gen_ids)
        bundles.append(bundle)
    multi_step.task_ids = _dedupe_nonempty([*(multi_step.task_ids or []), *task_ids])
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "active_task_ids": _dedupe_nonempty(task_ids),
    }
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/renders/{render_id}/revise",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def revise_poster_render(
    workflow_run_id: str,
    render_id: str,
    body: PosterReviseIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """单张返修：scope=background/style 走整张 i2i；scope=inpaint 走 mask inpaint。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    render = (
        await db.execute(
            select(PosterRender).where(
                PosterRender.id == render_id,
                PosterRender.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if render is None:
        raise _http("not_found", "poster render not found", 404)
    if not render.image_id:
        raise _http("render_no_image", "render has no image yet", 409)
    if body.scope == "inpaint":
        # 走 inpaint 子端点同一逻辑；要求 mask
        return await _do_poster_inpaint(
            db,
            user=user,
            run=run,
            render=render,
            instruction=body.instruction,
            mask_image_id=body.mask_image_id or "",
        )
    master = await _poster_selected_master(db, run.id)
    style_summary = (run.metadata_jsonb or {}).get("style_summary") or {}
    copy_step = await _step(db, run.id, "copy_analysis")
    copy_analysis = copy_step.output_json or {}
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    # 参考图：母版 + 当前 render 图（让模型保持版式）
    ref_ids = _dedupe_nonempty([master.image_id or "", render.image_id])
    image_params = _poster_image_params(
        aspect_ratio=render.aspect_ratio,
        quality_mode=str(render.metadata_jsonb.get("quality_mode") or run.quality_mode),
        count=1,
    )
    bundle, _, gen_ids = await _create_poster_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=_poster_revision_prompt(
            style_summary=style_summary,
            copy_analysis=copy_analysis,
            target_aspect=render.aspect_ratio,
            instruction=body.instruction,
            scope=body.scope,
        ),
        attachment_ids=ref_ids,
        idempotency_key=f"wf:{run.id[:18]}:rv:{render.id[:8]}:{new_uuid7()[:8]}",
        workflow_run_id=run.id,
        workflow_step_key="multi_size_generation",
        image_params=image_params,
        workflow_meta={
            "workflow_action": "poster_revise",
            "workflow_render_id": render.id,
            "workflow_master_id": master.id,
            "workflow_revision_scope": body.scope,
            "workflow_revision_source_image_id": render.image_id,
        },
    )
    render.task_ids = [*(render.task_ids or []), *gen_ids]
    render.status = "revising"
    multi_step = await _step(db, run.id, "multi_size_generation")
    multi_step.task_ids = _dedupe_nonempty([*(multi_step.task_ids or []), *gen_ids])
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "expected_render_count": 1,
        "active_render_id": render.id,
        "active_task_ids": _dedupe_nonempty(gen_ids),
    }
    if multi_step.status not in {"running"}:
        multi_step.status = "running"
    run.current_step = "multi_size_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@facade_entry
async def _do_poster_inpaint(
    db: AsyncSession,
    *,
    user: User,
    run: WorkflowRun,
    render: PosterRender,
    instruction: str,
    mask_image_id: str,
) -> WorkflowRunOut:
    """执行 inpaint：mask + 用户编辑意图 → mask_image_id 透传给 worker，
    worker 侧用 _wrap_inpaint_prompt 包裹（OpenAI invariant 模板，2026-05-07 实测）。"""
    if not mask_image_id:
        raise _http("missing_mask", "inpaint requires mask_image_id", 422)
    # mask 校验：和 render 同一用户
    await _validate_owned_images(
        db,
        user_id=user.id,
        image_ids=[mask_image_id],
        min_count=1,
        max_count=1,
    )
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    # 参考图：当前 render 图作为底图（mask 应用于其上）
    ref_ids = [render.image_id] if render.image_id else []
    quality_mode = str(render.metadata_jsonb.get("quality_mode") or run.quality_mode)
    image_params = _poster_image_params(
        aspect_ratio=render.aspect_ratio,
        quality_mode=quality_mode,
        count=1,
    )
    # prompt：只传用户原始编辑意图（短句），worker 侧会用 invariant 模板包装。
    bundle, _, gen_ids = await _create_poster_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=instruction.strip(),
        attachment_ids=ref_ids,
        idempotency_key=f"wf:{run.id[:18]}:in:{render.id[:8]}:{new_uuid7()[:8]}",
        workflow_run_id=run.id,
        workflow_step_key="multi_size_generation",
        image_params=image_params,
        workflow_meta={
            "workflow_action": "poster_inpaint",
            "workflow_render_id": render.id,
            "workflow_revision_source_image_id": render.image_id,
            "workflow_inpaint_mask_image_id": mask_image_id,
        },
        mask_image_id=mask_image_id,
    )
    render.task_ids = [*(render.task_ids or []), *gen_ids]
    render.status = "revising"
    multi_step = await _step(db, run.id, "multi_size_generation")
    multi_step.task_ids = _dedupe_nonempty([*(multi_step.task_ids or []), *gen_ids])
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "expected_render_count": 1,
        "active_render_id": render.id,
        "active_task_ids": _dedupe_nonempty(gen_ids),
    }
    if multi_step.status not in {"running"}:
        multi_step.status = "running"
    run.current_step = "multi_size_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=run.id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/renders/{render_id}/inpaint",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def inpaint_poster_render(
    workflow_run_id: str,
    render_id: str,
    body: PosterInpaintIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """局部 inpaint 单独端点；语义等价于 revise(scope="inpaint")，但 mask 必填。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    render = (
        await db.execute(
            select(PosterRender).where(
                PosterRender.id == render_id,
                PosterRender.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if render is None:
        raise _http("not_found", "poster render not found", 404)
    if not render.image_id:
        raise _http("render_no_image", "render has no image yet", 409)
    return await _do_poster_inpaint(
        db,
        user=user,
        run=run,
        render=render,
        instruction=body.instruction,
        mask_image_id=body.mask_image_id,
    )


FACADE_EXPORTS = (
    "POSTER_WORKFLOW_TYPE",
    "POSTER_WORKFLOW_STEPS",
    "POSTER_DEFAULT_TARGET_ASPECTS",
    "POSTER_MASTER_ASPECT",
    "_poster_image_params",
    "_poster_master_image_params",
    "_poster_find_preset_item",
    "_poster_style_from_preset",
    "_poster_load_style",
    "_poster_copy_analysis_prompt",
    "_poster_style_summary",
    "_poster_layout_safe_area",
    "_poster_text_fields_block",
    "_poster_brand_assets_block",
    "_poster_brand_attachment_ids",
    "_poster_master_prompt",
    "_poster_render_prompt",
    "_poster_revision_prompt",
    "_poster_seed_steps",
    "_create_poster_workflow_task",
    "_poster_parse_copy_analysis_text",
    "_poster_merge_copy_corrections",
    "_sync_poster_workflow_outputs",
    "create_poster_design_workflow",
    "approve_copy_analysis",
    "create_poster_masters",
    "approve_poster_master",
    "_poster_selected_master",
    "create_poster_renders",
    "revise_poster_render",
    "_do_poster_inpaint",
    "inpaint_poster_render",
)


def export_to_facade(facade: Any) -> None:
    _ROUTE_FACADE.export(facade, FACADE_EXPORTS)
