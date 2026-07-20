"""Storyboard patch, input validation, and prompt construction policies."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import MAX_PROMPT_CHARS
from lumen_core.models import WorkflowRun, WorkflowStep

from .common import (
    STORYBOARD_DEFAULT_DURATION_S,
    clean_string_list,
    http_error,
    run_metadata,
    short_hash,
    step_kind,
)
from .contracts import (
    StoryboardPatchIn,
    StoryboardShotCreateIn,
    StoryboardShotPatchIn,
)


def apply_storyboard_patch(
    run: WorkflowRun,
    body: StoryboardPatchIn,
    *,
    now_fn,
) -> dict[str, Any]:
    metadata = run_metadata(run)
    patch: dict[str, Any] = {}
    if body.title is not None:
        run.title = body.title.strip()
    if body.idea is not None:
        run.user_prompt = body.idea.strip()
    if body.style is not None:
        patch["style"] = body.style.strip()
    if body.script is not None:
        old_script = str(metadata.get("script") or "")
        new_script = body.script.strip()
        patch["script"] = new_script
        if new_script != old_script:
            patch["script_revision"] = int(metadata.get("script_revision") or 0) + 1
            if body.script_confirmed is None:
                patch["script_confirmed"] = False
    if body.script_confirmed is not None:
        patch["script_confirmed"] = body.script_confirmed
        if body.script_confirmed:
            patch["script_approved_revision"] = int(
                patch.get("script_revision", metadata.get("script_revision") or 0)
            )
            patch["script_approved_at"] = now_fn().isoformat()
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
    return patch


async def validate_asset_ids(
    db: AsyncSession,
    run: WorkflowRun,
    asset_ids: list[str],
    *,
    require_approved: bool = False,
) -> list[str]:
    ids = clean_string_list(asset_ids)
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
    found = {row.id: row for row in rows.all() if step_kind(row) == "asset"}
    missing = [asset_id for asset_id in ids if asset_id not in found]
    if missing:
        raise http_error(
            "invalid_asset_ids",
            "one or more assets are not in this storyboard",
            422,
        )
    if require_approved:
        not_ready = [
            asset_id for asset_id in ids if found[asset_id].status != "approved"
        ]
        if not_ready:
            raise http_error(
                "asset_not_approved",
                "all bound assets must be approved",
                422,
            )
    return ids


def shot_input_from_body(
    body: StoryboardShotCreateIn | StoryboardShotPatchIn,
    *,
    index: int | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = dict(existing or {})
    values = body.model_dump(exclude_unset=True)
    for key, value in values.items():
        if key == "asset_ids":
            data[key] = clean_string_list(value)
        elif isinstance(value, str):
            data[key] = value.strip()
        elif value is not None:
            data[key] = value
    if index is not None:
        data["index"] = index
    defaults = {
        "title": "",
        "purpose": "",
        "narration": "",
        "visual": "",
        "shot_type": "",
        "camera_move": "",
        "transition": "",
        "reference_notes": "",
        "duration_s": STORYBOARD_DEFAULT_DURATION_S,
        "asset_ids": [],
        "keyframe_prompt": "",
        "keyframe_source_hash": None,
    }
    for key, value in defaults.items():
        data.setdefault(key, value)
    return data


def shots_from_script(script: str) -> list[StoryboardShotCreateIn]:
    chunks = [
        chunk.strip()
        for chunk in re.split(r"(?:\n{2,}|[。.!?！？]\s*)", script)
        if chunk.strip()
    ]
    if not chunks:
        chunks = ["开场建立主体与氛围", "展示关键动作和变化", "收束到最终画面"]
    return [
        StoryboardShotCreateIn(
            title=f"镜头 {idx:02d}",
            purpose="推进故事节奏",
            narration=chunk[:1000],
            visual=chunk[:1200],
            shot_type="medium shot" if idx > 1 else "establishing shot",
            camera_move="smooth cinematic movement",
            transition="cut",
            duration_s=STORYBOARD_DEFAULT_DURATION_S,
        )
        for idx, chunk in enumerate(chunks[:60], start=1)
    ]


def asset_prompt(run: WorkflowRun, step: WorkflowStep, override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    metadata = run_metadata(run)
    data = dict(step.input_json or {})
    return "\n".join(
        part
        for part in [
            "Create a clean, production-ready visual reference image for a short video storyboard.",
            f"Project idea: {run.user_prompt}",
            f"Visual style: {metadata.get('style') or 'consistent cinematic commercial look'}",
            f"Asset type: {data.get('kind')}",
            f"Name: {data.get('name')}",
            f"Role: {data.get('role')}",
            f"Description: {data.get('description')}",
            f"Continuity requirements: {data.get('continuity')}",
            "No text overlays. Center the subject clearly. Make it useful as continuity reference.",
        ]
        if str(part).strip()
    )[:MAX_PROMPT_CHARS]


def shot_keyframe_prompt(
    run: WorkflowRun,
    shot: WorkflowStep,
    assets_by_id: dict[str, WorkflowStep],
    override: str | None,
) -> str:
    if override and override.strip():
        return override.strip()
    metadata = run_metadata(run)
    data = dict(shot.input_json or {})
    asset_lines: list[str] = []
    for asset_id in data.get("asset_ids") or []:
        asset = assets_by_id.get(asset_id)
        if asset is None:
            continue
        asset_data = dict(asset.input_json or {})
        asset_lines.append(
            f"- {asset_data.get('kind')}: {asset_data.get('name')} "
            f"({asset_data.get('description')})"
        )
    return "\n".join(
        part
        for part in [
            "Generate one polished keyframe for this storyboard shot.",
            f"Project idea: {run.user_prompt}",
            f"Style continuity: {metadata.get('style') or 'cinematic, coherent visual continuity'}",
            f"Shot title: {data.get('title')}",
            f"Purpose: {data.get('purpose')}",
            f"Narration: {data.get('narration')}",
            f"Visual: {data.get('visual')}",
            f"Shot type: {data.get('shot_type')}",
            f"Camera move: {data.get('camera_move')}",
            f"Transition: {data.get('transition')}",
            f"Reference notes: {data.get('reference_notes')}",
            "Bound approved references:",
            "\n".join(asset_lines),
            "No subtitles or watermarks. Compose as the first frame for image-to-video generation.",
        ]
        if str(part).strip()
    )[:MAX_PROMPT_CHARS]


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        timestamp, row_id = cursor.split("|", 1)
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return parsed, row_id
    except ValueError as exc:
        raise http_error("invalid_cursor", "cursor is invalid", 422) from exc


def encode_cursor(run: WorkflowRun) -> str:
    return f"{run.updated_at.isoformat()}|{run.id}"


def shot_input_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return short_hash(before) != short_hash(after)
