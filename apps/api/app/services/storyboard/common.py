"""Shared storyboard domain constants and pure value helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from fastapi import HTTPException

from lumen_core.constants import MAX_PROMPT_CHARS
from lumen_core.models import WorkflowRun, WorkflowStep


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

SHOT_STATUS_RANK = {
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


def http_error(
    code: str,
    message: str,
    status_code: int = 400,
    **details: Any,
) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return HTTPException(status_code=status_code, detail={"error": error})


def iso_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def clean_text(value: str | None, *, max_len: int, default: str = "") -> str:
    text = (value or "").strip()
    if not text:
        return default
    return text[:max_len]


def clean_string_list(
    values: Iterable[object] | None,
    *,
    max_len: int = 36,
) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text[:max_len])
    return cleaned


def short_hash(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def asset_step_key(asset_id: str) -> str:
    return f"asset:{asset_id}"


def shot_step_key(shot_id: str) -> str:
    return f"shot:{shot_id}"


def step_kind(step: WorkflowStep) -> str | None:
    if step.step_key.startswith("asset:"):
        return "asset"
    if step.step_key.startswith("shot:"):
        return "shot"
    if step.step_key == "assembly":
        return "assembly"
    return None


def image_url(image_id: str | None) -> str | None:
    return f"/api/images/{image_id}/binary" if image_id else None


def image_display_url(image_id: str | None) -> str | None:
    return f"/api/images/{image_id}/variants/display2048" if image_id else None


def video_url(video_id: str | None) -> str | None:
    return f"/api/videos/{video_id}/binary" if video_id else None


def video_poster_url(
    video_id: str | None,
    poster_storage_key: str | None,
) -> str | None:
    return f"/api/videos/{video_id}/poster" if video_id and poster_storage_key else None


def clear_shot_video_output(out_json: dict[str, Any]) -> dict[str, Any]:
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


def asset_hash_payload(asset: WorkflowStep) -> dict[str, Any]:
    input_data = dict(asset.input_json or {})
    output = dict(asset.output_json or {})
    return {
        "step_id": asset.id,
        "revision": input_data.get("revision"),
        "image_id": output.get("image_id"),
        "approved_at": output.get("approved_at") or iso_datetime(asset.approved_at),
    }


def shot_source_hash(
    shot: WorkflowStep,
    assets_by_id: dict[str, WorkflowStep],
) -> str:
    input_data = dict(shot.input_json or {})
    asset_refs = [
        asset_hash_payload(assets_by_id[asset_id])
        for asset_id in input_data.get("asset_ids", [])
        if isinstance(asset_id, str) and asset_id in assets_by_id
    ]
    payload = {
        "title": input_data.get("title"),
        "purpose": input_data.get("purpose"),
        "narration": input_data.get("narration"),
        "visual": input_data.get("visual"),
        "shot_type": input_data.get("shot_type"),
        "camera_move": input_data.get("camera_move"),
        "transition": input_data.get("transition"),
        "reference_notes": input_data.get("reference_notes"),
        "keyframe_prompt": input_data.get("keyframe_prompt"),
        "asset_refs": asset_refs,
    }
    return short_hash(payload)


def run_metadata(run: WorkflowRun) -> dict[str, Any]:
    return dict(run.metadata_jsonb or {})


def default_storyboard_metadata() -> dict[str, Any]:
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


def merge_run_metadata(run: WorkflowRun, patch: dict[str, Any]) -> None:
    current = default_storyboard_metadata()
    current.update(run_metadata(run))
    current.update(patch)
    run.metadata_jsonb = current


def rank_status(status: str) -> int:
    return SHOT_STATUS_RANK.get(status, 0)


def normalize_shot_indexes(shots: list[WorkflowStep]) -> None:
    ordered = sorted(
        shots,
        key=lambda step: (
            int((step.input_json or {}).get("index") or 0),
            step.created_at,
            step.id,
        ),
    )
    for index, shot in enumerate(ordered, start=1):
        data = dict(shot.input_json or {})
        if data.get("index") != index:
            data["index"] = index
            shot.input_json = data


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "MAX_PROMPT_CHARS",
    "SHOT_STATUS_RANK",
    "STORYBOARD_ASSEMBLY_WAITING_LEASE_S",
    "STORYBOARD_ASSEMBLY_WORKER_LEASE_S",
    "STORYBOARD_ASSET_KINDS",
    "STORYBOARD_DEFAULT_ASPECT_RATIO",
    "STORYBOARD_DEFAULT_DURATION_S",
    "STORYBOARD_DEFAULT_MODEL",
    "STORYBOARD_DEFAULT_RESOLUTION",
    "STORYBOARD_KEYFRAME_PARALLELISM",
    "STORYBOARD_WORKFLOW_TYPE",
    "asset_step_key",
    "asset_hash_payload",
    "clear_shot_video_output",
    "clean_string_list",
    "clean_text",
    "default_storyboard_metadata",
    "http_error",
    "image_display_url",
    "image_url",
    "iso_datetime",
    "merge_run_metadata",
    "normalize_shot_indexes",
    "rank_status",
    "run_metadata",
    "short_hash",
    "shot_source_hash",
    "shot_step_key",
    "step_kind",
    "storyboard_channel",
    "utc_now",
    "video_poster_url",
    "video_url",
]
