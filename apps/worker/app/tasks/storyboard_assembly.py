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

from sqlalchemy import select, update

from lumen_core.models import Video, WorkflowRun, WorkflowStep, new_uuid7

from ..db import SessionLocal, affected_rows
from ..sse_publish import publish_event
from ..storage import storage
from .video_generation import _postprocess_video_bytes


logger = logging.getLogger(__name__)

STORYBOARD_ASSEMBLY_LEASE_TTL_S = 2 * 60
STORYBOARD_ASSEMBLY_HEARTBEAT_INTERVAL_S = 30
STORYBOARD_ASSEMBLY_HEARTBEAT_FAILURE_LIMIT = 3


class _AssemblyAttemptLost(RuntimeError):
    pass


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


def _storyboard_channel(run_id: str) -> str:
    return f"storyboard:{run_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
            await session.rollback()
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
            await session.rollback()
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
            await session.rollback()
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


async def _complete_assembly(claim: _AssemblyClaim, video: Video) -> bool:
    completed_at = _now()
    output = {
        **claim.output_json,
        "video_id": video.id,
        "segment_ids": list(claim.segment_ids),
        "assembly_heartbeat_at": completed_at.isoformat(),
        "assembly_lease_expires_at": None,
        "assembly_completed_at": completed_at.isoformat(),
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
            await session.rollback()
            return False
        try:
            await session.commit()
        except BaseException:
            with suppress(Exception, asyncio.CancelledError):
                await session.rollback()
            raise
    return True


async def _delete_storage_keys(keys: list[str]) -> None:
    unique_keys = list(dict.fromkeys(keys))
    if not unique_keys:
        return
    cleanup = asyncio.ensure_future(
        asyncio.gather(
            *(asyncio.to_thread(storage.delete, key) for key in unique_keys),
            return_exceptions=True,
        )
    )
    try:
        results = await asyncio.shield(cleanup)
    except asyncio.CancelledError:

        def _log_late_cleanup(
            task: asyncio.Future[list[bool | BaseException]],
        ) -> None:
            with suppress(Exception):
                late_results = task.result()
                for key, result in zip(unique_keys, late_results, strict=False):
                    if isinstance(result, BaseException):
                        logger.warning(
                            "storyboard storage cleanup failed key=%s err=%s",
                            key,
                            result,
                        )

        cleanup.add_done_callback(_log_late_cleanup)
        raise
    for key, result in zip(unique_keys, results, strict=False):
        if isinstance(result, BaseException):
            logger.warning(
                "storyboard storage cleanup failed key=%s err=%s",
                key,
                result,
            )


async def _put_storage_bytes(key: str, data: bytes) -> bool:
    put_task = asyncio.create_task(
        asyncio.to_thread(storage.put_bytes_result, key, data)
    )
    try:
        result = await asyncio.shield(put_task)
    except asyncio.CancelledError:

        async def _cleanup_late_put() -> None:
            try:
                late_result = await put_task
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "storyboard storage write failed after cancellation key=%s err=%s",
                    key,
                    exc,
                )
                return
            if late_result.created:
                try:
                    await asyncio.to_thread(storage.delete, key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "storyboard storage cleanup failed after cancellation "
                        "key=%s err=%s",
                        key,
                        exc,
                    )

        cleanup = asyncio.create_task(_cleanup_late_put())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        raise
    return bool(result.created)


async def _store_assembly_result(
    claim: _AssemblyClaim,
    *,
    processed: dict[str, Any],
    diagnostics: dict[str, Any],
) -> Video:
    version = new_uuid7()
    video_key = (
        f"u/{claim.user_id}/storyboards/{claim.run_id}/assembly/{version}/output.mp4"
    )
    poster_key = (
        f"u/{claim.user_id}/storyboards/{claim.run_id}/assembly/{version}/poster.jpg"
    )
    created_keys: list[str] = []
    try:
        if await _put_storage_bytes(video_key, processed["video_bytes"]):
            created_keys.append(video_key)
        poster_storage_key = None
        if processed.get("poster_bytes"):
            if await _put_storage_bytes(poster_key, processed["poster_bytes"]):
                created_keys.append(poster_key)
            poster_storage_key = poster_key
        sha = hashlib.sha256(processed["video_bytes"]).hexdigest()
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
            size_bytes=len(processed["video_bytes"]),
            sha256=sha,
            etag=sha,
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
            },
        )
        if not await _complete_assembly(claim, video):
            raise _AssemblyAttemptLost("assembly attempt superseded before commit")
    except BaseException:
        await _delete_storage_keys(created_keys)
        raise
    return video


async def run_storyboard_assembly(
    ctx: dict[str, Any],
    run_id: str,
    expected_attempt_token: str | None = None,
) -> None:
    redis = ctx["redis"]
    claim: _AssemblyClaim | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    attempt_lost = asyncio.Event()
    try:
        claim = await _claim_assembly(
            run_id,
            expected_attempt_token=expected_attempt_token,
        )
        if claim is None:
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
        )
        await _cancel_heartbeat_task(heartbeat_task)
        heartbeat_task = None

        await _publish(
            redis,
            user_id=claim.user_id,
            run_id=run_id,
            event_name="storyboard.assembled",
            data={
                "video_id": video.id,
                "segment_ids": list(claim.segment_ids),
                "assembly_fingerprint": claim.fingerprint,
                "progress_pct": 100,
            },
        )
    except _AssemblyAttemptLost:
        logger.info("storyboard assembly attempt superseded run=%s", run_id)
    except asyncio.CancelledError:
        logger.info("storyboard assembly canceled run=%s", run_id)
        raise
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        logger.warning(
            "storyboard assembly failed run=%s err=%s",
            run_id,
            message,
            exc_info=True,
        )
        if claim is not None:
            await _fail_assembly(
                redis,
                claim=claim,
                code=message.split(":", 1)[0] or "assembly_failed",
                message=message,
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
