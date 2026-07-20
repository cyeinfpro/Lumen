"""Admin request-event querying and serialization.

The service deliberately has no dependency on ``app.routes``.  Route-specific
error and URL behavior is supplied through ``RequestEventsRuntime``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lumen_core.constants import (
    DEFAULT_IMAGE_RESPONSES_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    UPSTREAM_MODEL,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    User,
)
from lumen_core.utils import ensure_utc

logger = logging.getLogger(__name__)

RequestKind = Literal["all", "generation", "completion"]
RequestRange = Literal["24h", "7d", "30d"]
ImageRole = Literal["input", "output"]
ErrorFactory = Callable[[str, str, int], Exception]
UrlFactory = Callable[[str], str]


@dataclass(frozen=True)
class RequestEventsRuntime:
    http_error: ErrorFactory
    get_redis: Callable[[], Any]
    image_binary_url: UrlFactory
    image_variant_url: Callable[[str, str], str]


_REQUEST_EVENT_STATUSES = {
    "queued",
    "running",
    "streaming",
    "succeeded",
    "failed",
    "canceled",
}
_REQUEST_EVENT_RANGE_HOURS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}
_IMAGE_INFLIGHT_PREFIX = "generation:image_inflight:"


class _RequestEventImageOut(BaseModel):
    id: str
    roles: list[ImageRole]
    source: str
    url: str
    display_url: str
    preview_url: str | None
    thumb_url: str | None
    width: int
    height: int
    mime: str
    parent_image_id: str | None = None
    owner_generation_id: str | None = None


class _RequestEventLiveLane(BaseModel):
    """In-flight 状态下某条 lane 当前正在请求的 provider 快照。

    单 provider 模式：只有一个 lane（label 为空字符串或 "main"）。
    dual_race：image2 / responses 两条；image_jobs dual_race：generations / responses。
    status="failover" 表示刚切走但下一个 provider 还没选好（短暂窗口）。
    """

    label: str
    provider: str | None = None
    route: str | None = None
    endpoint: str | None = None
    status: str | None = None
    last_failed: str | None = None


class _RequestEventOut(BaseModel):
    id: str
    kind: Literal["generation", "completion"]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    status: str
    progress_stage: str
    attempt: int
    model: str
    user_id: str
    user_email: str
    conversation_id: str | None
    conversation_title: str | None
    message_id: str
    prompt: str | None = None
    action: str | None = None
    intent: str | None = None
    upstream_provider: str | None = None
    upstream_route: str | None = None
    upstream_endpoint: str | None = None
    queue_lane: str | None = None
    workflow_type: str | None = None
    workflow_step_key: str | None = None
    pixel_count: int | None = None
    size_bucket: str | None = None
    cost_class: str | None = None
    queue_wait_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    images: list[_RequestEventImageOut] = Field(default_factory=list)
    upstream: dict[str, Any] = Field(default_factory=dict)
    live_provider: str | None = None
    live_lanes: list[_RequestEventLiveLane] = Field(default_factory=list)


class _RequestEventModelStatOut(BaseModel):
    model: str
    count: int
    share: float


class _RequestEventsOut(BaseModel):
    items: list[_RequestEventOut] = Field(default_factory=list)
    total: int
    model_stats: list[_RequestEventModelStatOut] = Field(default_factory=list)


# Keep the service-level names readable for callers while making the concrete
# model identities match the historical route facade and OpenAPI schema names.
RequestEventImageOut = _RequestEventImageOut
RequestEventLiveLane = _RequestEventLiveLane
RequestEventOut = _RequestEventOut
RequestEventModelStatOut = _RequestEventModelStatOut
RequestEventsOut = _RequestEventsOut


def redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def json_str(data: dict[str, Any] | None, *keys: str) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def json_bool(data: dict[str, Any] | None, key: str) -> bool:
    return bool(isinstance(data, dict) and data.get(key) is True)


def request_provider_from_attempts(attempts: Any) -> str | None:
    if not isinstance(attempts, list):
        return None
    fallback: str | None = None
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        provider = json_str(
            attempt, "request_event_provider", "actual_provider", "provider"
        )
        if not provider or provider in {"dual_race", "dual_race_bonus"}:
            continue
        if (json_str(attempt, "status") or "").lower() == "used":
            return provider
        fallback = provider
    return fallback


def request_provider(upstream_request: dict[str, Any] | None) -> str | None:
    if not isinstance(upstream_request, dict):
        return None
    for key in (
        "request_event_provider",
        "actual_provider",
        "provider",
        "upstream_provider",
        "selected_provider",
        "transparent_pipeline_provider",
    ):
        provider = json_str(upstream_request, key)
        if provider and provider not in {"dual_race", "dual_race_bonus"}:
            return provider
    provider = request_provider_from_attempts(upstream_request.get("provider_attempts"))
    if provider:
        return provider
    diagnostics = upstream_request.get("generation_diagnostics")
    if isinstance(diagnostics, dict):
        for key in ("actual_provider", "provider"):
            provider = json_str(diagnostics, key)
            if provider and provider not in {"dual_race", "dual_race_bonus"}:
                return provider
        return request_provider_from_attempts(diagnostics.get("provider_attempts"))
    return None


def request_route(upstream_request: dict[str, Any] | None) -> str | None:
    route = json_str(
        upstream_request,
        "upstream_route",
        "actual_route",
        "image_route",
        "route",
        "primary_route",
    )
    if route:
        return route
    if json_bool(upstream_request, "is_dual_race_bonus"):
        return "dual_race_bonus"
    return None


def image_inflight_key(task_id: str) -> str:
    return f"{_IMAGE_INFLIGHT_PREFIX}{task_id}"


def is_inflight_status(status: str | None) -> bool:
    return (status or "") in {"queued", "running", "streaming"}


def decode_inflight_value(value: Any) -> str | None:
    text = redis_text(value)
    return text.strip() if text and text.strip() else None


def decode_inflight_hash(raw: Any) -> dict[str, str]:
    if not raw:
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        key_text = decode_inflight_value(key)
        val_text = decode_inflight_value(value)
        if key_text and val_text is not None:
            out[key_text] = val_text
    return out


async def fetch_image_inflight(
    redis: Any, task_ids: list[str]
) -> dict[str, dict[str, str]]:
    if not task_ids:
        return {}
    try:
        pipe = redis.pipeline()
        for task_id in task_ids:
            pipe.hgetall(image_inflight_key(task_id))
        rows = await pipe.execute()
    except Exception:
        logger.debug("image_inflight pipeline read failed", exc_info=True)
        return {}
    return {
        task_id: decoded
        for task_id, row in zip(task_ids, rows or [])
        if (decoded := decode_inflight_hash(row))
    }


def build_live_lanes_from_snapshot(
    snapshot: dict[str, str],
) -> tuple[str | None, list[_RequestEventLiveLane]]:
    if snapshot.get("mode") == "dual_race":
        labels = [
            snapshot.get("lane_a_label") or "image2",
            snapshot.get("lane_b_label") or "responses",
        ]
        if snapshot.get("lane_a_route", "").startswith("image_jobs"):
            labels[0] = "image_jobs:generations"
        if snapshot.get("lane_b_route", "").startswith("image_jobs"):
            labels[1] = "image_jobs:responses"
        lanes = [
            _RequestEventLiveLane(
                label=labels[0],
                provider=snapshot.get("lane_a_provider"),
                route=snapshot.get("lane_a_route"),
                endpoint=snapshot.get("lane_a_endpoint"),
                status=snapshot.get("lane_a_status"),
                last_failed=snapshot.get("lane_a_last_failed"),
            ),
            _RequestEventLiveLane(
                label=labels[1],
                provider=snapshot.get("lane_b_provider"),
                route=snapshot.get("lane_b_route"),
                endpoint=snapshot.get("lane_b_endpoint"),
                status=snapshot.get("lane_b_status"),
                last_failed=snapshot.get("lane_b_last_failed"),
            ),
        ]

        def label(lane: _RequestEventLiveLane) -> str:
            if lane.provider:
                return lane.provider
            if lane.status == "failover" and lane.last_failed:
                return f"切换中 (上一个 {lane.last_failed})"
            return "等待中"

        return f"{label(lanes[0])} vs {label(lanes[1])}", lanes

    lane = _RequestEventLiveLane(
        label="main",
        provider=snapshot.get("provider"),
        route=snapshot.get("actual_route") or snapshot.get("route"),
        endpoint=snapshot.get("endpoint"),
        status=snapshot.get("status"),
        last_failed=snapshot.get("last_failed"),
    )
    if lane.provider:
        summary = lane.provider
    elif lane.status == "failover" and lane.last_failed:
        summary = f"切换中 (上一个 {lane.last_failed})"
    else:
        summary = None
    return summary, [lane]


_MODEL_SHORT_LABELS = {
    UPSTREAM_MODEL: "image2",
    DEFAULT_IMAGE_RESPONSES_MODEL: "5.4",
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST: "5.4 mini",
}


def short_model(name: str) -> str:
    return _MODEL_SHORT_LABELS.get(name, name)


def responses_model_from_request(req: dict[str, Any], *, fast: bool) -> str:
    return json_str(req, "responses_model") or (
        DEFAULT_IMAGE_RESPONSES_MODEL_FAST if fast else DEFAULT_IMAGE_RESPONSES_MODEL
    )


def generation_model_label_from_request(
    upstream_request: dict[str, Any] | None,
    *,
    action: str,
    status: str,
) -> str:
    req = upstream_request if isinstance(upstream_request, dict) else {}
    explicit = json_str(req, "model", "upstream_model", "reasoning_model")
    if explicit:
        return short_model(explicit)
    route = request_route(req) or "responses"
    actual_route = json_str(req, "actual_route")
    endpoint = json_str(req, "actual_endpoint") or ""
    fast = bool(req.get("fast"))
    actual_model = _model_from_actual_route(
        req,
        actual_route=actual_route,
        endpoint=endpoint,
        fast=fast,
    )
    if actual_model is not None:
        return actual_model
    return _model_from_requested_route(
        req,
        route=route,
        action=action,
        status=status,
        fast=fast,
    )


def _model_from_actual_route(
    req: dict[str, Any],
    *,
    actual_route: str | None,
    endpoint: str,
    fast: bool,
) -> str | None:
    if endpoint.startswith(("image-jobs:responses", "responses:")):
        return short_model(responses_model_from_request(req, fast=fast))
    if endpoint.startswith(("image-jobs:", "images/")):
        return short_model(UPSTREAM_MODEL)
    if actual_route and actual_route.startswith(("image2", "image_jobs")):
        return short_model(UPSTREAM_MODEL)
    if actual_route and actual_route.startswith("responses"):
        return short_model(responses_model_from_request(req, fast=fast))
    return None


def _model_from_requested_route(
    req: dict[str, Any],
    *,
    route: str,
    action: str,
    status: str,
    fast: bool,
) -> str:
    if route == "image2":
        return short_model(UPSTREAM_MODEL)
    if route == "image_jobs":
        if (action == "generate" and not fast) or action == "edit":
            return short_model(UPSTREAM_MODEL)
        return short_model(responses_model_from_request(req, fast=fast))
    if route == "dual_race" and status in {"queued", "running"}:
        return f"竞速中: {short_model(responses_model_from_request(req, fast=fast))} / {short_model(UPSTREAM_MODEL)}"
    if route == "dual_race":
        return "历史未记录"
    return short_model(responses_model_from_request(req, fast=fast))


def generation_model_label(gen: Generation) -> str:
    return generation_model_label_from_request(
        gen.upstream_request if isinstance(gen.upstream_request, dict) else {},
        action=gen.action,
        status=gen.status,
    )


def request_event_model_stat_label(model: str) -> str:
    normalized = (model or "").strip()
    if normalized in {
        short_model(DEFAULT_IMAGE_RESPONSES_MODEL),
        short_model(DEFAULT_IMAGE_RESPONSES_MODEL_FAST),
        DEFAULT_IMAGE_RESPONSES_MODEL,
        DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    }:
        return "Codex 原生"
    if normalized in {short_model(UPSTREAM_MODEL), UPSTREAM_MODEL}:
        return "image2 直连"
    return normalized or "未记录"


def generation_endpoint(gen: Generation) -> str:
    req = gen.upstream_request if isinstance(gen.upstream_request, dict) else {}
    route = request_route(req) or "responses"
    endpoint = json_str(req, "actual_endpoint")
    if endpoint:
        return endpoint
    if route == "image2":
        return "images/edits" if gen.action == "edit" else "images/generations"
    if route == "image_jobs":
        return (
            "image-jobs:generations"
            if gen.action == "generate"
            else "responses:image_generation"
        )
    if route in {"dual_race", "dual_race_bonus"}:
        return route
    return "responses:image_generation"


def safe_upstream_details(upstream_request: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(upstream_request, dict):
        return {}
    allowed = {
        "action_source",
        "actual_endpoint",
        "actual_provider",
        "actual_route",
        "actual_source",
        "background",
        "fast",
        "image_job_endpoint_used",
        "image_job_expires_at",
        "image_job_format",
        "image_job_id",
        "image_job_url",
        "image_route",
        "mime",
        "moderation",
        "output_compression",
        "output_format",
        "pixel_count",
        "queue_lane",
        "queue_wait_ms",
        "render_quality",
        "request_event_provider",
        "responses_model",
        "revised_prompt",
        "route",
        "size_actual",
        "size_bucket",
        "cost_class",
        "source",
        "transparent_alpha_recovered",
        "transparent_pipeline_provider",
        "upstream_route",
        "web_search",
        "workflow_step_key",
        "workflow_type",
    }
    details = {
        key: upstream_request[key] for key in sorted(allowed) if key in upstream_request
    }
    context = upstream_request.get("context")
    if isinstance(context, dict):
        details["context"] = {
            key: context[key]
            for key in (
                "estimated_input_tokens",
                "included_messages_count",
                "summary_used",
                "summary_created",
                "fallback_reason",
                "compressor_model",
                "image_caption_count",
            )
            if key in context
        }
    return details


def normalize_request_event_status(
    status: str | None, *, http_error: ErrorFactory
) -> str | None:
    if status is None:
        return None
    normalized = status.strip().lower()
    if not normalized or normalized == "all":
        return None
    if normalized not in _REQUEST_EVENT_STATUSES:
        raise http_error("invalid_status", "unsupported request event status", 400)
    return normalized


def request_event_since(request_range: RequestRange, now: datetime) -> datetime:
    return now - timedelta(hours=_REQUEST_EVENT_RANGE_HOURS[request_range])


def request_event_sort_key(row: dict[str, Any]) -> tuple[bool, datetime, str]:
    task = row["task"]
    finished_at = getattr(task, "finished_at", None)
    sort_at = finished_at or getattr(task, "created_at", None)
    return (
        finished_at is None,
        ensure_utc(sort_at or datetime.min.replace(tzinfo=timezone.utc)),
        str(getattr(task, "id", "")),
    )


def request_event_time_filter(model: Any, since: datetime) -> Any:
    return or_(
        model.finished_at >= since,
        and_(model.finished_at.is_(None), model.created_at >= since),
    )


def event_image_out(
    img: Image,
    roles: set[ImageRole],
    variant_kinds: set[str],
    *,
    image_binary_url: UrlFactory,
    image_variant_url: Callable[[str, str], str],
) -> _RequestEventImageOut:
    return _RequestEventImageOut(
        id=img.id,
        roles=sorted(roles, key=lambda role: 0 if role == "output" else 1),
        source=img.source,
        url=image_binary_url(img.id),
        display_url=image_variant_url(img.id, "display2048"),
        preview_url=image_variant_url(img.id, "preview1024")
        if "preview1024" in variant_kinds
        else None,
        thumb_url=image_variant_url(img.id, "thumb256")
        if "thumb256" in variant_kinds
        else None,
        width=img.width,
        height=img.height,
        mime=img.mime,
        parent_image_id=img.parent_image_id,
        owner_generation_id=img.owner_generation_id,
    )


def message_output_image_refs(content: Any) -> list[tuple[str, str | None]]:
    if not isinstance(content, dict) or not isinstance(content.get("images"), list):
        return []
    refs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for item in content["images"]:
        if isinstance(item, str):
            image_id, generation_id = item, None
        elif isinstance(item, dict):
            image_id = item.get("image_id") or item.get("id")
            generation_id = (
                item.get("from_generation_id")
                or item.get("generation_id")
                or item.get("owner_generation_id")
            )
        else:
            continue
        if not isinstance(image_id, str) or not image_id or image_id in seen:
            continue
        seen.add(image_id)
        refs.append(
            (image_id, generation_id if isinstance(generation_id, str) else None)
        )
    return refs


def request_event_model_stats_from_counts(
    counts: dict[str, int],
) -> list[_RequestEventModelStatOut]:
    counts = {model: count for model, count in counts.items() if model and count > 0}
    total = sum(counts.values())
    return (
        [
            _RequestEventModelStatOut(model=model, count=count, share=count / total)
            for model, count in sorted(
                counts.items(), key=lambda entry: (-entry[1], entry[0])
            )
        ]
        if total
        else []
    )


async def request_event_model_stats_for_filters(
    db: AsyncSession,
    *,
    since: datetime,
    kind: RequestKind,
    status: str | None,
) -> list[_RequestEventModelStatOut]:
    counts: dict[str, int] = {}
    if kind in {"all", "generation"}:
        stmt = (
            select(Generation.upstream_request, Generation.action, Generation.status)
            .join(User, User.id == Generation.user_id)
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                User.deleted_at.is_(None), request_event_time_filter(Generation, since)
            )
        )
        if status:
            stmt = stmt.where(Generation.status == status)
        for request, action, task_status in (await db.execute(stmt)).all():
            label = request_event_model_stat_label(
                generation_model_label_from_request(
                    request if isinstance(request, dict) else {},
                    action=str(action),
                    status=str(task_status),
                )
            )
            counts[label] = counts.get(label, 0) + 1
    if kind in {"all", "completion"}:
        stmt = (
            select(Completion.model)
            .join(User, User.id == Completion.user_id)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                User.deleted_at.is_(None), request_event_time_filter(Completion, since)
            )
        )
        if status:
            stmt = stmt.where(Completion.status == status)
        for (model,) in (await db.execute(stmt)).all():
            label = request_event_model_stat_label(str(model or ""))
            counts[label] = counts.get(label, 0) + 1
    return request_event_model_stats_from_counts(counts)


def request_event_prompt(content: Any) -> str | None:
    return (
        content.get("text")
        if isinstance(content, dict) and isinstance(content.get("text"), str)
        else None
    )


async def _load_event_rows(
    db: AsyncSession,
    *,
    since: datetime,
    limit: int,
    kind: RequestKind,
    status: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if kind in {"all", "generation"}:
        stmt = (
            select(
                Generation,
                User.email,
                Conversation.id,
                Conversation.title,
                Message.intent,
            )
            .join(User, User.id == Generation.user_id)
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                User.deleted_at.is_(None), request_event_time_filter(Generation, since)
            )
            .order_by(
                desc(Generation.finished_at).nulls_first(),
                desc(Generation.created_at),
                desc(Generation.id),
            )
            .limit(limit)
        )
        if status:
            stmt = stmt.where(Generation.status == status)
        for task, email, conversation_id, title, intent in (
            await db.execute(stmt)
        ).all():
            rows.append(
                {
                    "kind": "generation",
                    "task": task,
                    "user_email": email,
                    "conversation_id": conversation_id,
                    "conversation_title": title,
                    "assistant_intent": intent,
                }
            )
    if kind in {"all", "completion"}:
        user_msg = aliased(Message)
        stmt = (
            select(
                Completion,
                User.email,
                Conversation.id,
                Conversation.title,
                Message.intent,
                user_msg.content,
            )
            .join(User, User.id == Completion.user_id)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(user_msg, user_msg.id == Message.parent_message_id, isouter=True)
            .where(
                User.deleted_at.is_(None), request_event_time_filter(Completion, since)
            )
            .order_by(
                desc(Completion.finished_at).nulls_first(),
                desc(Completion.created_at),
                desc(Completion.id),
            )
            .limit(limit)
        )
        if status:
            stmt = stmt.where(Completion.status == status)
        for task, email, conversation_id, title, intent, content in (
            await db.execute(stmt)
        ).all():
            rows.append(
                {
                    "kind": "completion",
                    "task": task,
                    "user_email": email,
                    "conversation_id": conversation_id,
                    "conversation_title": title,
                    "assistant_intent": intent,
                    "prompt": request_event_prompt(content),
                }
            )
    rows.sort(key=request_event_sort_key, reverse=True)
    return rows[:limit]


async def _image_context(
    db: AsyncSession,
    rows: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, set[ImageRole]]], dict[str, Image], dict[str, set[str]]]:
    image_roles, image_ids, generations_by_message = _seed_image_roles(rows)
    await _attach_message_output_roles(
        db,
        rows=rows,
        image_roles=image_roles,
        image_ids=image_ids,
        generations_by_message=generations_by_message,
    )
    await _attach_generation_output_roles(
        db,
        rows=rows,
        image_roles=image_roles,
        image_ids=image_ids,
    )
    images = await _load_images(db, image_ids)
    variants = await _load_variants(db, image_ids)
    return image_roles, images, variants


def _seed_image_roles(
    rows: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, set[ImageRole]]],
    set[str],
    dict[str, list[str]],
]:
    image_roles: dict[str, dict[str, set[ImageRole]]] = {}
    image_ids: set[str] = set()
    generations_by_message: dict[str, list[str]] = {}
    for row in rows:
        task = row["task"]
        if row["kind"] == "generation":
            generations_by_message.setdefault(task.message_id, []).append(task.id)
        roles = image_roles.setdefault(task.id, {})
        for image_id in list(getattr(task, "input_image_ids", None) or []):
            roles.setdefault(image_id, set()).add("input")
            image_ids.add(image_id)
    return image_roles, image_ids, generations_by_message


async def _attach_message_output_roles(
    db: AsyncSession,
    *,
    rows: list[dict[str, Any]],
    image_roles: dict[str, dict[str, set[ImageRole]]],
    image_ids: set[str],
    generations_by_message: dict[str, list[str]],
) -> None:
    message_ids = {
        row["task"].message_id
        for row in rows
        if isinstance(getattr(row["task"], "message_id", None), str)
    }
    if not message_ids:
        return
    message_rows = (
        await db.execute(
            select(Message.id, Message.content).where(Message.id.in_(message_ids))
        )
    ).all()
    refs_by_message = {
        message_id: message_output_image_refs(content)
        for message_id, content in message_rows
    }
    for row in rows:
        task = row["task"]
        for image_id, generation_id in refs_by_message.get(task.message_id, []):
            if not _output_reference_matches(
                row,
                generation_id=generation_id,
                generations_by_message=generations_by_message,
            ):
                continue
            image_roles.setdefault(task.id, {}).setdefault(image_id, set()).add(
                "output"
            )
            image_ids.add(image_id)


def _output_reference_matches(
    row: dict[str, Any],
    *,
    generation_id: str | None,
    generations_by_message: dict[str, list[str]],
) -> bool:
    if row["kind"] != "generation":
        return True
    task = row["task"]
    if generation_id:
        return generation_id == task.id
    return len(generations_by_message.get(task.message_id, [])) <= 1


async def _attach_generation_output_roles(
    db: AsyncSession,
    *,
    rows: list[dict[str, Any]],
    image_roles: dict[str, dict[str, set[ImageRole]]],
    image_ids: set[str],
) -> None:
    generation_ids = [row["task"].id for row in rows if row["kind"] == "generation"]
    if not generation_ids:
        return
    output_rows = (
        await db.execute(
            select(Image).where(
                Image.owner_generation_id.in_(generation_ids),
                Image.deleted_at.is_(None),
            )
        )
    ).scalars()
    for image in output_rows:
        if image.owner_generation_id:
            image_roles.setdefault(image.owner_generation_id, {}).setdefault(
                image.id, set()
            ).add("output")
            image_ids.add(image.id)


async def _load_images(
    db: AsyncSession,
    image_ids: set[str],
) -> dict[str, Image]:
    if not image_ids:
        return {}
    return {
        image.id: image
        for image in (
            await db.execute(
                select(Image).where(
                    Image.id.in_(image_ids),
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars()
    }


async def _load_variants(
    db: AsyncSession,
    image_ids: set[str],
) -> dict[str, set[str]]:
    variants: dict[str, set[str]] = {}
    if not image_ids:
        return variants
    rows = (
        await db.execute(
            select(ImageVariant.image_id, ImageVariant.kind).where(
                ImageVariant.image_id.in_(image_ids)
            )
        )
    ).all()
    for image_id, kind in rows:
        variants.setdefault(image_id, set()).add(kind)
    return variants


async def list_request_events(
    db: AsyncSession,
    *,
    limit: int,
    kind: RequestKind,
    status: str | None,
    request_range: RequestRange,
    runtime: RequestEventsRuntime,
) -> _RequestEventsOut:
    now = datetime.now(timezone.utc)
    since = request_event_since(request_range, now)
    status = normalize_request_event_status(status, http_error=runtime.http_error)
    model_stats = await request_event_model_stats_for_filters(
        db, since=since, kind=kind, status=status
    )
    rows = await _load_event_rows(
        db, since=since, limit=limit, kind=kind, status=status
    )
    image_roles, image_by_id, variants = await _image_context(db, rows)
    inflight_ids = [
        row["task"].id
        for row in rows
        if row["kind"] == "generation"
        and is_inflight_status(getattr(row["task"], "status", None))
    ]
    snapshots: dict[str, dict[str, str]] = {}
    if inflight_ids:
        try:
            redis = runtime.get_redis()
        except Exception:
            redis = None
        if redis is not None:
            snapshots = await fetch_image_inflight(redis, inflight_ids)

    items: list[_RequestEventOut] = []
    for row in rows:
        task = row["task"]
        req = task.upstream_request if isinstance(task.upstream_request, dict) else {}
        roles = image_roles.get(task.id, {})
        event_images = [
            event_image_out(
                image_by_id[image_id],
                image_roles_for_image,
                variants.get(image_id, set()),
                image_binary_url=runtime.image_binary_url,
                image_variant_url=runtime.image_variant_url,
            )
            for image_id, image_roles_for_image in sorted(
                roles.items(),
                key=lambda item: (
                    0 if "output" in item[1] else 1,
                    getattr(image_by_id.get(item[0]), "created_at", now),
                ),
            )
            if image_id in image_by_id
        ]
        generation = row["kind"] == "generation"
        model = generation_model_label(task) if generation else task.model
        endpoint = (
            generation_endpoint(task)
            if generation
            else json_str(req, "actual_endpoint", "endpoint") or "responses"
        )
        live_provider, live_lanes = (None, [])
        if generation and is_inflight_status(task.status) and task.id in snapshots:
            live_provider, live_lanes = build_live_lanes_from_snapshot(
                snapshots[task.id]
            )
        items.append(
            _RequestEventOut(
                id=task.id,
                kind=row["kind"],
                created_at=task.created_at,
                started_at=task.started_at,
                finished_at=task.finished_at,
                duration_ms=_duration_ms(task.started_at, task.finished_at, now),
                status=task.status,
                progress_stage=task.progress_stage,
                attempt=task.attempt,
                model=model,
                user_id=task.user_id,
                user_email=row["user_email"],
                conversation_id=row["conversation_id"],
                conversation_title=row["conversation_title"] or None,
                message_id=task.message_id,
                prompt=task.prompt if generation else row.get("prompt"),
                action=task.action if generation else None,
                intent=row.get("assistant_intent"),
                upstream_provider=request_provider(req),
                upstream_route=request_route(req),
                upstream_endpoint=endpoint,
                queue_lane=getattr(task, "queue_lane", None),
                workflow_type=getattr(task, "workflow_type", None),
                workflow_step_key=getattr(task, "workflow_step_key", None),
                pixel_count=getattr(task, "pixel_count", None),
                size_bucket=getattr(task, "size_bucket", None),
                cost_class=getattr(task, "cost_class", None),
                queue_wait_ms=getattr(task, "queue_wait_ms", None),
                tokens_in=None if generation else task.tokens_in,
                tokens_out=None if generation else task.tokens_out,
                error_code=task.error_code,
                error_message=task.error_message,
                images=event_images,
                upstream=safe_upstream_details(req),
                live_provider=live_provider,
                live_lanes=live_lanes,
            )
        )
    return _RequestEventsOut(items=items, total=len(items), model_stats=model_stats)


def _duration_ms(
    started_at: datetime | None,
    finished_at: datetime | None,
    now: datetime,
) -> int | None:
    if started_at is None:
        return None
    return max(
        0,
        int(
            (ensure_utc(finished_at or now) - ensure_utc(started_at)).total_seconds()
            * 1000
        ),
    )
