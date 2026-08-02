"""Task aggregation listing, cursor, and presentation routes."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date as date_cls, datetime, time, timedelta, timezone
from types import MappingProxyType
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    CompletionStatus,
    GenerationStatus,
)
from lumen_core.model_entities import (
    Completion,
    Conversation,
    Generation,
    Message,
)
from lumen_core.schema_models import (
    ActiveTasksOut,
    CompletionOut,
    GenerationOut,
    TaskItemOut,
    TaskListOut,
)

from ..db import get_db
from ..deps import CurrentUser
from ..services.task_listing import (
    TaskListingRuntime,
    TaskListRequest,
    build_task_list,
)
from .task_listing_parts.actions import (
    task_recommended_actions as _task_recommended_actions,
)

router = APIRouter()


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


_TASK_CURSOR_VERSION = 1
_TASK_KIND_RANK = MappingProxyType({"completion": 0, "generation": 1})

# _task_source 的回退派生值(依赖 project_id / message content / conversation
# default_params,无法在 SQL 层精确表达);这些值不进 JSON 下推,仍走内存过滤。
_DERIVED_SOURCES = frozenset({"project", "telegram", "chat"})

_TERMINAL_ERROR_CODES = frozenset(
    {
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
)
_WAITING_PROVIDER_CODES = frozenset(
    {
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
)


@dataclass(frozen=True, slots=True)
class TaskListQuery:
    status: Annotated[str | None, Query()] = None
    kind: Literal["all", "generation", "completion"] = "all"
    source: Annotated[str | None, Query()] = None
    conversation_id: Annotated[str | None, Query()] = None
    project_id: Annotated[str | None, Query()] = None
    date_filter: Annotated[str | None, Query(alias="date")] = None
    cursor: Annotated[str | None, Query()] = None
    error_code: Annotated[str | None, Query()] = None
    retryable: Annotated[bool | None, Query()] = None
    mine: Literal[0, 1] = 1
    limit: Annotated[int, Query(ge=1, le=500)] = 100


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


def _json_text_empty(expr: Any) -> Any:
    """JSON 文本提取的"缺失或空串"判定,与 _string_value 口径一致。"""
    return or_(expr.is_(None), expr == "")


def _json_text_present(expr: Any) -> Any:
    return and_(expr.is_not(None), expr != "")


def _task_has_project_id(model: Any) -> Any:
    """任务带非空 project_id 的 SQL 条件:upstream_request.workflow_run_id 或
    message content.workflow_run_id 非空(与 _task_project_meta 口径一致)。"""
    return or_(
        _json_text_present(model.upstream_request["workflow_run_id"].as_string()),
        model.message_id.in_(
            select(Message.id).where(
                _json_text_present(Message.content["workflow_run_id"].as_string())
            )
        ),
    )


def _derived_source_precondition(model: Any, source: str) -> Any | None:
    """派生源(source=project/telegram)的必要条件预筛。

    派生回退(_task_source)要求显式 source 缺失;显式 source 恰好等于该值时
    _task_source 优先返回显式值、同样命中过滤,因此附带显式相等分支。该预筛
    不比精确匹配更严:任何命中行都能通过,内存 _item_matches 继续兜底;
    零匹配时首轮即空,深挖循环立即退出。
    """
    explicit = model.upstream_request["source"].as_string()
    no_explicit = _json_text_empty(explicit)
    if source == "project":
        return or_(explicit == source, and_(no_explicit, _task_has_project_id(model)))
    if source == "telegram":
        # 派生顺序 project 优先于 telegram:telegram 回退要求无 project_id,
        # 且消息所在会话 default_params["telegram"] 为 true。CAST 成文本后
        # PG 的 JSON true 是 "true",sqlite 测试方言是 "1",两分支互补。
        telegram_flag = cast(
            Conversation.default_params["telegram"].as_string(), String
        )
        telegram_conversation = model.message_id.in_(
            select(Message.id).where(
                Message.conversation_id.in_(
                    select(Conversation.id).where(
                        or_(
                            telegram_flag == "true",
                            telegram_flag == "1",
                        )
                    )
                )
            )
        )
        return or_(
            explicit == source,
            and_(no_explicit, ~_task_has_project_id(model), telegram_conversation),
        )
    return None  # chat: 派生默认值,最宽,无需预筛


def _apply_task_item_filters(
    stmt: Any,
    model: Any,
    request: TaskListRequest,
) -> Any:
    """把可精确求值的 item 过滤条件下推到 SQL,零匹配时首轮即空。

    build_task_list 的深挖循环靠"批尾仍在页尾之上"判定是否继续拉深;纯内存
    过滤的 filter 零匹配时无法提前知道没有结果,会把用户两张任务表逐批扫完
    (全表分页扫描)。这里把 conversation_id / retryable / 显式 source /
    project_id 转成 SQL 条件,零匹配直接返回空集,循环首轮即退出。
    下推条件只做精确(或更宽松)的预筛 —— 内存 _item_matches 继续兜底,
    不会漏行;无法精确表达的派生值(source 的 project/telegram/chat 回退、
    conversation_default_params)保留在内存过滤。

    派生源(source=project/telegram)同样下推"必要不充分"预筛(_derived_source_
    precondition):零匹配派生源 filter 不再把两张任务表逐批扫完。
    """
    if request.conversation_id:
        stmt = stmt.where(
            model.message_id.in_(
                select(Message.id).where(
                    Message.conversation_id == request.conversation_id
                )
            )
        )
    if request.retryable is not None:
        # 与 _task_retryable 同一口径: canceled 可重试;failed 且 error_code
        # 不在终态码集合可重试。
        if request.retryable:
            condition = or_(
                model.status == "canceled",
                and_(
                    model.status == "failed",
                    or_(
                        model.error_code.is_(None),
                        model.error_code.not_in(_TERMINAL_ERROR_CODES),
                    ),
                ),
            )
        else:
            condition = and_(
                model.status != "canceled",
                or_(
                    model.status != "failed",
                    and_(
                        model.error_code.is_not(None),
                        model.error_code.in_(_TERMINAL_ERROR_CODES),
                    ),
                ),
            )
        stmt = stmt.where(condition)
    if request.source and request.source not in _DERIVED_SOURCES:
        # 显式 source 存在 upstream_request JSON 里;回退派生值不进这里。
        stmt = stmt.where(
            model.upstream_request["source"].as_string() == request.source
        )
    if request.source in _DERIVED_SOURCES:
        # 派生源的"必要不充分"预筛:零匹配时首轮即空,但只放宽不收紧,
        # 命中行由内存 _item_matches 精确过滤。
        precondition = _derived_source_precondition(model, request.source)
        if precondition is not None:
            stmt = stmt.where(precondition)
    if request.project_id:
        # project_id 来自 upstream_request.workflow_run_id 或 message
        # content.workflow_run_id(与 _task_project_meta 一致)。
        stmt = stmt.where(
            or_(
                model.upstream_request["workflow_run_id"].as_string()
                == request.project_id,
                model.message_id.in_(
                    select(Message.id).where(
                        Message.content["workflow_run_id"].as_string()
                        == request.project_id
                    )
                ),
            )
        )
    return stmt


def _task_listing_runtime() -> TaskListingRuntime:
    return TaskListingRuntime(
        apply_cursor=_apply_task_cursor,
        apply_date_filter=_apply_task_date_filter,
        apply_item_filters=_apply_task_item_filters,
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
    query: Annotated[TaskListQuery, Depends()],
) -> TaskListOut:
    _ = query.mine  # V1: always mine==1; flag accepted for API compat.
    return await build_task_list(
        db,
        _task_listing_runtime(),
        TaskListRequest(
            user_id=user.id,
            status=query.status,
            kind=query.kind,
            source=query.source,
            conversation_id=query.conversation_id,
            project_id=query.project_id,
            date_filter=query.date_filter,
            cursor=_decode_task_cursor(query.cursor),
            error_code=query.error_code,
            retryable=query.retryable,
            limit=query.limit,
        ),
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
