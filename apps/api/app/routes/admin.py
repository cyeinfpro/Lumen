"""Admin 路由（V1.0 收尾）：邮箱白名单管理 + 用户列表与聚合统计。

所有端点需要 role=admin（AdminUser 依赖）。写操作使用 verify_csrf。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Awaitable, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import and_, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lumen_core.constants import (
    DEFAULT_IMAGE_RESPONSES_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    UPSTREAM_MODEL,
)
from lumen_core.models import (
    AllowedEmail,
    AuthSession,
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    OutboxDeadLetter,
    OutboxEvent,
    User,
    VideoGeneration,
    WorkflowRun,
)
from lumen_core.schemas import AdminUserOut, AllowedEmailOut
from lumen_core.utils import ensure_utc
from lumen_core.byok_retention import retention_state as byok_retention_state

from ..audit import hash_email
from ..byok_service import read_byok_settings_cached, retention_policy_from_settings
from ..db import affected_rows, get_db
from ..deps import AdminUser, verify_csrf
from ..redis_client import get_redis
from ..security import hash_password
from ._admin_common import admin_http as _http, write_admin_audit
from .images import (
    ALLOWED_VARIANTS,
    DISPLAY_VARIANT,
    VARIANT_MEDIA_TYPE,
    _ensure_display_variant,
    _fs_path,
    _storage_streaming_response,
)
from .me import _cancel_account_active_tasks, _post_commit_account_task_cleanup


router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


_CONTEXT_METRIC_FIELDS = (
    "summary_attempts",
    "summary_successes",
    "summary_failures",
    "manual_compact_calls",
    "cold_start_count",
)
_CONTEXT_CIRCUIT_STATE_KEY = "context:circuit:breaker:state"
_CONTEXT_CIRCUIT_UNTIL_KEY = "context:circuit:breaker:until"


def _context_health_zero(
    *,
    degraded: bool = False,
    degrade_reason: str | None = None,
) -> dict:
    return {
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "circuit_breaker_state": "closed",
        "circuit_breaker_until": None,
        "last_24h": {
            "summary_attempts": 0,
            "summary_successes": 0,
            "summary_failures": 0,
            "summary_success_rate": 0.0,
            "summary_p50_latency_ms": 0,
            "summary_p95_latency_ms": 0,
            "manual_compact_calls": 0,
            "cold_start_count": 0,
            "fallback_reasons": {},
        },
    }


def _redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _redis_int(value: Any) -> int:
    text = _redis_text(value)
    if text is None or not text:
        return 0
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered[lower]
    interpolated = ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower)
    return int(round(interpolated))


def _extend_latency_samples(samples: list[int], raw: Any) -> None:
    text = _redis_text(raw)
    if not text:
        return
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            samples.extend(
                _redis_int(item) for item in parsed if _redis_int(item) >= 0
            )
            return
    except Exception:
        pass
    for part in text.split(","):
        value = _redis_int(part.strip())
        if value >= 0:
            samples.append(value)


def _fold_context_metrics(rows: list[dict[Any, Any]]) -> dict:
    totals = {field: 0 for field in _CONTEXT_METRIC_FIELDS}
    fallback_reasons: dict[str, int] = {}
    latency_samples: list[int] = []
    p50_values: list[int] = []
    p95_values: list[int] = []

    for row in rows:
        normalized = {str(_redis_text(k) or ""): v for k, v in row.items()}
        for field in _CONTEXT_METRIC_FIELDS:
            totals[field] += _redis_int(normalized.get(field))

        for key, value in normalized.items():
            reason: str | None = None
            for prefix in (
                "fallback_reasons:",
                "fallback_reason:",
                "fallback:",
                "fallback_reasons.",
                "fallback_reason.",
            ):
                if key.startswith(prefix):
                    reason = key[len(prefix):]
                    break
            if reason:
                fallback_reasons[reason] = fallback_reasons.get(
                    reason, 0
                ) + _redis_int(value)

        _extend_latency_samples(
            latency_samples, normalized.get("summary_latency_ms_samples")
        )
        _extend_latency_samples(latency_samples, normalized.get("summary_latency_samples"))
        p50 = _redis_int(normalized.get("summary_p50_latency_ms"))
        p95 = _redis_int(normalized.get("summary_p95_latency_ms"))
        if p50:
            p50_values.append(p50)
        if p95:
            p95_values.append(p95)

    attempts = totals["summary_attempts"]
    successes = totals["summary_successes"]
    success_rate = round(successes / attempts, 3) if attempts > 0 else 0.0
    return {
        **totals,
        "summary_success_rate": success_rate,
        "summary_p50_latency_ms": _percentile(latency_samples, 0.50)
        if latency_samples
        else _percentile(p50_values, 0.50),
        "summary_p95_latency_ms": _percentile(latency_samples, 0.95)
        if latency_samples
        else _percentile(p95_values, 0.95),
        "fallback_reasons": fallback_reasons,
    }


def _hourly_context_metric_keys(now: datetime) -> list[str]:
    current_hour = now.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    return [
        f"context:metrics:hourly:{(current_hour - timedelta(hours=offset)).strftime('%Y%m%d%H')}"
        for offset in range(24)
    ]


def _iso_z(dt: datetime) -> str:
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")


def _duration_ms(
    started_at: datetime | None,
    finished_at: datetime | None,
    *,
    now: datetime,
) -> int | None:
    if started_at is None:
        return None
    end = finished_at or now
    seconds = (ensure_utc(end) - ensure_utc(started_at)).total_seconds()
    return max(0, int(seconds * 1000))


def _json_str(data: dict[str, Any] | None, *keys: str) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _json_bool(data: dict[str, Any] | None, key: str) -> bool:
    return bool(isinstance(data, dict) and data.get(key) is True)


def _request_provider(upstream_request: dict[str, Any] | None) -> str | None:
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
        provider = _json_str(upstream_request, key)
        if provider and provider not in {"dual_race", "dual_race_bonus"}:
            return provider
    provider = _request_provider_from_attempts(
        upstream_request.get("provider_attempts")
    )
    if provider:
        return provider
    diagnostics = upstream_request.get("generation_diagnostics")
    if isinstance(diagnostics, dict):
        for key in ("actual_provider", "provider"):
            provider = _json_str(diagnostics, key)
            if provider and provider not in {"dual_race", "dual_race_bonus"}:
                return provider
        provider = _request_provider_from_attempts(diagnostics.get("provider_attempts"))
        if provider:
            return provider
    return None


def _request_provider_from_attempts(attempts: Any) -> str | None:
    if not isinstance(attempts, list):
        return None
    fallback: str | None = None
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        provider = _json_str(
            attempt, "request_event_provider", "actual_provider", "provider"
        )
        if not provider or provider in {"dual_race", "dual_race_bonus"}:
            continue
        status = (_json_str(attempt, "status") or "").lower()
        if status == "used":
            return provider
        fallback = provider
    return fallback


def _request_route(upstream_request: dict[str, Any] | None) -> str | None:
    route = _json_str(
        upstream_request,
        "upstream_route",
        "actual_route",
        "image_route",
        "route",
        "primary_route",
    )
    if route:
        return route
    if _json_bool(upstream_request, "is_dual_race_bonus"):
        return "dual_race_bonus"
    return None


_IMAGE_INFLIGHT_PREFIX = "generation:image_inflight:"


def _image_inflight_key(task_id: str) -> str:
    return f"{_IMAGE_INFLIGHT_PREFIX}{task_id}"


def _is_inflight_status(status: str | None) -> bool:
    return (status or "") in {"queued", "running", "streaming"}


def _decode_inflight_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _decode_inflight_hash(raw: Any) -> dict[str, str]:
    """HGETALL 出来的可能是 dict[bytes,bytes] / dict[str,str] / 空 dict — 统一成 str→str。"""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        key_text = _decode_inflight_value(key)
        val_text = _decode_inflight_value(value)
        if key_text and val_text is not None:
            out[key_text] = val_text
    return out


async def _fetch_image_inflight(
    redis: Any, task_ids: list[str]
) -> dict[str, dict[str, str]]:
    """批量 HGETALL；redis 不可用时静默降级返回空 dict，不影响 admin 列表本身可用。"""
    if not task_ids:
        return {}
    try:
        pipe = redis.pipeline()
        for task_id in task_ids:
            pipe.hgetall(_image_inflight_key(task_id))
        rows = await pipe.execute()
    except Exception:
        logger.debug("image_inflight pipeline read failed", exc_info=True)
        return {}
    snapshots: dict[str, dict[str, str]] = {}
    for task_id, row in zip(task_ids, rows or []):
        decoded = _decode_inflight_hash(row)
        if decoded:
            snapshots[task_id] = decoded
    return snapshots


def _build_live_lanes_from_snapshot(
    snapshot: dict[str, str],
) -> tuple[str | None, list[_RequestEventLiveLane]]:
    """快照 → (摘要, 结构化 lane 列表)。

    单 provider：返回 ("provider 名", [{label="main", provider=...}])
    dual_race：返回 ("A vs B", [{label="image2", ...}, {label="responses", ...}])，
    任一 lane 还没选到 provider 时填 "?" 占位（视觉上仍能看到正在等待）。
    """
    summary: str | None
    mode = snapshot.get("mode") or ""
    if mode == "dual_race":
        lane_a_label = snapshot.get("lane_a_label") or "image2"
        lane_b_label = snapshot.get("lane_b_label") or "responses"
        lane_a_provider = snapshot.get("lane_a_provider")
        lane_b_provider = snapshot.get("lane_b_provider")
        # image_jobs dual_race 走 _classify_inflight_lane → lane_a=image_jobs:generations
        # 等；这里 label 仍然只用 image2/responses 两个名字（作为用户识别即可），具体
        # endpoint 走 lane.endpoint 字段。要更精细的 label 可读 lane_*_route。
        if snapshot.get("lane_a_route", "").startswith("image_jobs"):
            lane_a_label = "image_jobs:generations"
        if snapshot.get("lane_b_route", "").startswith("image_jobs"):
            lane_b_label = "image_jobs:responses"
        lanes = [
            _RequestEventLiveLane(
                label=lane_a_label,
                provider=lane_a_provider,
                route=snapshot.get("lane_a_route"),
                endpoint=snapshot.get("lane_a_endpoint"),
                status=snapshot.get("lane_a_status"),
                last_failed=snapshot.get("lane_a_last_failed"),
            ),
            _RequestEventLiveLane(
                label=lane_b_label,
                provider=lane_b_provider,
                route=snapshot.get("lane_b_route"),
                endpoint=snapshot.get("lane_b_endpoint"),
                status=snapshot.get("lane_b_status"),
                last_failed=snapshot.get("lane_b_last_failed"),
            ),
        ]

        def _label_for(lane: _RequestEventLiveLane) -> str:
            if lane.provider:
                return lane.provider
            if lane.status == "failover" and lane.last_failed:
                return f"切换中 (上一个 {lane.last_failed})"
            return "等待中"

        summary = f"{_label_for(lanes[0])} vs {_label_for(lanes[1])}"
        return summary, lanes

    # 单 provider 模式（含 single / 缺省 mode）
    provider = snapshot.get("provider")
    lane = _RequestEventLiveLane(
        label="main",
        provider=provider,
        route=snapshot.get("actual_route") or snapshot.get("route"),
        endpoint=snapshot.get("endpoint"),
        status=snapshot.get("status"),
        last_failed=snapshot.get("last_failed"),
    )
    if provider:
        summary = provider
    elif lane.status == "failover" and lane.last_failed:
        summary = f"切换中 (上一个 {lane.last_failed})"
    else:
        summary = None
    return summary, [lane]


def _request_actual_route(upstream_request: dict[str, Any] | None) -> str | None:
    return _json_str(
        upstream_request,
        "actual_route",
        "actual_source",
        "actual_endpoint",
    )


# Compact display names — admin model column is narrow, the operator only
# needs to recognise which "brain" handled the request, not its full SKU.
# Mapping is intentionally lossy: gpt-image-2 / gpt-5.4 / gpt-5.4-mini are
# the only models in play today; if a request carries an explicit override
# we surface it verbatim instead.
_MODEL_SHORT_LABELS = {
    UPSTREAM_MODEL: "image2",
    DEFAULT_IMAGE_RESPONSES_MODEL: "5.4",
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST: "5.4 mini",
}


def _short_model(name: str) -> str:
    return _MODEL_SHORT_LABELS.get(name, name)


def _responses_model_from_request(req: dict[str, Any], *, fast: bool) -> str:
    value = _json_str(req, "responses_model")
    if value:
        return value
    return DEFAULT_IMAGE_RESPONSES_MODEL_FAST if fast else DEFAULT_IMAGE_RESPONSES_MODEL


def _generation_model_label_from_request(
    upstream_request: dict[str, Any] | None,
    *,
    action: str,
    status: str,
) -> str:
    req = upstream_request if isinstance(upstream_request, dict) else {}
    explicit = _json_str(req, "model", "upstream_model", "reasoning_model")
    if explicit:
        return _short_model(explicit)
    route = _request_route(req) or "responses"
    actual_route = _request_actual_route(req)
    actual_endpoint = _json_str(req, "actual_endpoint") or ""
    fast = bool(req.get("fast"))

    # image-jobs sidecar can ride either /v1/images/generations or /v1/responses;
    # the endpoint string is the source of truth, not the route name.
    if actual_endpoint.startswith("image-jobs:responses") or actual_endpoint.startswith(
        "responses:"
    ):
        return _short_model(_responses_model_from_request(req, fast=fast))
    if actual_endpoint.startswith("image-jobs:") or actual_endpoint.startswith("images/"):
        return _short_model(UPSTREAM_MODEL)
    if actual_route and actual_route.startswith("image2"):
        return _short_model(UPSTREAM_MODEL)
    if actual_route and actual_route.startswith("image_jobs"):
        return _short_model(UPSTREAM_MODEL)
    if actual_route and actual_route.startswith("responses"):
        return _short_model(_responses_model_from_request(req, fast=fast))
    if route == "image2":
        return _short_model(UPSTREAM_MODEL)
    if route == "image_jobs":
        # No actual_endpoint yet (still queued/running) — guess by action +
        # fast flag. generate without fast / edit usually go via gpt-image-2
        # directly; fast generate goes through responses with the mini brain.
        if action == "generate" and not fast:
            return _short_model(UPSTREAM_MODEL)
        if action == "edit":
            return _short_model(UPSTREAM_MODEL)
        return _short_model(_responses_model_from_request(req, fast=fast))
    if route == "dual_race":
        if status in {"queued", "running"}:
            responses_label = _short_model(_responses_model_from_request(req, fast=fast))
            return f"竞速中: {responses_label} / {_short_model(UPSTREAM_MODEL)}"
        return "历史未记录"
    return _short_model(_responses_model_from_request(req, fast=fast))


def _generation_model_label(gen: Generation) -> str:
    req = gen.upstream_request if isinstance(gen.upstream_request, dict) else {}
    return _generation_model_label_from_request(
        req,
        action=gen.action,
        status=gen.status,
    )


def _request_event_model_stat_label(model: str) -> str:
    normalized = (model or "").strip()
    if normalized in {
        _short_model(DEFAULT_IMAGE_RESPONSES_MODEL),
        _short_model(DEFAULT_IMAGE_RESPONSES_MODEL_FAST),
        DEFAULT_IMAGE_RESPONSES_MODEL,
        DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    }:
        return "Codex 原生"
    if normalized in {_short_model(UPSTREAM_MODEL), UPSTREAM_MODEL}:
        return "image2 直连"
    return normalized or "未记录"


def _generation_endpoint(gen: Generation) -> str:
    req = gen.upstream_request if isinstance(gen.upstream_request, dict) else {}
    route = _request_route(req) or "responses"
    actual_endpoint = _json_str(req, "actual_endpoint")
    if actual_endpoint:
        return actual_endpoint
    if route == "image2":
        return "images/edits" if gen.action == "edit" else "images/generations"
    if route == "image_jobs":
        return "image-jobs:generations" if gen.action == "generate" else "responses:image_generation"
    if route == "dual_race":
        return "dual_race"
    if route == "dual_race_bonus":
        return "dual_race_bonus"
    return "responses:image_generation"


def _admin_image_binary_url(image_id: str) -> str:
    return f"/api/admin/images/{image_id}/binary"


def _admin_image_variant_url(image_id: str, kind: str) -> str:
    return f"/api/admin/images/{image_id}/variants/{kind}"


def _safe_upstream_details(upstream_request: dict[str, Any] | None) -> dict[str, Any]:
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
    details: dict[str, Any] = {}
    for key in sorted(allowed):
        if key in upstream_request:
            details[key] = upstream_request[key]
    context = upstream_request.get("context")
    if isinstance(context, dict):
        details["context"] = {
            key: context.get(key)
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


class _RequestEventImageOut(BaseModel):
    id: str
    roles: list[Literal["input", "output"]]
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
    # In-flight 实时 provider 快照（只在 status in {queued,running,streaming} 时填）。
    # live_provider 是给列表那一行展示的人类可读摘要（dual_race 形如 "A vs B"）。
    # live_lanes 是结构化数据，详情面板/调试用。
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


async def _read_context_circuit(redis: Any, now: datetime) -> tuple[str, str | None]:
    raw_state = await redis.get(_CONTEXT_CIRCUIT_STATE_KEY)
    state_text = (_redis_text(raw_state) or "closed").strip()
    until: str | None = None
    if state_text.startswith("{"):
        try:
            parsed = json.loads(state_text)
            if isinstance(parsed, dict):
                state_text = str(parsed.get("state") or "closed")
                until = _redis_text(parsed.get("until"))
        except Exception:
            state_text = "closed"
    if state_text not in {"closed", "open", "half_open"}:
        state_text = "closed"

    if until is None:
        raw_until = await redis.get(_CONTEXT_CIRCUIT_UNTIL_KEY)
        until = _redis_text(raw_until)
    if until is None and state_text == "open":
        try:
            ttl_ms = await redis.pttl(_CONTEXT_CIRCUIT_STATE_KEY)
        except Exception:
            ttl_ms = -1
        if ttl_ms and ttl_ms > 0:
            until = _iso_z(now + timedelta(milliseconds=ttl_ms))
    if state_text != "open":
        until = None
    return state_text, until


@router.get("/context/health")
async def context_health(_admin: AdminUser) -> dict:
    out = _context_health_zero()
    redis = get_redis()
    now = datetime.now(timezone.utc)
    try:
        state, until = await _read_context_circuit(redis, now)
        metric_rows = []
        for key in _hourly_context_metric_keys(now):
            metric_rows.append(
                await cast(
                    Awaitable[dict[str, str]],
                    redis.hgetall(key),
                )
            )
        out["circuit_breaker_state"] = state
        out["circuit_breaker_until"] = until
        out["last_24h"] = _fold_context_metrics(metric_rows)
        return out
    except Exception:
        logger.warning("context health degraded", exc_info=True)
        return _context_health_zero(
            degraded=True,
            degrade_reason="redis_unavailable",
        )


# ---------- AllowedEmails ----------

class _AllowedEmailIn(BaseModel):
    email: EmailStr


@router.get("/allowed_emails")
async def list_allowed_emails(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    Inviter = aliased(User)
    rows = (
        await db.execute(
            select(AllowedEmail, Inviter.email)
            .join(
                Inviter,
                and_(
                    Inviter.id == AllowedEmail.invited_by,
                    Inviter.deleted_at.is_(None),
                ),
                isouter=True,
            )
            .order_by(AllowedEmail.created_at.desc())
        )
    ).all()
    items = [
        AllowedEmailOut(
            id=ae.id,
            email=ae.email,
            invited_by_email=inviter_email,
            created_at=ae.created_at,
        )
        for ae, inviter_email in rows
    ]
    return {"items": items}


@router.post(
    "/allowed_emails",
    response_model=AllowedEmailOut,
    status_code=201,
    dependencies=[Depends(verify_csrf)],
)
async def add_allowed_email(
    body: _AllowedEmailIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AllowedEmailOut:
    email = str(body.email).lower().strip()
    exists = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.email == email))
    ).scalar_one_or_none()
    if exists:
        raise _http("already_exists", "email already allowed", 409)

    ae = AllowedEmail(email=email, invited_by=admin.id)
    db.add(ae)
    await db.flush()
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.allowed_email.add",
        details={"email_hash": hash_email(email), "id": ae.id},
    )
    await db.commit()
    await db.refresh(ae)
    return AllowedEmailOut(
        id=ae.id,
        email=ae.email,
        invited_by_email=admin.email,
        created_at=ae.created_at,
    )


@router.delete(
    "/allowed_emails/{ae_id}",
    status_code=204,
    dependencies=[Depends(verify_csrf)],
)
async def delete_allowed_email(
    ae_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    ae = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.id == ae_id))
    ).scalar_one_or_none()
    if not ae:
        raise _http("not_found", "allowed email not found", 404)
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.allowed_email.delete",
        details={"email_hash": hash_email(ae.email), "id": ae.id},
    )
    await db.delete(ae)
    await db.commit()
    return None


# ---------- Users ----------

def _encode_cursor(created_at: datetime, user_id: str) -> str:
    raw = f"{ensure_utc(created_at).isoformat()}|{user_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise _http("invalid_cursor", "invalid cursor", 400) from exc
    if "|" not in raw:
        raise _http("invalid_cursor", "invalid cursor", 400)
    ts, uid = raw.split("|", 1)
    if not ts or not uid:
        raise _http("invalid_cursor", "invalid cursor", 400)
    try:
        created_at = ensure_utc(
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
        )
    except ValueError as exc:
        raise _http("invalid_cursor", "invalid cursor", 400) from exc
    return created_at, uid


class _AdminSetUserPasswordIn(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class _AdminUserHistoryImageOut(BaseModel):
    id: str
    url: str
    display_url: str
    preview_url: str | None = None
    thumb_url: str | None = None
    width: int
    height: int
    mime: str


class _AdminUserHistoryItemOut(BaseModel):
    id: str
    kind: Literal["generation"]
    created_at: datetime
    status: str
    prompt: str | None = None
    conversation_id: str | None = None
    conversation_title: str | None = None
    message_id: str | None = None
    retention_state: Literal["active", "hidden", "deleted"] = "active"
    images: list[_AdminUserHistoryImageOut] = Field(default_factory=list)


class _AdminUserHistoryOut(BaseModel):
    user: AdminUserOut
    items: list[_AdminUserHistoryItemOut]


def _admin_history_image_out(
    img: Image,
    variant_kinds: set[str],
) -> _AdminUserHistoryImageOut:
    return _AdminUserHistoryImageOut(
        id=img.id,
        url=_admin_image_binary_url(img.id),
        display_url=_admin_image_variant_url(img.id, DISPLAY_VARIANT),
        preview_url=(
            _admin_image_variant_url(img.id, "preview1024")
            if "preview1024" in variant_kinds
            else None
        ),
        thumb_url=(
            _admin_image_variant_url(img.id, "thumb256")
            if "thumb256" in variant_kinds
            else None
        ),
        width=img.width,
        height=img.height,
        mime=img.mime,
    )


@router.get("/users")
async def list_users(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> dict:
    # scalar subqueries for per-user counts
    gen_count = (
        select(func.count(Generation.id))
        .where(Generation.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    comp_count = (
        select(func.count(Completion.id))
        .where(Completion.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    # messages owned by user = messages in user's conversations with role='user'
    msg_count = (
        select(func.count(Message.id))
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )

    stmt = select(
        User.id,
        User.email,
        User.role,
        User.account_mode,
        User.display_name,
        User.created_at,
        gen_count.label("generations_count"),
        comp_count.label("completions_count"),
        msg_count.label("messages_count"),
    ).where(User.deleted_at.is_(None)).order_by(User.created_at.desc(), User.id.desc())

    if cursor:
        ts, uid = _decode_cursor(cursor)
        # keyset pagination (created_at, id) desc
        stmt = stmt.where(
            (User.created_at < ts)
            | ((User.created_at == ts) & (User.id < uid))
        )

    stmt = stmt.limit(limit + 1)
    rows = (await db.execute(stmt)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        AdminUserOut(
            id=r.id,
            email=r.email,
            role=r.role,
            account_mode=r.account_mode,
            display_name=r.display_name or None,
            created_at=r.created_at,
            generations_count=int(r.generations_count or 0),
            completions_count=int(r.completions_count or 0),
            messages_count=int(r.messages_count or 0),
        )
        for r in rows
    ]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)
    return {"items": items, "next_cursor": next_cursor}


async def _admin_user_out(db: AsyncSession, user_id: str) -> AdminUserOut:
    gen_count = (
        select(func.count(Generation.id))
        .where(Generation.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    comp_count = (
        select(func.count(Completion.id))
        .where(Completion.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    msg_count = (
        select(func.count(Message.id))
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    row = (
        await db.execute(
            select(
                User.id,
                User.email,
                User.role,
                User.account_mode,
                User.display_name,
                User.created_at,
                gen_count.label("generations_count"),
                comp_count.label("completions_count"),
                msg_count.label("messages_count"),
            ).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise _http("not_found", "user not found", 404)
    return AdminUserOut(
        id=row.id,
        email=row.email,
        role=row.role,
        account_mode=row.account_mode,
        display_name=row.display_name or None,
        created_at=row.created_at,
        generations_count=int(row.generations_count or 0),
        completions_count=int(row.completions_count or 0),
        messages_count=int(row.messages_count or 0),
    )


@router.get("/users/{user_id}/history", response_model=_AdminUserHistoryOut)
async def get_user_history(
    user_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
) -> _AdminUserHistoryOut:
    user_out = await _admin_user_out(db, user_id)
    rows = (
        await db.execute(
            select(
                Generation,
                Conversation.id.label("conversation_id"),
                Conversation.title.label("conversation_title"),
            )
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Generation.user_id == user_id,
                Message.deleted_at.is_(None),
                Conversation.deleted_at.is_(None),
            )
            .order_by(desc(Generation.created_at), desc(Generation.id))
            .limit(limit)
        )
    ).all()
    generations = [row[0] for row in rows]
    gen_ids = [gen.id for gen in generations]
    images_by_gen: dict[str, list[Image]] = {}
    variant_map: dict[str, set[str]] = {}
    if gen_ids:
        images = list(
            (
                await db.execute(
                    select(Image)
                    .where(
                        Image.owner_generation_id.in_(gen_ids),
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            ).scalars()
        )
        for img in images:
            if img.owner_generation_id:
                images_by_gen.setdefault(img.owner_generation_id, []).append(img)
        if images:
            variant_rows = (
                await db.execute(
                    select(ImageVariant.image_id, ImageVariant.kind).where(
                        ImageVariant.image_id.in_([img.id for img in images])
                    )
                )
            ).all()
            for image_id, kind in variant_rows:
                variant_map.setdefault(image_id, set()).add(kind)

    policy = retention_policy_from_settings(await read_byok_settings_cached(db))
    items: list[_AdminUserHistoryItemOut] = []
    for gen, conversation_id, conversation_title in rows:
        item_images = [
            _admin_history_image_out(img, variant_map.get(img.id, set()))
            for img in images_by_gen.get(gen.id, [])
        ]
        items.append(
            _AdminUserHistoryItemOut(
                id=gen.id,
                kind="generation",
                created_at=gen.created_at,
                status=gen.status,
                prompt=gen.prompt,
                conversation_id=conversation_id,
                conversation_title=conversation_title or None,
                message_id=gen.message_id,
                retention_state=byok_retention_state(
                    account_mode=user_out.account_mode,
                    created_at=gen.created_at,
                    policy=policy,
                ),
                images=item_images,
            )
        )
    return _AdminUserHistoryOut(user=user_out, items=items)


@router.patch(
    "/users/{user_id}/password",
    dependencies=[Depends(verify_csrf)],
)
async def set_user_password(
    user_id: str,
    body: _AdminSetUserPasswordIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    target = (
        await db.execute(
            select(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise _http("not_found", "user not found", 404)
    target.password_hash = hash_password(body.password)
    now = datetime.now(timezone.utc)
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == target.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.user.password_set",
        target_user_id=target.id,
        details={"target_email_hash": hash_email(target.email)},
        autocommit=False,
    )
    await db.commit()
    return {"ok": True}


@router.delete(
    "/users/{user_id}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_user(
    user_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    if user_id == admin.id:
        raise _http("cannot_delete_self", "admin cannot delete own account", 400)
    target = (
        await db.execute(
            select(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise _http("not_found", "user not found", 404)

    now = datetime.now(timezone.utc)
    target.deleted_at = now
    sessions_result = await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == target.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    conversations_result = await db.execute(
        update(Conversation)
        .where(Conversation.user_id == target.id, Conversation.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    images_result = await db.execute(
        update(Image)
        .where(Image.user_id == target.id, Image.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    task_cleanup = await _cancel_account_active_tasks(
        db,
        user_id=target.id,
        canceled_at=now,
        account_mode=getattr(target, "account_mode", "wallet"),
    )
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.user.delete",
        target_user_id=target.id,
        details={
            "target_email_hash": hash_email(target.email),
            "sessions_revoked": affected_rows(sessions_result),
            "conversations_deleted": affected_rows(conversations_result),
            "images_deleted": affected_rows(images_result),
            "generations_canceled": task_cleanup["generations_canceled"],
            "completions_canceled": task_cleanup["completions_canceled"],
        },
        autocommit=False,
    )
    await db.commit()
    await _post_commit_account_task_cleanup(user_id=target.id, cleanup=task_cleanup)
    return {"ok": True}


# ---------- Request events ----------

_REQUEST_EVENT_STATUSES = {
    "queued",
    "running",
    "streaming",
    "succeeded",
    "failed",
    "canceled",
}
_REQUEST_EVENT_RANGE_HOURS = {
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
}


def _normalize_request_event_status(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = status.strip().lower()
    if not normalized or normalized == "all":
        return None
    if normalized not in _REQUEST_EVENT_STATUSES:
        raise _http("invalid_status", "unsupported request event status", 400)
    return normalized


def _request_event_since(
    range: Literal["24h", "7d", "30d"],
    now: datetime,
) -> datetime:
    return now - timedelta(hours=_REQUEST_EVENT_RANGE_HOURS[range])


def _request_event_sort_key(row: dict[str, Any]) -> tuple[bool, datetime, str]:
    task = row["task"]
    finished_at = getattr(task, "finished_at", None)
    sort_at = finished_at or getattr(task, "created_at", None)
    if sort_at is None:
        sort_at = datetime.min.replace(tzinfo=timezone.utc)
    # 进行中（finished_at IS NULL）必须排在已完成之前；reverse=True 时 True 优先。
    # Why: 不然忙碌窗口下两侧 SQL 各自 LIMIT 把已完成行填满，in-flight 行被挤出列表，
    # 监控页 stat tile "进行中" 数量大于 0 但表里看不到任何对应行。
    return (
        finished_at is None,
        ensure_utc(sort_at),
        str(getattr(task, "id", "")),
    )


def _request_event_time_filter(model: Any, since: datetime) -> Any:
    return or_(
        model.finished_at >= since,
        and_(model.finished_at.is_(None), model.created_at >= since),
    )


def _event_image_out(
    img: Image,
    roles: set[Literal["input", "output"]],
    variant_kinds: set[str],
) -> _RequestEventImageOut:
    return _RequestEventImageOut(
        id=img.id,
        roles=sorted(roles, key=lambda role: 0 if role == "output" else 1),
        source=img.source,
        url=_admin_image_binary_url(img.id),
        display_url=_admin_image_variant_url(img.id, DISPLAY_VARIANT),
        preview_url=(
            _admin_image_variant_url(img.id, "preview1024")
            if "preview1024" in variant_kinds
            else None
        ),
        thumb_url=(
            _admin_image_variant_url(img.id, "thumb256")
            if "thumb256" in variant_kinds
            else None
        ),
        width=img.width,
        height=img.height,
        mime=img.mime,
        parent_image_id=img.parent_image_id,
        owner_generation_id=img.owner_generation_id,
    )


def _message_output_image_refs(content: Any) -> list[tuple[str, str | None]]:
    if not isinstance(content, dict):
        return []
    images = content.get("images")
    if not isinstance(images, list):
        return []
    refs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for item in images:
        image_id: Any = None
        from_generation_id: Any = None
        if isinstance(item, dict):
            image_id = item.get("image_id") or item.get("id")
            from_generation_id = (
                item.get("from_generation_id")
                or item.get("generation_id")
                or item.get("owner_generation_id")
            )
        elif isinstance(item, str):
            image_id = item
        if not isinstance(image_id, str) or not image_id:
            continue
        if image_id in seen:
            continue
        seen.add(image_id)
        refs.append(
            (
                image_id,
                from_generation_id if isinstance(from_generation_id, str) else None,
            )
        )
    return refs


def _request_event_model_stats_from_counts(
    counts: dict[str, int],
) -> list[_RequestEventModelStatOut]:
    normalized_counts = {
        model: count
        for model, count in counts.items()
        if model and count > 0
    }

    total = sum(normalized_counts.values())
    if total <= 0:
        return []

    return [
        _RequestEventModelStatOut(
            model=model,
            count=count,
            share=count / total,
        )
        for model, count in sorted(
            normalized_counts.items(),
            key=lambda entry: (-entry[1], entry[0]),
        )
    ]


async def _request_event_model_stats_for_filters(
    db: AsyncSession,
    *,
    since: datetime,
    kind: Literal["all", "generation", "completion"],
    status: str | None,
) -> list[_RequestEventModelStatOut]:
    counts: dict[str, int] = {}

    if kind in {"all", "generation"}:
        gen_stats_stmt = (
            select(Generation.upstream_request, Generation.action, Generation.status)
            .join(User, User.id == Generation.user_id)
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                User.deleted_at.is_(None),
                _request_event_time_filter(Generation, since),
            )
        )
        if status:
            gen_stats_stmt = gen_stats_stmt.where(Generation.status == status)
        gen_stats_rows = (await db.execute(gen_stats_stmt)).all()
        for upstream_request, action, gen_status in gen_stats_rows:
            label = _generation_model_label_from_request(
                upstream_request if isinstance(upstream_request, dict) else {},
                action=str(action),
                status=str(gen_status),
            )
            stat_label = _request_event_model_stat_label(label)
            counts[stat_label] = counts.get(stat_label, 0) + 1

    if kind in {"all", "completion"}:
        comp_stats_stmt = (
            select(Completion.model)
            .join(User, User.id == Completion.user_id)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                User.deleted_at.is_(None),
                _request_event_time_filter(Completion, since),
            )
        )
        if status:
            comp_stats_stmt = comp_stats_stmt.where(Completion.status == status)
        for (model,) in (await db.execute(comp_stats_stmt)).all():
            stat_label = _request_event_model_stat_label(str(model or ""))
            counts[stat_label] = counts.get(stat_label, 0) + 1

    return _request_event_model_stats_from_counts(counts)


def _request_event_prompt(user_content: Any) -> str | None:
    if not isinstance(user_content, dict):
        return None
    text = user_content.get("text")
    return text if isinstance(text, str) else None


@router.get("/request_events", response_model=_RequestEventsOut)
async def list_request_events(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=200),
    kind: Literal["all", "generation", "completion"] = Query(default="all"),
    status: str | None = Query(default=None, max_length=32),
    range: Literal["24h", "7d", "30d"] = Query(default="24h"),
) -> _RequestEventsOut:
    UserMsg = aliased(Message)
    now = datetime.now(timezone.utc)
    since = _request_event_since(range, now)
    status = _normalize_request_event_status(status)
    model_stats = await _request_event_model_stats_for_filters(
        db,
        since=since,
        kind=kind,
        status=status,
    )
    event_rows: list[dict[str, Any]] = []

    if kind in {"all", "generation"}:
        gen_stmt = (
            select(
                Generation,
                User.email.label("user_email"),
                Conversation.id.label("conversation_id"),
                Conversation.title.label("conversation_title"),
                Message.intent.label("assistant_intent"),
            )
            .join(User, User.id == Generation.user_id)
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                User.deleted_at.is_(None),
                _request_event_time_filter(Generation, since),
            )
            .order_by(
                # nulls_first：把 in-flight (NULL finished_at) 行排在每侧 LIMIT 的最前，
                # 否则忙碌窗口下进行中的任务会被已完成行挤出 SQL 结果，监控页彻底看不到。
                desc(Generation.finished_at).nulls_first(),
                desc(Generation.created_at),
                desc(Generation.id),
            )
            .limit(limit)
        )
        if status:
            gen_stmt = gen_stmt.where(Generation.status == status)
        for gen, user_email, conversation_id, conversation_title, assistant_intent in (
            await db.execute(gen_stmt)
        ).all():
            event_rows.append(
                {
                    "kind": "generation",
                    "task": gen,
                    "user_email": user_email,
                    "conversation_id": conversation_id,
                    "conversation_title": conversation_title,
                    "assistant_intent": assistant_intent,
                }
            )

    if kind in {"all", "completion"}:
        comp_stmt = (
            select(
                Completion,
                User.email.label("user_email"),
                Conversation.id.label("conversation_id"),
                Conversation.title.label("conversation_title"),
                Message.intent.label("assistant_intent"),
                UserMsg.content.label("user_content"),
            )
            .join(User, User.id == Completion.user_id)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(UserMsg, UserMsg.id == Message.parent_message_id, isouter=True)
            .where(
                User.deleted_at.is_(None),
                _request_event_time_filter(Completion, since),
            )
            .order_by(
                # nulls_first：见 generation 侧注释，理由相同——保证 in-flight 优先入选。
                desc(Completion.finished_at).nulls_first(),
                desc(Completion.created_at),
                desc(Completion.id),
            )
            .limit(limit)
        )
        if status:
            comp_stmt = comp_stmt.where(Completion.status == status)
        for (
            comp,
            user_email,
            conversation_id,
            conversation_title,
            assistant_intent,
            user_content,
        ) in (await db.execute(comp_stmt)).all():
            event_rows.append(
                {
                    "kind": "completion",
                    "task": comp,
                    "user_email": user_email,
                    "conversation_id": conversation_id,
                    "conversation_title": conversation_title,
                    "assistant_intent": assistant_intent,
                    "prompt": _request_event_prompt(user_content),
                }
            )

    event_rows.sort(key=_request_event_sort_key, reverse=True)
    event_rows = event_rows[:limit]

    gen_ids = [
        row["task"].id
        for row in event_rows
        if row["kind"] == "generation"
    ]
    image_roles_by_event: dict[str, dict[str, set[Literal["input", "output"]]]] = {}
    image_ids: set[str] = set()
    generation_ids_by_message: dict[str, list[str]] = {}

    for row in event_rows:
        task = row["task"]
        if row["kind"] == "generation":
            generation_ids_by_message.setdefault(task.message_id, []).append(task.id)
        roles = image_roles_by_event.setdefault(task.id, {})
        for image_id in list(getattr(task, "input_image_ids", None) or []):
            roles.setdefault(image_id, set()).add("input")
            image_ids.add(image_id)

    message_output_refs: dict[str, list[tuple[str, str | None]]] = {}
    message_ids = {
        row["task"].message_id
        for row in event_rows
        if isinstance(getattr(row["task"], "message_id", None), str)
    }
    if message_ids:
        message_rows = (
            await db.execute(
                select(Message.id, Message.content).where(Message.id.in_(message_ids))
            )
        ).all()
        message_output_refs = {
            message_id: _message_output_image_refs(content)
            for message_id, content in message_rows
        }

    for row in event_rows:
        task = row["task"]
        roles = image_roles_by_event.setdefault(task.id, {})
        refs = message_output_refs.get(task.message_id, [])
        for image_id, from_generation_id in refs:
            if row["kind"] == "generation":
                if from_generation_id:
                    if from_generation_id != task.id:
                        continue
                elif len(generation_ids_by_message.get(task.message_id, [])) > 1:
                    continue
            roles.setdefault(image_id, set()).add("output")
            image_ids.add(image_id)

    output_image_rows: list[Image] = []
    if gen_ids:
        output_image_rows = list(
            (
                await db.execute(
                    select(Image)
                    .where(
                        Image.owner_generation_id.in_(gen_ids),
                        Image.deleted_at.is_(None),
                    )
                    .order_by(desc(Image.created_at), desc(Image.id))
                )
            ).scalars()
        )
        for img in output_image_rows:
            if img.owner_generation_id:
                image_roles_by_event.setdefault(img.owner_generation_id, {}).setdefault(
                    img.id, set()
                ).add("output")
                image_ids.add(img.id)

    input_image_ids = {
        image_id
        for roles in image_roles_by_event.values()
        for image_id in roles
    }
    all_images: list[Image] = []
    if input_image_ids:
        all_images = list(
            (
                await db.execute(
                    select(Image).where(
                        Image.id.in_(input_image_ids),
                        Image.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
    image_by_id = {img.id: img for img in all_images}

    # In-flight provider 实时快照：只查"还在跑"的 generation 行，避免把所有终态行都
    # 喂给 Redis pipeline。Redis 不可用时 _fetch_image_inflight 返回空 dict，列表本身
    # 仍然可用（live_provider 为 null，前端回落到既有"等待上游结果"语义）。
    inflight_task_ids = [
        row["task"].id
        for row in event_rows
        if row["kind"] == "generation"
        and _is_inflight_status(getattr(row["task"], "status", None))
    ]
    inflight_snapshots: dict[str, dict[str, str]] = {}
    if inflight_task_ids:
        try:
            inflight_redis = get_redis()
        except Exception:
            inflight_redis = None
        if inflight_redis is not None:
            inflight_snapshots = await _fetch_image_inflight(
                inflight_redis, inflight_task_ids
            )

    variant_map: dict[str, set[str]] = {}
    if image_ids:
        variant_rows = (
            await db.execute(
                select(ImageVariant.image_id, ImageVariant.kind).where(
                    ImageVariant.image_id.in_(image_ids)
                )
            )
        ).all()
        for image_id, variant_kind in variant_rows:
            variant_map.setdefault(image_id, set()).add(variant_kind)

    items: list[_RequestEventOut] = []
    for row in event_rows:
        task = row["task"]
        req = task.upstream_request if isinstance(task.upstream_request, dict) else {}
        event_images: list[_RequestEventImageOut] = []
        roles_for_event = image_roles_by_event.get(task.id, {})
        def image_created_at(image_id: str) -> datetime:
            image = image_by_id.get(image_id)
            return image.created_at if image is not None else now

        ordered_image_ids = sorted(
            roles_for_event,
            key=lambda image_id: (
                0 if "output" in roles_for_event[image_id] else 1,
                image_created_at(image_id),
            ),
            reverse=False,
        )
        for image_id in ordered_image_ids:
            event_image = image_by_id.get(image_id)
            if event_image is None:
                continue
            event_images.append(
                _event_image_out(
                    event_image,
                    roles_for_event[image_id],
                    variant_map.get(image_id, set()),
                )
            )

        if row["kind"] == "generation":
            model_label = _generation_model_label(task)
            upstream_endpoint = _generation_endpoint(task)
            prompt = task.prompt
            action = task.action
            tokens_in = None
            tokens_out = None
        else:
            model_label = task.model
            upstream_endpoint = (
                _json_str(req, "actual_endpoint", "endpoint") or "responses"
            )
            prompt = row.get("prompt")
            action = None
            tokens_in = task.tokens_in
            tokens_out = task.tokens_out

        live_provider: str | None = None
        live_lanes: list[_RequestEventLiveLane] = []
        if (
            row["kind"] == "generation"
            and _is_inflight_status(task.status)
            and task.id in inflight_snapshots
        ):
            live_provider, live_lanes = _build_live_lanes_from_snapshot(
                inflight_snapshots[task.id]
            )

        items.append(
            _RequestEventOut(
                id=task.id,
                kind=row["kind"],
                created_at=task.created_at,
                started_at=task.started_at,
                finished_at=task.finished_at,
                duration_ms=_duration_ms(task.started_at, task.finished_at, now=now),
                status=task.status,
                progress_stage=task.progress_stage,
                attempt=task.attempt,
                model=model_label,
                user_id=task.user_id,
                user_email=row["user_email"],
                conversation_id=row["conversation_id"],
                conversation_title=row["conversation_title"] or None,
                message_id=task.message_id,
                prompt=prompt,
                action=action,
                intent=row.get("assistant_intent"),
                upstream_provider=_request_provider(req),
                upstream_route=_request_route(req),
                upstream_endpoint=upstream_endpoint,
                queue_lane=getattr(task, "queue_lane", None),
                workflow_type=getattr(task, "workflow_type", None),
                workflow_step_key=getattr(task, "workflow_step_key", None),
                pixel_count=getattr(task, "pixel_count", None),
                size_bucket=getattr(task, "size_bucket", None),
                cost_class=getattr(task, "cost_class", None),
                queue_wait_ms=getattr(task, "queue_wait_ms", None),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                error_code=task.error_code,
                error_message=task.error_message,
                images=event_images,
                upstream=_safe_upstream_details(req),
                live_provider=live_provider,
                live_lanes=live_lanes,
            )
        )

    return _RequestEventsOut(
        items=items,
        total=len(items),
        model_stats=model_stats,
    )


@router.get("/images/{image_id}/binary")
async def get_admin_image_binary(
    image_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    img = (
        await db.execute(
            select(Image).where(Image.id == image_id, Image.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    return _storage_streaming_response(
        _fs_path(img.storage_key),
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=31536000, immutable",
    )


@router.get("/images/{image_id}/variants/{kind}")
async def get_admin_image_variant(
    image_id: str,
    kind: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if kind not in ALLOWED_VARIANTS:
        raise _http("invalid_variant", "unsupported image variant", 400)
    img = (
        await db.execute(
            select(Image).where(Image.id == image_id, Image.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    variant = (
        await db.execute(
            select(ImageVariant).where(
                ImageVariant.image_id == img.id,
                ImageVariant.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if variant is None:
        if kind != DISPLAY_VARIANT:
            raise _http("not_found", "variant not found", 404)
        variant = await _ensure_display_variant(db, img)
        await db.commit()
    return _storage_streaming_response(
        _fs_path(variant.storage_key),
        media_type=VARIANT_MEDIA_TYPE.get(kind, "application/octet-stream"),
        etag=f'"{variant.image_id}-{variant.kind}"',
        cache_control="private, max-age=31536000, immutable",
    )


# ---------- DLQ (Outbox dead-letter management) ----------

class _DlqItemOut(BaseModel):
    id: str
    outbox_id: str | None
    event_type: str
    payload: dict[str, Any]
    error_class: str | None
    error_message: str | None
    retry_count: int
    failed_at: datetime
    resolved_at: datetime | None


DlqTaskKind = Literal[
    "generation",
    "completion",
    "video_generation",
    "storyboard_assembly",
]

DlqKind = DlqTaskKind | Literal["sse"]

_DLQ_KIND_BY_EVENT_TYPE: dict[str, DlqKind] = {
    "outbox.generation": "generation",
    "outbox.completion": "completion",
    "outbox.video_generation": "video_generation",
    "outbox.storyboard_assembly": "storyboard_assembly",
    "outbox.sse": "sse",
}


async def _dlq_task_exists(
    db: AsyncSession,
    *,
    kind: DlqTaskKind,
    task_id: str,
) -> bool:
    if kind == "generation":
        stmt = select(Generation.id).join(User, User.id == Generation.user_id)
    elif kind == "completion":
        stmt = select(Completion.id).join(User, User.id == Completion.user_id)
    elif kind == "video_generation":
        stmt = select(VideoGeneration.id).join(
            User,
            User.id == VideoGeneration.user_id,
        )
    else:
        stmt = (
            select(WorkflowRun.id)
            .join(User, User.id == WorkflowRun.user_id)
            .where(WorkflowRun.type == "storyboard")
        )
    exists = (
        await db.execute(
            stmt.where(
                stmt.selected_columns[0] == task_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return exists is not None


async def _soft_deleted_dlq_task_ids(
    db: AsyncSession,
    *,
    kind: DlqTaskKind,
    task_ids: set[str],
) -> set[str]:
    if not task_ids:
        return set()
    if kind == "generation":
        stmt = (
            select(Generation.id)
            .join(User, User.id == Generation.user_id)
            .where(
                Generation.id.in_(task_ids),
                User.deleted_at.is_not(None),
            )
        )
    elif kind == "completion":
        stmt = (
            select(Completion.id)
            .join(User, User.id == Completion.user_id)
            .where(
                Completion.id.in_(task_ids),
                User.deleted_at.is_not(None),
            )
        )
    elif kind == "video_generation":
        stmt = (
            select(VideoGeneration.id)
            .join(User, User.id == VideoGeneration.user_id)
            .where(
                VideoGeneration.id.in_(task_ids),
                User.deleted_at.is_not(None),
            )
        )
    else:
        stmt = (
            select(WorkflowRun.id)
            .join(User, User.id == WorkflowRun.user_id)
            .where(
                WorkflowRun.id.in_(task_ids),
                WorkflowRun.type == "storyboard",
                User.deleted_at.is_not(None),
            )
        )
    return set((await db.execute(stmt)).scalars())


async def _soft_deleted_dlq_row_ids(
    db: AsyncSession,
    rows: list[OutboxDeadLetter],
) -> set[str]:
    task_rows_by_kind: dict[DlqTaskKind, dict[str, set[str]]] = {}
    sse_rows_by_user: dict[str, set[str]] = {}

    for row in rows:
        kind = _DLQ_KIND_BY_EVENT_TYPE.get(row.event_type)
        if kind is None:
            continue
        payload = dict(row.payload or {})
        if kind == "sse":
            user_id = payload.get("user_id")
            if isinstance(user_id, str) and user_id:
                sse_rows_by_user.setdefault(user_id, set()).add(row.id)
            continue
        task_id = payload.get("task_id") or payload.get("id")
        if isinstance(task_id, str) and task_id:
            task_rows_by_kind.setdefault(kind, {}).setdefault(task_id, set()).add(
                row.id
            )

    row_ids: set[str] = set()
    for kind, rows_by_task in task_rows_by_kind.items():
        deleted_task_ids = await _soft_deleted_dlq_task_ids(
            db,
            kind=kind,
            task_ids=set(rows_by_task),
        )
        for task_id in deleted_task_ids:
            row_ids.update(rows_by_task[task_id])

    if sse_rows_by_user:
        deleted_user_ids = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(sse_rows_by_user),
                        User.deleted_at.is_not(None),
                    )
                )
            ).scalars()
        )
        for user_id in deleted_user_ids:
            row_ids.update(sse_rows_by_user[user_id])

    return row_ids


@router.get("/dlq")
async def list_dlq(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    include_resolved: bool = Query(default=False),
) -> dict:
    stmt = select(OutboxDeadLetter)
    if not include_resolved:
        stmt = stmt.where(OutboxDeadLetter.resolved_at.is_(None))
    stmt = stmt.order_by(desc(OutboxDeadLetter.failed_at)).limit(limit)
    rows = list((await db.execute(stmt)).scalars())
    items = [
        _DlqItemOut(
            id=r.id,
            outbox_id=r.outbox_id,
            event_type=r.event_type,
            payload=dict(r.payload or {}),
            error_class=r.error_class,
            error_message=r.error_message,
            retry_count=r.retry_count,
            failed_at=r.failed_at,
            resolved_at=r.resolved_at,
        )
        for r in rows
    ]
    return {"items": items, "total": len(items)}


@router.post("/dlq/{dlq_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_dlq(
    dlq_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = (
        await db.execute(
            select(OutboxDeadLetter)
            .where(OutboxDeadLetter.id == dlq_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not row:
        raise _http("not_found", "dlq item not found", 404)
    if row.resolved_at is not None:
        raise _http("already_resolved", "dlq item already resolved", 409)

    kind = _DLQ_KIND_BY_EVENT_TYPE.get(row.event_type)
    if kind is None:
        raise _http(
            "unsupported_event_type",
            f"DLQ retry does not support {row.event_type}",
            422,
        )
    if row.error_class not in {"OutboxEnqueueFailed", "OutboxPublishFailed"}:
        raise _http(
            "unrepairable_dlq_payload",
            "malformed or invalid outbox payload must be repaired before retry",
            422,
        )

    payload = dict(row.payload or {})
    task_id = payload.get("task_id") or payload.get("id")
    if kind == "sse":
        user_id = payload.get("user_id")
        if (
            not isinstance(user_id, str)
            or not user_id
            or not isinstance(payload.get("channel"), str)
            or not payload.get("channel")
            or not isinstance(payload.get("event_name"), str)
            or not payload.get("event_name")
            or not isinstance(payload.get("data"), dict)
        ):
            raise _http("invalid_payload", "DLQ SSE payload is invalid", 400)
        exists = (
            await db.execute(
                select(User.id).where(
                    User.id == user_id,
                    User.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    else:
        if not isinstance(task_id, str) or not task_id:
            raise _http("invalid_task_id", "dlq payload task_id is invalid", 400)
        exists = task_id if await _dlq_task_exists(
            db,
            kind=kind,
            task_id=task_id,
        ) else None
    if exists is None:
        logger.info(
            "dlq retry skipped: task_or_user_missing dlq_id=%s task_id=%s "
            "event_type=%s",
            dlq_id,
            task_id,
            row.event_type,
        )
        raise _http(
            "task_not_found",
            "dlq payload references an unknown task or deleted user",
            404,
        )

    outbox = None
    if row.outbox_id:
        outbox = (
            await db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.id == row.outbox_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    if outbox is None:
        outbox = OutboxEvent(kind=kind, payload={}, published_at=None)
        db.add(outbox)
        await db.flush()
        row.outbox_id = outbox.id
    elif outbox.kind != kind:
        raise _http(
            "outbox_kind_mismatch",
            "DLQ event type does not match its outbox row",
            409,
        )

    payload["outbox_id"] = str(outbox.id)
    outbox.payload = payload
    outbox.published_at = None
    row.retry_count = (row.retry_count or 0) + 1
    row.error_message = "retry scheduled via durable outbox"
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.dlq.retry",
        details={
            "dlq_id": dlq_id,
            "event_type": row.event_type,
            "requeued": True,
            "task_id": task_id,
            "outbox_id": outbox.id,
        },
    )
    await db.commit()
    return {
        "ok": True,
        "dlq_id": dlq_id,
        "requeued": True,
        "resolved": False,
        "outbox_id": outbox.id,
    }


@router.post("/dlq/sweep-deleted-users", dependencies=[Depends(verify_csrf)])
async def sweep_dlq_for_deleted_users(
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict:
    """Mark DLQ rows whose owning user was soft-deleted as resolved.

    Why: ``retry_dlq`` joins ``User.deleted_at IS NULL`` for safety, which
    means dead letters owned by soft-deleted users can never be retried and
    silently accumulate. This sweeper closes them out as ``resolved`` (not
    physically deleted, so the audit/forensics trail is preserved) and
    writes an admin audit row capturing the sweep size.
    """
    swept_ids: list[str] = []
    scanned = 0
    now = datetime.now(timezone.utc)
    cursor: tuple[datetime, str] | None = None
    while True:
        stmt = select(OutboxDeadLetter).where(
            OutboxDeadLetter.resolved_at.is_(None),
            OutboxDeadLetter.event_type.in_(tuple(_DLQ_KIND_BY_EVENT_TYPE)),
        )
        if cursor is not None:
            failed_at, dlq_id = cursor
            stmt = stmt.where(
                or_(
                    OutboxDeadLetter.failed_at > failed_at,
                    and_(
                        OutboxDeadLetter.failed_at == failed_at,
                        OutboxDeadLetter.id > dlq_id,
                    ),
                )
            )
        rows = list(
            (
                await db.execute(
                    stmt.order_by(
                        OutboxDeadLetter.failed_at.asc(),
                        OutboxDeadLetter.id.asc(),
                    ).limit(limit)
                )
            ).scalars()
        )
        if not rows:
            break

        scanned += len(rows)
        deleted_owner_row_ids = await _soft_deleted_dlq_row_ids(db, rows)
        for row in rows:
            if row.id not in deleted_owner_row_ids:
                continue
            row.resolved_at = now
            row.error_message = (
                (row.error_message or "") + " | swept: owner soft-deleted"
            ).strip(" |")
            swept_ids.append(row.id)

        cursor = (rows[-1].failed_at, rows[-1].id)
        if len(rows) < limit:
            break

    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.dlq.sweep_deleted_users",
        details={"swept": len(swept_ids), "scanned": scanned},
    )
    await db.commit()
    logger.info(
        "dlq sweep deleted-users admin=%s swept=%d scanned=%d",
        admin.id,
        len(swept_ids),
        scanned,
    )
    return {"ok": True, "swept": len(swept_ids), "scanned": scanned}
