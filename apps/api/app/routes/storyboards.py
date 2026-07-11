"""Storyboard project routes.

This module keeps the redesigned storyboard workflow out of the monolithic
``/video`` page and out of the existing apparel/poster workflow routes.  The
state is still stored in WorkflowRun/WorkflowStep so no new table is required.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Iterable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    EV_GEN_QUEUED,
    GenerationStatus,
    Intent,
    MAX_PROMPT_CHARS,
    Role,
    VideoGenerationStatus,
    task_channel,
)
from lumen_core.models import (
    Conversation,
    Generation,
    Image,
    Message,
    OutboxEvent,
    User,
    Video,
    VideoGeneration,
    WorkflowRun,
    WorkflowStep,
    new_uuid7,
)
from lumen_core.schemas import ChatParamsIn, ImageParamsIn, VideoCreateIn, VideoOut

from ..arq_pool import get_arq_pool
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..redis_client import get_redis
from ..sse_publish import publish_sse_event
from .messages import (
    _create_assistant_task,
    _publish_message_appended,
)
from .videos import _create_video_generation_record, _video_out


router = APIRouter(prefix="/storyboards", tags=["storyboards"])
logger = logging.getLogger(__name__)

STORYBOARD_WORKFLOW_TYPE = "storyboard"
STORYBOARD_CHANNEL_PREFIX = "storyboard:"

STORYBOARD_ASSET_KINDS = {"character", "scene", "prop"}
STORYBOARD_DEFAULT_MODEL = "seedance-2.0"
STORYBOARD_DEFAULT_RESOLUTION = "720p"
STORYBOARD_DEFAULT_ASPECT_RATIO = "16:9"
STORYBOARD_DEFAULT_DURATION_S = 5
STORYBOARD_KEYFRAME_PARALLELISM = 4
STORYBOARD_ASSEMBLY_WAITING_LEASE_S = 5 * 60
STORYBOARD_ASSEMBLY_WORKER_LEASE_S = 2 * 60

_SHOT_STATUS_RANK = {
    "draft": 0,
    "approved": 1,
    "keyframe_generating": 2,
    "keyframe_ready": 3,
    "keyframe_approved": 4,
    "generating": 5,
    "done": 6,
}


def storyboard_channel(run_id: str) -> str:
    return f"{STORYBOARD_CHANNEL_PREFIX}{run_id}"


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": msg}
    if details:
        error["details"] = details
    return HTTPException(status_code=http, detail={"error": error})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _clean_text(value: str | None, *, max_len: int, default: str = "") -> str:
    text = (value or "").strip()
    if not text:
        return default
    return text[:max_len]


def _clean_string_list(
    values: Iterable[object] | None,
    *,
    max_len: int = 36,
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text[:max_len])
    return out


def _short_hash(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _asset_step_key(asset_id: str) -> str:
    return f"asset:{asset_id}"


def _shot_step_key(shot_id: str) -> str:
    return f"shot:{shot_id}"


def _step_kind(step: WorkflowStep) -> str | None:
    if step.step_key.startswith("asset:"):
        return "asset"
    if step.step_key.startswith("shot:"):
        return "shot"
    if step.step_key == "assembly":
        return "assembly"
    return None


def _image_url(image_id: str | None) -> str | None:
    return f"/api/images/{image_id}/binary" if image_id else None


def _image_display_url(image_id: str | None) -> str | None:
    return f"/api/images/{image_id}/variants/display2048" if image_id else None


def _video_url(video_id: str | None) -> str | None:
    return f"/api/videos/{video_id}/binary" if video_id else None


def _video_poster_url(
    video_id: str | None, poster_storage_key: str | None
) -> str | None:
    return f"/api/videos/{video_id}/poster" if video_id and poster_storage_key else None


def _clear_shot_video_output(out_json: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(out_json)
    for key in (
        "video_generation_id",
        "video_id",
        "video_status",
        "video_progress_stage",
        "video_progress_pct",
        "video_submission",
    ):
        cleaned.pop(key, None)
    return cleaned


def _run_metadata(run: WorkflowRun) -> dict[str, Any]:
    return dict(run.metadata_jsonb or {})


def _default_storyboard_metadata() -> dict[str, Any]:
    return {
        "style": "",
        "script": "",
        "script_confirmed": False,
        "script_revision": 0,
        "script_approved_revision": 0,
        "script_approved_at": None,
        "aspect_ratio": STORYBOARD_DEFAULT_ASPECT_RATIO,
        "resolution": STORYBOARD_DEFAULT_RESOLUTION,
        "generate_audio": True,
        "model": STORYBOARD_DEFAULT_MODEL,
        "seed": None,
        "conversation_id": None,
    }


def _merge_run_metadata(run: WorkflowRun, patch: dict[str, Any]) -> None:
    current = _default_storyboard_metadata()
    current.update(_run_metadata(run))
    current.update(patch)
    run.metadata_jsonb = current


class StoryboardAssetOut(BaseModel):
    id: str
    kind: str
    name: str
    role: str
    description: str
    continuity: str
    revision: int
    status: str
    prompt: str
    image_id: str | None = None
    image_url: str | None = None
    display_url: str | None = None
    generation_id: str | None = None
    approved_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class StoryboardShotOut(BaseModel):
    id: str
    index: int
    title: str
    purpose: str
    narration: str
    visual: str
    shot_type: str
    camera_move: str
    transition: str
    reference_notes: str
    duration_s: int
    asset_ids: list[str]
    keyframe_prompt: str
    keyframe_source_hash: str | None = None
    current_source_hash: str
    keyframe_stale: bool = False
    status: str
    keyframe_image_id: str | None = None
    keyframe_image_url: str | None = None
    keyframe_display_url: str | None = None
    keyframe_generation_id: str | None = None
    keyframe_approved_at: str | None = None
    video_generation_id: str | None = None
    video: VideoOut | None = None
    video_status: str | None = None
    video_progress_stage: str | None = None
    video_progress_pct: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class StoryboardAssemblyOut(BaseModel):
    status: str
    video_id: str | None = None
    video_url: str | None = None
    poster_url: str | None = None
    segment_count: int = 0
    segment_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    updated_at: datetime | None = None


class StoryboardRunOut(BaseModel):
    id: str
    conversation_id: str | None = None
    title: str
    idea: str
    style: str
    script: str
    script_confirmed: bool
    script_revision: int
    aspect_ratio: str
    resolution: str
    model: str
    generate_audio: bool
    seed: int | None = None
    status: str
    current_stage: str
    assets: list[StoryboardAssetOut]
    shots: list[StoryboardShotOut]
    assembly: StoryboardAssemblyOut | None = None
    thumbnail_url: str | None = None
    created_at: datetime
    updated_at: datetime


class StoryboardRunListItemOut(BaseModel):
    id: str
    title: str
    idea: str
    status: str
    current_stage: str
    asset_count: int
    approved_asset_count: int
    shot_count: int
    done_shot_count: int
    thumbnail_url: str | None = None
    created_at: datetime
    updated_at: datetime


class StoryboardRunListOut(BaseModel):
    items: list[StoryboardRunListItemOut]
    next_cursor: str | None = None


class StoryboardCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    idea: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    style: str = Field(default="", max_length=2000)
    aspect_ratio: str = Field(default=STORYBOARD_DEFAULT_ASPECT_RATIO, max_length=16)
    resolution: str = Field(default=STORYBOARD_DEFAULT_RESOLUTION, max_length=16)
    model: str = Field(default=STORYBOARD_DEFAULT_MODEL, max_length=64)
    generate_audio: bool = True
    seed: int | None = Field(default=None, ge=-1, le=4_294_967_295)


class StoryboardPatchIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    idea: str | None = Field(default=None, min_length=1, max_length=MAX_PROMPT_CHARS)
    style: str | None = Field(default=None, max_length=2000)
    script: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    script_confirmed: bool | None = None
    aspect_ratio: str | None = Field(default=None, max_length=16)
    resolution: str | None = Field(default=None, max_length=16)
    model: str | None = Field(default=None, max_length=64)
    generate_audio: bool | None = None
    seed: int | None = Field(default=None, ge=-1, le=4_294_967_295)
    current_stage: str | None = Field(default=None, max_length=32)


class StoryboardAssetCreateIn(BaseModel):
    kind: Literal["character", "scene", "prop"] = "character"
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=2000)
    continuity: str = Field(default="", max_length=2000)


class StoryboardAssetPatchIn(BaseModel):
    kind: Literal["character", "scene", "prop"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    continuity: str | None = Field(default=None, max_length=2000)


class StoryboardGenerateIn(BaseModel):
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)


class StoryboardShotCreateIn(BaseModel):
    title: str = Field(default="", max_length=160)
    purpose: str = Field(default="", max_length=1000)
    narration: str = Field(default="", max_length=2000)
    visual: str = Field(default="", max_length=2000)
    shot_type: str = Field(default="", max_length=80)
    camera_move: str = Field(default="", max_length=80)
    transition: str = Field(default="", max_length=80)
    reference_notes: str = Field(default="", max_length=2000)
    duration_s: int = Field(default=STORYBOARD_DEFAULT_DURATION_S, ge=3, le=15)
    asset_ids: list[str] = Field(default_factory=list, max_length=16)
    keyframe_prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)


class StoryboardShotPatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    purpose: str | None = Field(default=None, max_length=1000)
    narration: str | None = Field(default=None, max_length=2000)
    visual: str | None = Field(default=None, max_length=2000)
    shot_type: str | None = Field(default=None, max_length=80)
    camera_move: str | None = Field(default=None, max_length=80)
    transition: str | None = Field(default=None, max_length=80)
    reference_notes: str | None = Field(default=None, max_length=2000)
    duration_s: int | None = Field(default=None, ge=3, le=15)
    asset_ids: list[str] | None = Field(default=None, max_length=16)
    keyframe_prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)


class StoryboardShotsRebuildIn(BaseModel):
    shots: list[StoryboardShotCreateIn] | None = Field(default=None, max_length=60)
    replace: bool = True


class StoryboardShotMoveIn(BaseModel):
    direction: Literal[-1, 1]


class StoryboardSubmitShotIn(BaseModel):
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    duration_s: int | None = Field(default=None, ge=3, le=15)
    idempotency_key: str | None = Field(default=None, max_length=96)


@dataclass
class StoryboardImageTask:
    generation_id: str
    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    outbox_payloads: list[dict[str, Any]]
    outbox_rows: list[OutboxEvent]


async def _publish_storyboard_event(
    user_id: str,
    run_id: str,
    event_name: str,
    data: dict[str, Any],
) -> None:
    try:
        await publish_sse_event(
            get_redis(),
            user_id=user_id,
            channel=storyboard_channel(run_id),
            event_name=event_name,
            data={"storyboard_id": run_id, **data},
        )
    except Exception:
        logger.warning(
            "storyboard SSE publish failed user=%s run=%s event=%s",
            user_id,
            run_id,
            event_name,
            exc_info=True,
        )


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


async def _get_or_create_storyboard_conversation(
    db: AsyncSession,
    *,
    user: User,
    run: WorkflowRun,
) -> Conversation:
    if run.conversation_id:
        conv = await _get_owned_conversation(
            db, user_id=user.id, conversation_id=run.conversation_id
        )
    else:
        conv = Conversation(
            user_id=user.id,
            title=run.title or "分镜项目",
            archived=True,
            default_params={},
        )
        db.add(conv)
        await db.flush()
        run.conversation_id = conv.id
    params = dict(conv.default_params or {})
    params["workflow_type"] = STORYBOARD_WORKFLOW_TYPE
    params["hidden_from_conversations"] = True
    conv.default_params = params
    conv.title = run.title or conv.title
    conv.archived = True
    _merge_run_metadata(run, {"conversation_id": conv.id})
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
        WorkflowRun.type == STORYBOARD_WORKFLOW_TYPE,
        WorkflowRun.deleted_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    run = (await db.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise _http("not_found", "storyboard not found", 404)
    return run


async def _load_steps(
    db: AsyncSession,
    run_id: str,
    *,
    lock: bool = False,
) -> list[WorkflowStep]:
    stmt = (
        select(WorkflowStep)
        .where(WorkflowStep.workflow_run_id == run_id)
        .order_by(WorkflowStep.created_at.asc(), WorkflowStep.id.asc())
    )
    if lock:
        stmt = stmt.with_for_update()
    rows = (await db.execute(stmt)).scalars()
    return list(rows.all())


async def _get_step(
    db: AsyncSession,
    run: WorkflowRun,
    step_id: str,
    *,
    kind: Literal["asset", "shot"] | None = None,
    lock: bool = False,
) -> WorkflowStep:
    stmt = select(WorkflowStep).where(
        WorkflowStep.id == step_id,
        WorkflowStep.workflow_run_id == run.id,
    )
    if lock:
        stmt = stmt.with_for_update()
    step = (await db.execute(stmt)).scalar_one_or_none()
    if step is None or (kind is not None and _step_kind(step) != kind):
        raise _http("not_found", "storyboard step not found", 404)
    return step


async def _assembly_step(
    db: AsyncSession,
    run: WorkflowRun,
    *,
    lock: bool = False,
) -> WorkflowStep:
    stmt = select(WorkflowStep).where(
        WorkflowStep.workflow_run_id == run.id,
        WorkflowStep.step_key == "assembly",
    )
    if lock:
        stmt = stmt.with_for_update()
    step = (await db.execute(stmt)).scalar_one_or_none()
    if step is None:
        step = WorkflowStep(
            workflow_run_id=run.id,
            step_key="assembly",
            status="waiting",
            input_json={},
            output_json={"segment_ids": []},
        )
        db.add(step)
        await db.flush()
    return step


def _asset_out(step: WorkflowStep) -> StoryboardAssetOut:
    inp = dict(step.input_json or {})
    out = dict(step.output_json or {})
    image_id = out.get("image_id") if isinstance(out.get("image_id"), str) else None
    generation_id = (
        out.get("generation_id") if isinstance(out.get("generation_id"), str) else None
    )
    return StoryboardAssetOut(
        id=step.id,
        kind=_clean_text(str(inp.get("kind") or "character"), max_len=32),
        name=_clean_text(str(inp.get("name") or ""), max_len=120, default="未命名设定"),
        role=_clean_text(str(inp.get("role") or ""), max_len=160),
        description=_clean_text(str(inp.get("description") or ""), max_len=2000),
        continuity=_clean_text(str(inp.get("continuity") or ""), max_len=2000),
        revision=int(inp.get("revision") or 1),
        status=step.status,
        prompt=_clean_text(str(out.get("prompt") or ""), max_len=MAX_PROMPT_CHARS),
        image_id=image_id,
        image_url=_image_url(image_id),
        display_url=_image_display_url(image_id),
        generation_id=generation_id,
        approved_at=(
            str(out.get("approved_at"))
            if isinstance(out.get("approved_at"), str)
            else _iso(step.approved_at)
        ),
        error_code=out.get("error_code")
        if isinstance(out.get("error_code"), str)
        else None,
        error_message=(
            out.get("error_message")
            if isinstance(out.get("error_message"), str)
            else None
        ),
        created_at=step.created_at,
        updated_at=step.updated_at,
    )


def _asset_hash_payload(asset: WorkflowStep) -> dict[str, Any]:
    inp = dict(asset.input_json or {})
    out = dict(asset.output_json or {})
    return {
        "step_id": asset.id,
        "revision": inp.get("revision"),
        "image_id": out.get("image_id"),
        "approved_at": out.get("approved_at") or _iso(asset.approved_at),
    }


def _shot_source_hash(shot: WorkflowStep, assets_by_id: dict[str, WorkflowStep]) -> str:
    inp = dict(shot.input_json or {})
    asset_refs = [
        _asset_hash_payload(assets_by_id[asset_id])
        for asset_id in inp.get("asset_ids", [])
        if isinstance(asset_id, str) and asset_id in assets_by_id
    ]
    payload = {
        "title": inp.get("title"),
        "purpose": inp.get("purpose"),
        "narration": inp.get("narration"),
        "visual": inp.get("visual"),
        "shot_type": inp.get("shot_type"),
        "camera_move": inp.get("camera_move"),
        "transition": inp.get("transition"),
        "reference_notes": inp.get("reference_notes"),
        "keyframe_prompt": inp.get("keyframe_prompt"),
        "asset_refs": asset_refs,
    }
    return _short_hash(payload)


def _storyboard_video_submission_fingerprint(
    *,
    step: WorkflowStep,
    keyframe_image_id: str,
) -> str:
    inp = dict(step.input_json or {})
    out = dict(step.output_json or {})
    return _short_hash(
        {
            "keyframe_generation_id": out.get("keyframe_generation_id"),
            "keyframe_image_id": keyframe_image_id,
            "keyframe_source_hash": inp.get("keyframe_source_hash"),
        }
    )


def _new_storyboard_video_idempotency_key(
    *,
    run_id: str,
    step_id: str,
    submission_fingerprint: str,
) -> str:
    token = _short_hash(
        {
            "run_id": run_id,
            "step_id": step_id,
            "submission_fingerprint": submission_fingerprint,
            "nonce": new_uuid7(),
        }
    )[:16]
    return f"sb:{run_id}:{step_id}:v:{token}"[:96]


def _resolve_storyboard_video_idempotency_key(
    *,
    run_id: str,
    step: WorkflowStep,
    keyframe_image_id: str,
    requested_key: str | None,
) -> tuple[str, str]:
    submission_fingerprint = _storyboard_video_submission_fingerprint(
        step=step,
        keyframe_image_id=keyframe_image_id,
    )
    if requested_key:
        return requested_key[:96], submission_fingerprint
    out = dict(step.output_json or {})
    raw_submission = out.get("video_submission")
    submission = raw_submission if isinstance(raw_submission, dict) else {}
    existing_key = submission.get("idempotency_key")
    if (
        submission.get("fingerprint") == submission_fingerprint
        and isinstance(existing_key, str)
        and existing_key
    ):
        return existing_key[:96], submission_fingerprint
    return (
        _new_storyboard_video_idempotency_key(
            run_id=run_id,
            step_id=step.id,
            submission_fingerprint=submission_fingerprint,
        ),
        submission_fingerprint,
    )


def _storyboard_assembly_fingerprint(segment_ids: Iterable[str]) -> str:
    return _short_hash({"segment_ids": list(segment_ids)})


def _storyboard_assembly_idempotency_key(
    *,
    run_id: str,
    fingerprint: str,
) -> str:
    return f"sb:{run_id}:assembly:{fingerprint}"[:96]


def _parse_assembly_datetime(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str) and raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _assembly_lease_expiry(
    assembly: WorkflowStep,
    output: dict[str, Any],
) -> datetime | None:
    explicit_expiry = _parse_assembly_datetime(output.get("assembly_lease_expires_at"))
    if explicit_expiry is not None:
        return explicit_expiry

    claimed_at = _parse_assembly_datetime(output.get("assembly_claimed_at"))
    if assembly.status == "compositing" and claimed_at is not None:
        heartbeat_at = _parse_assembly_datetime(output.get("assembly_heartbeat_at"))
        base = heartbeat_at or claimed_at
        return base + timedelta(seconds=STORYBOARD_ASSEMBLY_WORKER_LEASE_S)

    enqueued_at = _parse_assembly_datetime(output.get("assembly_enqueued_at"))
    updated_at = _parse_assembly_datetime(getattr(assembly, "updated_at", None))
    waiting_base = enqueued_at or updated_at
    if waiting_base is None:
        return None
    return waiting_base + timedelta(seconds=STORYBOARD_ASSEMBLY_WAITING_LEASE_S)


def _assembly_attempt_is_stale(
    assembly: WorkflowStep,
    output: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    if assembly.status not in {"waiting", "compositing"}:
        return False
    expires_at = _assembly_lease_expiry(assembly, output)
    if expires_at is None:
        return False
    current = _parse_assembly_datetime(now) or _now()
    return expires_at <= current


def _assembly_request_is_replay(
    assembly: WorkflowStep,
    output: dict[str, Any],
    fingerprint: str,
    *,
    now: datetime | None = None,
) -> bool:
    if output.get("assembly_fingerprint") != fingerprint:
        return False
    if assembly.status == "done":
        return True
    if assembly.status not in {"waiting", "compositing"}:
        return False
    return not _assembly_attempt_is_stale(assembly, output, now=now)


def _assembly_status_for_response(
    assembly: WorkflowStep,
    output: dict[str, Any],
) -> str:
    attempt_token = output.get("assembly_attempt_token")
    if (
        assembly.status == "waiting"
        and isinstance(attempt_token, str)
        and attempt_token
    ):
        return "compositing"
    return assembly.status


def _shot_out(
    step: WorkflowStep,
    *,
    assets_by_id: dict[str, WorkflowStep],
    video_generations: dict[str, VideoGeneration],
    videos_by_generation: dict[str, Video],
) -> StoryboardShotOut:
    inp = dict(step.input_json or {})
    out = dict(step.output_json or {})
    keyframe_image_id = (
        out.get("keyframe_image_id")
        if isinstance(out.get("keyframe_image_id"), str)
        else None
    )
    video_generation_id = (
        out.get("video_generation_id")
        if isinstance(out.get("video_generation_id"), str)
        else None
    )
    current_source_hash = _shot_source_hash(step, assets_by_id)
    stored_source_hash = (
        inp.get("keyframe_source_hash")
        if isinstance(inp.get("keyframe_source_hash"), str)
        else None
    )
    keyframe_stale = bool(
        keyframe_image_id and stored_source_hash != current_source_hash
    )
    video_generation = (
        video_generations.get(video_generation_id) if video_generation_id else None
    )
    video = (
        videos_by_generation.get(video_generation_id) if video_generation_id else None
    )
    return StoryboardShotOut(
        id=step.id,
        index=int(inp.get("index") or 0),
        title=_clean_text(
            str(inp.get("title") or ""), max_len=160, default="未命名分镜"
        ),
        purpose=_clean_text(str(inp.get("purpose") or ""), max_len=1000),
        narration=_clean_text(str(inp.get("narration") or ""), max_len=2000),
        visual=_clean_text(str(inp.get("visual") or ""), max_len=2000),
        shot_type=_clean_text(str(inp.get("shot_type") or ""), max_len=80),
        camera_move=_clean_text(str(inp.get("camera_move") or ""), max_len=80),
        transition=_clean_text(str(inp.get("transition") or ""), max_len=80),
        reference_notes=_clean_text(
            str(inp.get("reference_notes") or ""), max_len=2000
        ),
        duration_s=int(inp.get("duration_s") or STORYBOARD_DEFAULT_DURATION_S),
        asset_ids=_clean_string_list(inp.get("asset_ids") or []),
        keyframe_prompt=_clean_text(
            str(inp.get("keyframe_prompt") or ""), max_len=MAX_PROMPT_CHARS
        ),
        keyframe_source_hash=stored_source_hash,
        current_source_hash=current_source_hash,
        keyframe_stale=keyframe_stale,
        status=step.status,
        keyframe_image_id=keyframe_image_id,
        keyframe_image_url=_image_url(keyframe_image_id),
        keyframe_display_url=_image_display_url(keyframe_image_id),
        keyframe_generation_id=(
            out.get("keyframe_generation_id")
            if isinstance(out.get("keyframe_generation_id"), str)
            else None
        ),
        keyframe_approved_at=(
            out.get("keyframe_approved_at")
            if isinstance(out.get("keyframe_approved_at"), str)
            else None
        ),
        video_generation_id=video_generation_id,
        video=_video_out(video) if video is not None else None,
        video_status=video_generation.status if video_generation is not None else None,
        video_progress_stage=(
            video_generation.progress_stage if video_generation is not None else None
        ),
        video_progress_pct=(
            video_generation.progress_pct if video_generation is not None else None
        ),
        error_code=out.get("error_code")
        if isinstance(out.get("error_code"), str)
        else None,
        error_message=(
            out.get("error_message")
            if isinstance(out.get("error_message"), str)
            else None
        ),
        created_at=step.created_at,
        updated_at=step.updated_at,
    )


async def _sync_storyboard_outputs(db: AsyncSession, run: WorkflowRun) -> None:
    steps = await _load_steps(db, run.id, lock=True)
    generation_ids = {
        task_id
        for step in steps
        for task_id in (step.task_ids or [])
        if isinstance(task_id, str) and task_id
    }
    video_generation_ids = {
        out.get("video_generation_id")
        for out in (dict(step.output_json or {}) for step in steps)
        if isinstance(out.get("video_generation_id"), str)
    }
    recovered_video_generations = await _recover_storyboard_video_generations(
        db, run=run, steps=steps
    )
    changed = False
    if recovered_video_generations:
        for step in steps:
            if _step_kind(step) != "shot":
                continue
            out = dict(step.output_json or {})
            if isinstance(out.get("video_generation_id"), str):
                continue
            recovered = recovered_video_generations.get(step.step_key)
            if recovered is None:
                continue
            out["video_generation_id"] = recovered.id
            step.output_json = out
            step.task_ids = _clean_string_list([*(step.task_ids or []), recovered.id])
            if _rank_status(step.status) >= _rank_status(
                "keyframe_approved"
            ) and _rank_status(step.status) < _rank_status("generating"):
                step.status = "generating"
            video_generation_ids.add(recovered.id)
            changed = True
    generations: dict[str, Generation] = {}
    if generation_ids:
        generation_rows = (
            await db.execute(
                select(Generation).where(
                    Generation.id.in_(generation_ids),
                    Generation.user_id == run.user_id,
                )
            )
        ).scalars()
        generations = {row.id: row for row in generation_rows.all()}
    images_by_generation: dict[str, Image] = {}
    if generation_ids:
        image_rows = (
            await db.execute(
                select(Image).where(
                    Image.owner_generation_id.in_(generation_ids),
                    Image.user_id == run.user_id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars()
        for image in image_rows.all():
            if (
                image.owner_generation_id
                and image.owner_generation_id not in images_by_generation
            ):
                images_by_generation[image.owner_generation_id] = image

    video_generations: dict[str, VideoGeneration] = {}
    if video_generation_ids:
        video_generation_rows = (
            await db.execute(
                select(VideoGeneration).where(
                    VideoGeneration.id.in_(video_generation_ids),
                    VideoGeneration.user_id == run.user_id,
                )
            )
        ).scalars()
        video_generations = {row.id: row for row in video_generation_rows.all()}
    videos_by_generation: dict[str, Video] = {}
    if video_generation_ids:
        video_rows = (
            await db.execute(
                select(Video).where(
                    Video.owner_generation_id.in_(video_generation_ids),
                    Video.deleted_at.is_(None),
                )
            )
        ).scalars()
        videos_by_generation = {
            row.owner_generation_id: row
            for row in video_rows.all()
            if row.owner_generation_id is not None
        }

    for step in steps:
        kind = _step_kind(step)
        out = dict(step.output_json or {})
        if kind == "asset":
            generation_id = out.get("generation_id")
            gen = (
                generations.get(generation_id)
                if isinstance(generation_id, str)
                else None
            )
            if gen is None:
                continue
            asset_image = images_by_generation.get(gen.id)
            if (
                gen.status == GenerationStatus.SUCCEEDED.value
                and asset_image is not None
            ):
                if out.get("image_id") != asset_image.id or step.status == "generating":
                    out.update(
                        {
                            "image_id": asset_image.id,
                            "error_code": None,
                            "error_message": None,
                        }
                    )
                    step.output_json = out
                    step.image_ids = [asset_image.id]
                    if step.status == "generating":
                        step.status = "ready"
                    changed = True
            elif gen.status in {
                GenerationStatus.FAILED.value,
                GenerationStatus.CANCELED.value,
            }:
                if step.status == "generating":
                    out.update(
                        {
                            "error_code": gen.error_code or gen.status,
                            "error_message": gen.error_message
                            or "asset generation failed",
                        }
                    )
                    step.output_json = out
                    step.status = "waiting_input"
                    changed = True
        elif kind == "shot":
            keyframe_generation_id = out.get("keyframe_generation_id")
            gen = (
                generations.get(keyframe_generation_id)
                if isinstance(keyframe_generation_id, str)
                else None
            )
            if gen is not None:
                keyframe_image = images_by_generation.get(gen.id)
                if (
                    gen.status == GenerationStatus.SUCCEEDED.value
                    and keyframe_image is not None
                ):
                    if out.get("keyframe_image_id") != keyframe_image.id:
                        out.update(
                            {
                                "keyframe_image_id": keyframe_image.id,
                                "error_code": None,
                                "error_message": None,
                            }
                        )
                        step.output_json = out
                        step.image_ids = [keyframe_image.id]
                        if step.status == "keyframe_generating":
                            step.status = "keyframe_ready"
                        changed = True
                elif (
                    gen.status
                    in {
                        GenerationStatus.FAILED.value,
                        GenerationStatus.CANCELED.value,
                    }
                    and step.status == "keyframe_generating"
                ):
                    out.update(
                        {
                            "error_code": gen.error_code or gen.status,
                            "error_message": gen.error_message
                            or "keyframe generation failed",
                        }
                    )
                    step.output_json = out
                    step.status = "approved"
                    changed = True

            video_generation_id = out.get("video_generation_id")
            vg = (
                video_generations.get(video_generation_id)
                if isinstance(video_generation_id, str)
                else None
            )
            if vg is None:
                continue
            video = videos_by_generation.get(vg.id)
            if vg.status == VideoGenerationStatus.SUCCEEDED.value and video is not None:
                if step.status != "done":
                    step.status = "done"
                    out.pop("video_submission", None)
                    out.update({"error_code": None, "error_message": None})
                    step.output_json = out
                    changed = True
            elif (
                vg.status
                in {
                    VideoGenerationStatus.FAILED.value,
                    VideoGenerationStatus.CANCELED.value,
                    VideoGenerationStatus.EXPIRED.value,
                }
                and step.status == "generating"
            ):
                out.update(
                    {
                        "error_code": vg.error_code or vg.status,
                        "error_message": vg.error_message or "video generation failed",
                    }
                )
                out.pop("video_submission", None)
                step.output_json = out
                step.status = "keyframe_approved"
                changed = True
    if changed:
        await db.flush()


async def _recover_storyboard_video_generations(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    steps: list[WorkflowStep],
) -> dict[str, VideoGeneration]:
    expected_fingerprints: dict[str, str] = {}
    for step in steps:
        if _step_kind(step) != "shot":
            continue
        out = dict(step.output_json or {})
        if isinstance(out.get("video_generation_id"), str):
            continue
        keyframe_image_id = out.get("keyframe_image_id")
        if not isinstance(keyframe_image_id, str) or not keyframe_image_id:
            continue
        expected_fingerprints[step.step_key] = _storyboard_video_submission_fingerprint(
            step=step,
            keyframe_image_id=keyframe_image_id,
        )
    if not expected_fingerprints:
        return {}
    rows = (
        await db.execute(
            select(VideoGeneration)
            .where(
                VideoGeneration.user_id == run.user_id,
                VideoGeneration.upstream_request["workflow_type"].as_string()
                == STORYBOARD_WORKFLOW_TYPE,
                VideoGeneration.upstream_request["workflow_run_id"].as_string()
                == run.id,
            )
            .order_by(desc(VideoGeneration.created_at), desc(VideoGeneration.id))
        )
    ).scalars()
    recovered: dict[str, VideoGeneration] = {}
    for row in rows.all():
        request = row.upstream_request if isinstance(row.upstream_request, dict) else {}
        step_key = request.get("workflow_step_key")
        expected_fingerprint = (
            expected_fingerprints.get(step_key) if isinstance(step_key, str) else None
        )
        if (
            isinstance(step_key, str)
            and expected_fingerprint is not None
            and request.get("storyboard_video_submission_fingerprint")
            == expected_fingerprint
            and step_key not in recovered
        ):
            recovered[step_key] = row
    return recovered


async def _build_run_out(db: AsyncSession, run: WorkflowRun) -> StoryboardRunOut:
    await _sync_storyboard_outputs(db, run)
    await db.flush()
    await db.refresh(run)
    steps = await _load_steps(db, run.id)
    assets = [step for step in steps if _step_kind(step) == "asset"]
    shots = [step for step in steps if _step_kind(step) == "shot"]
    assembly = next((step for step in steps if step.step_key == "assembly"), None)
    assets_by_id = {step.id: step for step in assets}

    video_generation_ids = [
        dict(step.output_json or {}).get("video_generation_id")
        for step in shots
        if isinstance(dict(step.output_json or {}).get("video_generation_id"), str)
    ]
    video_generations: dict[str, VideoGeneration] = {}
    if video_generation_ids:
        video_generation_rows = (
            await db.execute(
                select(VideoGeneration).where(
                    VideoGeneration.id.in_(video_generation_ids),
                    VideoGeneration.user_id == run.user_id,
                )
            )
        ).scalars()
        video_generations = {row.id: row for row in video_generation_rows.all()}
    videos_by_generation: dict[str, Video] = {}
    if video_generation_ids:
        video_rows = (
            await db.execute(
                select(Video).where(
                    Video.owner_generation_id.in_(video_generation_ids),
                    Video.deleted_at.is_(None),
                )
            )
        ).scalars()
        videos_by_generation = {
            row.owner_generation_id: row
            for row in video_rows.all()
            if row.owner_generation_id is not None
        }

    shot_outs = sorted(
        [
            _shot_out(
                step,
                assets_by_id=assets_by_id,
                video_generations=video_generations,
                videos_by_generation=videos_by_generation,
            )
            for step in shots
        ],
        key=lambda item: (item.index, item.created_at, item.id),
    )
    asset_outs = sorted(
        [_asset_out(step) for step in assets],
        key=lambda item: (item.created_at, item.id),
    )
    md = _default_storyboard_metadata()
    md.update(_run_metadata(run))

    assembly_out: StoryboardAssemblyOut | None = None
    if assembly is not None:
        out = dict(assembly.output_json or {})
        video_id = out.get("video_id") if isinstance(out.get("video_id"), str) else None
        video: Video | None = None
        if video_id:
            video = (
                await db.execute(
                    select(Video).where(
                        Video.id == video_id,
                        Video.user_id == run.user_id,
                        Video.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
        segment_ids = [
            item for item in (out.get("segment_ids") or []) if isinstance(item, str)
        ]
        assembly_out = StoryboardAssemblyOut(
            status=_assembly_status_for_response(assembly, out),
            video_id=video_id,
            video_url=_video_url(video_id),
            poster_url=_video_poster_url(
                video_id, video.poster_storage_key if video else None
            ),
            segment_count=len(segment_ids),
            segment_ids=segment_ids,
            error_code=out.get("error_code")
            if isinstance(out.get("error_code"), str)
            else None,
            error_message=(
                out.get("error_message")
                if isinstance(out.get("error_message"), str)
                else None
            ),
            updated_at=assembly.updated_at,
        )

    thumbnail_url = (
        assembly_out.poster_url
        if assembly_out and assembly_out.poster_url
        else next(
            (
                shot.keyframe_display_url or shot.keyframe_image_url
                for shot in shot_outs
                if shot.keyframe_image_id
            ),
            None,
        )
        or next(
            (
                asset.display_url or asset.image_url
                for asset in asset_outs
                if asset.image_id
            ),
            None,
        )
    )

    return StoryboardRunOut(
        id=run.id,
        conversation_id=run.conversation_id,
        title=run.title,
        idea=run.user_prompt,
        style=str(md.get("style") or ""),
        script=str(md.get("script") or ""),
        script_confirmed=bool(md.get("script_confirmed")),
        script_revision=int(md.get("script_revision") or 0),
        aspect_ratio=str(md.get("aspect_ratio") or STORYBOARD_DEFAULT_ASPECT_RATIO),
        resolution=str(md.get("resolution") or STORYBOARD_DEFAULT_RESOLUTION),
        model=str(md.get("model") or STORYBOARD_DEFAULT_MODEL),
        generate_audio=bool(md.get("generate_audio", True)),
        seed=md.get("seed") if isinstance(md.get("seed"), int) else None,
        status=run.status,
        current_stage=run.current_step,
        assets=asset_outs,
        shots=shot_outs,
        assembly=assembly_out,
        thumbnail_url=thumbnail_url,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


async def _list_item_out(
    db: AsyncSession, run: WorkflowRun
) -> StoryboardRunListItemOut:
    out = await _build_run_out(db, run)
    return StoryboardRunListItemOut(
        id=out.id,
        title=out.title,
        idea=out.idea,
        status=out.status,
        current_stage=out.current_stage,
        asset_count=len(out.assets),
        approved_asset_count=sum(1 for item in out.assets if item.status == "approved"),
        shot_count=len(out.shots),
        done_shot_count=sum(1 for item in out.shots if item.status == "done"),
        thumbnail_url=out.thumbnail_url,
        created_at=out.created_at,
        updated_at=out.updated_at,
    )


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        ts, row_id = cursor.split("|", 1)
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return dt, row_id
    except ValueError as exc:
        raise _http("invalid_cursor", "cursor is invalid", 422) from exc


def _encode_cursor(run: WorkflowRun) -> str:
    return f"{run.updated_at.isoformat()}|{run.id}"


def _asset_prompt(run: WorkflowRun, step: WorkflowStep, override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    md = _run_metadata(run)
    inp = dict(step.input_json or {})
    return "\n".join(
        part
        for part in [
            "Create a clean, production-ready visual reference image for a short video storyboard.",
            f"Project idea: {run.user_prompt}",
            f"Visual style: {md.get('style') or 'consistent cinematic commercial look'}",
            f"Asset type: {inp.get('kind')}",
            f"Name: {inp.get('name')}",
            f"Role: {inp.get('role')}",
            f"Description: {inp.get('description')}",
            f"Continuity requirements: {inp.get('continuity')}",
            "No text overlays. Center the subject clearly. Make it useful as continuity reference.",
        ]
        if str(part).strip()
    )[:MAX_PROMPT_CHARS]


def _shot_keyframe_prompt(
    run: WorkflowRun,
    shot: WorkflowStep,
    assets_by_id: dict[str, WorkflowStep],
    override: str | None,
) -> str:
    if override and override.strip():
        return override.strip()
    md = _run_metadata(run)
    inp = dict(shot.input_json or {})
    asset_lines: list[str] = []
    for asset_id in inp.get("asset_ids") or []:
        asset = assets_by_id.get(asset_id)
        if asset is None:
            continue
        asset_in = dict(asset.input_json or {})
        asset_lines.append(
            f"- {asset_in.get('kind')}: {asset_in.get('name')} ({asset_in.get('description')})"
        )
    return "\n".join(
        part
        for part in [
            "Generate one polished keyframe for this storyboard shot.",
            f"Project idea: {run.user_prompt}",
            f"Style continuity: {md.get('style') or 'cinematic, coherent visual continuity'}",
            f"Shot title: {inp.get('title')}",
            f"Purpose: {inp.get('purpose')}",
            f"Narration: {inp.get('narration')}",
            f"Visual: {inp.get('visual')}",
            f"Shot type: {inp.get('shot_type')}",
            f"Camera move: {inp.get('camera_move')}",
            f"Transition: {inp.get('transition')}",
            f"Reference notes: {inp.get('reference_notes')}",
            "Bound approved references:",
            "\n".join(asset_lines),
            "No subtitles or watermarks. Compose as the first frame for image-to-video generation.",
        ]
        if str(part).strip()
    )[:MAX_PROMPT_CHARS]


async def _create_storyboard_image_task(
    *,
    db: AsyncSession,
    user: User,
    run: WorkflowRun,
    step: WorkflowStep,
    prompt: str,
    attachment_ids: list[str],
    purpose: Literal["asset", "keyframe"],
) -> StoryboardImageTask:
    conv = await _get_or_create_storyboard_conversation(db, user=user, run=run)
    user_msg = Message(
        conversation_id=conv.id,
        role=Role.USER.value,
        content={
            "text": prompt,
            "attachments": [{"image_id": image_id} for image_id in attachment_ids],
            "workflow_type": STORYBOARD_WORKFLOW_TYPE,
            "workflow_run_id": run.id,
            "workflow_step_key": step.step_key,
            "storyboard_purpose": purpose,
        },
        intent=None,
        status=None,
    )
    db.add(user_msg)
    await db.flush()
    md = _run_metadata(run)
    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=getattr(user, "account_mode", "wallet"),
        conv=conv,
        user_msg=user_msg,
        intent=Intent.IMAGE_TO_IMAGE if attachment_ids else Intent.TEXT_TO_IMAGE,
        idempotency_key=f"storyboard:{run.id}:{step.id}:{purpose}:{new_uuid7()}",
        image_params=ImageParamsIn.model_validate(
            {
                "aspect_ratio": str(
                    md.get("aspect_ratio") or STORYBOARD_DEFAULT_ASPECT_RATIO
                ),
                "count": 1,
                "quality": "2k",
                "render_quality": "medium",
            }
        ),
        chat_params=ChatParamsIn(),
        system_prompt=None,
        attachment_ids=attachment_ids,
        text=prompt,
        user_email=getattr(user, "email", None),
        request_metadata={
            "source": "storyboard",
            "action_source": f"storyboard_{purpose}",
            "workflow_type": STORYBOARD_WORKFLOW_TYPE,
            "workflow_run_id": run.id,
            "workflow_step_key": step.step_key,
            "storyboard_purpose": purpose,
            "input_images": [
                {"image_id": image_id, "role": "reference"}
                for image_id in attachment_ids
            ],
            "primary_input_image_id": attachment_ids[0] if attachment_ids else None,
        },
    )
    if not result.generation_ids:
        raise _http("task_not_created", "image generation task was not created", 500)
    generation_id = result.generation_ids[0]
    return StoryboardImageTask(
        generation_id=generation_id,
        conversation_id=conv.id,
        user_message_id=user_msg.id,
        assistant_message_id=result.assistant_msg.id,
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )


async def _publish_storyboard_image_task(
    *,
    db: AsyncSession,
    user_id: str,
    task: StoryboardImageTask,
) -> None:
    if await _enqueue_storyboard_image_task(user_id=user_id, task=task):
        await _mark_storyboard_image_tasks_published(db, [task])


async def _publish_storyboard_image_tasks(
    *,
    db: AsyncSession,
    user_id: str,
    tasks: list[StoryboardImageTask],
) -> None:
    semaphore = asyncio.Semaphore(STORYBOARD_KEYFRAME_PARALLELISM)

    async def publish_one(task: StoryboardImageTask) -> bool:
        async with semaphore:
            return await _enqueue_storyboard_image_task(user_id=user_id, task=task)

    results = await asyncio.gather(
        *(publish_one(task) for task in tasks),
        return_exceptions=True,
    )
    published = [
        task for task, result in zip(tasks, results, strict=False) if result is True
    ]
    for result in results:
        if isinstance(result, Exception):
            logger.warning("storyboard image task publish failed: %s", result)
    if published:
        await _mark_storyboard_image_tasks_published(db, published)


async def _enqueue_storyboard_image_task(
    *,
    user_id: str,
    task: StoryboardImageTask,
) -> bool:
    redis = get_redis()
    try:
        await _publish_message_appended(
            redis=redis,
            user_id=user_id,
            conv_id=task.conversation_id,
            message_ids=[task.user_message_id, task.assistant_message_id],
        )
        pool = await get_arq_pool()
        for payload in task.outbox_payloads:
            enqueue_kwargs: dict[str, Any] = {}
            defer_s = payload.get("defer_s")
            if isinstance(defer_s, (int, float)) and defer_s > 0:
                enqueue_kwargs["_defer_by"] = float(defer_s)
            enqueue_kwargs["_job_id"] = arq_job_id(
                str(payload["kind"]),
                str(payload["task_id"]),
                payload.get("outbox_id"),
            )
            await pool.enqueue_job(
                "run_generation",
                payload["task_id"],
                **enqueue_kwargs,
            )
            event_data: dict[str, Any] = {
                "generation_id": payload["task_id"],
                "message_id": task.assistant_message_id,
                "conversation_id": task.conversation_id,
                "kind": payload["kind"],
            }
            for key in ("trace_id", "source", "action_source"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    event_data[key] = value
            input_images = payload.get("input_images")
            if isinstance(input_images, list):
                event_data["input_images"] = input_images
            await publish_sse_event(
                redis,
                user_id=user_id,
                channel=task_channel(str(payload["task_id"])),
                event_name=EV_GEN_QUEUED,
                data=event_data,
            )
    except Exception:
        logger.warning(
            "storyboard image task enqueue failed user=%s conv=%s msg=%s",
            user_id,
            task.conversation_id,
            task.assistant_message_id,
            exc_info=True,
        )
        return False
    return True


async def _mark_storyboard_image_tasks_published(
    db: AsyncSession,
    tasks: list[StoryboardImageTask],
) -> None:
    try:
        now = _now()
        for task in tasks:
            for row in task.outbox_rows:
                row.published_at = now
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.warning("storyboard outbox rollback failed", exc_info=True)
        logger.warning("storyboard outbox mark-published failed", exc_info=True)


def _rank_status(status: str) -> int:
    return _SHOT_STATUS_RANK.get(status, 0)


def _normalize_shot_indexes(shots: list[WorkflowStep]) -> None:
    for index, shot in enumerate(
        sorted(
            shots,
            key=lambda step: (
                int((step.input_json or {}).get("index") or 0),
                step.created_at,
                step.id,
            ),
        ),
        start=1,
    ):
        data = dict(shot.input_json or {})
        if data.get("index") != index:
            data["index"] = index
            shot.input_json = data


async def _validate_asset_ids(
    db: AsyncSession,
    run: WorkflowRun,
    asset_ids: list[str],
    *,
    require_approved: bool = False,
) -> list[str]:
    ids = _clean_string_list(asset_ids)
    if not ids:
        return []
    rows = (
        await db.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.id.in_(ids),
            )
        )
    ).scalars()
    found = {row.id: row for row in rows.all() if _step_kind(row) == "asset"}
    missing = [asset_id for asset_id in ids if asset_id not in found]
    if missing:
        raise _http(
            "invalid_asset_ids", "one or more assets are not in this storyboard", 422
        )
    if require_approved:
        not_ready = [
            asset_id for asset_id in ids if found[asset_id].status != "approved"
        ]
        if not_ready:
            raise _http("asset_not_approved", "all bound assets must be approved", 422)
    return ids


def _shot_input_from_body(
    body: StoryboardShotCreateIn | StoryboardShotPatchIn,
    *,
    index: int | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = dict(existing or {})
    values = body.model_dump(exclude_unset=True)
    for key, value in values.items():
        if key == "asset_ids":
            data[key] = _clean_string_list(value)
        elif isinstance(value, str):
            data[key] = value.strip()
        elif value is not None:
            data[key] = value
    if index is not None:
        data["index"] = index
    data.setdefault("title", "")
    data.setdefault("purpose", "")
    data.setdefault("narration", "")
    data.setdefault("visual", "")
    data.setdefault("shot_type", "")
    data.setdefault("camera_move", "")
    data.setdefault("transition", "")
    data.setdefault("reference_notes", "")
    data.setdefault("duration_s", STORYBOARD_DEFAULT_DURATION_S)
    data.setdefault("asset_ids", [])
    data.setdefault("keyframe_prompt", "")
    data.setdefault("keyframe_source_hash", None)
    return data


def _shots_from_script(script: str) -> list[StoryboardShotCreateIn]:
    chunks = [
        chunk.strip()
        for chunk in re.split(r"(?:\n{2,}|[。.!?！？]\s*)", script)
        if chunk.strip()
    ]
    if not chunks:
        chunks = ["开场建立主体与氛围", "展示关键动作和变化", "收束到最终画面"]
    shots = []
    for idx, chunk in enumerate(chunks[:60], start=1):
        title = f"镜头 {idx:02d}"
        visual = chunk[:1200]
        shots.append(
            StoryboardShotCreateIn(
                title=title,
                purpose="推进故事节奏",
                narration=chunk[:1000],
                visual=visual,
                shot_type="medium shot" if idx > 1 else "establishing shot",
                camera_move="smooth cinematic movement",
                transition="cut",
                duration_s=STORYBOARD_DEFAULT_DURATION_S,
            )
        )
    return shots


@router.post("", response_model=StoryboardRunOut, dependencies=[Depends(verify_csrf)])
async def create_storyboard(
    body: StoryboardCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    title = body.title.strip()
    run = WorkflowRun(
        user_id=user.id,
        type=STORYBOARD_WORKFLOW_TYPE,
        status="draft",
        title=title,
        user_prompt=body.idea.strip(),
        product_image_ids=[],
        current_step="idea",
        quality_mode="premium",
        metadata_jsonb={
            **_default_storyboard_metadata(),
            "style": body.style.strip(),
            "aspect_ratio": body.aspect_ratio.strip()
            or STORYBOARD_DEFAULT_ASPECT_RATIO,
            "resolution": body.resolution.strip() or STORYBOARD_DEFAULT_RESOLUTION,
            "model": body.model.strip() or STORYBOARD_DEFAULT_MODEL,
            "generate_audio": body.generate_audio,
            "seed": body.seed,
        },
    )
    db.add(run)
    await db.flush()
    await _assembly_step(db, run)
    conv = await _get_or_create_storyboard_conversation(db, user=user, run=run)
    run.conversation_id = conv.id
    await db.commit()
    await db.refresh(run)
    return await _build_run_out(db, run)


@router.get("", response_model=StoryboardRunListOut)
async def list_storyboards(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=24, ge=1, le=60),
) -> StoryboardRunListOut:
    stmt = select(WorkflowRun).where(
        WorkflowRun.user_id == user.id,
        WorkflowRun.type == STORYBOARD_WORKFLOW_TYPE,
        WorkflowRun.deleted_at.is_(None),
    )
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        updated_at, row_id = decoded
        stmt = stmt.where(
            (WorkflowRun.updated_at < updated_at)
            | ((WorkflowRun.updated_at == updated_at) & (WorkflowRun.id < row_id))
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id)).limit(
                    limit + 1
                )
            )
        )
        .scalars()
        .all()
    )
    page = rows[:limit]
    items = [await _list_item_out(db, row) for row in page]
    next_cursor = _encode_cursor(page[-1]) if len(rows) > limit and page else None
    await db.commit()
    return StoryboardRunListOut(items=items, next_cursor=next_cursor)


@router.get("/{run_id}", response_model=StoryboardRunOut)
async def get_storyboard(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.patch(
    "/{run_id}", response_model=StoryboardRunOut, dependencies=[Depends(verify_csrf)]
)
async def patch_storyboard(
    run_id: str,
    body: StoryboardPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    md = _run_metadata(run)
    patch: dict[str, Any] = {}
    if body.title is not None:
        run.title = body.title.strip()
    if body.idea is not None:
        run.user_prompt = body.idea.strip()
    if body.style is not None:
        patch["style"] = body.style.strip()
    if body.script is not None:
        old_script = str(md.get("script") or "")
        new_script = body.script.strip()
        patch["script"] = new_script
        if new_script != old_script:
            patch["script_revision"] = int(md.get("script_revision") or 0) + 1
            if body.script_confirmed is None:
                patch["script_confirmed"] = False
    if body.script_confirmed is not None:
        patch["script_confirmed"] = body.script_confirmed
        if body.script_confirmed:
            patch["script_approved_revision"] = int(
                patch.get("script_revision", md.get("script_revision") or 0)
            )
            patch["script_approved_at"] = _now().isoformat()
    for key in ("aspect_ratio", "resolution", "model"):
        value = getattr(body, key)
        if value is not None:
            patch[key] = value.strip()
    if body.generate_audio is not None:
        patch["generate_audio"] = body.generate_audio
    if "seed" in body.model_fields_set:
        patch["seed"] = body.seed
    if body.current_stage is not None:
        run.current_step = body.current_stage.strip() or run.current_step
    if patch:
        _merge_run_metadata(run, patch)
    if run.status == "draft" and (run.user_prompt or patch.get("script")):
        run.status = "in_progress"
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_event(
        user.id, run.id, "storyboard.updated", {"run": out.model_dump(mode="json")}
    )
    return out


@router.delete("/{run_id}", dependencies=[Depends(verify_csrf)])
async def delete_storyboard(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    deleted_at = _now()
    run.deleted_at = deleted_at
    if run.conversation_id:
        conv = (
            await db.execute(
                select(Conversation).where(
                    Conversation.id == run.conversation_id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conv is not None:
            conv.deleted_at = deleted_at
    await db.commit()
    await _publish_storyboard_event(user.id, run.id, "storyboard.deleted", {})
    return {"ok": True}


@router.post(
    "/{run_id}/assets",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def add_asset(
    run_id: str,
    body: StoryboardAssetCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = WorkflowStep(
        workflow_run_id=run.id,
        step_key=_asset_step_key(new_uuid7()),
        status="waiting_input",
        input_json={
            "kind": body.kind,
            "name": body.name.strip(),
            "role": body.role.strip(),
            "description": body.description.strip(),
            "continuity": body.continuity.strip(),
            "revision": 1,
        },
        output_json={},
    )
    db.add(step)
    run.current_step = "assets"
    run.status = "in_progress"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.patch(
    "/{run_id}/assets/{step_id}",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_asset(
    run_id: str,
    step_id: str,
    body: StoryboardAssetPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="asset", lock=True)
    data = dict(step.input_json or {})
    changed = False
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        clean = value.strip() if isinstance(value, str) else value
        if data.get(key) != clean:
            data[key] = clean
            changed = True
    if changed:
        data["revision"] = int(data.get("revision") or 1) + 1
        step.input_json = data
        if step.status == "approved":
            step.status = (
                "ready" if (step.output_json or {}).get("image_id") else "waiting_input"
            )
            step.approved_at = None
            out_json = dict(step.output_json or {})
            out_json.pop("approved_at", None)
            step.output_json = out_json
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/assets/{step_id}/generate",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_asset(
    run_id: str,
    step_id: str,
    body: StoryboardGenerateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="asset", lock=True)
    prompt = _asset_prompt(run, step, body.prompt)
    task = await _create_storyboard_image_task(
        db=db,
        user=user,
        run=run,
        step=step,
        prompt=prompt,
        attachment_ids=[],
        purpose="asset",
    )
    step.status = "generating"
    step.task_ids = [task.generation_id]
    step.output_json = {
        **(step.output_json or {}),
        "prompt": prompt,
        "generation_id": task.generation_id,
        "image_id": None,
        "approved_at": None,
        "error_code": None,
        "error_message": None,
    }
    step.approved_at = None
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_image_task(db=db, user_id=user.id, task=task)
    await _publish_storyboard_event(
        user.id,
        run.id,
        "storyboard.asset_generating",
        {"asset_id": step.id, "generation_id": task.generation_id},
    )
    return out


@router.post(
    "/{run_id}/assets/{step_id}/approve",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_asset(
    run_id: str,
    step_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="asset", lock=True)
    await _sync_storyboard_outputs(db, run)
    out_json = dict(step.output_json or {})
    if not out_json.get("image_id"):
        raise _http(
            "asset_image_required", "generate an asset image before approval", 422
        )
    now = _now()
    step.status = "approved"
    step.approved_at = now
    step.approved_by = user.id
    out_json["approved_at"] = now.isoformat()
    step.output_json = out_json
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_event(
        user.id, run.id, "storyboard.asset_ready", {"asset_id": step.id}
    )
    return out


@router.delete(
    "/{run_id}/assets/{step_id}",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def delete_asset(
    run_id: str,
    step_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="asset", lock=True)
    await db.delete(step)
    shots = [
        s for s in await _load_steps(db, run.id, lock=True) if _step_kind(s) == "shot"
    ]
    for shot in shots:
        data = dict(shot.input_json or {})
        asset_ids = [
            asset_id for asset_id in data.get("asset_ids", []) if asset_id != step_id
        ]
        if asset_ids != data.get("asset_ids", []):
            data["asset_ids"] = asset_ids
            shot.input_json = data
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/shots/rebuild",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def rebuild_shots(
    run_id: str,
    body: StoryboardShotsRebuildIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    existing = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "shot"]
    if body.replace:
        for shot in existing:
            await db.delete(shot)
        await db.flush()
        existing = []
    md = _run_metadata(run)
    shots = (
        body.shots
        if body.shots is not None
        else _shots_from_script(str(md.get("script") or run.user_prompt))
    )
    offset = len(existing)
    for idx, item in enumerate(shots, start=1 + offset):
        data = _shot_input_from_body(item, index=idx)
        data["asset_ids"] = await _validate_asset_ids(
            db, run, data.get("asset_ids") or []
        )
        step = WorkflowStep(
            workflow_run_id=run.id,
            step_key=_shot_step_key(new_uuid7()),
            status="draft",
            input_json=data,
            output_json={},
        )
        db.add(step)
    run.current_step = "shots"
    run.status = "in_progress"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/shots",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def add_shot(
    run_id: str,
    body: StoryboardShotCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    shots = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "shot"]
    data = _shot_input_from_body(body, index=len(shots) + 1)
    data["asset_ids"] = await _validate_asset_ids(db, run, data.get("asset_ids") or [])
    step = WorkflowStep(
        workflow_run_id=run.id,
        step_key=_shot_step_key(new_uuid7()),
        status="draft",
        input_json=data,
        output_json={},
    )
    db.add(step)
    run.current_step = "shots"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.patch(
    "/{run_id}/shots/{step_id}",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_shot(
    run_id: str,
    step_id: str,
    body: StoryboardShotPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    data = _shot_input_from_body(body, existing=dict(step.input_json or {}))
    data["asset_ids"] = await _validate_asset_ids(db, run, data.get("asset_ids") or [])
    before_hash = _short_hash(step.input_json or {})
    after_hash = _short_hash(data)
    step.input_json = data
    if before_hash != after_hash and step.status in {
        "keyframe_ready",
        "keyframe_approved",
        "generating",
        "done",
    }:
        step.status = "approved"
        out_json = _clear_shot_video_output(dict(step.output_json or {}))
        out_json.pop("keyframe_approved_at", None)
        step.output_json = out_json
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/shots/{step_id}/approve",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_shot(
    run_id: str,
    step_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    if _rank_status(step.status) < _rank_status("approved"):
        step.status = "approved"
    step.approved_at = _now()
    step.approved_by = user.id
    run.current_step = "shots"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/shots/{step_id}/keyframe",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_shot_keyframe(
    run_id: str,
    step_id: str,
    body: StoryboardGenerateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    if _rank_status(step.status) < _rank_status("approved"):
        raise _http(
            "shot_not_approved", "approve the shot before keyframe generation", 422
        )
    asset_ids = await _validate_asset_ids(
        db,
        run,
        (step.input_json or {}).get("asset_ids") or [],
        require_approved=True,
    )
    assets = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "asset"]
    assets_by_id = {asset.id: asset for asset in assets}
    attachment_ids = _clean_string_list(
        [
            dict(assets_by_id[asset_id].output_json or {}).get("image_id")
            for asset_id in asset_ids
            if asset_id in assets_by_id
        ]
    )
    source_hash = _shot_source_hash(step, assets_by_id)
    prompt = _shot_keyframe_prompt(run, step, assets_by_id, body.prompt)
    task = await _create_storyboard_image_task(
        db=db,
        user=user,
        run=run,
        step=step,
        prompt=prompt,
        attachment_ids=attachment_ids,
        purpose="keyframe",
    )
    inp = dict(step.input_json or {})
    inp["keyframe_prompt"] = prompt
    inp["keyframe_source_hash"] = source_hash
    step.input_json = inp
    step.status = "keyframe_generating"
    step.task_ids = [task.generation_id]
    step.output_json = {
        **_clear_shot_video_output(dict(step.output_json or {})),
        "keyframe_generation_id": task.generation_id,
        "keyframe_image_id": None,
        "keyframe_approved_at": None,
        "error_code": None,
        "error_message": None,
    }
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_image_task(db=db, user_id=user.id, task=task)
    await _publish_storyboard_event(
        user.id,
        run.id,
        "storyboard.keyframe_generating",
        {"shot_id": step.id, "generation_id": task.generation_id},
    )
    return out


@router.post(
    "/{run_id}/shots/keyframes/generate-all",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_all_keyframes(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    await _sync_storyboard_outputs(db, run)
    steps = await _load_steps(db, run.id, lock=True)
    assets = [step for step in steps if _step_kind(step) == "asset"]
    shots = [step for step in steps if _step_kind(step) == "shot"]
    assets_by_id = {asset.id: asset for asset in assets}
    not_approved = [
        shot.id
        for shot in shots
        if _rank_status(shot.status) < _rank_status("approved")
        or shot.status in {"keyframe_generating", "generating"}
    ]
    if not_approved:
        raise _http(
            "shots_not_approved",
            "all shots must be approved and idle before batch keyframe generation",
            422,
            shot_ids=not_approved,
        )
    candidates = [
        shot
        for shot in shots
        if (
            not dict(shot.output_json or {}).get("keyframe_image_id")
            or dict(shot.input_json or {}).get("keyframe_source_hash")
            != _shot_source_hash(shot, assets_by_id)
        )
    ]
    planned: list[tuple[WorkflowStep, list[str], str, str]] = []
    for shot in candidates:
        asset_ids = await _validate_asset_ids(
            db,
            run,
            (shot.input_json or {}).get("asset_ids") or [],
            require_approved=True,
        )
        attachment_ids = _clean_string_list(
            [
                dict(assets_by_id[asset_id].output_json or {}).get("image_id")
                for asset_id in asset_ids
                if asset_id in assets_by_id
            ]
        )
        source_hash = _shot_source_hash(shot, assets_by_id)
        prompt = _shot_keyframe_prompt(run, shot, assets_by_id, None)
        planned.append((shot, attachment_ids, source_hash, prompt))
    tasks: list[tuple[WorkflowStep, StoryboardImageTask]] = []
    for shot, attachment_ids, source_hash, prompt in planned:
        task = await _create_storyboard_image_task(
            db=db,
            user=user,
            run=run,
            step=shot,
            prompt=prompt,
            attachment_ids=attachment_ids,
            purpose="keyframe",
        )
        inp = dict(shot.input_json or {})
        inp["keyframe_prompt"] = prompt
        inp["keyframe_source_hash"] = source_hash
        shot.input_json = inp
        shot.status = "keyframe_generating"
        shot.task_ids = [task.generation_id]
        shot.output_json = {
            **_clear_shot_video_output(dict(shot.output_json or {})),
            "keyframe_generation_id": task.generation_id,
            "keyframe_image_id": None,
            "keyframe_approved_at": None,
            "error_code": None,
            "error_message": None,
        }
        tasks.append((shot, task))
    run = await _get_run(db, user_id=user.id, run_id=run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_image_tasks(
        db=db,
        user_id=user.id,
        tasks=[task for _shot, task in tasks],
    )
    for shot, task in tasks:
        await _publish_storyboard_event(
            user.id,
            run.id,
            "storyboard.keyframe_generating",
            {"shot_id": shot.id, "generation_id": task.generation_id},
        )
    return out


@router.post(
    "/{run_id}/shots/{step_id}/keyframe/approve",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_keyframe(
    run_id: str,
    step_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    await _sync_storyboard_outputs(db, run)
    assets = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "asset"]
    source_hash = _shot_source_hash(step, {asset.id: asset for asset in assets})
    inp = dict(step.input_json or {})
    out_json = dict(step.output_json or {})
    if not out_json.get("keyframe_image_id"):
        raise _http("keyframe_required", "generate a keyframe before approval", 422)
    if inp.get("keyframe_source_hash") != source_hash:
        raise _http(
            "keyframe_stale", "keyframe is stale; regenerate before approval", 422
        )
    now = _now()
    step.status = "keyframe_approved"
    step.approved_at = now
    step.approved_by = user.id
    out_json["keyframe_approved_at"] = now.isoformat()
    step.output_json = out_json
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_event(
        user.id, run.id, "storyboard.keyframe_ready", {"shot_id": step.id}
    )
    return out


@router.post(
    "/{run_id}/shots/{step_id}/submit",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def submit_shot(
    run_id: str,
    step_id: str,
    body: StoryboardSubmitShotIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    if getattr(user, "account_mode", "wallet") != "wallet":
        raise _http(
            "account_mode_forbidden", "video generation requires wallet mode", 403
        )
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    await _sync_storyboard_outputs(db, run)
    assets = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "asset"]
    source_hash = _shot_source_hash(step, {asset.id: asset for asset in assets})
    inp = dict(step.input_json or {})
    out_json = dict(step.output_json or {})
    keyframe_id = out_json.get("keyframe_image_id")
    if step.status != "keyframe_approved" or not keyframe_id:
        raise _http(
            "keyframe_not_approved", "approve the keyframe before video submission", 422
        )
    if inp.get("keyframe_source_hash") != source_hash:
        raise _http(
            "keyframe_stale", "keyframe is stale; regenerate before submission", 422
        )
    md = _run_metadata(run)
    prompt = (
        body.prompt
        or inp.get("keyframe_prompt")
        or inp.get("visual")
        or run.user_prompt
    )
    idempotency_key, submission_fingerprint = _resolve_storyboard_video_idempotency_key(
        run_id=run.id,
        step=step,
        keyframe_image_id=str(keyframe_id),
        requested_key=body.idempotency_key,
    )
    out_json["video_submission"] = {
        "fingerprint": submission_fingerprint,
        "idempotency_key": idempotency_key,
        "created_at": _now().isoformat(),
    }
    out_json.update({"error_code": None, "error_message": None})
    step.output_json = out_json
    step.status = "generating"
    run.current_step = "videos"
    await db.flush()
    video_body = VideoCreateIn.model_validate(
        {
            "action": "i2v",
            "model": str(md.get("model") or STORYBOARD_DEFAULT_MODEL),
            "prompt": str(prompt)[:MAX_PROMPT_CHARS],
            "input_image_id": str(keyframe_id),
            "duration_s": body.duration_s
            or int(inp.get("duration_s") or STORYBOARD_DEFAULT_DURATION_S),
            "resolution": str(md.get("resolution") or STORYBOARD_DEFAULT_RESOLUTION),
            "aspect_ratio": str(
                md.get("aspect_ratio") or STORYBOARD_DEFAULT_ASPECT_RATIO
            ),
            "generate_audio": bool(md.get("generate_audio", True)),
            "seed": md.get("seed") if isinstance(md.get("seed"), int) else None,
            "watermark": False,
            "idempotency_key": idempotency_key,
        }
    )
    video_out = await _create_video_generation_record(
        db,
        video_body,
        user,
        request=request,
        workflow_metadata={
            "workflow_type": STORYBOARD_WORKFLOW_TYPE,
            "workflow_run_id": run.id,
            "workflow_step_key": step.step_key,
            "storyboard_purpose": "shot_video",
            "storyboard_keyframe_image_id": str(keyframe_id),
            "storyboard_video_submission_fingerprint": submission_fingerprint,
            "source": "storyboard",
            "action_source": "storyboard_video",
        },
    )
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    step.status = "generating"
    step.task_ids = [video_out.id]
    step.output_json = {
        **(step.output_json or {}),
        "video_generation_id": video_out.id,
        "error_code": None,
        "error_message": None,
    }
    run.current_step = "videos"
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_event(
        user.id,
        run.id,
        "storyboard.shot_submitted",
        {"shot_id": step.id, "video_generation_id": video_out.id},
    )
    return out


@router.post(
    "/{run_id}/shots/submit-all",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def submit_all_shots(
    run_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id)
    out = await _build_run_out(db, run)
    candidates = [
        shot
        for shot in out.shots
        if shot.status == "keyframe_approved"
        and shot.keyframe_image_id
        and not shot.keyframe_stale
    ]
    for shot in candidates:
        await submit_shot(run_id, shot.id, StoryboardSubmitShotIn(), request, user, db)
    run = await _get_run(db, user_id=user.id, run_id=run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/assemble",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def assemble_storyboard(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    await _sync_storyboard_outputs(db, run)
    steps = await _load_steps(db, run.id, lock=True)
    shots = [step for step in steps if _step_kind(step) == "shot"]
    _normalize_shot_indexes(shots)
    if not shots:
        raise _http("shots_required", "create shots before assembly", 422)
    not_done = [shot.id for shot in shots if shot.status != "done"]
    if not_done:
        raise _http(
            "shots_not_done",
            "all shots must be done before assembly",
            422,
            shot_ids=not_done,
        )
    ordered = sorted(shots, key=lambda s: int((s.input_json or {}).get("index") or 0))
    segment_ids = [
        str((shot.output_json or {}).get("video_generation_id"))
        for shot in ordered
        if (shot.output_json or {}).get("video_generation_id")
    ]
    if len(segment_ids) != len(ordered):
        raise _http(
            "segment_missing", "one or more shots are missing generated video ids", 422
        )
    assembly = await _assembly_step(db, run, lock=True)
    assembly_fingerprint = _storyboard_assembly_fingerprint(segment_ids)
    assembly_idempotency_key = _storyboard_assembly_idempotency_key(
        run_id=run.id,
        fingerprint=assembly_fingerprint,
    )
    current_output = dict(assembly.output_json or {})
    attempt_now = _now()
    if _assembly_request_is_replay(
        assembly,
        current_output,
        assembly_fingerprint,
        now=attempt_now,
    ):
        out = await _build_run_out(db, run)
        await db.commit()
        return out

    stale_recovery = current_output.get(
        "assembly_fingerprint"
    ) == assembly_fingerprint and _assembly_attempt_is_stale(
        assembly,
        current_output,
        now=attempt_now,
    )
    previous_attempt_token = current_output.get("assembly_attempt_token")
    if not isinstance(previous_attempt_token, str) or not previous_attempt_token:
        previous_attempt_token = None
    raw_recovery_count = current_output.get("assembly_recovery_count")
    recovery_count = (
        raw_recovery_count
        if isinstance(raw_recovery_count, int) and raw_recovery_count >= 0
        else 0
    )
    if stale_recovery:
        recovery_count += 1

    attempt_token = new_uuid7()
    if assembly.status != "compositing":
        assembly.status = "waiting"
    lease_expires_at = attempt_now + timedelta(
        seconds=STORYBOARD_ASSEMBLY_WAITING_LEASE_S
    )
    assembly.output_json = {
        **current_output,
        "segment_ids": segment_ids,
        "assembly_fingerprint": assembly_fingerprint,
        "assembly_idempotency_key": assembly_idempotency_key,
        "assembly_attempt_token": attempt_token,
        "assembly_enqueued_at": attempt_now.isoformat(),
        "assembly_claimed_at": None,
        "assembly_heartbeat_at": None,
        "assembly_lease_expires_at": lease_expires_at.isoformat(),
        "assembly_completed_at": None,
        "assembly_recovery_count": recovery_count,
        "assembly_recovery_reason": "lease_expired" if stale_recovery else None,
        "assembly_superseded_attempt_token": (
            previous_attempt_token if stale_recovery else None
        ),
        "video_id": None,
        "error_code": None,
        "error_message": None,
    }
    payload: dict[str, Any] = {
        "task_id": run.id,
        "run_id": run.id,
        "user_id": user.id,
        "kind": "storyboard_assembly",
        "assembly_fingerprint": assembly_fingerprint,
        "assembly_idempotency_key": assembly_idempotency_key,
        "assembly_attempt_token": attempt_token,
        "assembly_lease_expires_at": lease_expires_at.isoformat(),
        "assembly_recovered": stale_recovery,
    }
    outbox = OutboxEvent(kind="storyboard_assembly", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    outbox_id = str(outbox.id)
    payload["outbox_id"] = outbox_id
    outbox.payload = dict(payload)
    assembly.task_ids = [outbox.id]
    run.current_step = "assembly"
    out = await _build_run_out(db, run)
    await db.commit()
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "run_storyboard_assembly",
            run.id,
            attempt_token,
            _job_id=arq_job_id("storyboard_assembly", run.id, outbox_id),
        )
    except Exception:
        logger.warning(
            "storyboard assembly enqueue failed run=%s", run.id, exc_info=True
        )
    await _publish_storyboard_event(
        user.id,
        run.id,
        "storyboard.assembling",
        {
            "segment_ids": segment_ids,
            "assembly_fingerprint": assembly_fingerprint,
            "assembly_attempt_token": attempt_token,
            "recovered": stale_recovery,
        },
    )
    return out


@router.delete(
    "/{run_id}/shots/{step_id}",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def delete_shot(
    run_id: str,
    step_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    await db.delete(step)
    await db.flush()
    shots = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "shot"]
    _normalize_shot_indexes(shots)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/shots/{step_id}/move",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def move_shot(
    run_id: str,
    step_id: str,
    body: StoryboardShotMoveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    target = await _get_step(db, run, step_id, kind="shot", lock=True)
    shots = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "shot"]
    ordered = sorted(
        shots,
        key=lambda s: (int((s.input_json or {}).get("index") or 0), s.created_at, s.id),
    )
    pos = next((idx for idx, shot in enumerate(ordered) if shot.id == target.id), -1)
    new_pos = pos + body.direction
    if pos < 0 or new_pos < 0 or new_pos >= len(ordered):
        out = await _build_run_out(db, run)
        await db.commit()
        return out
    ordered[pos], ordered[new_pos] = ordered[new_pos], ordered[pos]
    for index, shot in enumerate(ordered, start=1):
        data = dict(shot.input_json or {})
        data["index"] = index
        shot.input_json = data
    out = await _build_run_out(db, run)
    await db.commit()
    return out
