"""Durable, fail-closed voice work. Redis only wakes jobs; the DB owns state."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import uuid
import wave
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select

from lumen_core.aliyun_speech import SpeechReceipt
from lumen_core.models import User
from lumen_core.voice_domain import (
    MAX_OUTPUT_BYTES,
    MAX_SAMPLE_BYTES,
    MODEL,
    SAMPLE_RATE,
    private_path,
)
from lumen_core.voice_models import SpeechJob, SpeechSegment, VoiceProfile

_LEASE = timedelta(minutes=5)
_RECONCILE = timedelta(seconds=25)
_ACTIVE_VOICE = ("queued", "enrolling", "polling", "unknown", "revoking")
_ACTIVE_JOB = ("queued", "running")
_MAX_PENDING_ATTEMPTS = 25
_MAX_DOWNLOAD_ATTEMPTS = 8


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _owned_voice(owner: str, voice_id: str):
    return select(VoiceProfile).where(
        VoiceProfile.id == voice_id, VoiceProfile.user_id == owner
    )


def _owned_job(owner: str, job_id: str):
    return select(SpeechJob).where(SpeechJob.id == job_id, SpeechJob.user_id == owner)


def _eligible(row: Any, now: datetime) -> bool:
    return _utc(row.due_at) <= now and (
        row.lease_until is None or _utc(row.lease_until) <= now
    )


def _unlock(row: Any, delay: timedelta = timedelta()) -> None:
    row.lease_token = None
    row.lease_until = None
    row.due_at = _now() + delay


def _retry(exc: Exception) -> timedelta:
    try:
        seconds = float(getattr(exc, "retry_after", 0) or 0)
    except (ValueError, TypeError):
        seconds = 0
    return timedelta(seconds=max(10, min(300, seconds)))


def _category(exc: Exception) -> str:
    category = getattr(exc, "category", "")
    code = getattr(exc, "code", "")
    if category == "retryable":
        return {"rate_limit": "rate_limited", "not_connected": "not_sent"}.get(code, "unknown")
    if category == "rejected" and code == "not_found":
        return "not_found"
    return category if category in {
        "not_sent", "throttled", "rate_limited", "rejected", "ambiguous",
        "unknown", "not_found", "invalid_response", "download",
    } else "unknown"


def _wav_pcm(data: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    if not 0 < len(data) <= MAX_OUTPUT_BYTES:
        raise ValueError("invalid_wav_size")
    with wave.open(io.BytesIO(data), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, sample_rate):
            raise ValueError("invalid_wav_format")
        frames = wav.getnframes()
        if frames < 1 or frames * 2 > MAX_OUTPUT_BYTES:
            raise ValueError("invalid_wav_frames")
        payload = wav.readframes(frames)
        if len(payload) != frames * 2:
            raise ValueError("truncated_wav")
        return payload


def _stream_pcm(part, budget: int, write) -> int:
    if not 0 < os.fstat(part.fileno()).st_size <= MAX_OUTPUT_BYTES:
        raise ValueError("invalid_wav_size")
    with wave.open(part, "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (
            1, 2, SAMPLE_RATE
        ):
            raise ValueError("invalid_wav_format")
        remaining = source.getnframes()
        size = remaining * 2
        if remaining < 1 or size > budget:
            raise ValueError("speech_artifact_too_large")
        while remaining:
            frames = min(remaining, 32_768)
            data = source.readframes(frames)
            if len(data) != frames * 2:
                raise ValueError("truncated_wav")
            write(data)
            remaining -= frames
    return size


def _join_pcm(root: Path, keys: list[str], output) -> None:
    total_bytes = 0
    output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
    for key in keys:
        with private_path(root, key).open("rb") as part:
            total_bytes += _stream_pcm(part, MAX_OUTPUT_BYTES - 44 - total_bytes, output.writeframesraw)
    if not total_bytes:
        raise ValueError("empty_speech_artifact")


def join_wav(root: Path, keys: list[str]) -> bytes:
    """In-memory adapter for tests; workers stream to a durable temporary file."""
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        _join_pcm(root, keys, output)
    return stream.getvalue()


@contextmanager
def _atomic_output(root: Path, key: str):
    path = private_path(root, key)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _save(root: Path, key: str, data: bytes) -> None:
    with _atomic_output(root, key) as handle:
        handle.write(data)


def _assemble(root: Path, keys: list[str], key: str) -> None:
    with _atomic_output(root, key) as handle:
        if len(keys) == 1:
            # Segment ingestion already validated this private artifact. Keep its
            # container metadata (including vendor-provided provenance tags).
            with private_path(root, keys[0]).open("rb") as part:
                _stream_pcm(part, MAX_OUTPUT_BYTES - 44, lambda _: None)
                part.seek(0)
                shutil.copyfileobj(part, handle, length=64 * 1024)
        else:
            with wave.open(handle, "wb") as output:
                _join_pcm(root, keys, output)


def _read_sample(root: Path, key: str) -> bytes:
    with private_path(root, key).open("rb") as handle:
        data = handle.read(MAX_SAMPLE_BYTES + 1)
    if len(data) > MAX_SAMPLE_BYTES:
        raise ValueError("sample_too_large")
    source_rate = 16_000
    pcm = _wav_pcm(data, sample_rate=source_rate)
    if not 3 * source_rate * 2 <= len(pcm) <= 20 * source_rate * 2:
        raise ValueError("invalid_sample_duration")
    return data


class VoicePipeline:
    def __init__(
        self, session_factory, client_for, media_root: Path, settle=None, *,
        owner_allowed=None, binding: tuple[str, str] | None = None,
    ):
        self._session = session_factory
        self._client_for = client_for
        self._root = Path(media_root)
        self._settle = settle
        self._allow_owner = owner_allowed
        self._binding = binding

    def _binding_matches(self, voice: VoiceProfile) -> bool:
        return self._binding is None or (voice.region, voice.workspace_id) == self._binding

    async def _owner_active(self, db, owner_id: str) -> bool:
        user = await db.get(User, owner_id)
        return bool(user is not None and user.deleted_at is None and (
            self._allow_owner is None or self._allow_owner(owner_id)
        ))

    async def _claim_voice(self, user_id: str, voice_id: str):
        async with self._session() as db, db.begin():
            voice = (
                await db.execute(_owned_voice(user_id, voice_id).with_for_update())
            ).scalar_one_or_none()
            now = _now()
            if voice is None or not self._binding_matches(voice):
                return None
            expired = voice.saved_at is None and _utc(voice.draft_expires_at) <= now
            if (expired or not await self._owner_active(db, user_id)) and voice.state != "revoked":
                voice.revoked_at = voice.revoked_at or now
                voice.state = "revoking"
            purge = voice.state in {"ready", "revoked"} and voice.source_key is not None
            if (voice.state not in _ACTIVE_VOICE and not purge) or not _eligible(voice, now):
                return None
            token = str(uuid.uuid4())
            voice.lease_token, voice.lease_until = token, now + _LEASE
            voice.attempts += 1
            action = "purge" if purge else voice.state
            if action == "queued":
                voice.state = "enrolling"
            return voice, token, action

    async def enroll(self, user_id: str, voice_id: str) -> None:
        claim = await self._claim_voice(user_id, voice_id)
        if claim is None:
            return
        voice, token, action = claim
        state, code, remote_id, drop_source = "unknown", None, None, False
        delay = _RECONCILE
        provider_started = False
        try:
            if action == "purge":
                state, drop_source = voice.state, True
            elif voice.model != MODEL:
                raise ValueError("invalid_voice_model")
            elif action == "revoking" and not voice.provider_voice_id and not voice.enrollment_sent:
                state, drop_source = "revoked", True
            else:
                client = self._client_for(voice)
                if action == "queued":
                    if not voice.source_key:
                        raise ValueError("sample_missing")
                    wav = await asyncio.to_thread(_read_sample, self._root, voice.source_key)
                    async with self._session() as db, db.begin():
                        row = (
                            await db.execute(_owned_voice(user_id, voice_id).with_for_update())
                        ).scalar_one_or_none()
                        if row is None or row.lease_token != token:
                            return
                        if row.revoked_at or not await self._owner_active(db, user_id):
                            row.revoked_at = row.revoked_at or _now()
                            row.state = "revoking"
                            _unlock(row)
                            return
                        row.enrollment_sent = True
                    provider_started = True
                    remote_id = await client.create_voice(wav, voice.provider_prefix, "zh")
                    state, delay = "polling", timedelta()
                elif action == "revoking":
                    remote_id = voice.provider_voice_id
                    if not remote_id and voice.enrollment_sent:
                        remote_id = await client.find_voice(voice.provider_prefix)
                        if not remote_id:
                            code = "voice_cleanup_unconfirmed"
                    if remote_id:
                        try:
                            await client.delete_voice(remote_id)
                        except Exception as exc:
                            if _category(exc) != "not_found":
                                raise
                    state = "revoking" if code else "revoked"
                    drop_source = True
                elif voice.provider_voice_id:
                    remote_id = voice.provider_voice_id
                    raw = await client.query_voice(remote_id)
                    if raw == "OK":
                        state, drop_source = "ready", True
                    elif raw == "DEPLOYING" and voice.attempts < _MAX_PENDING_ATTEMPTS:
                        state = "polling"
                    elif raw in ("FAILED", "FAIL", "UNDEPLOYED"):
                        state, code = "failed", "voice_enrollment_failed"
                    else:
                        state, code, delay = "unknown", "voice_provider_unconfirmed", timedelta(minutes=10)
                elif voice.enrollment_sent:
                    remote_id = await client.find_voice(voice.provider_prefix)
                    if remote_id:
                        state, delay = "polling", timedelta()
                    else:
                        code, delay = "voice_enrollment_unconfirmed", timedelta(minutes=10)
                else:
                    # A crashed preflight has not marked any possible external send.
                    state, delay = "queued", timedelta()
        except Exception as exc:
            category = _category(exc)
            code = "voice_provider_" + category
            if action == "revoking":
                state, delay, drop_source = "revoking", timedelta(minutes=10), True
            elif action == "queued" and (
                not provider_started or category in {"rejected", "not_found", "not_sent", "throttled", "rate_limited"}
            ):
                if provider_started and voice.attempts < _MAX_PENDING_ATTEMPTS and category in {
                    "not_sent", "throttled", "rate_limited",
                }:
                    state, delay = "queued", _retry(exc)
                else:
                    state = "failed"
            else:
                state, delay = "unknown", timedelta(minutes=10)
        source_key = None
        async with self._session() as db, db.begin():
            row = (
                await db.execute(_owned_voice(user_id, voice_id).with_for_update())
            ).scalar_one_or_none()
            if row is None or row.lease_token != token:
                return
            if remote_id:
                row.provider_voice_id = remote_id
            if row.revoked_at is not None and state != "revoked":
                state = "revoking"
            if state == "queued" or (action == "queued" and state == "failed"):
                row.enrollment_sent = False
            row.state, row.last_error_code = state, code
            if state == "revoked":
                row.provider_voice_id = None
            if drop_source:
                source_key = row.source_key
            _unlock(row, delay)
        if source_key:
            try:
                await asyncio.to_thread(private_path(self._root, source_key).unlink, missing_ok=True)
            except (OSError, ValueError):
                return
            async with self._session() as db, db.begin():
                row = (
                    await db.execute(_owned_voice(user_id, voice_id).with_for_update())
                ).scalar_one_or_none()
                if row is not None and row.source_key == source_key:
                    row.source_key = None

    async def revoke(self, user_id: str, voice_id: str) -> None:
        async with self._session() as db, db.begin():
            row = (
                await db.execute(_owned_voice(user_id, voice_id).with_for_update())
            ).scalar_one_or_none()
            if row is None or row.state == "revoked":
                return
            row.revoked_at = row.revoked_at or _now()
            row.state, row.due_at = "revoking", _now()
            # Keep any live lease. Its receipt must still be stored for deletion.
        await self.enroll(user_id, voice_id)

    async def synthesize(self, user_id: str, job_id: str) -> None:
        for _ in range(6):
            if not await self._synthesize_step(user_id, job_id):
                return

    async def _synthesize_step(self, user_id: str, job_id: str) -> bool:
        if self._settle is None:
            return False  # never send a paid request without settlement wiring
        token, action, job, segment, voice = await self._next(user_id, job_id)
        if token is None:
            return False
        receipt = None
        key = None
        code = None
        delay = timedelta()
        next_state = None
        provider_started = False
        try:
            if action == "join":
                keys = segment
                key = f"outputs/{job_id}/final-{token}.wav"
                await asyncio.to_thread(_assemble, self._root, keys, key)
            elif action == "send":
                client = self._client_for(voice)
                provider_started = True
                receipt = await client.synthesize(
                    segment.body, voice.provider_voice_id,
                    rate=job.params.get("rate", 1.0),
                    instruction=job.params.get("instruction"),
                )
                next_state = "download"
            elif action == "download":
                client = self._client_for(voice)
                receipt = SpeechReceipt(
                    audio_url=segment.result_url, characters=segment.usage_chars,
                    request_id=segment.provider_request_id,
                )
                data = await client.download(receipt)
                await asyncio.to_thread(_wav_pcm, data)
                key = f"outputs/{job_id}/{segment.id}-{token}.wav"
                await asyncio.to_thread(_save, self._root, key, data)
                next_state = "ready"
        except Exception as exc:
            category = _category(exc)
            code = "speech_" + category
            if action == "join":
                code = "speech_assembly_failed"
            elif action == "send":
                if not provider_started:
                    next_state, code = "failed", "speech_provider_binding_unavailable"
                elif category in {"not_sent", "throttled", "rate_limited"} and segment.attempts < 5:
                    next_state, delay = "queued", _retry(exc)
                elif category in {"not_sent", "throttled", "rate_limited", "rejected", "not_found"}:
                    next_state = "failed"
                else:
                    next_state = "unknown"
            else:
                next_state = "download" if segment.attempts < _MAX_DOWNLOAD_ATTEMPTS else "failed"
                delay = _retry(exc)
                code = "speech_download_failed"
        if action == "send" and next_state == "download":
            if not await self._remember_receipt(user_id, job_id, token, segment.id, receipt):
                return False
            action = "settle"
        elif action == "settle":
            next_state = "download"
        await self._finish(
            user_id, job_id, token, action, segment, next_state, key, code, delay
        )
        return action != "join" and not delay

    async def _remember_receipt(self, owner, job_id, token, segment_id, receipt):
        """Record known upstream success even when local settlement is unavailable."""
        async with self._session() as db, db.begin():
            job = (
                await db.execute(_owned_job(owner, job_id).with_for_update())
            ).scalar_one_or_none()
            if job is None or job.lease_token != token:
                return False
            segment = await db.get(SpeechSegment, segment_id, with_for_update=True)
            if segment is None or segment.job_id != job.id or segment.state != "sending":
                return False
            segment.result_url = receipt.audio_url
            segment.provider_request_id = receipt.request_id
            segment.usage_chars = receipt.characters
            return True

    async def _next(self, user_id: str, job_id: str):
        empty = (None, None, None, None, None)
        async with self._session() as db, db.begin():
            # Stable lock ordering: every speech mutation locks job first, then voice.
            job = (
                await db.execute(_owned_job(user_id, job_id).with_for_update())
            ).scalar_one_or_none()
            if job is None or job.state not in _ACTIVE_JOB or not _eligible(job, _now()):
                return empty
            voice = (
                await db.execute(_owned_voice(user_id, job.voice_id).with_for_update())
            ).scalar_one_or_none()
            # Do not lease or mutate older bindings when the admin switches scope.
            if voice is not None and not self._binding_matches(voice):
                return empty
            rows = (
                await db.execute(
                    select(SpeechSegment).where(SpeechSegment.job_id == job.id).order_by(
                        SpeechSegment.segment_no, SpeechSegment.revision.desc()
                    )
                )
            ).scalars().all()
            latest = {}
            for row in rows:
                latest.setdefault(row.segment_no, row)
            if not latest:
                job.state, job.last_error_code = "failed", "speech_no_segments"
                return empty
            disabled = (
                voice is None or not await self._owner_active(db, user_id)
                or voice.revoked_at is not None or voice.state != "ready"
                or not voice.provider_voice_id
                or (job.kind == "speech" and voice.saved_at is None)
                or (voice.saved_at is None and _utc(voice.draft_expires_at) <= _now())
            )
            pending_receipt = next(
                (row for row in latest.values() if row.state == "sending" and row.result_url),
                None,
            )
            if pending_receipt is not None:
                # A paid response already exists. Finish its local accounting
                # before considering cancellation or any new upstream work.
                token = str(uuid.uuid4())
                job.state, job.lease_token, job.lease_until = "running", token, _now() + _LEASE
                return token, "settle", job, pending_receipt, voice
            interrupted = False
            for row in latest.values():
                if row.state == "sending":
                    row.state, row.last_error_code = "unknown", "speech_result_unknown"
                if row.state == "unknown":
                    interrupted = True
                if disabled and row.state == "queued":
                    await self._settle(db, job, row, "release")
                    row.state, row.last_error_code = "failed", "voice_unavailable"
                if disabled and row.state == "download":
                    row.state, row.last_error_code = "failed", "voice_unavailable"
                    row.result_url = None
            if interrupted:
                # Do not retain holds for later segments that will never be sent.
                for row in latest.values():
                    if row.state == "queued":
                        await self._settle(db, job, row, "release")
                        row.state, row.last_error_code = "failed", "speech_paused_before_send"
                job.state, job.last_error_code = "unknown", "speech_result_unknown"
                _unlock(job)
                return empty
            if disabled:
                job.state, job.last_error_code = "failed", "voice_unavailable"
                _unlock(job)
                return empty
            selected = next(
                (row for row in latest.values() if row.state in {"queued", "download"}), None
            )
            if selected is None and any(row.state != "ready" for row in latest.values()):
                job.state, job.last_error_code = "failed", "speech_segment_failed"
                _unlock(job)
                return empty
            token = str(uuid.uuid4())
            job.state, job.lease_token, job.lease_until = "running", token, _now() + _LEASE
            if selected is None:
                return token, "join", job, [row.artifact_key for row in latest.values()], voice
            action = "download" if selected.state == "download" else "send"
            if action == "send":
                selected.state = "sending"
            selected.attempts += 1
            return token, action, job, selected, voice

    async def _finish(
        self, owner, job_id, token, action, segment, next_state, key, code, delay
    ):
        remove = key
        async with self._session() as db, db.begin():
            job = (
                await db.execute(_owned_job(owner, job_id).with_for_update())
            ).scalar_one_or_none()
            if job is not None and job.lease_token == token:
                voice = (
                    await db.execute(_owned_voice(owner, job.voice_id).with_for_update())
                ).scalar_one_or_none()
                revoked = (
                    voice is None or voice.revoked_at is not None or not await self._owner_active(db, owner)
                )
                if action == "join":
                    if not revoked and code is None:
                        # Keep an already downloaded prior final valid; cleanup is separate.
                        job.artifact_key, job.state, remove = key, "ready", None
                        job.last_error_code = None
                    else:
                        job.state, job.last_error_code = "failed", code or "voice_unavailable"
                else:
                    row = await db.get(SpeechSegment, segment.id, with_for_update=True)
                    if action == "settle":
                        await self._settle(db, job, row, "settle")
                        job.usage_chars += row.usage_chars
                        row.attempts = 0
                    elif action == "send" and next_state == "failed":
                        await self._settle(db, job, row, "release")
                    row.state, row.last_error_code = next_state, code
                    if revoked and next_state not in {"unknown", "queued", "failed"}:
                        row.state, row.last_error_code = "failed", "voice_unavailable"
                        row.result_url = None
                    elif next_state == "ready":
                        row.artifact_key, row.result_url, remove = key, None, None
                    elif next_state == "failed":
                        row.result_url = None
                    job.last_error_code = row.last_error_code
                _unlock(job, delay)
        if remove:
            await asyncio.to_thread(private_path(self._root, remove).unlink, missing_ok=True)

    async def sweep(self) -> None:
        now = _now()
        bound = ()
        if self._binding is not None:
            region, workspace = self._binding
            bound = (
                VoiceProfile.region == region, VoiceProfile.workspace_id == workspace,
            )
        async with self._session() as db:
            voices = (
                await db.execute(
                    select(VoiceProfile.user_id, VoiceProfile.id)
                    .where(
                        or_(
                            (VoiceProfile.state.in_(_ACTIVE_VOICE) & (VoiceProfile.due_at <= now)),
                            (
                                VoiceProfile.state.in_(("ready", "revoked"))
                                & VoiceProfile.source_key.is_not(None)
                                & (VoiceProfile.due_at <= now)
                            ),
                            (
                                VoiceProfile.revoked_at.is_(None)
                                & VoiceProfile.user_id.in_(select(User.id).where(User.deleted_at.is_not(None)))
                            ),
                            (
                                VoiceProfile.saved_at.is_(None)
                                & (VoiceProfile.draft_expires_at <= now)
                                & (VoiceProfile.revoked_at.is_(None))
                            ),
                        ),
                        or_(VoiceProfile.lease_until.is_(None), VoiceProfile.lease_until <= now),
                        *bound,
                    )
                    .order_by(VoiceProfile.due_at).limit(8)
                )
            ).all()
            jobs = (
                await db.execute(
                    select(SpeechJob.user_id, SpeechJob.id)
                    .join(VoiceProfile, (
                        (VoiceProfile.id == SpeechJob.voice_id)
                        & (VoiceProfile.user_id == SpeechJob.user_id)
                    ))
                    .where(
                        *bound,
                        SpeechJob.state.in_(_ACTIVE_JOB),
                        SpeechJob.due_at <= now,
                        or_(SpeechJob.lease_until.is_(None), SpeechJob.lease_until <= now),
                    )
                    .order_by(SpeechJob.due_at).limit(8)
                )
            ).all()
        # Bound provider concurrency and make one durable step per recovered job.
        for start in range(0, len(voices), 4):
            await asyncio.gather(*(self.enroll(*row) for row in voices[start:start + 4]))
        for start in range(0, len(jobs), 4):
            await asyncio.gather(*(self._synthesize_step(*row) for row in jobs[start:start + 4]))
