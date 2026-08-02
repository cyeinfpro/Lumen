"""Shared workflow persistence, cleanup, and response projection helpers."""

from __future__ import annotations

from datetime import datetime
import logging
import re
from typing import Any, Iterable

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core import billing as billing_core
from lumen_core.constants import CompletionStatus, GenerationStatus
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    ModelCandidate,
    ModelLibraryItem,
    PosterMaster,
    PosterRender,
    User,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.schemas import (
    GenerationOut,
    ModelCandidateOut,
    PosterMasterOut,
    PosterRenderOut,
    QualityReportOut,
    WorkflowRunOut,
    WorkflowStepOut,
)

from ...billing_cache_state import invalidate_balance_cache  # noqa: F401
from ...db import affected_rows
from ...redis_client import get_redis  # noqa: F401
from ...services.active_task_cleanup import (
    cancel_completion_rows,
    cancel_generation_rows,
    post_commit_best_effort_cleanup,
)
from ...services.generation_queue import (
    GenerationQueueReleaseToken,
    capture_queued_generation_cleanup_entries,
    capture_generation_queue_state,
    completion_cancel_requires_durable_settlement,
    current_execution_epoch,
    generation_cancel_requires_durable_settlement,
    release_generation_queue_state,
)
from ..domain.apparel_library import (
    normalize_age_segment as _normalize_age_segment,
)  # noqa: F401
from .output_sync import coerce_string_list as _coerce_string_list  # noqa: F401
from .output_sync import load_quality_reports as _load_quality_reports  # noqa: F401
from .serialization import clean_string_list as _clean_string_list  # noqa: F401
from .serialization import dedupe_nonempty as _dedupe_nonempty  # noqa: F401
from .serialization import http as _http  # noqa: F401
from .serialization import now as _now  # noqa: F401
from .workflow_assets import (
    attach_workflow_assets as _attach_workflow_assets_impl,
    image_out_map as _image_out_map,
)
from .workflow_tasks import (
    accessory_plan_from_product_analysis as _accessory_plan_from_product_analysis,
    accessory_preview_image_params as _accessory_preview_image_params,
    accessory_preview_prompt as _accessory_preview_prompt,
    candidate_image_params as _candidate_image_params,
    coerce_accessory_plan_payload as _coerce_accessory_plan_payload,
    create_workflow_task as _create_workflow_task,
    image_params as _image_params,
    merge_product_corrections as _merge_product_corrections,
    publish_bundles as _publish_bundles,
    revision_prompt as _revision_prompt,
)


WORKFLOW_TYPE = "apparel_model_showcase"
WORKFLOW_STEPS = (
    "upload_product",
    "product_analysis",
    "model_settings",
    "model_candidates",
    "model_approval",
    "showcase_generation",
    "quality_review",
    "delivery",
)
POSTER_WORKFLOW_TYPE = "poster_design"
POSTER_WORKFLOW_STEPS = (
    "copy_input",
    "style_selection",
    "copy_analysis",
    "master_generation",
    "master_approval",
    "multi_size_generation",
    "delivery",
)
_QUEUE_REDIS_UNSET = object()
_WORKFLOW_ASSET_TYPE_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,63}$")
logger = logging.getLogger("app.routes.workflows")


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
        conv = await _get_owned_conversation(
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


async def _asset_step(db: AsyncSession, run_id: str, step_key: str) -> WorkflowStep:
    """Step lookup for asset attachment, where source_step_key is client input.

    A missing step here is invalid input (422) rather than workflow corruption
    (500), because the key comes from the HTTP request body.
    """
    row = (
        await db.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run_id,
                WorkflowStep.step_key == step_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("invalid_source_step", f"unknown workflow step: {step_key}", 422)
    return row


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


async def _workflow_steps_and_candidates(
    db: AsyncSession,
    run: WorkflowRun,
) -> tuple[list[WorkflowStep], list[ModelCandidate]]:
    steps = await _load_steps(db, run.id)
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
        "queued_generation_execution_epochs": {},
        "queued_generation_queue_tokens": {},
        "running_generation_ids": [],
        "streaming_completion_ids": [],
        "deferred_generation_ids": [],
        "deferred_completion_ids": [],
    }


async def _release_workflow_generation_queue_state(
    redis: Any,
    task_id: str,
    *,
    expected_execution_epoch: int,
    ownership_token: GenerationQueueReleaseToken,
) -> bool:
    return await release_generation_queue_state(
        redis,
        task_id,
        expected_execution_epoch=expected_execution_epoch,
        ownership_token=ownership_token,
    )


async def _post_commit_workflow_generated_cleanup(
    *,
    user_id: str,
    cleanup: dict[str, Any],
) -> None:
    queued_generation_ids = _cleanup_string_list(cleanup, "queued_generation_ids")
    running_generation_ids = _cleanup_string_list(cleanup, "running_generation_ids")
    streaming_completion_ids = _cleanup_string_list(cleanup, "streaming_completion_ids")
    deferred_generation_ids = _cleanup_string_list(
        cleanup,
        "deferred_generation_ids",
    )
    deferred_completion_ids = _cleanup_string_list(
        cleanup,
        "deferred_completion_ids",
    )
    released_holds = cleanup.get("holds_released")
    cancel_ids = [
        *running_generation_ids,
        *streaming_completion_ids,
        *deferred_generation_ids,
        *deferred_completion_ids,
    ]
    redis = get_redis() if queued_generation_ids or cancel_ids else None
    queued_generation_entries = (
        await capture_queued_generation_cleanup_entries(redis, cleanup)
        if redis is not None
        else []
    )
    await post_commit_best_effort_cleanup(
        redis,
        user_id=user_id,
        queued_entries=queued_generation_entries,
        cancel_ids=cancel_ids,
        invalidate_balance_required=(
            isinstance(released_holds, int) and released_holds > 0
        ),
        release_queue_state=_release_workflow_generation_queue_state,
        invalidate_balance=invalidate_balance_cache,
        logger=logger,
        queue_failure_message=(
            "workflow delete image_queue release failed task=%s err=%s"
        ),
        cancel_failure_message=("workflow delete cancel signal failed task=%s err=%s"),
        balance_failure_message=(
            "workflow delete balance cache invalidation failed user=%s err=%s"
        ),
    )


async def _cancel_workflow_generation_rows(
    generation_rows: Iterable[Generation],
    *,
    deleted_at: datetime,
    cancel_message: str,
    queue_redis: Any,
) -> tuple[
    list[Generation],
    list[str],
    dict[str, int],
    dict[str, Any],
    list[str],
    list[str],
]:
    cleanup = await cancel_generation_rows(
        list(generation_rows),
        canceled_at=deleted_at,
        cancel_message=cancel_message,
        queue_redis=queue_redis,
        capture_queue_ownership=True,
        logger=logger,
        snapshot_failure_message=(
            "workflow delete image_queue ownership snapshot failed task=%s err=%s"
        ),
        requires_durable_settlement=(generation_cancel_requires_durable_settlement),
        execution_epoch_for=current_execution_epoch,
        capture_queue_state=capture_generation_queue_state,
    )
    return (
        cleanup.queued_rows,
        cleanup.queued_ids,
        cleanup.queued_execution_epochs,
        cleanup.queued_queue_tokens,
        cleanup.deferred_ids,
        cleanup.running_ids,
    )


async def _soft_delete_workflow_generated_images(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    deleted_at: datetime,
    cancel_message: str,
    account_mode: str = "wallet",
    queue_redis: Any = _QUEUE_REDIS_UNSET,
) -> dict[str, Any]:
    """Soft-delete images produced by a workflow and cancel its active tasks.

    Images explicitly saved into the user's model library are preserved; those
    are no longer just transient task outputs.
    """
    if getattr(run, "deleted_at", None) is not None:
        return _empty_workflow_generated_cleanup()
    if queue_redis is _QUEUE_REDIS_UNSET:
        queue_redis = get_redis()

    steps, candidates = await _workflow_steps_and_candidates(db, run)
    task_ids = _workflow_direct_task_ids(steps, candidates)
    image_ids = _workflow_direct_image_ids(steps, candidates)
    generation_rows = await _workflow_generation_rows_from_task_ids(
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
    queued_generation_execution_epochs: dict[str, int] = {}
    queued_generation_queue_tokens: dict[str, Any] = {}
    deferred_generation_ids: list[str] = []
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
            queued_generation_execution_epochs,
            queued_generation_queue_tokens,
            deferred_generation_ids,
            running_generation_ids,
        ) = await _cancel_workflow_generation_rows(
            canceled_generation_rows,
            deleted_at=deleted_at,
            cancel_message=cancel_message,
            queue_redis=queue_redis,
        )
        canceled_generations = len(canceled_generation_rows)

    canceled_completions = 0
    canceled_completion_rows: list[Completion] = []
    queued_completion_rows: list[Completion] = []
    deferred_completion_ids: list[str] = []
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
        completion_cleanup = await cancel_completion_rows(
            canceled_completion_rows,
            canceled_at=deleted_at,
            cancel_message=cancel_message,
            requires_durable_settlement=(completion_cancel_requires_durable_settlement),
        )
        queued_completion_rows = completion_cleanup.queued_rows
        deferred_completion_ids = completion_cleanup.deferred_ids
        streaming_completion_ids = completion_cleanup.streaming_ids
        canceled_completions = len(canceled_completion_rows)

    released_holds = 0
    should_release_queued_holds = account_mode == "wallet"
    if (
        not should_release_queued_holds
        and (queued_generation_rows or queued_completion_rows)
        and await _workflow_wallet_exists(db, run.user_id)
    ):
        should_release_queued_holds = True
    if should_release_queued_holds:
        for generation in queued_generation_rows:
            released_holds += int(
                await _release_soft_deleted_task_hold(
                    db,
                    user_id=run.user_id,
                    ref_type="generation",
                    ref_id=billing_core.generation_billing_ref_id(generation),
                    reason=cancel_message,
                )
            )
        for completion in queued_completion_rows:
            released_holds += int(
                await _release_soft_deleted_task_hold(
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

    cleanup = _empty_workflow_generated_cleanup()
    cleanup.update(
        {
            "images_deleted": deleted_images,
            "generations_canceled": canceled_generations,
            "completions_canceled": canceled_completions,
            "holds_released": released_holds,
            "queued_generation_ids": queued_generation_ids,
            "queued_generation_execution_epochs": (queued_generation_execution_epochs),
            "queued_generation_queue_tokens": queued_generation_queue_tokens,
            "running_generation_ids": running_generation_ids,
            "streaming_completion_ids": streaming_completion_ids,
            "deferred_generation_ids": deferred_generation_ids,
            "deferred_completion_ids": deferred_completion_ids,
        }
    )
    return cleanup


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
    return await _attach_workflow_assets_impl(
        db,
        run=run,
        user_id=user_id,
        image_ids=image_ids,
        asset_type=asset_type,
        source_step_key=source_step_key,
        label=label,
        added_at=added_at,
        step_loader=_asset_step,
    )


async def _build_run_out(db: AsyncSession, run: WorkflowRun) -> WorkflowRunOut:
    """Build a response projection without reconciling or writing workflow state."""
    steps = await _load_steps(db, run.id)
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
    reports = await _load_quality_reports(db, run.id)

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
        generations = await _workflow_generation_rows_from_task_ids(
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

    image_map = await _image_out_map(db, owned_images)
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


# Public workflow contracts.
accessory_plan_from_product_analysis = _accessory_plan_from_product_analysis
accessory_preview_image_params = _accessory_preview_image_params
accessory_preview_prompt = _accessory_preview_prompt
attach_workflow_assets = _attach_workflow_assets
build_run_out = _build_run_out
candidate_image_params = _candidate_image_params
coerce_accessory_plan_payload = _coerce_accessory_plan_payload
create_workflow_task = _create_workflow_task
get_or_create_workflow_conversation = _get_or_create_workflow_conversation
get_owned_conversation = _get_owned_conversation
get_run = _get_run
image_out_map = _image_out_map
image_params = _image_params
infer_age_segment_from_workflow = _infer_age_segment_from_workflow
load_steps = _load_steps
merge_product_corrections = _merge_product_corrections
metadata_model_profile_from_prompt = _metadata_model_profile_from_prompt
post_commit_workflow_generated_cleanup = _post_commit_workflow_generated_cleanup
primary_candidate_image_id = _primary_candidate_image_id
publish_bundles = _publish_bundles
revision_prompt = _revision_prompt
selected_candidate = _selected_candidate
soft_delete_workflow_generated_images = _soft_delete_workflow_generated_images
step = _step
workflow_generation_rows_from_task_ids = _workflow_generation_rows_from_task_ids
