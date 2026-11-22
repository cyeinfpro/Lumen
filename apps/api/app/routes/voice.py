"""Owner-scoped voice workbench, enabled by the instance provider settings."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import lumen_core.billing as billing
from lumen_core.model_entities.accounts import User
from lumen_core.voice_config import VOICE_WORKER_READY_KEY, load_voice_provider_config
from lumen_core.voice_domain import billable_chars, private_path, split_text
from lumen_core.voice_models import SpeechJob, SpeechSegment, VoiceProfile

from ..arq_pool import get_arq_pool
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..redis_client import get_redis
from ..voice_input import MAX_SOURCE_BYTES, VoiceInputError, normalize_voice_sample

router = APIRouter(tags=["voice"])
log = logging.getLogger(__name__)
MODEL = "qwen-audio-3.1-tts-plus"
PRICE_MICRO = MappingProxyType({"cn-beijing": 140, "ap-southeast-1": 219})
MAX_UPLOAD = MAX_SOURCE_BYTES
MAX_TEXT = 10_000
MAX_PRICE_UNITS_DAY = 60_000
MAX_VOICES_PER_DAY = 5
MAX_UNSAVED = 4
PREVIEW_TEXT = "清晨，第一缕阳光照进城市。让我们从这里出发。"
ACTIVE_WORK = frozenset({"queued", "running", "unknown"})
WRITE = (Depends(verify_csrf),)
DB = Annotated[AsyncSession, Depends(get_db)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


class QuoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)


class GenerateIn(QuoteIn):
    voice_id: str = Field(max_length=64)
    rate: float = Field(default=1, ge=0.5, le=2, allow_inf_nan=False)
    instruction: str | None = Field(default=None, max_length=240)


class SaveIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    save: bool = True


def _utc():
    return datetime.now(timezone.utc)


def _failure(code: str, status: int, message: str) -> NoReturn:
    raise HTTPException(status, detail={"error": {"code": code, "message": message}})


async def _need(db: AsyncSession):
    config = await load_voice_provider_config(db)
    if config is None:
        _failure("VOICE_DISABLED", 404, "请先在管理后台配置阿里百炼 API Key")
    return config


def _root():
    return Path(settings.storage_root) / "private-voice"


def _chars(text: str) -> int:
    return billable_chars(text)


def _chunks(text: str) -> list[str]:
    if not text.strip():
        return []
    try:
        return split_text(text)
    except ValueError:
        _failure("SPEECH_TEXT_INVALID", 422, "稿件超过限制或包含不能朗读的内容，请调整后再试")


async def _ready(config) -> bool:
    try:
        marker = await asyncio.wait_for(get_redis().get(VOICE_WORKER_READY_KEY), timeout=1)
    except Exception:
        return False
    expected = config.ready_marker
    return marker == expected or marker == expected.encode("ascii")


async def _require_worker(config, region: str, workspace: str):
    if (region, workspace) != (config.region, config.workspace_id):
        _failure("VOICE_PROVIDER_BINDING_CHANGED", 409, "此音色属于原百炼工作空间，请切回原配置或重新创建音色")
    if not await _ready(config):
        _failure("VOICE_WORKER_UNAVAILABLE", 503, "声音服务正在加载配置，稍后再试")


def _digest(*payload):
    wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def _check(existing, fingerprint):
    if existing.request_hash != fingerprint:
        _failure("VOICE_REQUEST_CONFLICT", 409, "同一请求标识不能用于不同的内容")


def _voice_out(row, preview=None):
    return {
        "id": str(row.id),
        "name": row.name,
        "state": row.state,
        "saved": row.saved_at is not None,
        "preview_url": preview,
    }


async def _lock_user(db, user):
    if (await db.execute(select(User.id).where(User.id == user.id, User.deleted_at.is_(None)).with_for_update())).scalar_one_or_none() is None:
        _failure("VOICE_DISABLED", 404, "声音工作台暂未开放")


async def _voice(db, user, voice_id, *, lock=False, allow_revoked=False):
    stmt = select(VoiceProfile).where(VoiceProfile.user_id == user.id, VoiceProfile.id == voice_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row or (row.revoked_at is not None and not allow_revoked):
        _failure("VOICE_NOT_FOUND", 404, "音色不存在")
    return row


async def _job(db, user, job_id, *, lock=False):
    stmt = select(SpeechJob).where(SpeechJob.user_id == user.id, SpeechJob.id == job_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        _failure("SPEECH_NOT_FOUND", 404, "配音任务不存在")
    return row


async def _segments_for_jobs(db, job_ids):
    if not job_ids:
        return {}
    latest = select(
        SpeechSegment.job_id, SpeechSegment.segment_no,
        func.max(SpeechSegment.revision).label("revision"),
    ).where(SpeechSegment.job_id.in_(job_ids)).group_by(
        SpeechSegment.job_id, SpeechSegment.segment_no,
    ).subquery()
    rows = (await db.execute(select(
        SpeechSegment.job_id, SpeechSegment.segment_no, SpeechSegment.revision,
        SpeechSegment.state, SpeechSegment.body,
    ).join(latest, (
        (SpeechSegment.job_id == latest.c.job_id)
        & (SpeechSegment.segment_no == latest.c.segment_no)
        & (SpeechSegment.revision == latest.c.revision)
    )).order_by(SpeechSegment.job_id, SpeechSegment.segment_no))).all()
    by_job = {}
    for segment in rows:
        by_job.setdefault(segment.job_id, []).append(segment)
    return by_job


async def _latest_segments(db, job_id):
    return (await _segments_for_jobs(db, [job_id])).get(job_id, [])


def _job_item(row, segments):
    return {
        "id": str(row.id), "voice_id": str(row.voice_id), "state": row.state, "kind": row.kind,
        "segments": [{"segment_no": s.segment_no, "revision": s.revision, "state": s.state} for s in segments],
        "download_url": f"/api/speech/jobs/{row.id}/audio" if row.artifact_key else None,
        "estimated_characters": sum(_chars(s.body) for s in segments),
    }


async def _job_out(db, row):
    return _job_item(row, await _latest_segments(db, row.id))


async def _wake():
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job("process_voice_work")
    except Exception:  # durable rows will be claimed by the worker cron
        log.warning("voice worker immediate wake unavailable")


def _latest_preview_id(owner_id, voice_id):
    return select(SpeechJob.id).where(
        SpeechJob.user_id == owner_id, SpeechJob.voice_id == voice_id,
        SpeechJob.kind == "preview", SpeechJob.artifact_key.is_not(None),
    ).order_by(SpeechJob.created_at.desc(), SpeechJob.id.desc()).limit(1)


def _preview_link(job_id):
    return f"/api/speech/jobs/{job_id}/audio" if job_id is not None else None


async def _preview_url(db, row):
    if row.revoked_at is not None:
        return None
    job_id = (await db.scalars(_latest_preview_id(row.user_id, row.id))).first()
    return _preview_link(job_id)


@router.get("/speech/capabilities")
async def capabilities(user: CurrentUser, db: DB):
    config = await load_voice_provider_config(db)
    region = config.region if config is not None else None
    ready = config is not None and await _ready(config)
    return {"enabled": config is not None, "ready": ready, "region": region,
            "price_micro_per_character": PRICE_MICRO.get(region),
            "preview_estimated_micro": _chars(PREVIEW_TEXT) * PRICE_MICRO[region] if region else None,
            "max_input_bytes": MAX_UPLOAD, "max_text_characters": MAX_TEXT}


@router.post("/speech/quote", dependencies=WRITE)
async def quote(payload: QuoteIn, user: CurrentUser, db: DB):
    region = (await _need(db)).region
    count = sum(_chars(part) for part in _chunks(payload.text.strip()))
    return {"characters": count, "estimated_micro": count * PRICE_MICRO[region], "currency": "CNY"}


@router.get("/voices")
async def list_voices(user: CurrentUser, db: DB):
    preview_id = _latest_preview_id(VoiceProfile.user_id, VoiceProfile.id).scalar_subquery()
    rows = (await db.execute(select(VoiceProfile, preview_id).where(
        VoiceProfile.user_id == user.id, VoiceProfile.revoked_at.is_(None),
    ).order_by(VoiceProfile.created_at.desc(), VoiceProfile.id.desc()).limit(100))).all()
    return {"items": [_voice_out(row, _preview_link(job_id)) for row, job_id in rows]}


def _write_sample(path: Path, data: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as dest:
            dest.write(data)
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@router.post("/voices", status_code=202, dependencies=WRITE)
async def enroll(
    request: Request,
    user: CurrentUser, db: DB, key: Key,
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form(min_length=1, max_length=80)],
    start_ms: Annotated[int, Form(ge=0, le=60_000)],
    end_ms: Annotated[int, Form(gt=0, le=60_000)],
    consent_kind: Annotated[Literal["self", "authorized"], Form()],
    consent_confirmed: Annotated[bool, Form()],
    channel: Annotated[int, Form(ge=0, le=1)] = 0,
):
    config = await _need(db)
    name = name.strip()
    if not consent_confirmed or not name:
        _failure("VOICE_CONSENT_REQUIRED", 422, "请填写名称并确认你有权使用这一段声音")
    try:
        sample = await file.read(MAX_UPLOAD + 1)
    finally:
        await file.close()
    if len(sample) > MAX_UPLOAD:
        _failure("VOICE_FILE_TOO_LARGE", 413, "录音文件不能超过 10 MB")
    fingerprint = _digest(name, start_ms, end_ms, channel, consent_kind, hashlib.sha256(sample).hexdigest())
    lookup = select(VoiceProfile).where(VoiceProfile.user_id == user.id, VoiceProfile.client_key == key)
    existing = (await db.scalars(lookup)).first()
    if existing:
        _check(existing, fingerprint)
        return _voice_out(existing)
    try:
        await _require_worker(config, config.region, config.workspace_id)
        capture_capacity = getattr(request.app.state, "voice_capture_capacity", None)
        if capture_capacity is None:
            _failure("VOICE_WORKER_UNAVAILABLE", 503, "声音服务暂不可用，请稍后重试")
        async with capture_capacity:
            normalized, quality = await normalize_voice_sample(sample, start_ms, end_ms, channel=channel)
    except VoiceInputError:
        _failure("VOICE_SAMPLE_INVALID", 422, "无法使用这段录音，请检查文件和选段后重试")
    await _lock_user(db, user)
    existing = (await db.scalars(lookup)).first()
    if existing:
        _check(existing, fingerprint)
        return _voice_out(existing)
    latest = await _need(db)
    if latest.revision != config.revision:
        _failure("VOICE_WORKER_UNAVAILABLE", 503, "声音配置已更新，请重试")
    region, workspace = latest.region, latest.workspace_id
    now = _utc()
    daily = await db.scalar(select(func.count()).select_from(VoiceProfile).where(
        VoiceProfile.user_id == user.id, VoiceProfile.created_at >= now - timedelta(days=1)))
    drafts = await db.scalar(select(func.count()).select_from(VoiceProfile).where(
        VoiceProfile.user_id == user.id, VoiceProfile.saved_at.is_(None), VoiceProfile.revoked_at.is_(None)))
    if (daily or 0) >= MAX_VOICES_PER_DAY or (drafts or 0) >= MAX_UNSAVED:
        _failure("VOICE_ENROLL_LIMIT", 429, "音色创建达到上限，先保存或删除未用音色")
    source_key = f"samples/{uuid.uuid4()}.wav"
    row = VoiceProfile(
        user_id=user.id, name=name, source_key=source_key, consent_kind=consent_kind,
        consent_at=now, draft_expires_at=now + timedelta(days=1),
        provider_prefix="lv" + uuid.uuid4().hex[:8], region=region, workspace_id=workspace,
        model=MODEL, client_key=key, request_hash=fingerprint, state="queued",
    )
    db.add(row)
    await db.flush()
    await asyncio.to_thread(_write_sample, private_path(_root(), source_key), normalized)
    await db.commit()
    await _wake()
    return {**_voice_out(row), "quality": quality}


@router.patch("/voices/{voice_id}", dependencies=WRITE)
async def save(voice_id: str, payload: SaveIn, user: CurrentUser, db: DB):
    row = await _voice(db, user, voice_id, lock=True)
    if not payload.name.strip():
        _failure("VOICE_NAME_EMPTY", 422, "请填写音色名称")
    preview = await _preview_url(db, row)
    if payload.save:
        if row.state != "ready" or preview is None:
            _failure("VOICE_PREVIEW_REQUIRED", 409, "请先完成音色试听")
        row.saved_at = row.saved_at or _utc()
    row.name = payload.name.strip()
    await db.commit()
    return _voice_out(row, preview)


@router.delete("/voices/{voice_id}", status_code=202, dependencies=WRITE)
async def revoke(voice_id: str, user: CurrentUser, db: DB):
    row = await _voice(db, user, voice_id, lock=True, allow_revoked=True)
    if row.revoked_at is None:
        row.revoked_at = _utc()
        row.state = "revoking"
        row.due_at = _utc()
        await db.commit()
        await _wake()
    return _voice_out(row)


async def _hold_segments(db, user, segments, region, price):
    try:
        for segment in segments:
            chars = _chars(segment.body)
            await billing.hold(
                db, str(user.id), chars * price, ref_type="speech_segment",
                ref_id=str(segment.id), idempotency_key=f"speech:hold:{segment.id}",
                meta={"characters": chars, "region": region, "model": MODEL},
            )
    except billing.BillingError as exc:
        if 400 <= exc.status_code < 500:
            _failure(exc.code, exc.status_code, "余额不足或账户当前不能生成配音")
        raise


async def _create_job(db, user, key, voice_id, text, rate, instruction, *, kind):
    text = text.strip()
    if not text:
        _failure("SPEECH_TEXT_EMPTY", 422, "请输入要朗读的文字")
    # Speech synthesis supports directives, but the launch leaves them disabled until live QA.
    if instruction and instruction.strip():
        _failure("SPEECH_INSTRUCTION_NOT_ENABLED", 422, "当前只开放语速设置")
    await _lock_user(db, user)
    fingerprint = _digest(str(voice_id), kind, text, rate)
    existing = (await db.scalars(select(SpeechJob).where(
        SpeechJob.user_id == user.id, SpeechJob.client_key == key))).first()
    if existing:
        _check(existing, fingerprint)
        return await _job_out(db, existing)
    voice = await _voice(db, user, voice_id, lock=True)
    if voice.state != "ready" or (kind == "speech" and voice.saved_at is None):
        _failure("VOICE_NOT_READY", 409, "音色还未就绪，请先完成试听并保存")
    if voice.saved_at is None and voice.draft_expires_at <= _utc():
        _failure("VOICE_DRAFT_EXPIRED", 409, "试听音色已过期，请重新创建")
    if kind == "preview":
        existing_preview = (await db.scalars(select(SpeechJob).where(
            SpeechJob.user_id == user.id, SpeechJob.voice_id == voice.id,
            SpeechJob.kind == "preview", SpeechJob.state.in_(tuple(ACTIVE_WORK | {"ready"})),
        ).order_by(SpeechJob.created_at.desc()).limit(1))).first()
        if existing_preview:
            return await _job_out(db, existing_preview)
    active = await db.scalar(select(func.count()).select_from(SpeechJob).where(
        SpeechJob.user_id == user.id, SpeechJob.state.in_(tuple(ACTIVE_WORK))))
    if (active or 0) >= 3:
        _failure("SPEECH_CONCURRENCY_LIMIT", 429, "请先等已有配音任务完成")
    await _require_worker(await _need(db), voice.region, voice.workspace_id)
    segments = _chunks(text)
    if not segments:
        _failure("SPEECH_TEXT_EMPTY", 422, "请输入要朗读的文字")
    await _check_daily(db, user, sum(_chars(part) for part in segments))
    row = SpeechJob(
        user_id=user.id, voice_id=voice.id, kind=kind, client_key=key,
        request_hash=fingerprint, params={"rate": rate, "price_micro_per_char": PRICE_MICRO[voice.region]}, state="queued",
        estimated_chars=sum(_chars(part) for part in segments),
    )
    db.add(row)
    await db.flush()
    rows = [SpeechSegment(job_id=row.id, segment_no=i, revision=1, body=part, state="queued")
            for i, part in enumerate(segments)]
    db.add_all(rows)
    await db.flush()
    await _hold_segments(db, user, rows, voice.region, PRICE_MICRO[voice.region])
    await db.commit()
    await _wake()
    return await _job_out(db, row)


async def _check_daily(db, user, add):
    bodies = await db.scalars(select(SpeechSegment.body).join(
        SpeechJob, SpeechJob.id == SpeechSegment.job_id).where(
        SpeechJob.user_id == user.id, SpeechSegment.created_at >= _utc() - timedelta(days=1)))
    if sum(_chars(value) for value in bodies) + add > MAX_PRICE_UNITS_DAY:
        _failure("SPEECH_DAILY_LIMIT", 429, "今日已达配音字符上限")


@router.post("/voices/{voice_id}/preview", status_code=202, dependencies=WRITE)
async def preview(voice_id: str, user: CurrentUser, db: DB, key: Key):
    return await _create_job(db, user, key, voice_id, PREVIEW_TEXT, 1, None, kind="preview")


@router.post("/speech/jobs", status_code=202, dependencies=WRITE)
async def generate(payload: GenerateIn, user: CurrentUser, db: DB, key: Key):
    return await _create_job(db, user, key, payload.voice_id, payload.text,
                             payload.rate, payload.instruction, kind="speech")


@router.get("/speech/jobs")
async def jobs(user: CurrentUser, db: DB):
    rows = (await db.scalars(select(SpeechJob).where(
        SpeechJob.user_id == user.id,
    ).order_by(SpeechJob.created_at.desc(), SpeechJob.id.desc()).limit(50))).all()
    segments = await _segments_for_jobs(db, [row.id for row in rows])
    return {"items": [_job_item(row, segments.get(row.id, [])) for row in rows]}


@router.get("/speech/jobs/{job_id}")
async def job_status(job_id: str, user: CurrentUser, db: DB):
    return await _job_out(db, await _job(db, user, job_id))


@router.get("/speech/jobs/{job_id}/audio", response_class=FileResponse)
async def download(job_id: str, user: CurrentUser, db: DB):
    job = await _job(db, user, job_id)
    if job.kind == "preview":
        await _voice(db, user, job.voice_id)
    if not job.artifact_key:
        _failure("SPEECH_AUDIO_ABSENT", 404, "音频尚未就绪")
    path = private_path(_root(), job.artifact_key)
    if not await asyncio.to_thread(path.is_file):
        _failure("SPEECH_AUDIO_ABSENT", 404, "音频尚未就绪")
    return FileResponse(
        path, media_type="audio/wav", filename=f"lumen-{job.id}.wav",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/speech/jobs/{job_id}/segments/{segment_no}/retry", status_code=202, dependencies=WRITE)
async def retry(job_id: str, segment_no: int, user: CurrentUser, db: DB, key: Key):
    _need(user)
    await _lock_user(db, user)
    # Worker settlement locks the same job before its voice. Match that order.
    row = await _job(db, user, job_id, lock=True)
    voice = await _voice(db, user, row.voice_id, lock=True)
    params = dict(row.params or {})
    retries = dict(params.get("retry_keys") or {})
    if key in retries:
        if retries[key] != segment_no:
            _failure("VOICE_REQUEST_CONFLICT", 409, "同一请求标识不能用于不同的段落")
        return await _job_out(db, row)
    segments = await _latest_segments(db, row.id)
    original = next((s for s in segments if s.segment_no == segment_no), None)
    if row.state not in {"failed", "ready"} or voice.state != "ready" or (
        original is None or original.state not in {"failed", "ready"}
    ):
        _failure("SPEECH_CANNOT_RETRY", 409, "该段仍在执行或结果待确认")
    if len(retries) >= 50:
        _failure("SPEECH_RETRY_LIMIT", 429, "此任务已达到重做上限")
    await _require_worker(await _need(db), voice.region, voice.workspace_id)
    await _check_daily(db, user, _chars(original.body))
    new = SpeechSegment(job_id=row.id, segment_no=segment_no, revision=original.revision + 1,
                        body=original.body, state="queued")
    db.add(new)
    await db.flush()
    price = params.get("price_micro_per_char")
    if type(price) is not int or price <= 0:
        _failure("SPEECH_PRICE_ABSENT", 409, "该任务缺少计费快照，不能重做")
    await _hold_segments(db, user, [new], voice.region, price)
    retries[key] = segment_no
    params["retry_keys"] = retries
    row.params = params
    row.state = "queued"
    row.due_at = _utc()
    row.lease_until = None
    row.lease_token = None
    await db.commit()
    await _wake()
    return await _job_out(db, row)
