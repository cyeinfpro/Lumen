"""Storyboard assembly worker task."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from arq import Retry
from sqlalchemy import select, update

from lumen_core.models import Video, WorkflowRun, WorkflowStep, new_uuid7

from ..artifact_commit import (
    ArtifactAdoption,
    ArtifactCommitOutcomeUnknown,
    commit_error_or_default,
    commit_with_adoption_probe,
    rollback_artifact_transaction,
)
from ..db import SessionLocal, affected_rows
from ..sse_publish import publish_event
from ..storage import storage
from ..storage_writes import StorageWriteCoordinator
from ..video_artifacts import postprocess_video_bytes as _postprocess_video_bytes
from .storyboard_assembly_commit import (
    ASSEMBLY_ARTIFACT_IDENTITY_KEY,
    AssemblyArtifactVerificationError as AssemblyArtifactVerificationError,
    AssemblyCommitDeferralRuntime,
    AssemblyCommitReconcileRuntime,
    AssemblyRecoveryCandidateInvalid,
    AssemblyRecoveryCandidateMissing,
    assembly_commit_reconcile_delay,
    assembly_candidate_matches_attempt,
    assembly_recovery_marker_bytes,
    assembly_recovery_marker_key,
    assembly_video_candidate,
    defer_assembly_commit_reconcile,
    enqueue_assembly_commit_reconcile as _enqueue_assembly_commit_reconcile,
    handle_assembly_failure,
    load_assembly_commit_reconcile_target,
    load_assembly_recovery_candidate,
    probe_assembly_adoption,
    publish_assembly_success,
    reconcile_assembly_commit,
    record_assembly_commit_unknown,
    verify_assembly_candidate_artifacts,
    verify_assembly_recovery_marker,
)


logger = logging.getLogger(__name__)

STORYBOARD_ASSEMBLY_LEASE_TTL_S = 2 * 60
STORYBOARD_ASSEMBLY_HEARTBEAT_INTERVAL_S = 30
STORYBOARD_ASSEMBLY_HEARTBEAT_FAILURE_LIMIT = 3
STORYBOARD_ASSEMBLY_COMMIT_RECONCILE_MAX_ATTEMPTS = 4


class _AssemblyAttemptLost(RuntimeError):
    pass


class _AssemblyCommitOutcomeUnknown(ArtifactCommitOutcomeUnknown):
    def __init__(self, message: str, *, video: Video) -> None:
        super().__init__(message)
        self.video = video


@dataclass(frozen=True)
class _AssemblyClaim:
    run_id: str
    user_id: str
    step_id: str
    attempt_token: str
    fingerprint: str
    idempotency_key: str | None
    segment_ids: tuple[str, ...]
    output_json: dict[str, Any]


@dataclass(frozen=True)
class _AssemblyCommitReconcileTarget:
    claim: _AssemblyClaim
    video: Video


def _storyboard_channel(run_id: str) -> str:
    return f"storyboard:{run_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _storyboard_retry_attempt(ctx: dict[str, Any]) -> int:
    raw_job_try = ctx.get("job_try")
    if not isinstance(raw_job_try, int) or raw_job_try <= 0:
        return 0
    return raw_job_try - 1


def _concat_file_line(path: Path) -> str:
    # ffmpeg concat demuxer uses a simple quoted format; backslash-escape quotes.
    value = str(path).replace("\\", "\\\\").replace("'", "\\'")
    return f"file '{value}'"


async def _publish(
    redis: Any,
    *,
    user_id: str,
    run_id: str,
    event_name: str,
    data: dict[str, Any],
) -> None:
    await publish_event(
        redis,
        user_id,
        _storyboard_channel(run_id),
        event_name,
        {"storyboard_id": run_id, **data},
    )


def _assembly_attempt_predicates(
    *,
    step_id: str,
    status: str,
    attempt_token: str,
    fingerprint: str,
) -> tuple[Any, ...]:
    return (
        WorkflowStep.id == step_id,
        WorkflowStep.step_key == "assembly",
        WorkflowStep.status == status,
        WorkflowStep.output_json["assembly_attempt_token"].as_string() == attempt_token,
        WorkflowStep.output_json["assembly_fingerprint"].as_string() == fingerprint,
    )


async def _claim_waiting_assembly(
    session: Any,
    *,
    step_id: str,
    attempt_token: str,
    fingerprint: str,
    output_json: dict[str, Any],
    status: str = "waiting",
) -> bool:
    result = await session.execute(
        update(WorkflowStep)
        .where(
            *_assembly_attempt_predicates(
                step_id=step_id,
                status=status,
                attempt_token=attempt_token,
                fingerprint=fingerprint,
            ),
            WorkflowStep.output_json["assembly_claimed_at"].as_string().is_(None),
        )
        .values(status="compositing", output_json=output_json)
    )
    return affected_rows(result) == 1


async def _claim_assembly(
    run_id: str,
    *,
    expected_attempt_token: str | None,
) -> _AssemblyClaim | None:
    async with SessionLocal() as session:
        run = (
            await session.execute(
                select(WorkflowRun).where(
                    WorkflowRun.id == run_id,
                    WorkflowRun.type == "storyboard",
                    WorkflowRun.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if run is None:
            logger.warning("storyboard assembly run not found run=%s", run_id)
            return None

        assembly = (
            await session.execute(
                select(WorkflowStep).where(
                    WorkflowStep.workflow_run_id == run.id,
                    WorkflowStep.step_key == "assembly",
                )
            )
        ).scalar_one_or_none()
        if assembly is None:
            logger.warning("storyboard assembly step not found run=%s", run_id)
            return None
        if assembly.status not in {"waiting", "compositing"}:
            return None

        output = dict(assembly.output_json or {})
        claimed_at = output.get("assembly_claimed_at")
        if isinstance(claimed_at, str) and claimed_at:
            return None
        if claimed_at is not None:
            logger.warning(
                "storyboard assembly claim timestamp invalid run=%s value=%r",
                run_id,
                claimed_at,
            )
            return None
        attempt_token = output.get("assembly_attempt_token")
        fingerprint = output.get("assembly_fingerprint")
        if not isinstance(attempt_token, str) or not attempt_token:
            logger.warning("storyboard assembly attempt token missing run=%s", run_id)
            return None
        if not isinstance(fingerprint, str) or not fingerprint:
            logger.warning("storyboard assembly fingerprint missing run=%s", run_id)
            return None
        if expected_attempt_token and expected_attempt_token != attempt_token:
            return None

        idempotency_key = output.get("assembly_idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            idempotency_key = None
        segment_ids = tuple(
            item
            for item in output.get("segment_ids") or []
            if isinstance(item, str) and item
        )
        claim_now = _now()
        claimed_output = {
            **output,
            "assembly_claimed_at": claim_now.isoformat(),
            "assembly_heartbeat_at": claim_now.isoformat(),
            "assembly_lease_expires_at": (
                claim_now + timedelta(seconds=STORYBOARD_ASSEMBLY_LEASE_TTL_S)
            ).isoformat(),
            "assembly_commit_state": None,
            "assembly_commit_candidate": None,
            "assembly_commit_unknown_at": None,
            "assembly_commit_reconcile_attempt": 0,
            "error_code": None,
            "error_message": None,
        }
        claimed = await _claim_waiting_assembly(
            session,
            step_id=assembly.id,
            attempt_token=attempt_token,
            fingerprint=fingerprint,
            output_json=claimed_output,
            status=assembly.status,
        )
        if not claimed:
            # 抢锁失败在返回值上和「前置条件不满足」都是 None，调用方分不开（E-7）。
            # 真正的 DB 错误会以异常冒泡，这条日志专门用来标记「锁被别人拿走了」。
            await session.rollback()
            logger.warning(
                "storyboard assembly claim lost run=%s step=%s",
                run_id,
                assembly.id,
            )
            return None
        try:
            await session.commit()
        except BaseException:
            with suppress(Exception, asyncio.CancelledError):
                await session.rollback()
            raise

        return _AssemblyClaim(
            run_id=run_id,
            user_id=run.user_id,
            step_id=assembly.id,
            attempt_token=attempt_token,
            fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            segment_ids=segment_ids,
            output_json=claimed_output,
        )


async def _renew_assembly_lease(claim: _AssemblyClaim) -> bool:
    heartbeat_at = _now()
    output = {
        **claim.output_json,
        "assembly_heartbeat_at": heartbeat_at.isoformat(),
        "assembly_lease_expires_at": (
            heartbeat_at + timedelta(seconds=STORYBOARD_ASSEMBLY_LEASE_TTL_S)
        ).isoformat(),
    }
    async with SessionLocal() as session:
        result = await session.execute(
            update(WorkflowStep)
            .where(
                *_assembly_attempt_predicates(
                    step_id=claim.step_id,
                    status="compositing",
                    attempt_token=claim.attempt_token,
                    fingerprint=claim.fingerprint,
                )
            )
            .values(output_json=output)
        )
        if affected_rows(result) != 1:
            # 续租失败等于 attempt 已被别人接管；无日志的话调用方只看到 False（E-7）。
            await session.rollback()
            logger.warning(
                "storyboard assembly lease renew lost run=%s step=%s",
                claim.run_id,
                claim.step_id,
            )
            return False
        try:
            await session.commit()
        except BaseException:
            with suppress(Exception, asyncio.CancelledError):
                await session.rollback()
            raise
    return True


async def _assembly_heartbeat(
    claim: _AssemblyClaim,
    attempt_lost: asyncio.Event,
) -> None:
    consecutive_failures = 0
    while True:
        await asyncio.sleep(STORYBOARD_ASSEMBLY_HEARTBEAT_INTERVAL_S)
        try:
            renewed = await _renew_assembly_lease(claim)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1
            logger.warning(
                "storyboard assembly heartbeat failed run=%s attempt=%s "
                "streak=%d err=%s",
                claim.run_id,
                claim.attempt_token,
                consecutive_failures,
                exc,
            )
            if consecutive_failures >= STORYBOARD_ASSEMBLY_HEARTBEAT_FAILURE_LIMIT:
                attempt_lost.set()
                return
            continue
        if not renewed:
            attempt_lost.set()
            return
        consecutive_failures = 0


def _raise_if_attempt_lost(attempt_lost: asyncio.Event) -> None:
    if attempt_lost.is_set():
        raise _AssemblyAttemptLost("assembly attempt lease lost")


async def _cancel_heartbeat_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.debug("storyboard assembly heartbeat cleanup failed", exc_info=True)


async def _load_segment_paths(claim: _AssemblyClaim) -> list[Path]:
    if not claim.segment_ids:
        raise RuntimeError("shots_required")
    async with SessionLocal() as session:
        videos = list(
            (
                await session.execute(
                    select(Video).where(
                        Video.owner_generation_id.in_(claim.segment_ids),
                        Video.user_id == claim.user_id,
                        Video.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    video_by_generation = {
        video.owner_generation_id: video
        for video in videos
        if video.owner_generation_id is not None
    }
    missing = [
        segment_id
        for segment_id in claim.segment_ids
        if segment_id not in video_by_generation
    ]
    if missing:
        raise RuntimeError(f"segment_video_missing: {','.join(missing[:8])}")
    return [
        storage.path_for(video_by_generation[segment_id].storage_key)
        for segment_id in claim.segment_ids
    ]


async def _fail_assembly(
    redis: Any,
    *,
    claim: _AssemblyClaim,
    code: str,
    message: str,
) -> bool:
    failed_at = _now()
    output = {
        **claim.output_json,
        "assembly_heartbeat_at": failed_at.isoformat(),
        "assembly_lease_expires_at": None,
        "assembly_commit_state": "failed",
        "assembly_commit_candidate": None,
        "assembly_commit_unknown_at": None,
        "error_code": code,
        "error_message": message[:1000],
    }
    async with SessionLocal() as session:
        result = await session.execute(
            update(WorkflowStep)
            .where(
                *_assembly_attempt_predicates(
                    step_id=claim.step_id,
                    status="compositing",
                    attempt_token=claim.attempt_token,
                    fingerprint=claim.fingerprint,
                )
            )
            .values(status="failed", output_json=output)
        )
        if affected_rows(result) != 1:
            # 失败状态没写进去（attempt 已被接管）。不记日志的话这次失败原因会
            # 彻底丢失，排查时只能看到一个卡在 compositing 的 step（E-7）。
            await session.rollback()
            logger.warning(
                "storyboard assembly fail update lost run=%s step=%s code=%s",
                claim.run_id,
                claim.step_id,
                code,
            )
            return False
        try:
            await session.commit()
        except BaseException:
            with suppress(Exception, asyncio.CancelledError):
                await session.rollback()
            raise
    await _publish(
        redis,
        user_id=claim.user_id,
        run_id=claim.run_id,
        event_name="storyboard.assembly_failed",
        data={"error_code": code, "error_message": message[:1000]},
    )
    return True


_assembly_video_candidate = assembly_video_candidate
_assembly_candidate_matches_attempt = assembly_candidate_matches_attempt


async def _load_assembly_commit_reconcile_target(
    run_id: str,
    *,
    expected_attempt_token: str | None,
    candidate: dict[str, Any] | None,
    require_recovery_candidate: bool = False,
) -> _AssemblyCommitReconcileTarget | None:
    return await load_assembly_commit_reconcile_target(
        run_id,
        expected_attempt_token=expected_attempt_token,
        candidate=candidate,
        session_factory=SessionLocal,
        claim_factory=_AssemblyClaim,
        target_factory=_AssemblyCommitReconcileTarget,
        recovery_candidate_loader=lambda **kwargs: load_assembly_recovery_candidate(
            storage=storage,
            **kwargs,
        ),
        require_recovery_candidate=require_recovery_candidate,
    )


async def _record_assembly_commit_unknown(
    claim: _AssemblyClaim,
    video: Video,
    *,
    reconcile_attempt: int,
) -> bool:
    return await record_assembly_commit_unknown(
        claim,
        video,
        reconcile_attempt=reconcile_attempt,
        recorded_at=_now(),
        lease_ttl_s=STORYBOARD_ASSEMBLY_LEASE_TTL_S,
        session_factory=SessionLocal,
        attempt_predicates=_assembly_attempt_predicates,
    )


async def _defer_assembly_commit_reconcile(
    redis: Any,
    *,
    claim: _AssemblyClaim,
    video: Video,
    reconcile_attempt: int,
) -> None:
    await defer_assembly_commit_reconcile(
        redis,
        claim=claim,
        video=video,
        reconcile_attempt=reconcile_attempt,
        runtime=AssemblyCommitDeferralRuntime(
            max_attempts=STORYBOARD_ASSEMBLY_COMMIT_RECONCILE_MAX_ATTEMPTS,
            fail_assembly=_fail_assembly,
            record_commit_unknown=_record_assembly_commit_unknown,
            enqueue_commit_reconcile=_enqueue_assembly_commit_reconcile,
            logger=logger,
        ),
    )


async def _publish_assembly_success(
    redis: Any,
    *,
    claim: _AssemblyClaim,
    video: Video,
) -> None:
    await publish_assembly_success(
        redis,
        claim=claim,
        video=video,
        publish=_publish,
    )


async def _complete_assembly(claim: _AssemblyClaim, video: Video) -> bool:
    completed_at = _now()
    output = {
        **claim.output_json,
        "video_id": video.id,
        "segment_ids": list(claim.segment_ids),
        "assembly_heartbeat_at": completed_at.isoformat(),
        "assembly_lease_expires_at": None,
        "assembly_completed_at": completed_at.isoformat(),
        "assembly_commit_state": "adopted",
        "assembly_commit_candidate": None,
        "assembly_commit_unknown_at": None,
        "assembly_commit_reconcile_attempt": 0,
        "error_code": None,
        "error_message": None,
    }
    async with SessionLocal() as session:
        session.add(video)
        result = await session.execute(
            update(WorkflowStep)
            .where(
                *_assembly_attempt_predicates(
                    step_id=claim.step_id,
                    status="compositing",
                    attempt_token=claim.attempt_token,
                    fingerprint=claim.fingerprint,
                )
            )
            .values(status="done", output_json=output)
        )
        if affected_rows(result) != 1:
            # 四处里最要紧的一处：视频已经合成好了，但 step 没能标 done。
            # 静默 return False 会让这段成片凭空消失在日志外（E-7）。
            rolled_back = await rollback_artifact_transaction(
                session,
                logger=logger,
                label=(
                    f"storyboard assembly CAS run={claim.run_id} "
                    f"attempt={claim.attempt_token}"
                ),
            )
            if not rolled_back:
                raise _AssemblyCommitOutcomeUnknown(
                    f"storyboard assembly rollback outcome unknown run={claim.run_id} "
                    f"attempt={claim.attempt_token}",
                    video=video,
                )
            logger.warning(
                "storyboard assembly complete update lost run=%s step=%s video=%s",
                claim.run_id,
                claim.step_id,
                video.id,
            )
            return False
        commit_result = await commit_with_adoption_probe(
            session,
            probe=lambda: _probe_assembly_adoption(claim, video),
            logger=logger,
            label=(
                f"storyboard assembly run={claim.run_id} attempt={claim.attempt_token}"
            ),
        )
        if commit_result.adopted:
            return True
        if commit_result.outcome is ArtifactAdoption.NOT_ADOPTED:
            raise commit_error_or_default(
                commit_result,
                label=f"storyboard assembly run={claim.run_id}",
            )
        unknown = _AssemblyCommitOutcomeUnknown(
            f"storyboard assembly commit outcome unknown run={claim.run_id} "
            f"attempt={claim.attempt_token}",
            video=video,
        )
        if commit_result.commit_error is not None:
            raise unknown from commit_result.commit_error
        raise unknown


async def _probe_assembly_adoption(
    claim: _AssemblyClaim,
    video: Video,
) -> ArtifactAdoption:
    return await probe_assembly_adoption(
        claim,
        video,
        session_factory=SessionLocal,
        candidate_matches_attempt=assembly_candidate_matches_attempt,
    )


async def _verify_assembly_candidate_artifacts(video: Video) -> None:
    await asyncio.to_thread(
        verify_assembly_candidate_artifacts,
        video,
        path_for=storage.path_for,
    )


async def _verify_assembly_recovery_marker(video: Video) -> None:
    await asyncio.to_thread(
        verify_assembly_recovery_marker,
        video,
        read_bytes=storage.get_bytes,
    )


async def _delete_assembly_recovery_marker(
    storage_writes: StorageWriteCoordinator,
    video: Video,
) -> None:
    await storage_writes.delete_files([assembly_recovery_marker_key(video)])


async def _reconcile_assembly_commit(
    redis: Any,
    *,
    target: _AssemblyCommitReconcileTarget,
    reconcile_attempt: int,
    storage_writes: StorageWriteCoordinator,
) -> None:
    await reconcile_assembly_commit(
        redis,
        target=target,
        reconcile_attempt=reconcile_attempt,
        storage_writes=storage_writes,
        runtime=AssemblyCommitReconcileRuntime(
            probe_adoption=_probe_assembly_adoption,
            verify_candidate_artifacts=_verify_assembly_candidate_artifacts,
            fail_assembly=_fail_assembly,
            defer_reconcile=_defer_assembly_commit_reconcile,
            delete_recovery_marker=_delete_assembly_recovery_marker,
            publish_success=_publish_assembly_success,
            verify_recovery_marker=_verify_assembly_recovery_marker,
            complete_assembly=_complete_assembly,
            commit_unknown_error=_AssemblyCommitOutcomeUnknown,
            logger=logger,
        ),
    )


async def _store_assembly_result(
    claim: _AssemblyClaim,
    *,
    processed: dict[str, Any],
    diagnostics: dict[str, Any],
    storage_writes: StorageWriteCoordinator,
) -> Video:
    version = new_uuid7()
    video_key = (
        f"u/{claim.user_id}/storyboards/{claim.run_id}/assembly/{version}/output.mp4"
    )
    poster_key = (
        f"u/{claim.user_id}/storyboards/{claim.run_id}/assembly/{version}/poster.jpg"
    )
    video_bytes = processed["video_bytes"]
    poster_bytes = processed.get("poster_bytes")
    video_sha = hashlib.sha256(video_bytes).hexdigest()
    poster_identity = (
        {
            "storage_key": poster_key,
            "size_bytes": len(poster_bytes),
            "sha256": hashlib.sha256(poster_bytes).hexdigest(),
        }
        if poster_bytes
        else None
    )
    poster_storage_key = poster_key if poster_bytes else None
    video = Video(
        id=new_uuid7(),
        user_id=claim.user_id,
        owner_generation_id=None,
        storage_key=video_key,
        poster_storage_key=poster_storage_key,
        mime="video/mp4",
        width=int(processed.get("width") or 0),
        height=int(processed.get("height") or 0),
        duration_ms=int(processed.get("duration_ms") or 0),
        fps=processed.get("fps"),
        size_bytes=len(video_bytes),
        sha256=video_sha,
        etag=video_sha,
        has_audio=bool(processed.get("has_audio")),
        faststart=bool(processed.get("faststart")),
        visibility="private",
        metadata_jsonb={
            **diagnostics,
            "workflow_type": "storyboard",
            "workflow_run_id": claim.run_id,
            "segment_ids": list(claim.segment_ids),
            "assembly_fingerprint": claim.fingerprint,
            "assembly_idempotency_key": claim.idempotency_key,
            "assembly_attempt_token": claim.attempt_token,
            "assembled_at": _now().isoformat(),
            ASSEMBLY_ARTIFACT_IDENTITY_KEY: {
                "video": {
                    "storage_key": video_key,
                    "size_bytes": len(video_bytes),
                    "sha256": video_sha,
                },
                "poster": poster_identity,
            },
        },
    )
    marker_key = assembly_recovery_marker_key(video)
    files = [(video_key, video_bytes)]
    if poster_bytes:
        files.append((poster_key, poster_bytes))
    files.append((marker_key, assembly_recovery_marker_bytes(video)))
    created_keys = await storage_writes.write_files(files)
    try:
        if not await _complete_assembly(claim, video):
            raise _AssemblyAttemptLost("assembly attempt superseded before commit")
        await storage_writes.delete_files([marker_key])
    except ArtifactCommitOutcomeUnknown:
        raise
    except BaseException:
        await storage_writes.delete_files(created_keys)
        raise
    return video


async def run_storyboard_assembly(
    ctx: dict[str, Any],
    run_id: str,
    expected_attempt_token: str | None = None,
    commit_candidate: dict[str, Any] | None = None,
    reconcile_attempt: int = 0,
) -> None:
    redis = ctx["redis"]
    storage_writes: StorageWriteCoordinator = ctx["storage_write_coordinator"]
    retry_attempt = _storyboard_retry_attempt(ctx)
    reconcile_attempt = max(max(0, int(reconcile_attempt)), retry_attempt)
    claim: _AssemblyClaim | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    attempt_lost = asyncio.Event()
    try:
        if commit_candidate is not None:
            reconcile_target = await _load_assembly_commit_reconcile_target(
                run_id,
                expected_attempt_token=expected_attempt_token,
                candidate=commit_candidate,
            )
            if reconcile_target is None:
                logger.info(
                    "storyboard assembly commit candidate superseded run=%s attempt=%s",
                    run_id,
                    expected_attempt_token,
                )
                return
            claim = reconcile_target.claim
            await _reconcile_assembly_commit(
                redis,
                target=reconcile_target,
                reconcile_attempt=reconcile_attempt,
                storage_writes=storage_writes,
            )
            return
        claim = await _claim_assembly(
            run_id,
            expected_attempt_token=expected_attempt_token,
        )
        if claim is None:
            reconcile_target = await _load_assembly_commit_reconcile_target(
                run_id,
                expected_attempt_token=expected_attempt_token,
                candidate=None,
                require_recovery_candidate=reconcile_attempt > 0,
            )
            if reconcile_target is not None:
                claim = reconcile_target.claim
                await _reconcile_assembly_commit(
                    redis,
                    target=reconcile_target,
                    reconcile_attempt=reconcile_attempt,
                    storage_writes=storage_writes,
                )
            return
        heartbeat_task = asyncio.create_task(_assembly_heartbeat(claim, attempt_lost))

        await _publish(
            redis,
            user_id=claim.user_id,
            run_id=run_id,
            event_name="storyboard.assembling",
            data={
                "segment_ids": list(claim.segment_ids),
                "assembly_fingerprint": claim.fingerprint,
                "progress_pct": 10,
            },
        )
        _raise_if_attempt_lost(attempt_lost)
        segment_paths = await _load_segment_paths(claim)
        _raise_if_attempt_lost(attempt_lost)
        concat_bytes = await asyncio.to_thread(_concat_segments_sync, segment_paths)
        _raise_if_attempt_lost(attempt_lost)
        processed, diagnostics = await asyncio.to_thread(
            _postprocess_video_bytes, concat_bytes
        )
        _raise_if_attempt_lost(attempt_lost)
        video = await _store_assembly_result(
            claim,
            processed=processed,
            diagnostics=diagnostics,
            storage_writes=storage_writes,
        )
        await _cancel_heartbeat_task(heartbeat_task)
        heartbeat_task = None

        await _publish_assembly_success(redis, claim=claim, video=video)
    except _AssemblyCommitOutcomeUnknown as exc:
        logger.error(
            "storyboard assembly commit pending reconciliation run=%s "
            "attempt=%s err=%s",
            run_id,
            claim.attempt_token if claim is not None else expected_attempt_token,
            exc,
        )
        if claim is None:
            raise
        await _defer_assembly_commit_reconcile(
            redis,
            claim=claim,
            video=exc.video,
            reconcile_attempt=reconcile_attempt,
        )
    except AssemblyRecoveryCandidateMissing as exc:
        claim = exc.claim
        await _fail_assembly(
            redis,
            claim=claim,
            code="assembly_recovery_candidate_missing",
            message=str(exc),
        )
    except AssemblyRecoveryCandidateInvalid as exc:
        claim = exc.claim
        await _fail_assembly(
            redis,
            claim=claim,
            code="assembly_recovery_candidate_invalid",
            message=str(exc),
        )
    except ArtifactCommitOutcomeUnknown:
        logger.exception(
            "storyboard assembly commit outcome unknown without candidate run=%s "
            "attempt=%s",
            run_id,
            claim.attempt_token if claim is not None else expected_attempt_token,
        )
        raise
    except _AssemblyAttemptLost:
        logger.info("storyboard assembly attempt superseded run=%s", run_id)
    except asyncio.CancelledError:
        logger.info("storyboard assembly canceled run=%s", run_id)
        raise
    except Retry:
        raise
    except Exception as exc:  # noqa: BLE001
        await handle_assembly_failure(
            redis,
            run_id=run_id,
            claim=claim,
            reconcile_attempt=reconcile_attempt,
            max_reconcile_attempts=(STORYBOARD_ASSEMBLY_COMMIT_RECONCILE_MAX_ATTEMPTS),
            failure=exc,
            fail_assembly=_fail_assembly,
            retry_delay=assembly_commit_reconcile_delay,
            logger=logger,
        )
    finally:
        await _cancel_heartbeat_task(heartbeat_task)


def _concat_segments_sync(segment_paths: list[Path]) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg_missing")
    with tempfile.TemporaryDirectory(prefix="lumen-storyboard-") as tmp:
        tmpdir = Path(tmp)
        concat_list = tmpdir / "concat.txt"
        output = tmpdir / "assembly.mp4"
        concat_list.write_text(
            "\n".join(_concat_file_line(path) for path in segment_paths) + "\n",
            encoding="utf-8",
        )
        base_args = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
        ]
        copy_proc = subprocess.run(
            [*base_args, "-c", "copy", str(output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        if copy_proc.returncode != 0 or not output.is_file():
            output.unlink(missing_ok=True)
            transcode_proc = subprocess.run(
                [
                    *base_args,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+faststart",
                    str(output),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=600,
                check=False,
            )
            if transcode_proc.returncode != 0 or not output.is_file():
                stderr = transcode_proc.stderr.decode("utf-8", "replace")[-1200:]
                copy_stderr = copy_proc.stderr.decode("utf-8", "replace")[-600:]
                raise RuntimeError(
                    f"ffmpeg_concat_failed: copy={copy_stderr}; transcode={stderr}"
                )
        return output.read_bytes()
