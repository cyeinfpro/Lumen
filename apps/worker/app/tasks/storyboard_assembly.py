"""Storyboard assembly worker task."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from lumen_core.models import Video, WorkflowRun, WorkflowStep, new_uuid7

from ..db import SessionLocal
from ..sse_publish import publish_event
from ..storage import storage
from .video_generation import _postprocess_video_bytes


logger = logging.getLogger(__name__)


def _storyboard_channel(run_id: str) -> str:
    return f"storyboard:{run_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _step_kind(step: WorkflowStep) -> str | None:
    if step.step_key.startswith("shot:"):
        return "shot"
    if step.step_key == "assembly":
        return "assembly"
    return None


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


async def _fail_assembly(
    redis: Any,
    *,
    run_id: str,
    user_id: str | None,
    code: str,
    message: str,
) -> None:
    async with SessionLocal() as session:
        step = (
            await session.execute(
                select(WorkflowStep).where(
                    WorkflowStep.workflow_run_id == run_id,
                    WorkflowStep.step_key == "assembly",
                )
            )
        ).scalar_one_or_none()
        if step is not None:
            out = dict(step.output_json or {})
            out.update({"error_code": code, "error_message": message[:1000]})
            step.output_json = out
            step.status = "failed"
            await session.commit()
        if user_id:
            await _publish(
                redis,
                user_id=user_id,
                run_id=run_id,
                event_name="storyboard.assembly_failed",
                data={"error_code": code, "error_message": message[:1000]},
            )


async def run_storyboard_assembly(ctx: dict[str, Any], run_id: str) -> None:
    redis = ctx["redis"]
    user_id: str | None = None
    try:
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
                return
            user_id = run.user_id
            steps = list(
                (
                    await session.execute(
                        select(WorkflowStep).where(WorkflowStep.workflow_run_id == run.id)
                    )
                )
                .scalars()
                .all()
            )
            assembly = next((s for s in steps if s.step_key == "assembly"), None)
            if assembly is None:
                raise RuntimeError("assembly_step_missing")
            shot_steps = [s for s in steps if _step_kind(s) == "shot"]
            if not shot_steps:
                raise RuntimeError("shots_required")
            not_done = [s.id for s in shot_steps if s.status != "done"]
            if not_done:
                raise RuntimeError(f"shots_not_done: {','.join(not_done[:8])}")
            ordered = sorted(
                shot_steps,
                key=lambda s: int((s.input_json or {}).get("index") or 0),
            )
            segment_ids = [
                str((shot.output_json or {}).get("video_generation_id") or "")
                for shot in ordered
            ]
            if any(not item for item in segment_ids):
                raise RuntimeError("segment_missing")
            videos = list(
                (
                    await session.execute(
                        select(Video).where(
                            Video.owner_generation_id.in_(segment_ids),
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
            missing = [sid for sid in segment_ids if sid not in video_by_generation]
            if missing:
                raise RuntimeError(f"segment_video_missing: {','.join(missing[:8])}")
            assembly.status = "compositing"
            assembly.output_json = {
                **(assembly.output_json or {}),
                "segment_ids": segment_ids,
                "error_code": None,
                "error_message": None,
            }
            await session.commit()

        await _publish(
            redis,
            user_id=user_id,
            run_id=run_id,
            event_name="storyboard.assembling",
            data={"segment_ids": segment_ids, "progress_pct": 10},
        )

        segment_paths = [
            storage.path_for(video_by_generation[sid].storage_key) for sid in segment_ids
        ]
        concat_bytes = await asyncio.to_thread(_concat_segments_sync, segment_paths)
        processed, diagnostics = await asyncio.to_thread(
            _postprocess_video_bytes, concat_bytes
        )
        version = new_uuid7()
        video_key = f"u/{user_id}/storyboards/{run_id}/assembly/{version}/output.mp4"
        poster_key = f"u/{user_id}/storyboards/{run_id}/assembly/{version}/poster.jpg"
        await storage.aput_bytes(video_key, processed["video_bytes"])
        poster_storage_key = None
        if processed.get("poster_bytes"):
            await storage.aput_bytes(poster_key, processed["poster_bytes"])
            poster_storage_key = poster_key
        sha = hashlib.sha256(processed["video_bytes"]).hexdigest()

        async with SessionLocal() as session:
            assembly = (
                await session.execute(
                    select(WorkflowStep).where(
                        WorkflowStep.workflow_run_id == run_id,
                        WorkflowStep.step_key == "assembly",
                    )
                )
            ).scalar_one_or_none()
            if assembly is None:
                raise RuntimeError("assembly_step_missing_after_concat")
            video = Video(
                id=new_uuid7(),
                user_id=user_id,
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
                    "workflow_run_id": run_id,
                    "segment_ids": segment_ids,
                    "assembled_at": _now().isoformat(),
                },
            )
            session.add(video)
            assembly.status = "done"
            assembly.output_json = {
                **(assembly.output_json or {}),
                "video_id": video.id,
                "segment_ids": segment_ids,
                "error_code": None,
                "error_message": None,
            }
            await session.commit()

        await _publish(
            redis,
            user_id=user_id,
            run_id=run_id,
            event_name="storyboard.assembled",
            data={"video_id": video.id, "segment_ids": segment_ids, "progress_pct": 100},
        )
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        logger.warning("storyboard assembly failed run=%s err=%s", run_id, message, exc_info=True)
        await _fail_assembly(
            redis,
            run_id=run_id,
            user_id=user_id,
            code=message.split(":", 1)[0] or "assembly_failed",
            message=message,
        )


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
