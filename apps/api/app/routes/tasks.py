"""Tasks 路由（DESIGN §5.5）：generations / completions 快照 + cancel/retry + 聚合。"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    CompletionStage,
    CompletionStatus,
    EV_COMP_QUEUED,
    EV_GEN_QUEUED,
    GenerationStage,
    GenerationStatus,
    task_channel,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.models import Completion, Generation, OutboxEvent, WalletTransaction
from lumen_core.schemas import (
    ActiveTasksOut,
    CompletionOut,
    GenerationOut,
    TaskItemOut,
    TaskListOut,
    TaskRecommendedActionOut,
)

from ..arq_pool import get_arq_pool
from ..billing_cache_state import invalidate_balance_cache
from ..canvas_services.task_guard import reject_canvas_retry
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..observability import task_publish_errors_total
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from ..services.generation_queue import release_generation_queue_state
from ..services.task_listing import TaskListingRuntime, build_task_list
from ..sse_publish import publish_sse_event


router = APIRouter()
logger = logging.getLogger(__name__)

_TASK_CURSOR_VERSION = 1
_TASK_KIND_RANK = {"completion": 0, "generation": 1}

_TERMINAL_ERROR_CODES = {
    "authentication_error",
    "permission_error",
    "unauthorized",
    "invalid_api_key",
    "NO_ACTIVE_API_KEY",
    "no_active_api_key",
    "INSUFFICIENT_BALANCE",
    "insufficient_credits",
    "WALLET_FROZEN",
    "wallet_frozen",
    "invalid_request_error",
    "invalid_request",
    "invalid_param",
    "invalid_value",
    "validation_error",
    "prompt_too_long",
    "bad_reference_image",
    "reference_missing",
    "missing_input_images",
    "reference_image_too_large",
    "moderation_blocked",
    "content_policy_violation",
    "safety_violation",
    "no_mask_capable_provider",
}
_WAITING_PROVIDER_CODES = {
    "all_accounts_failed",
    "all_providers_failed",
    "provider_exhausted",
    "no_providers",
    "rate_limit_error",
    "rate_limit_exceeded",
    "upstream_rate_limited",
    "quota_exceeded",
    "service_unavailable",
    "upstream_error",
}


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


def _generation_billing_ref_id(task_id: str, retry_count: int | None) -> str:
    return billing_core.retry_billing_ref_id(task_id, retry_count)


def _generation_billing_retry_count(task: Generation) -> int:
    return billing_core.generation_billing_retry_count(task)


def _completion_billing_ref_id(task_id: str, retry_count: int | None) -> str:
    return billing_core.retry_billing_ref_id(task_id, retry_count)


def _completion_billing_retry_count(task: Completion) -> int:
    return billing_core.completion_billing_retry_count(task)


def _completion_task_billing_ref_id(task: Completion) -> str:
    return billing_core.completion_billing_ref_id(task)


async def _setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    try:
        return await get_setting(db, spec)
    except (AssertionError, IndexError):
        if key.startswith("billing."):
            return None
        raise


async def _billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.enabled"),
        False,
    )


async def _billing_allow_negative(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.allow_negative_balance"),
        False,
    )


async def _generation_retry_hold_micro(db: AsyncSession, gen: Generation) -> int:
    if not await _billing_enabled(db):
        return 0
    request = _json_dict(getattr(gen, "upstream_request", None))
    image_count = _generation_request_image_count(gen)
    tier = _string_value(request.get("billing_tier"))
    if tier in {"1k", "2k", "4k"}:
        amount, _tier = await billing_core.estimate_image_cost_for_tier(
            db,
            tier=tier,
            n=image_count,
        )
        return int(amount or 0)
    pixels = (
        int(getattr(gen, "upstream_pixels", 0) or 0)
        or _task_request_int(gen, "pixel_count")
        or _task_request_int(gen, "upstream_pixels")
        or 0
    )
    if pixels <= 0:
        size = getattr(gen, "size_requested", None)
        if isinstance(size, str) and "x" in size:
            width_raw, height_raw = size.lower().split("x", 1)
            if width_raw.isdigit() and height_raw.isdigit():
                pixels = int(width_raw) * int(height_raw)
    amount, _tier = await billing_core.estimate_image_cost(
        db,
        size_px=max(0, pixels),
        n=image_count,
        thresholds=billing_core.parse_thresholds(
            await _setting_raw(db, "billing.image_size_thresholds")
        ),
    )
    return int(amount or 0)


async def _hold_generation_retry_wallet(
    db: AsyncSession,
    user_id: str,
    gen: Generation,
) -> bool:
    if not await _billing_enabled(db):
        return False
    amount = await _generation_retry_hold_micro(db, gen)
    if amount <= 0:
        return False
    retry_count = _generation_billing_retry_count(gen)
    ref_id = _generation_billing_ref_id(gen.id, retry_count)
    try:
        tx = await billing_core.hold(
            db,
            user_id,
            amount,
            ref_type="generation",
            ref_id=ref_id,
            idempotency_key=f"hold:{ref_id}",
            allow_negative=await _billing_allow_negative(db),
            meta={
                "generation_id": gen.id,
                "reason": "generation retry",
                "retry_count": retry_count,
            },
        )
    except billing_core.BillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    return tx is not None


async def _completion_retry_hold_micro(
    db: AsyncSession,
    completion: Completion,
    previous_retry_count: int,
) -> int:
    if not await _billing_enabled(db):
        return 0
    prev_ref_id = _completion_billing_ref_id(completion.id, previous_retry_count)
    hold_tx = (
        await db.execute(
            select(WalletTransaction)
            .where(
                WalletTransaction.user_id == completion.user_id,
                WalletTransaction.kind == "hold",
                WalletTransaction.ref_type == "completion",
                WalletTransaction.ref_id == prev_ref_id,
            )
            .order_by(WalletTransaction.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if hold_tx is None:
        return 0
    try:
        return max(0, -int(hold_tx.amount_micro))
    except (TypeError, ValueError):
        return 0


async def _hold_completion_retry_wallet(
    db: AsyncSession,
    user_id: str,
    completion: Completion,
    previous_retry_count: int,
) -> bool:
    if not await _billing_enabled(db):
        return False
    amount = await _completion_retry_hold_micro(db, completion, previous_retry_count)
    if amount <= 0:
        return False
    next_retry_count = previous_retry_count + 1
    ref_id = _completion_billing_ref_id(completion.id, next_retry_count)
    try:
        tx = await billing_core.hold(
            db,
            user_id,
            amount,
            ref_type="completion",
            ref_id=ref_id,
            idempotency_key=f"hold:{ref_id}",
            allow_negative=await _billing_allow_negative(db),
            meta={
                "completion_id": completion.id,
                "reason": "completion retry",
                "billing_retry_count": next_retry_count,
                "previous_billing_retry_count": previous_retry_count,
            },
        )
    except billing_core.BillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    return tx is not None


def _task_request(task: Generation | Completion) -> dict[str, Any]:
    value = getattr(task, "upstream_request", None)
    return value if isinstance(value, dict) else {}


def _task_request_value(task: Generation | Completion, key: str) -> Any:
    request = _task_request(task)
    value = request.get(key)
    if value is None and isinstance(request.get("queue_metadata"), dict):
        value = request["queue_metadata"].get(key)
    return value


def _task_request_str(task: Generation | Completion, key: str) -> str | None:
    value = _task_request_value(task, key)
    return value if isinstance(value, str) and value else None


def _task_request_int(task: Generation | Completion, key: str) -> int | None:
    value = _task_request_value(task, key)
    return value if isinstance(value, int) and value >= 0 else None


def _json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _generation_request_image_count(gen: Generation) -> int:
    request = _json_dict(getattr(gen, "upstream_request", None))
    raw = request.get("n")
    if raw is None:
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(10, value))


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _task_sort_at(task: Generation | Completion) -> datetime:
    return (
        task.created_at or task.started_at or datetime.min.replace(tzinfo=timezone.utc)
    )


def _task_sort_expr(model: Any) -> Any:
    return func.coalesce(
        model.created_at,
        model.started_at,
        datetime.min.replace(tzinfo=timezone.utc),
    )


def _encode_task_cursor(sort_at: datetime, kind: str, task_id: str) -> str:
    payload = {
        "v": _TASK_CURSOR_VERSION,
        "at": sort_at.isoformat(),
        "kind": kind,
        "id": task_id,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_task_cursor(raw: str | None) -> tuple[datetime, str, str] | None:
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != _TASK_CURSOR_VERSION:
            raise ValueError("version mismatch")
        sort_at = datetime.fromisoformat(str(payload["at"]).replace("Z", "+00:00"))
        if sort_at.tzinfo is None:
            sort_at = sort_at.replace(tzinfo=timezone.utc)
        kind = str(payload["kind"])
        task_id = str(payload["id"])
        if kind not in {"generation", "completion"} or not task_id:
            raise ValueError("invalid cursor fields")
        return sort_at, kind, task_id
    except Exception as exc:  # noqa: BLE001
        raise _http("invalid_cursor", "cursor is invalid", 422) from exc


def _task_kind_rank(kind: str) -> int:
    return _TASK_KIND_RANK[kind]


def _same_timestamp_cursor_mode(
    *,
    model_kind: Literal["generation", "completion"],
    cursor_kind: str,
) -> Literal["all", "same_kind_id", "none"]:
    model_rank = _task_kind_rank(model_kind)
    cursor_rank = _task_kind_rank(cursor_kind)
    if model_rank < cursor_rank:
        return "all"
    if model_rank == cursor_rank:
        return "same_kind_id"
    return "none"


def _apply_task_cursor(
    stmt: Any,
    model: Any,
    cursor: tuple[datetime, str, str] | None,
    *,
    model_kind: Literal["generation", "completion"],
) -> Any:
    if cursor is None:
        return stmt
    sort_at, cursor_kind, task_id = cursor
    sort_expr = _task_sort_expr(model)
    mode = _same_timestamp_cursor_mode(
        model_kind=model_kind,
        cursor_kind=cursor_kind,
    )
    if mode == "all":
        return stmt.where(or_(sort_expr < sort_at, sort_expr == sort_at))
    if mode == "none":
        return stmt.where(sort_expr < sort_at)
    return stmt.where(
        or_(
            sort_expr < sort_at,
            and_(sort_expr == sort_at, model.id < task_id),
        )
    )


def _apply_task_date_filter(stmt: Any, model: Any, raw_date: str | None) -> Any:
    if not raw_date:
        return stmt
    try:
        day = date_cls.fromisoformat(raw_date)
    except ValueError as exc:
        raise _http("invalid_date", "date must be YYYY-MM-DD", 422) from exc
    start = datetime.combine(day, time.min, timezone.utc)
    end = start + timedelta(days=1)
    sort_expr = _task_sort_expr(model)
    return stmt.where(sort_expr >= start, sort_expr < end)


def _task_error_code(task: Generation | Completion) -> str | None:
    return _string_value(getattr(task, "error_code", None))


def _task_retryable(kind: str, status: str, error_code: str | None) -> bool:
    if status == "canceled":
        return True
    if status != "failed":
        return False
    if not error_code:
        return True
    return error_code not in _TERMINAL_ERROR_CODES


def _task_recommended_actions(
    *,
    kind: str,
    status: str,
    error_code: str | None,
    retryable: bool,
) -> list[TaskRecommendedActionOut]:
    if status == "canceled":
        return [
            TaskRecommendedActionOut(id="retry", label="重新开始", kind="retry"),
        ]
    if status != "failed":
        return []

    code = (error_code or "").strip()
    actions: list[TaskRecommendedActionOut] = []
    if retryable:
        actions.append(TaskRecommendedActionOut(id="retry", label="重试", kind="retry"))

    if code in {"INSUFFICIENT_BALANCE", "insufficient_credits"}:
        actions.extend(
            [
                TaskRecommendedActionOut(
                    id="open_wallet",
                    label="去充值",
                    kind="link",
                    href="/me/wallet",
                ),
                TaskRecommendedActionOut(
                    id="reduce_cost",
                    label="降低质量/数量",
                    kind="adjust",
                ),
            ]
        )
    elif code in {
        "NO_ACTIVE_API_KEY",
        "no_active_api_key",
        "authentication_error",
        "permission_error",
        "unauthorized",
        "invalid_api_key",
        "upstream_auth_error",
    }:
        actions.append(
            TaskRecommendedActionOut(
                id="open_api_key",
                label="检查 API Key",
                kind="link",
                href="/settings/api-key",
            )
        )
    elif code in {
        "invalid_request_error",
        "invalid_request",
        "invalid_param",
        "invalid_value",
        "validation_error",
        "prompt_too_long",
        "upstream_context_too_long",
    }:
        actions.append(
            TaskRecommendedActionOut(id="edit_input", label="调整输入", kind="adjust")
        )
    elif code in {
        "bad_reference_image",
        "reference_missing",
        "missing_input_images",
        "reference_image_too_large",
        "no_mask_capable_provider",
    }:
        actions.append(
            TaskRecommendedActionOut(
                id="fix_reference",
                label="检查参考图/Mask",
                kind="adjust",
            )
        )
    elif code in {
        "moderation_blocked",
        "content_policy_violation",
        "safety_violation",
    }:
        actions.append(
            TaskRecommendedActionOut(
                id="edit_prompt", label="调整提示词", kind="adjust"
            )
        )
    elif not retryable:
        actions.append(
            TaskRecommendedActionOut(
                id="view_details", label="查看详情", kind="details"
            )
        )
    return actions[:3]


def _task_project_meta(
    task: Generation | Completion,
    message_content: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None]:
    request = _json_dict(getattr(task, "upstream_request", None))
    content = _json_dict(message_content)
    project_id = _string_value(request.get("workflow_run_id")) or _string_value(
        content.get("workflow_run_id")
    )
    workflow_type = _string_value(request.get("workflow_type"))
    workflow_step_key = _string_value(
        request.get("workflow_step_key")
    ) or _string_value(content.get("workflow_step_key"))
    return project_id, workflow_type, workflow_step_key


def _task_source(
    task: Generation | Completion,
    *,
    project_id: str | None,
    conversation_default_params: dict[str, Any] | None,
) -> str:
    request = _json_dict(getattr(task, "upstream_request", None))
    explicit = _string_value(request.get("source"))
    if explicit:
        return explicit
    if project_id:
        return "project"
    if _json_dict(conversation_default_params).get("telegram") is True:
        return "telegram"
    return "chat"


def _task_substage(
    task: Generation | Completion,
    *,
    kind: str,
    retrying: bool,
    waiting_provider: bool,
    cancelled: bool,
    retryable: bool,
) -> str | None:
    status = str(getattr(task, "status", ""))
    progress_stage = str(getattr(task, "progress_stage", ""))
    if cancelled:
        return "cancelled"
    if retrying:
        return "upstream_retrying"
    if status == "failed":
        return "retryable" if retryable else "terminal"
    if status == "succeeded":
        return "display_ready" if kind == "generation" else "completed"
    if waiting_provider:
        return "waiting_provider"
    if status == "queued":
        return "waiting_queue"
    if kind == "completion" and status == "streaming":
        return progress_stage or "streaming"
    return None


def _build_task_item(
    kind: Literal["generation", "completion"],
    task: Generation | Completion,
    *,
    conversation_id: str | None = None,
    message_content: dict[str, Any] | None = None,
    conversation_default_params: dict[str, Any] | None = None,
    thumb_url: str | None = None,
    queue_position: int | None = None,
    sort_at: datetime | None = None,
) -> TaskItemOut:
    request = _task_request(task)
    diagnostics = _json_dict(getattr(task, "diagnostics", None))
    if not diagnostics:
        diagnostics = _json_dict(request.get("generation_diagnostics"))
    project_id, workflow_type, workflow_step_key = _task_project_meta(
        task,
        message_content,
    )
    workflow_type = (
        workflow_type
        or getattr(task, "workflow_type", None)
        or _task_request_str(task, "workflow_type")
    )
    workflow_step_key = (
        workflow_step_key
        or getattr(task, "workflow_step_key", None)
        or _task_request_str(task, "workflow_step_key")
    )
    source = _task_source(
        task,
        project_id=project_id,
        conversation_default_params=conversation_default_params,
    )
    status = str(getattr(task, "status", ""))
    error_code = _task_error_code(task)
    retryable = _task_retryable(kind=kind, status=status, error_code=error_code)
    cancelled = status == "canceled"
    retrying = status == "queued" and bool(error_code) and task.attempt > 0
    waiting_provider = (
        status == "queued"
        and kind == "generation"
        and error_code in _WAITING_PROVIDER_CODES
    )
    substage = _string_value(request.get("substage")) or _string_value(
        diagnostics.get("substage")
    )
    if substage is None:
        substage = _task_substage(
            task,
            kind=kind,
            retrying=retrying,
            waiting_provider=waiting_provider,
            cancelled=cancelled,
            retryable=retryable,
        )
    if sort_at is None:
        sort_at = _task_sort_at(task)
    cursor = _encode_task_cursor(sort_at, kind, task.id)
    prompt = getattr(task, "prompt", None) if kind == "generation" else None
    queue_wait = getattr(task, "queue_wait_ms", None)
    if queue_wait is None:
        queue_wait = _task_request_int(task, "queue_wait_ms")
    title = (
        prompt
        if isinstance(prompt, str) and prompt
        else ("图像生成" if kind == "generation" else "文本回复")
    )
    return TaskItemOut(
        kind=kind,
        id=task.id,
        message_id=task.message_id,
        status=status,
        progress_stage=task.progress_stage,
        stage=task.progress_stage,
        substage=substage,
        started_at=task.started_at,
        finished_at=task.finished_at,
        created_at=task.created_at,
        date=sort_at,
        cursor=cursor,
        conversation_id=conversation_id,
        project_id=project_id,
        workflow_type=workflow_type,
        workflow_step_key=workflow_step_key,
        source=source,
        action_source=getattr(task, "action_source", None)
        or _task_request_str(task, "action_source"),
        trace_id=getattr(task, "trace_id", None) or _task_request_str(task, "trace_id"),
        queue_lane=getattr(task, "queue_lane", None)
        or _task_request_str(task, "queue_lane"),
        pixel_count=getattr(task, "pixel_count", None)
        or _task_request_int(task, "pixel_count"),
        size_bucket=getattr(task, "size_bucket", None)
        or _task_request_str(task, "size_bucket"),
        cost_class=getattr(task, "cost_class", None)
        or _task_request_str(task, "cost_class"),
        queue_wait_ms=queue_wait,
        title=title[:160] if title else None,
        prompt=prompt if isinstance(prompt, str) else None,
        error_code=error_code,
        error_message=_string_value(getattr(task, "error_message", None)),
        retryable=retryable,
        recommended_actions=_task_recommended_actions(
            kind=kind,
            status=status,
            error_code=error_code,
            retryable=retryable,
        ),
        thumb_url=thumb_url,
        queue_position=queue_position,
        retrying=retrying,
        waiting_provider=waiting_provider,
        cancelled=cancelled,
        source_image_id=(
            _string_value(getattr(task, "primary_input_image_id", None))
            if kind == "generation"
            else None
        ),
    )


def _variant_thumb_url(image_id: str, kinds: set[str]) -> str:
    if "preview1024" in kinds:
        return f"/api/images/{image_id}/variants/preview1024"
    if "thumb256" in kinds:
        return f"/api/images/{image_id}/variants/thumb256"
    return f"/api/images/{image_id}/binary"


_release_generation_queue_state = release_generation_queue_state


async def _release_queued_task_hold(
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
            idempotency_key=f"cancel:{ref_type}:{ref_id}",
            meta={"reason": reason},
        )
    except billing_core.BillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    return tx is not None


async def _task_wallet_exists(db: AsyncSession, user_id: str) -> bool:
    wallet = await billing_core.get_wallet(db, user_id, lock=False, create=False)
    return wallet is not None


async def _task_should_release_wallet_hold(
    db: AsyncSession,
    user: Any,
) -> bool:
    if getattr(user, "account_mode", "wallet") == "wallet":
        return True
    return await _task_wallet_exists(db, user.id)


# ---------- generations ----------


@router.get("/generations/{gen_id}", response_model=GenerationOut)
async def get_generation(
    gen_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationOut:
    gen = (
        await db.execute(
            select(Generation).where(
                Generation.id == gen_id, Generation.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    return GenerationOut.model_validate(gen)


@router.post("/generations/{gen_id}/cancel", dependencies=[Depends(verify_csrf)])
async def cancel_generation(
    gen_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    gen = (
        await db.execute(
            select(Generation)
            .where(Generation.id == gen_id, Generation.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    if gen.status not in (
        GenerationStatus.QUEUED.value,
        GenerationStatus.RUNNING.value,
    ):
        raise _http("not_cancelable", f"status is {gen.status}", 409)

    redis = get_redis()
    was_queued = gen.status == GenerationStatus.QUEUED.value
    if gen.status == GenerationStatus.RUNNING.value:
        # The worker still owns an upstream call and the image queue lease.
        # Keep the task visible as running until the worker observes the cancel
        # flag, stops the upstream awaitable, and writes the final canceled row.
        # Why no explicit commit: this branch makes no field mutation on `gen`
        # — only the SELECT FOR UPDATE row lock is held. The lock is released
        # at session exit by ``get_db``'s context manager (rollback on raise,
        # commit on clean return); calling commit() here would just be wasted
        # round-trip with identical lock-release timing.
        try:
            await redis.set(f"task:{gen.id}:cancel", "1", ex=3600)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel flag write failed gen=%s err=%s", gen.id, exc)
            raise _http("cancel_unavailable", "cancel signal unavailable", 503) from exc
        return {"status": gen.status}

    gen.status = GenerationStatus.CANCELED.value
    gen.finished_at = datetime.now(timezone.utc)
    released_hold = False
    if await _task_should_release_wallet_hold(db, user):
        released_hold = await _release_queued_task_hold(
            db,
            user_id=user.id,
            ref_type="generation",
            ref_id=_generation_billing_ref_id(
                gen.id, _generation_billing_retry_count(gen)
            ),
            reason="queued generation cancelled by user",
        )
    await db.commit()
    if released_hold:
        await invalidate_balance_cache(user.id)
    # Queued tasks do not have an upstream process to stop. Clear any stale
    # image_queue side state so a canceled queued row cannot keep capacity.
    try:
        if was_queued:
            await _release_generation_queue_state(redis, gen.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cancel image_queue release failed gen=%s err=%s",
            gen.id,
            exc,
        )
    try:
        await publish_sse_event(
            redis,
            user_id=user.id,
            channel=task_channel(gen.id),
            event_name="generation.canceled",
            data={
                "generation_id": gen.id,
                "message_id": gen.message_id,
                "stage": GenerationStage.FINALIZING.value,
                "substage": "cancelled",
                "cancelled": True,
                "code": "cancelled",
                "message": "cancelled by user",
                "retriable": True,
                "recommended_actions": [
                    {"id": "retry", "label": "重新开始", "kind": "retry"}
                ],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "queued generation cancel publish failed gen=%s err=%s", gen.id, exc
        )
    return {"status": gen.status}


@router.post("/generations/{gen_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_generation(
    gen_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    gen = (
        await db.execute(
            select(Generation)
            .where(Generation.id == gen_id, Generation.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    reject_canvas_retry(gen)
    if gen.status not in (
        GenerationStatus.FAILED.value,
        GenerationStatus.CANCELED.value,
    ):
        raise _http("not_retryable", f"status is {gen.status}", 409)

    redis = get_redis()
    try:
        await redis.delete(f"task:{gen.id}:cancel")
    except Exception as exc:  # noqa: BLE001
        logger.warning("retry cancel-flag cleanup failed gen=%s err=%s", gen.id, exc)
        raise _http(
            "retry_unavailable", "could not clear prior cancel signal", 503
        ) from exc

    gen.status = GenerationStatus.QUEUED.value
    gen.progress_stage = GenerationStage.QUEUED.value
    gen.attempt = 0
    gen.billing_retry_count = _generation_billing_retry_count(gen) + 1
    gen.error_code = None
    gen.error_message = None
    gen.started_at = None
    gen.finished_at = None
    held_retry = False
    if await _task_should_release_wallet_hold(db, user):
        held_retry = await _hold_generation_retry_wallet(db, user.id, gen)

    payload = {"task_id": gen.id, "user_id": user.id, "kind": "generation"}
    outbox = OutboxEvent(kind="generation", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()
    if held_retry:
        await invalidate_balance_cache(user.id)

    # best-effort publish
    await _publish_queued(payload, gen.message_id)
    return {"status": gen.status}


# ---------- completions ----------


@router.get("/completions/{comp_id}", response_model=CompletionOut)
async def get_completion(
    comp_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CompletionOut:
    comp = (
        await db.execute(
            select(Completion).where(
                Completion.id == comp_id, Completion.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if not comp:
        raise _http("not_found", "completion not found", 404)
    return CompletionOut.model_validate(comp)


@router.post("/completions/{comp_id}/cancel", dependencies=[Depends(verify_csrf)])
async def cancel_completion(
    comp_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    comp = (
        await db.execute(
            select(Completion)
            .where(Completion.id == comp_id, Completion.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not comp:
        raise _http("not_found", "completion not found", 404)
    if comp.status not in (
        CompletionStatus.QUEUED.value,
        CompletionStatus.STREAMING.value,
    ):
        raise _http("not_cancelable", f"status is {comp.status}", 409)

    redis = get_redis()
    if comp.status == CompletionStatus.STREAMING.value:
        try:
            await redis.set(f"task:{comp.id}:cancel", "1", ex=3600)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel flag write failed comp=%s err=%s", comp.id, exc)
            raise _http("cancel_unavailable", "cancel signal unavailable", 503) from exc
        return {"status": "canceling", "cancel_requested": True}

    comp.status = CompletionStatus.CANCELED.value
    comp.progress_stage = CompletionStage.FINALIZING.value
    comp.finished_at = datetime.now(timezone.utc)
    released_hold = False
    if await _task_should_release_wallet_hold(db, user):
        released_hold = await _release_queued_task_hold(
            db,
            user_id=user.id,
            ref_type="completion",
            ref_id=_completion_task_billing_ref_id(comp),
            reason="queued completion cancelled by user",
        )
    await db.commit()
    if released_hold:
        await invalidate_balance_cache(user.id)
    return {"status": comp.status}


@router.post("/completions/{comp_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_completion(
    comp_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    comp = (
        await db.execute(
            select(Completion)
            .where(Completion.id == comp_id, Completion.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not comp:
        raise _http("not_found", "completion not found", 404)
    reject_canvas_retry(comp)
    if comp.status not in (
        CompletionStatus.FAILED.value,
        CompletionStatus.CANCELED.value,
    ):
        raise _http("not_retryable", f"status is {comp.status}", 409)

    redis = get_redis()
    try:
        await redis.delete(f"task:{comp.id}:cancel")
    except Exception as exc:  # noqa: BLE001
        logger.warning("retry cancel-flag cleanup failed comp=%s err=%s", comp.id, exc)
        raise _http(
            "retry_unavailable", "could not clear prior cancel signal", 503
        ) from exc

    comp.status = CompletionStatus.QUEUED.value
    comp.progress_stage = CompletionStage.QUEUED.value
    comp.attempt = 0
    comp.error_code = None
    comp.error_message = None
    comp.started_at = None
    comp.finished_at = None
    previous_retry_count = _completion_billing_retry_count(comp)
    held_retry = False
    if await _task_should_release_wallet_hold(db, user):
        held_retry = await _hold_completion_retry_wallet(
            db,
            user.id,
            comp,
            previous_retry_count,
        )
    upstream_request = dict(comp.upstream_request or {})
    upstream_request["billing_retry_count"] = previous_retry_count + 1
    comp.upstream_request = upstream_request or None

    payload = {"task_id": comp.id, "user_id": user.id, "kind": "completion"}
    outbox = OutboxEvent(kind="completion", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()
    if held_retry:
        await invalidate_balance_cache(user.id)

    await _publish_queued(payload, comp.message_id)
    return {"status": comp.status}


# ---------- aggregate ----------


def _task_listing_runtime() -> TaskListingRuntime:
    return TaskListingRuntime(
        apply_cursor=_apply_task_cursor,
        apply_date_filter=_apply_task_date_filter,
        build_item=_build_task_item,
        encode_cursor=_encode_task_cursor,
        json_dict=_json_dict,
        kind_rank=_task_kind_rank,
        sort_at=_task_sort_at,
        sort_expr=_task_sort_expr,
        variant_thumb_url=_variant_thumb_url,
    )


@router.get("/tasks", response_model=TaskListOut)
async def list_tasks(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    status: Annotated[str | None, Query()] = None,
    kind: Literal["all", "generation", "completion"] = "all",
    source: Annotated[str | None, Query()] = None,
    conversation_id: Annotated[str | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
    date_filter: Annotated[str | None, Query(alias="date")] = None,
    cursor: Annotated[str | None, Query()] = None,
    error_code: Annotated[str | None, Query()] = None,
    retryable: Annotated[bool | None, Query()] = None,
    mine: Literal[0, 1] = 1,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> TaskListOut:
    _ = mine  # V1: always mine==1; flag accepted for API compat.
    return await build_task_list(
        db,
        _task_listing_runtime(),
        user_id=user.id,
        status=status,
        kind=kind,
        source=source,
        conversation_id=conversation_id,
        project_id=project_id,
        date_filter=date_filter,
        cursor=_decode_task_cursor(cursor),
        error_code=error_code,
        retryable=retryable,
        limit=limit,
    )


@router.get("/tasks/mine/active", response_model=ActiveTasksOut)
async def list_my_active_tasks(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=200, ge=1, le=500),
) -> ActiveTasksOut:
    """用户级中心任务列表：返回当前用户所有未完成 generations / completions 的完整字段。

    前端启动 / SSE 重连时一次性 hydrate 到 store，让 GlobalTaskTray 显示**所有会话**的
    进行中任务（包括其他会话提交后未访问的）。"""
    # Pull a little extra from each table before the cross-table merge so one
    # busy task type does not starve the other in the final `limit` window.
    query_limit = limit * 2
    gens = (
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user.id,
                    Generation.status.in_(
                        [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
                    ),
                )
                .order_by(Generation.created_at.desc())
                .limit(query_limit)
            )
        )
        .scalars()
        .all()
    )
    comps = (
        (
            await db.execute(
                select(Completion)
                .where(
                    Completion.user_id == user.id,
                    Completion.status.in_(
                        [
                            CompletionStatus.QUEUED.value,
                            CompletionStatus.STREAMING.value,
                        ]
                    ),
                )
                .order_by(Completion.created_at.desc())
                .limit(query_limit)
            )
        )
        .scalars()
        .all()
    )
    items: list[tuple[datetime, str, Generation | Completion]] = []
    for gen in gens:
        items.append((gen.created_at, "generation", gen))
    for comp in comps:
        items.append((comp.created_at, "completion", comp))
    items.sort(key=lambda item: item[0], reverse=True)
    items = items[:limit]
    return ActiveTasksOut(
        generations=[
            GenerationOut.model_validate(item)
            for _created_at, kind, item in items
            if kind == "generation"
        ],
        completions=[
            CompletionOut.model_validate(item)
            for _created_at, kind, item in items
            if kind == "completion"
        ],
    )


# ---------- helpers ----------


async def _publish_queued(payload: dict, message_id: str) -> None:
    """Best-effort arq enqueue + PubSub on retry. Outbox publisher is the source of truth."""
    try:
        redis = get_redis()
        kind = payload["kind"]
        fn_name = "run_completion" if kind == "completion" else "run_generation"
        ev_name = EV_COMP_QUEUED if kind == "completion" else EV_GEN_QUEUED
        id_field = "completion_id" if kind == "completion" else "generation_id"
        # Enqueue via arq so the Worker's registered functions consume it.
        pool = await get_arq_pool()
        await pool.enqueue_job(
            fn_name,
            payload["task_id"],
            _job_id=arq_job_id(kind, payload["task_id"], payload.get("outbox_id")),
        )
        await publish_sse_event(
            redis,
            user_id=payload["user_id"],
            channel=task_channel(payload["task_id"]),
            event_name=ev_name,
            data={
                id_field: payload["task_id"],
                "message_id": message_id,
                "kind": kind,
                "stage": "queued",
                "substage": "waiting_queue",
                "retrying": False,
                "waiting_provider": False,
                "cancelled": False,
            },
        )
    except Exception:
        kind = str(payload.get("kind") or "unknown")
        task_publish_errors_total.labels(kind=kind).inc()
        logger.warning(
            "best-effort queued task publish failed kind=%s task_id=%s",
            kind,
            payload.get("task_id"),
            exc_info=True,
        )
