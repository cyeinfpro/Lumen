"""Shared workflow persistence, cleanup, and response projection helpers."""

from __future__ import annotations

from datetime import datetime
import importlib
import logging
import re
import sys
from typing import Any, Iterable

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core import billing as billing_core
from lumen_core.constants import CompletionStatus, GenerationStatus, Intent, Role
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    ModelCandidate,
    ModelLibraryItem,
    PosterMaster,
    PosterRender,
    User,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.schemas import (
    ChatParamsIn,
    GenerationOut,
    ImageOut,
    ImageParamsIn,
    ModelCandidateOut,
    PosterMasterOut,
    PosterRenderOut,
    QualityReportOut,
    WorkflowRunListItemOut,
    WorkflowRunOut,
    WorkflowStepOut,
)

from ..billing_cache_state import invalidate_balance_cache  # noqa: F401
from ..db import affected_rows
from ..redis_client import get_redis  # noqa: F401
from ..services.generation_queue import release_generation_queue_state
from ..workflow_domain.apparel_library import _normalize_age_segment
from ..workflow_domain.showcase_model_policy import (
    _accessory_age_direction,
    _accessory_strength_direction,
)
from ..workflow_domain.workflow_contracts import _PublishBundle
from .facade import FacadeRuntime
from .output_sync import (
    _coerce_string_list,
    _load_quality_reports,  # noqa: F401
)
from .serialization import (
    _clean_string_list,
    _dedupe_nonempty,
    _http,
    _now,  # noqa: F401
)


FACADE_RUNTIME = FacadeRuntime("workflow-runtime-facade")
_SERVICE_MODULE = f"{__package__}.workflow_runtime"
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
_WORKFLOW_ASSET_TYPE_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,63}$")
logger = logging.getLogger("app.routes.workflows")


def _runtime() -> Any:
    module = sys.modules.get(_SERVICE_MODULE)
    if module is None:
        module = importlib.import_module(_SERVICE_MODULE)
    return FACADE_RUNTIME.current(module)


def _primary_candidate_image_id(candidate: ModelCandidate) -> str | None:
    if candidate.contact_sheet_image_id:
        return candidate.contact_sheet_image_id
    brief = candidate.model_brief_json or {}
    candidate_image_ids = brief.get("candidate_image_ids")
    if isinstance(candidate_image_ids, list):
        for image_id in candidate_image_ids:
            if isinstance(image_id, str) and image_id:
                return image_id
    return None


def _infer_age_segment_from_workflow(run: WorkflowRun) -> str:
    meta = run.metadata_jsonb or {}
    profile = meta.get("model_profile")
    if isinstance(profile, dict):
        age = _normalize_age_segment(profile.get("age_segment"))
        if age != "user_favorites":
            return age
    return _infer_age_segment_from_text(run.user_prompt or "")


def _metadata_model_profile_from_prompt(text: str) -> dict[str, Any]:
    gender = None
    if "女性" in text or "女" in text:
        gender = "female"
    elif "男性" in text or "男" in text:
        gender = "male"
    appearance = None
    for zh, value in (
        ("欧美", "european"),
        ("亚洲", "asian"),
        ("拉美", "latin"),
        ("中东", "middle_eastern"),
        ("非洲", "african"),
    ):
        if zh in text:
            appearance = value
            break
    return {
        "age_segment": _normalize_age_segment(_infer_age_segment_from_text(text)),
        "gender": gender,
        "appearance_direction": appearance,
    }


def _infer_age_segment_from_text(text: str) -> str:
    if "幼儿" in text:
        return "toddler"
    if any(word in text for word in ("儿童", "童装", "小朋友", "孩子")):
        return "child"
    if "青少年" in text:
        return "teen"
    if "青年" in text:
        return "young_adult"
    if "中年" in text or "中老年" in text:
        return "middle_aged"
    if "老年" in text:
        return "senior"
    if "熟龄" in text or "成年" in text:
        return "adult"
    return "user_favorites"


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
    workflow_type: str = WORKFLOW_TYPE,
) -> Conversation:
    if conversation_id:
        conv = await _runtime()._get_owned_conversation(
            db, user_id=user.id, conversation_id=conversation_id
        )
        params = dict(conv.default_params or {})
        params["workflow_type"] = workflow_type
        params["hidden_from_conversations"] = True
        conv.default_params = params
        return conv
    conv = Conversation(
        user_id=user.id,
        title=title,
        archived=True,
        default_params={
            "workflow_type": workflow_type,
            "hidden_from_conversations": True,
        },
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


async def _load_steps(
    db: AsyncSession,
    run_id: str,
    *,
    lock: bool = False,
) -> list[WorkflowStep]:
    stmt = select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id)
    if lock:
        stmt = stmt.with_for_update()
    rows = (await db.execute(stmt)).scalars().all()
    # apparel 与 poster 的 step_key 互不重叠；合并成一张顺序表，
    # 未识别的 key 保留尾部稳定顺序。
    order: dict[str, int] = {}
    for idx, key in enumerate(WORKFLOW_STEPS):
        order[key] = idx
    for idx, key in enumerate(POSTER_WORKFLOW_STEPS):
        order[key] = len(WORKFLOW_STEPS) + idx
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


async def _workflow_steps_and_candidates(
    db: AsyncSession,
    run: WorkflowRun,
) -> tuple[list[WorkflowStep], list[ModelCandidate]]:
    steps = await _runtime()._load_steps(db, run.id)
    candidates = list(
        (
            await db.execute(
                select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    return steps, candidates


def _workflow_direct_task_ids(
    steps: Iterable[WorkflowStep],
    candidates: Iterable[ModelCandidate],
) -> list[str]:
    return _dedupe_nonempty(
        [
            *(task_id for step in steps for task_id in (step.task_ids or [])),
            *(
                task_id
                for candidate in candidates
                for task_id in (candidate.task_ids or [])
            ),
        ]
    )


def _workflow_direct_image_ids(
    steps: Iterable[WorkflowStep],
    candidates: Iterable[ModelCandidate],
) -> list[str]:
    return _dedupe_nonempty(
        [
            *(image_id for step in steps for image_id in (step.image_ids or [])),
            *(
                image_id
                for candidate in candidates
                for image_id in _candidate_reference_image_ids(candidate)
            ),
        ]
    )


def _candidate_reference_image_ids(candidate: ModelCandidate) -> list[str]:
    brief = getattr(candidate, "model_brief_json", None) or {}
    raw_candidate_ids = brief.get("candidate_image_ids")
    candidate_image_ids = (
        raw_candidate_ids if isinstance(raw_candidate_ids, list) else []
    )
    return _dedupe_nonempty(
        [
            *(
                image_id
                for image_id in candidate_image_ids
                if isinstance(image_id, str)
            ),
            *(
                image_id
                for image_id in (
                    candidate.contact_sheet_image_id,
                    candidate.portrait_image_id,
                    candidate.front_image_id,
                    candidate.side_image_id,
                    candidate.back_image_id,
                )
                if isinstance(image_id, str)
            ),
        ]
    )


async def _workflow_generation_rows_from_task_ids(
    db: AsyncSession,
    *,
    user_id: str,
    task_ids: list[str],
    include_dual_bonus: bool,
) -> list[Generation]:
    task_ids = _dedupe_nonempty(task_ids)
    if not task_ids:
        return []
    base_generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.id.in_(task_ids),
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not include_dual_bonus:
        return base_generations
    bonus_generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.upstream_request["parent_generation_id"].astext.in_(
                        task_ids
                    ),
                    Generation.upstream_request["is_dual_race_bonus"]
                    .as_boolean()
                    .is_(True),
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [*base_generations, *bonus_generations]


async def _release_soft_deleted_task_hold(
    db: AsyncSession,
    *,
    user_id: str,
    ref_type: str,
    ref_id: str,
    reason: str,
) -> bool:
    try:
        tx = await billing_core.release(
            db,
            user_id,
            ref_type=ref_type,
            ref_id=ref_id,
            idempotency_key=f"workflow_delete:{ref_type}:{ref_id}",
            meta={"reason": reason},
        )
    except billing_core.BillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    return tx is not None


async def _workflow_wallet_exists(db: AsyncSession, user_id: str) -> bool:
    wallet = await billing_core.get_wallet(db, user_id, lock=False, create=False)
    return wallet is not None


def _cleanup_string_list(cleanup: dict[str, Any], key: str) -> list[str]:
    values = cleanup.get(key)
    if not isinstance(values, list):
        return []
    return _dedupe_nonempty(value for value in values if isinstance(value, str))


def _empty_workflow_generated_cleanup() -> dict[str, Any]:
    return {
        "images_deleted": 0,
        "generations_canceled": 0,
        "completions_canceled": 0,
        "holds_released": 0,
        "queued_generation_ids": [],
        "running_generation_ids": [],
        "streaming_completion_ids": [],
    }


async def _release_workflow_generation_queue_state(redis: Any, task_id: str) -> None:
    await release_generation_queue_state(redis, task_id)


async def _post_commit_workflow_generated_cleanup(
    *,
    user_id: str,
    cleanup: dict[str, Any],
) -> None:
    runtime = _runtime()
    queued_generation_ids = runtime._cleanup_string_list(
        cleanup, "queued_generation_ids"
    )
    running_generation_ids = runtime._cleanup_string_list(
        cleanup, "running_generation_ids"
    )
    streaming_completion_ids = runtime._cleanup_string_list(
        cleanup, "streaming_completion_ids"
    )
    released_holds = cleanup.get("holds_released")
    if (
        not queued_generation_ids
        and not running_generation_ids
        and not streaming_completion_ids
    ):
        if isinstance(released_holds, int) and released_holds > 0:
            try:
                await runtime.invalidate_balance_cache(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "workflow delete balance cache invalidation failed user=%s err=%s",
                    user_id,
                    exc,
                )
        return

    redis = runtime.get_redis()
    for task_id in queued_generation_ids:
        try:
            await runtime._release_workflow_generation_queue_state(redis, task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow delete image_queue release failed task=%s err=%s",
                task_id,
                exc,
            )
    for task_id in [*running_generation_ids, *streaming_completion_ids]:
        try:
            await redis.set(f"task:{task_id}:cancel", "1", ex=3600)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow delete cancel signal failed task=%s err=%s",
                task_id,
                exc,
            )
    if isinstance(released_holds, int) and released_holds > 0:
        try:
            await runtime.invalidate_balance_cache(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow delete balance cache invalidation failed user=%s err=%s",
                user_id,
                exc,
            )


def _cancel_workflow_generation_rows(
    generation_rows: Iterable[Generation],
    *,
    deleted_at: datetime,
    cancel_message: str,
) -> tuple[list[Generation], list[str], list[str]]:
    queued_rows: list[Generation] = []
    queued_ids: list[str] = []
    running_ids: list[str] = []
    for generation in generation_rows:
        # Running rows retain their hold until the worker confirms that the
        # upstream awaitable stopped; only queued rows finalize here.
        if generation.status == GenerationStatus.QUEUED.value:
            queued_rows.append(generation)
            queued_ids.append(generation.id)
            generation.status = GenerationStatus.CANCELED.value
            generation.progress_stage = "finalizing"
            generation.finished_at = deleted_at
            generation.error_code = "cancelled"
            generation.error_message = cancel_message
        elif generation.status == GenerationStatus.RUNNING.value:
            running_ids.append(generation.id)
    return queued_rows, queued_ids, running_ids


async def _soft_delete_workflow_generated_images(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    deleted_at: datetime,
    cancel_message: str,
    account_mode: str = "wallet",
) -> dict[str, Any]:
    """Soft-delete images produced by a workflow and cancel its active tasks.

    Images explicitly saved into the user's model library are preserved; those
    are no longer just transient task outputs.
    """
    runtime = _runtime()
    if getattr(run, "deleted_at", None) is not None:
        return runtime._empty_workflow_generated_cleanup()

    steps, candidates = await runtime._workflow_steps_and_candidates(db, run)
    task_ids = runtime._workflow_direct_task_ids(steps, candidates)
    image_ids = runtime._workflow_direct_image_ids(steps, candidates)
    generation_rows = await runtime._workflow_generation_rows_from_task_ids(
        db,
        user_id=run.user_id,
        task_ids=task_ids,
        include_dual_bonus=True,
    )
    generation_ids = _dedupe_nonempty(generation.id for generation in generation_rows)

    canceled_generations = 0
    canceled_generation_rows: list[Generation] = []
    queued_generation_rows: list[Generation] = []
    queued_generation_ids: list[str] = []
    running_generation_ids: list[str] = []
    if generation_ids:
        canceled_generation_rows = list(
            (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.user_id == run.user_id,
                        Generation.id.in_(generation_ids),
                        Generation.status.in_(
                            [
                                GenerationStatus.QUEUED.value,
                                GenerationStatus.RUNNING.value,
                            ]
                        ),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        (
            queued_generation_rows,
            queued_generation_ids,
            running_generation_ids,
        ) = runtime._cancel_workflow_generation_rows(
            canceled_generation_rows,
            deleted_at=deleted_at,
            cancel_message=cancel_message,
        )
        canceled_generations = len(canceled_generation_rows)

    canceled_completions = 0
    canceled_completion_rows: list[Completion] = []
    queued_completion_rows: list[Completion] = []
    streaming_completion_ids: list[str] = []
    if task_ids:
        canceled_completion_rows = list(
            (
                await db.execute(
                    select(Completion)
                    .where(
                        Completion.user_id == run.user_id,
                        Completion.id.in_(task_ids),
                        Completion.status.in_(
                            [
                                CompletionStatus.QUEUED.value,
                                CompletionStatus.STREAMING.value,
                            ]
                        ),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for completion in canceled_completion_rows:
            if completion.status == CompletionStatus.QUEUED.value:
                queued_completion_rows.append(completion)
                completion.status = CompletionStatus.CANCELED.value
                completion.progress_stage = "finalizing"
                completion.finished_at = deleted_at
                completion.error_code = "cancelled"
                completion.error_message = cancel_message
            elif completion.status == CompletionStatus.STREAMING.value:
                streaming_completion_ids.append(completion.id)
        canceled_completions = len(canceled_completion_rows)

    released_holds = 0
    should_release_queued_holds = account_mode == "wallet"
    if (
        not should_release_queued_holds
        and (queued_generation_rows or queued_completion_rows)
        and await runtime._workflow_wallet_exists(db, run.user_id)
    ):
        should_release_queued_holds = True
    if should_release_queued_holds:
        for generation in queued_generation_rows:
            released_holds += int(
                await runtime._release_soft_deleted_task_hold(
                    db,
                    user_id=run.user_id,
                    ref_type="generation",
                    ref_id=billing_core.generation_billing_ref_id(generation),
                    reason=cancel_message,
                )
            )
        for completion in queued_completion_rows:
            released_holds += int(
                await runtime._release_soft_deleted_task_hold(
                    db,
                    user_id=run.user_id,
                    ref_type="completion",
                    ref_id=billing_core.completion_billing_ref_id(completion),
                    reason=cancel_message,
                )
            )

    deleted_images = 0
    image_matchers = []
    if generation_ids:
        image_matchers.append(Image.owner_generation_id.in_(generation_ids))
    if image_ids:
        image_matchers.append(Image.id.in_(image_ids))
    if image_matchers:
        preserved_library_images = select(ModelLibraryItem.image_id).where(
            ModelLibraryItem.user_id == run.user_id,
            ModelLibraryItem.image_id.is_not(None),
        )
        result = await db.execute(
            update(Image)
            .where(
                Image.user_id == run.user_id,
                Image.deleted_at.is_(None),
                or_(*image_matchers),
                ~Image.id.in_(preserved_library_images),
            )
            .values(deleted_at=deleted_at)
            .execution_options(synchronize_session=False)
        )
        deleted_images = affected_rows(result)

    cleanup = runtime._empty_workflow_generated_cleanup()
    cleanup.update(
        {
            "images_deleted": deleted_images,
            "generations_canceled": canceled_generations,
            "completions_canceled": canceled_completions,
            "holds_released": released_holds,
            "queued_generation_ids": queued_generation_ids,
            "running_generation_ids": running_generation_ids,
            "streaming_completion_ids": streaming_completion_ids,
        }
    )
    return cleanup


def _revision_prompt(
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


def _accessory_preview_prompt(
    *,
    accessory_plan: dict[str, Any],
    style_prompt: str,
    age_context: str = "",
) -> str:
    items = accessory_plan.get("items")
    item_list = _clean_string_list(
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
    age_direction = _accessory_age_direction(" ".join([age_context, style]).strip())
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
        f"配饰强度：{_accessory_strength_direction(strength)}。"
        "配饰必须真实贴合身体和透视：耳饰在耳垂位置，项链贴合颈部，包带、腰带、鞋帽与姿态一致；"
        "不能漂浮、变形、穿模，不能遮挡脸、手、脚和身体轮廓。"
        "不要让配饰遮挡未来商品展示区域；不要添加多余道具、家具、背景场景或手持物，"
        "除非明确列在配饰里。"
        f"年龄与风格：{age_direction} "
        f"补充方向：{style}。"
        "输出风格：高质量真实商业摄影参考图，清晰、干净、可作为后续服饰电商生成的稳定参考。"
    )


def _accessory_plan_from_product_analysis(
    product_analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_items = (product_analysis or {}).get("styling_recommendations")
    items = _clean_string_list(_coerce_string_list(raw_items), max_items=3, max_len=80)
    return {
        "enabled": True,
        "items": items,
        "strength": "subtle",
    }


def _coerce_accessory_plan_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    enabled = bool(value.get("enabled", True))
    strength = str(value.get("strength") or "subtle")
    if strength not in {"subtle", "medium", "strong"}:
        strength = "subtle"
    items = value.get("items")
    return {
        "enabled": enabled,
        "items": _clean_string_list(
            (str(item) for item in items) if isinstance(items, list) else [],
            max_items=12,
            max_len=80,
        ),
        "strength": strength,
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

    result = await _runtime()._create_assistant_task(
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
    redis = _runtime().get_redis()
    for bundle in bundles:
        await _runtime()._publish_message_appended(
            redis=redis,
            user_id=user_id,
            conv_id=conv_id,
            message_ids=bundle.message_ids,
        )
        await _runtime()._publish_assistant_task(
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


def _candidate_image_params() -> ImageParamsIn:
    params = _runtime()._image_params(
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        fast=False,
    )
    return params.model_copy(
        update={"output_format": "png", "output_compression": None}
    )


def _accessory_preview_image_params() -> ImageParamsIn:
    params = _runtime()._image_params(
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        final_quality="high",
        fast=False,
    )
    return params.model_copy(
        update={"output_format": "png", "output_compression": None}
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
    final["confirmed_at"] = _runtime()._now().isoformat()
    return final


def _next_action_for(run: WorkflowRun) -> str:
    if run.status == "completed":
        return "查看交付"
    if run.type == POSTER_WORKFLOW_TYPE:
        return {
            "copy_analysis": "确认海报文案",
            "master_generation": "生成母版方案",
            "master_approval": "选定母版",
            "multi_size_generation": "生成/确认多尺寸",
            "delivery": "下载海报成品",
        }.get(run.current_step, "继续海报项目")
    return {
        "product_analysis": "确认商品约束",
        "model_settings": "生成模特候选",
        "model_candidates": "等待模特候选",
        "model_approval": "确认模特",
        "showcase_generation": "开始生成展示图",
        "quality_review": "查看质检",
        "delivery": "下载最终图",
    }.get(run.current_step, "继续项目")


def _workflow_asset_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("workflow_run_id") or ""),
        str(record.get("image_id") or ""),
        str(record.get("asset_type") or ""),
        str(record.get("source_step_key") or ""),
    )


def _workflow_asset_records(
    *,
    run: WorkflowRun,
    image_ids: list[str],
    asset_type: str,
    source_step_key: str,
    label: str | None,
    added_at: datetime,
) -> list[dict[str, Any]]:
    clean_label = (label or "").strip() or None
    records: list[dict[str, Any]] = []
    for image_id in image_ids:
        record: dict[str, Any] = {
            "workflow_run_id": run.id,
            "workflow_type": run.type,
            "project_title": run.title,
            "image_id": image_id,
            "asset_type": asset_type,
            "source_step_key": source_step_key,
            "added_at": added_at.isoformat(),
        }
        if clean_label:
            record["label"] = clean_label
        records.append(record)
    return records


def _merge_workflow_asset_metadata(
    metadata: dict[str, Any] | None,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(metadata or {})
    existing_raw = payload.get("assets")
    existing = (
        [
            dict(record)
            for record in existing_raw
            if isinstance(record, dict) and isinstance(record.get("image_id"), str)
        ]
        if isinstance(existing_raw, list)
        else []
    )
    replace_keys = {_workflow_asset_key(record) for record in records}
    merged = [
        record for record in existing if _workflow_asset_key(record) not in replace_keys
    ]
    merged.extend(records)
    payload["assets"] = merged[-200:]
    payload["asset_image_ids"] = _dedupe_nonempty(
        str(record.get("image_id") or "") for record in payload["assets"]
    )
    payload["asset_count"] = len(payload["asset_image_ids"])
    return payload


def _merge_image_workflow_asset_metadata(
    metadata: dict[str, Any] | None,
    record: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(metadata or {})
    existing_raw = payload.get("workflow_assets")
    existing = (
        [
            dict(item)
            for item in existing_raw
            if isinstance(item, dict) and isinstance(item.get("workflow_run_id"), str)
        ]
        if isinstance(existing_raw, list)
        else []
    )
    key = _workflow_asset_key(record)
    merged = [item for item in existing if _workflow_asset_key(item) != key]
    merged.append(record)
    payload["workflow_assets"] = merged[-50:]
    payload["latest_workflow_asset"] = record
    return payload


async def _attach_workflow_assets(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    user_id: str,
    image_ids: list[str],
    asset_type: str,
    source_step_key: str,
    label: str | None = None,
    added_at: datetime | None = None,
) -> list[dict[str, Any]]:
    clean_asset_type = (asset_type or "").strip()
    if not _WORKFLOW_ASSET_TYPE_RE.fullmatch(clean_asset_type):
        raise _http("invalid_asset_type", "asset_type is invalid", 422)
    clean_step_key = (source_step_key or "").strip()
    if not clean_step_key:
        raise _http("missing_source_step", "source_step_key is required", 422)
    deduped_image_ids = _dedupe_nonempty(image_ids)
    if not deduped_image_ids:
        raise _http("missing_images", "image_ids cannot be empty", 422)

    images = list(
        (
            await db.execute(
                select(Image).where(
                    Image.user_id == user_id,
                    Image.id.in_(deduped_image_ids),
                    Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    image_by_id = {image.id: image for image in images}
    missing = [
        image_id for image_id in deduped_image_ids if image_id not in image_by_id
    ]
    if missing:
        raise _http(
            "image_not_found",
            "one or more images are not available for this workflow",
            404,
        )

    runtime = _runtime()
    step = await runtime._step(db, run.id, clean_step_key)
    now = added_at or runtime._now()
    records = _workflow_asset_records(
        run=run,
        image_ids=deduped_image_ids,
        asset_type=clean_asset_type,
        source_step_key=clean_step_key,
        label=label,
        added_at=now,
    )
    run.metadata_jsonb = _merge_workflow_asset_metadata(run.metadata_jsonb, records)
    step.image_ids = _dedupe_nonempty([*(step.image_ids or []), *deduped_image_ids])
    existing_step_asset_ids = (step.output_json or {}).get("asset_image_ids")
    if not isinstance(existing_step_asset_ids, list):
        existing_step_asset_ids = []
    step_asset_ids = _dedupe_nonempty([*existing_step_asset_ids, *deduped_image_ids])
    step.output_json = {
        **(step.output_json or {}),
        "asset_image_ids": step_asset_ids,
        "asset_count": len(step_asset_ids),
        "asset_updated_at": now.isoformat(),
    }
    for record in records:
        image = image_by_id[record["image_id"]]
        image.metadata_jsonb = _merge_image_workflow_asset_metadata(
            image.metadata_jsonb,
            record,
        )
    return records


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
    return {
        image.id: _runtime()._image_to_out(image, variant_map.get(image.id))
        for image in images
    }


def _image_to_out(img: Image, variant_kinds: set[str] | None = None) -> ImageOut:
    variant_kinds = variant_kinds or set()
    metadata = img.metadata_jsonb if isinstance(img.metadata_jsonb, dict) else {}
    billing_label = (
        metadata.get("billing_label")
        if isinstance(metadata.get("billing_label"), str)
        else None
    )
    billing_exempt_reason = (
        metadata.get("billing_exempt_reason")
        if isinstance(metadata.get("billing_exempt_reason"), str)
        else None
    )
    is_dual_race_bonus = metadata.get("is_dual_race_bonus") is True
    billing_free = (
        metadata.get("billing_free") is True
        or is_dual_race_bonus
        or billing_label == "free"
    )
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
        metadata_jsonb=metadata,
        is_dual_race_bonus=is_dual_race_bonus,
        billing_free=billing_free,
        billing_label=billing_label,
        billing_exempt_reason=billing_exempt_reason,
    )


async def _build_run_out(db: AsyncSession, run: WorkflowRun) -> WorkflowRunOut:
    """Build a response projection without reconciling or writing workflow state."""
    runtime = _runtime()
    steps = await runtime._load_steps(db, run.id)
    candidates = list(
        (
            await db.execute(
                select(ModelCandidate)
                .where(ModelCandidate.workflow_run_id == run.id)
                .order_by(ModelCandidate.candidate_index.asc())
            )
        )
        .scalars()
        .all()
    )
    reports = await runtime._load_quality_reports(db, run.id)

    # 先拉海报相关行；poster_masters/renders 的 image_id 和 task_ids 要
    # 加入下面 owned_images / generations 的扫描集合。
    poster_masters_rows: list[PosterMaster] = []
    poster_renders_rows: list[PosterRender] = []
    if run.type == POSTER_WORKFLOW_TYPE:
        poster_masters_rows = list(
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
        poster_renders_rows = list(
            (
                await db.execute(
                    select(PosterRender)
                    .where(PosterRender.workflow_run_id == run.id)
                    .order_by(PosterRender.created_at.asc(), PosterRender.id.asc())
                )
            )
            .scalars()
            .all()
        )

    all_task_ids: set[str] = set()
    image_ids: set[str] = set(run.product_image_ids or [])
    for step in steps:
        all_task_ids.update(step.task_ids or [])
        image_ids.update(step.image_ids or [])
    for candidate in candidates:
        all_task_ids.update(candidate.task_ids or [])
        image_ids.update(_candidate_reference_image_ids(candidate))
    for report in reports:
        image_ids.add(report.image_id)
    for master in poster_masters_rows:
        all_task_ids.update(master.task_ids or [])
        if master.image_id:
            image_ids.add(master.image_id)
    for render in poster_renders_rows:
        all_task_ids.update(render.task_ids or [])
        if render.image_id:
            image_ids.add(render.image_id)

    generations: list[Generation] = []
    if all_task_ids:
        generations = await runtime._workflow_generation_rows_from_task_ids(
            db,
            user_id=run.user_id,
            task_ids=list(all_task_ids),
            include_dual_bonus=True,
        )
    if all_task_ids:
        owned_images = list(
            (
                await db.execute(
                    select(Image)
                    .where(
                        or_(
                            Image.id.in_(image_ids)
                            if image_ids
                            else Image.id == "__none__",
                            Image.owner_generation_id.in_(all_task_ids),
                        ),
                        Image.user_id == run.user_id,
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            )
            .scalars()
            .all()
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
            )
            .scalars()
            .all()
        )
    else:
        owned_images = []

    image_map = await runtime._image_out_map(db, owned_images)
    product_image_ids = set(run.product_image_ids or [])
    product_images = [
        image_map[iid] for iid in (run.product_image_ids or []) if iid in image_map
    ]
    generated_images = [
        image_map[image.id]
        for image in owned_images
        # 项目内的“非商品图”要都能被前端按 id 找到：
        # 包括候选图、展示图，以及从模特库选入并 materialize 到当前用户空间的参考图。
        if image.id not in product_image_ids and image.id in image_map
    ]

    poster_masters_out = [
        PosterMasterOut.model_validate(m) for m in poster_masters_rows
    ]
    poster_renders_out = [
        PosterRenderOut.model_validate(r) for r in poster_renders_rows
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
        poster_masters=poster_masters_out,
        poster_renders=poster_renders_out,
        product_images=product_images,
        generated_images=generated_images,
        generations=[GenerationOut.model_validate(g) for g in generations],
    )


def _list_item_from_run(
    run: WorkflowRun, output_count: int = 0
) -> WorkflowRunListItemOut:
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
        next_action=_runtime()._next_action_for(run),
    )


FACADE_EXPORTS = (
    "_primary_candidate_image_id",
    "_infer_age_segment_from_workflow",
    "_metadata_model_profile_from_prompt",
    "_infer_age_segment_from_text",
    "_get_owned_conversation",
    "_get_or_create_workflow_conversation",
    "_get_run",
    "_load_steps",
    "_step",
    "_workflow_steps_and_candidates",
    "_workflow_direct_task_ids",
    "_workflow_direct_image_ids",
    "_candidate_reference_image_ids",
    "_workflow_generation_rows_from_task_ids",
    "_release_soft_deleted_task_hold",
    "_workflow_wallet_exists",
    "_cleanup_string_list",
    "_empty_workflow_generated_cleanup",
    "_release_workflow_generation_queue_state",
    "_post_commit_workflow_generated_cleanup",
    "_cancel_workflow_generation_rows",
    "_soft_delete_workflow_generated_images",
    "_revision_prompt",
    "_accessory_preview_prompt",
    "_accessory_plan_from_product_analysis",
    "_coerce_accessory_plan_payload",
    "_create_workflow_task",
    "_publish_bundles",
    "_fixed_size_for_quality",
    "_image_params",
    "_candidate_image_params",
    "_accessory_preview_image_params",
    "_merge_product_corrections",
    "_next_action_for",
    "_workflow_asset_key",
    "_workflow_asset_records",
    "_merge_workflow_asset_metadata",
    "_merge_image_workflow_asset_metadata",
    "_attach_workflow_assets",
    "_image_out_map",
    "_image_to_out",
    "_build_run_out",
    "_list_item_from_run",
)


def export_to_facade(facade: Any) -> None:
    """Install route-bound aliases while keeping service imports route-free."""

    from .facade import bind_facade

    for name in FACADE_EXPORTS:
        value = getattr(sys.modules[_SERVICE_MODULE], name)
        setattr(
            facade,
            name,
            bind_facade(value, facade=facade, runtime=FACADE_RUNTIME),
        )
    facade._PublishBundle = _PublishBundle


__all__ = [*FACADE_EXPORTS, "_PublishBundle"]
