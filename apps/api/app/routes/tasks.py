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
from lumen_core.models import Completion, Generation, OutboxEvent
from lumen_core.models import Conversation, Image, ImageVariant, Message
from lumen_core.schemas import (
    ActiveTasksOut,
    CompletionOut,
    GenerationOut,
    TaskItemOut,
    TaskListOut,
    TaskRecommendedActionOut,
)

from ..arq_pool import get_arq_pool
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..observability import task_publish_errors_total
from ..redis_client import get_redis
from ..sse_publish import publish_sse_event


router = APIRouter()
logger = logging.getLogger(__name__)

_IMAGE_QUEUE_ACTIVE_KEY = "generation:image_queue:active"
_IMAGE_QUEUE_TASK_PROVIDER_PREFIX = "generation:image_queue:task_provider:"
_IMAGE_QUEUE_PROVIDER_ACTIVE_PREFIX = "generation:image_queue:provider_active:"
_DUAL_RACE_SENTINEL_PREFIX = "__dr:"
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
    return HTTPException(status_code=http, detail={"error": {"code": code, "message": msg}})


def _image_task_provider_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{task_id}"


def _image_provider_active_key(provider_name: str) -> str:
    return f"{_IMAGE_QUEUE_PROVIDER_ACTIVE_PREFIX}{provider_name}"


def _redis_text(value: object) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return None


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


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _task_sort_at(task: Generation | Completion) -> datetime:
    return task.created_at or task.started_at or datetime.min.replace(tzinfo=timezone.utc)


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
            TaskRecommendedActionOut(id="edit_prompt", label="调整提示词", kind="adjust")
        )
    elif not retryable:
        actions.append(
            TaskRecommendedActionOut(id="view_details", label="查看详情", kind="details")
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
    workflow_step_key = _string_value(request.get("workflow_step_key")) or _string_value(
        content.get("workflow_step_key")
    )
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


def _task_item(
    kind: Literal["generation", "completion"],
    task: Generation | Completion,
) -> TaskItemOut:
    request = _task_request(task)
    diagnostics = request.get("generation_diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    status = str(getattr(task, "status", ""))
    error_code = _task_error_code(task)
    retryable = _task_retryable(kind=kind, status=status, error_code=error_code)
    cancelled = status == "canceled"
    waiting_provider = bool(
        status in {"queued", "running"} and error_code in _WAITING_PROVIDER_CODES
    )
    retrying = bool(status == "queued" and error_code and retryable)
    substage = request.get("substage") or diagnostics.get("substage")
    if not isinstance(substage, str):
        substage = _task_substage(
            task,
            kind=kind,
            retrying=retrying,
            waiting_provider=waiting_provider,
            cancelled=cancelled,
            retryable=retryable,
        )
    queue_wait = getattr(task, "queue_wait_ms", None)
    if queue_wait is None:
        queue_wait = _task_request_int(task, "queue_wait_ms")
    return TaskItemOut(
        kind=kind,
        id=task.id,
        message_id=task.message_id,
        status=status,
        progress_stage=task.progress_stage,
        stage=task.progress_stage,
        started_at=task.started_at,
        created_at=getattr(task, "created_at", None),
        finished_at=getattr(task, "finished_at", None),
        source=getattr(task, "source", None) or _task_request_str(task, "source"),
        action_source=getattr(task, "action_source", None)
        or _task_request_str(task, "action_source"),
        trace_id=getattr(task, "trace_id", None) or _task_request_str(task, "trace_id"),
        project_id=_task_request_str(task, "project_id")
        or _task_request_str(task, "workflow_run_id"),
        workflow_type=getattr(task, "workflow_type", None)
        or _task_request_str(task, "workflow_type"),
        workflow_step_key=getattr(task, "workflow_step_key", None)
        or _task_request_str(task, "workflow_step_key"),
        queue_lane=getattr(task, "queue_lane", None)
        or _task_request_str(task, "queue_lane"),
        pixel_count=getattr(task, "pixel_count", None)
        or _task_request_int(task, "pixel_count"),
        size_bucket=getattr(task, "size_bucket", None)
        or _task_request_str(task, "size_bucket"),
        cost_class=getattr(task, "cost_class", None)
        or _task_request_str(task, "cost_class"),
        queue_wait_ms=queue_wait,
        queue_position=_task_request_int(task, "queue_position"),
        substage=substage,
        retrying=retrying,
        waiting_provider=waiting_provider,
        cancelled=cancelled,
        error_code=error_code,
        error_message=getattr(task, "error_message", None),
        retryable=retryable,
        recommended_actions=_task_recommended_actions(
            kind=kind,
            status=status,
            error_code=error_code,
            retryable=retryable,
        ),
        thumb_url=_task_request_str(task, "thumb_url"),
    )


def _variant_thumb_url(image_id: str, kinds: set[str]) -> str:
    if "preview1024" in kinds:
        return f"/api/images/{image_id}/variants/preview1024"
    if "thumb256" in kinds:
        return f"/api/images/{image_id}/variants/thumb256"
    return f"/api/images/{image_id}/binary"


async def _release_generation_queue_state(redis: Any, task_id: str) -> None:
    task_provider_key = _image_task_provider_key(task_id)
    provider_name = _redis_text(await redis.get(task_provider_key))
    pipe_fn = getattr(redis, "pipeline", None)
    pipe = pipe_fn(transaction=False) if callable(pipe_fn) else None

    async def _zrem(key: str, member: str) -> None:
        if pipe is not None:
            pipe.zrem(key, member)
        else:
            await redis.zrem(key, member)

    async def _delete(key: str) -> None:
        if pipe is not None:
            pipe.delete(key)
        else:
            await redis.delete(key)

    if provider_name:
        if provider_name.startswith(_DUAL_RACE_SENTINEL_PREFIX):
            await _zrem(_IMAGE_QUEUE_ACTIVE_KEY, provider_name)
        else:
            await _zrem(_IMAGE_QUEUE_ACTIVE_KEY, task_id)
            await _zrem(_image_provider_active_key(provider_name), task_id)
    else:
        await _zrem(_IMAGE_QUEUE_ACTIVE_KEY, task_id)
    await _delete(task_provider_key)
    await _delete(f"task:{task_id}:lease")
    if pipe is not None:
        await pipe.execute()


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
            select(Generation).where(
                Generation.id == gen_id, Generation.user_id == user.id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    if gen.status not in (GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value):
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
    await db.commit()
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
        logger.warning("queued generation cancel publish failed gen=%s err=%s", gen.id, exc)
    return {"status": gen.status}


@router.post("/generations/{gen_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_generation(
    gen_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    gen = (
        await db.execute(
            select(Generation).where(
                Generation.id == gen_id, Generation.user_id == user.id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    if gen.status not in (GenerationStatus.FAILED.value, GenerationStatus.CANCELED.value):
        raise _http("not_retryable", f"status is {gen.status}", 409)

    redis = get_redis()
    # Why: clearing the prior cancel flag is best-effort cleanup. The
    # worker double-checks the cancel key before each terminal write, so
    # even if a stale flag survives a transient redis blip, the worst
    # case is a re-cancel on the next attempt — never a corrupted row.
    # Don't 503 the user for a transient redis issue.
    try:
        await redis.delete(f"task:{gen.id}:cancel")
    except Exception as exc:  # noqa: BLE001
        logger.warning("retry cancel-flag cleanup failed gen=%s err=%s", gen.id, exc)

    gen.status = GenerationStatus.QUEUED.value
    gen.progress_stage = GenerationStage.QUEUED.value
    gen.attempt = 0
    gen.error_code = None
    gen.error_message = None
    gen.started_at = None
    gen.finished_at = None

    payload = {"task_id": gen.id, "user_id": user.id, "kind": "generation"}
    outbox = OutboxEvent(kind="generation", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()

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
            select(Completion).where(
                Completion.id == comp_id, Completion.user_id == user.id
            ).with_for_update()
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
    await db.commit()
    return {"status": comp.status}


@router.post("/completions/{comp_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_completion(
    comp_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    comp = (
        await db.execute(
            select(Completion).where(
                Completion.id == comp_id, Completion.user_id == user.id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not comp:
        raise _http("not_found", "completion not found", 404)
    if comp.status not in (CompletionStatus.FAILED.value, CompletionStatus.CANCELED.value):
        raise _http("not_retryable", f"status is {comp.status}", 409)

    redis = get_redis()
    try:
        await redis.delete(f"task:{comp.id}:cancel")
    except Exception as exc:  # noqa: BLE001
        logger.warning("retry cancel-flag cleanup failed comp=%s err=%s", comp.id, exc)
        raise _http("retry_unavailable", "could not clear prior cancel signal", 503) from exc

    comp.status = CompletionStatus.QUEUED.value
    comp.progress_stage = CompletionStage.QUEUED.value
    comp.attempt = 0
    comp.error_code = None
    comp.error_message = None
    comp.started_at = None
    comp.finished_at = None

    payload = {"task_id": comp.id, "user_id": user.id, "kind": "completion"}
    outbox = OutboxEvent(kind="completion", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()

    await _publish_queued(payload, comp.message_id)
    return {"status": comp.status}


# ---------- aggregate ----------

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
    parsed_cursor = _decode_task_cursor(cursor)
    query_limit = min(max(limit * 3, limit + 1), 1000)

    gen_stmt = select(Generation).where(Generation.user_id == user.id)
    comp_stmt = select(Completion).where(Completion.user_id == user.id)
    if status == "active":
        gen_stmt = gen_stmt.where(
            Generation.status.in_(
                [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
            )
        )
        comp_stmt = comp_stmt.where(
            Completion.status.in_(
                [CompletionStatus.QUEUED.value, CompletionStatus.STREAMING.value]
            )
        )
    elif status == "terminal":
        gen_stmt = gen_stmt.where(
            Generation.status.in_(
                [
                    GenerationStatus.SUCCEEDED.value,
                    GenerationStatus.FAILED.value,
                    GenerationStatus.CANCELED.value,
                ]
            )
        )
        comp_stmt = comp_stmt.where(
            Completion.status.in_(
                [
                    CompletionStatus.SUCCEEDED.value,
                    CompletionStatus.FAILED.value,
                    CompletionStatus.CANCELED.value,
                ]
            )
        )
    elif status == GenerationStatus.RUNNING.value:
        gen_stmt = gen_stmt.where(Generation.status == GenerationStatus.RUNNING.value)
        comp_stmt = comp_stmt.where(
            Completion.status == CompletionStatus.STREAMING.value
        )
    elif status == CompletionStatus.STREAMING.value:
        gen_stmt = gen_stmt.where(Generation.status == GenerationStatus.RUNNING.value)
        comp_stmt = comp_stmt.where(
            Completion.status == CompletionStatus.STREAMING.value
        )
    elif status:
        gen_stmt = gen_stmt.where(Generation.status == status)
        comp_stmt = comp_stmt.where(Completion.status == status)

    if error_code:
        gen_stmt = gen_stmt.where(Generation.error_code == error_code)
        comp_stmt = comp_stmt.where(Completion.error_code == error_code)

    gen_stmt = _apply_task_date_filter(gen_stmt, Generation, date_filter)
    comp_stmt = _apply_task_date_filter(comp_stmt, Completion, date_filter)
    gen_stmt = _apply_task_cursor(
        gen_stmt,
        Generation,
        parsed_cursor,
        model_kind="generation",
    )
    comp_stmt = _apply_task_cursor(
        comp_stmt,
        Completion,
        parsed_cursor,
        model_kind="completion",
    )

    gens: list[Generation] = []
    comps: list[Completion] = []
    if kind in {"all", "generation"}:
        gens = (
            await db.execute(
                gen_stmt.order_by(_task_sort_expr(Generation).desc(), Generation.id.desc())
                .limit(query_limit)
            )
        ).scalars().all()
    if kind in {"all", "completion"}:
        comps = (
            await db.execute(
                comp_stmt.order_by(_task_sort_expr(Completion).desc(), Completion.id.desc())
                .limit(query_limit)
            )
        ).scalars().all()

    message_ids = [task.message_id for task in [*gens, *comps]]
    message_meta: dict[str, tuple[str, dict[str, Any]]] = {}
    if message_ids:
        rows = (
            await db.execute(
                select(Message.id, Message.conversation_id, Message.content).where(
                    Message.id.in_(message_ids)
                )
            )
        ).all()
        message_meta = {
            mid: (conv_id, _json_dict(content)) for mid, conv_id, content in rows
        }

    conv_defaults: dict[str, dict[str, Any]] = {}
    conv_ids = {conv_id for conv_id, _content in message_meta.values() if conv_id}
    if conv_ids:
        rows = (
            await db.execute(
                select(Conversation.id, Conversation.default_params).where(
                    Conversation.id.in_(conv_ids)
                )
            )
        ).all()
        conv_defaults = {conv_id: _json_dict(params) for conv_id, params in rows}

    image_by_gen: dict[str, Image] = {}
    variant_kinds: dict[str, set[str]] = {}
    if gens:
        gen_ids = [g.id for g in gens]
        image_rows = (
            await db.execute(
                select(Image)
                .where(
                    Image.owner_generation_id.in_(gen_ids),
                    Image.deleted_at.is_(None),
                )
                .order_by(Image.created_at.asc(), Image.id.asc())
            )
        ).scalars().all()
        for image in image_rows:
            owner_id = image.owner_generation_id
            if owner_id and owner_id not in image_by_gen:
                image_by_gen[owner_id] = image
        if image_by_gen:
            rows = (
                await db.execute(
                    select(ImageVariant.image_id, ImageVariant.kind).where(
                        ImageVariant.image_id.in_(
                            [img.id for img in image_by_gen.values()]
                        )
                    )
                )
            ).all()
            for image_id, variant_kind in rows:
                variant_kinds.setdefault(image_id, set()).add(variant_kind)

    queued_generation_ids = (
        (
            await db.execute(
                select(Generation.id)
                .where(
                    Generation.user_id == user.id,
                    Generation.status == GenerationStatus.QUEUED.value,
                )
                .order_by(_task_sort_expr(Generation).asc(), Generation.id.asc())
            )
        )
        .scalars()
        .all()
    )
    queue_positions = {
        generation_id: index + 1 for index, generation_id in enumerate(queued_generation_ids)
    }

    sortable_items: list[tuple[datetime, str, str, TaskItemOut]] = []

    def add_item(task: Generation | Completion, item_kind: str) -> None:
        msg_conv_id, msg_content = message_meta.get(task.message_id, (None, {}))
        task_project_id, workflow_type, workflow_step_key = _task_project_meta(
            task,
            msg_content,
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
        task_source = _task_source(
            task,
            project_id=task_project_id,
            conversation_default_params=conv_defaults.get(msg_conv_id or ""),
        )
        if source and task_source != source:
            return
        if conversation_id and msg_conv_id != conversation_id:
            return
        if project_id and task_project_id != project_id:
            return

        err_code = _task_error_code(task)
        is_retryable = _task_retryable(item_kind, task.status, err_code)
        if retryable is not None and is_retryable is not retryable:
            return
        is_cancelled = task.status == "canceled"
        is_retrying = task.status == "queued" and bool(err_code) and task.attempt > 0
        is_waiting_provider = (
            task.status == "queued"
            and item_kind == "generation"
            and bool(err_code in _WAITING_PROVIDER_CODES)
        )
        substage = _task_substage(
            task,
            kind=item_kind,
            retrying=is_retrying,
            waiting_provider=is_waiting_provider,
            cancelled=is_cancelled,
            retryable=is_retryable,
        )
        sort_at = _task_sort_at(task)
        task_cursor = _encode_task_cursor(sort_at, item_kind, task.id)
        thumb_url = None
        prompt = getattr(task, "prompt", None) if item_kind == "generation" else None
        if item_kind == "generation":
            image = image_by_gen.get(task.id)
            if image is not None:
                thumb_url = _variant_thumb_url(
                    image.id,
                    variant_kinds.get(image.id, set()),
                )
        queue_wait = getattr(task, "queue_wait_ms", None)
        if queue_wait is None:
            queue_wait = _task_request_int(task, "queue_wait_ms")
        title = (
            prompt
            if isinstance(prompt, str) and prompt
            else ("图像生成" if item_kind == "generation" else "文本回复")
        )
        sortable_items.append(
            (
                sort_at,
                item_kind,
                task.id,
                TaskItemOut(
                    kind=item_kind,  # type: ignore[arg-type]
                    id=task.id,
                    message_id=task.message_id,
                    status=task.status,
                    progress_stage=task.progress_stage,
                    stage=task.progress_stage,
                    substage=substage,
                    started_at=task.started_at,
                    finished_at=task.finished_at,
                    created_at=task.created_at,
                    date=sort_at,
                    cursor=task_cursor,
                    conversation_id=msg_conv_id,
                    project_id=task_project_id,
                    workflow_type=workflow_type,
                    workflow_step_key=workflow_step_key,
                    source=task_source,
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
                    error_code=err_code,
                    error_message=_string_value(getattr(task, "error_message", None)),
                    retryable=is_retryable,
                    recommended_actions=_task_recommended_actions(
                        kind=item_kind,
                        status=task.status,
                        error_code=err_code,
                        retryable=is_retryable,
                    ),
                    thumb_url=thumb_url,
                    queue_position=queue_positions.get(task.id),
                    retrying=is_retrying,
                    waiting_provider=is_waiting_provider,
                    cancelled=is_cancelled,
                    source_image_id=(
                        _string_value(getattr(task, "primary_input_image_id", None))
                        if item_kind == "generation"
                        else None
                    ),
                ),
            )
        )

    for gen in gens:
        add_item(gen, "generation")
    for comp in comps:
        add_item(comp, "completion")

    sortable_items.sort(
        key=lambda pair: (pair[0], _task_kind_rank(pair[1]), pair[2]),
        reverse=True,
    )
    page = sortable_items[:limit]
    next_cursor = None
    if len(sortable_items) > limit and page:
        sort_at, item_kind, task_id, _item = page[-1]
        next_cursor = _encode_task_cursor(sort_at, item_kind, task_id)
    return TaskListOut(items=[item for *_prefix, item in page], next_cursor=next_cursor)


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
        await db.execute(
            select(Generation).where(
                Generation.user_id == user.id,
                Generation.status.in_(
                    [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
                ),
            )
            .order_by(Generation.created_at.desc())
            .limit(query_limit)
        )
    ).scalars().all()
    comps = (
        await db.execute(
            select(Completion).where(
                Completion.user_id == user.id,
                Completion.status.in_(
                    [CompletionStatus.QUEUED.value, CompletionStatus.STREAMING.value]
                ),
            )
            .order_by(Completion.created_at.desc())
            .limit(query_limit)
        )
    ).scalars().all()
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
