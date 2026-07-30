"""Transactional storyboard metadata, asset, and shot commands."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    Conversation,
    User,
    WorkflowRun,
    WorkflowStep,
)

from ...services.storyboard import patching as storyboard_patching
from ...services.storyboard.common import (
    STORYBOARD_DEFAULT_ASPECT_RATIO,
    STORYBOARD_DEFAULT_MODEL,
    STORYBOARD_DEFAULT_RESOLUTION,
    STORYBOARD_WORKFLOW_TYPE,
    asset_step_key,
    clear_shot_video_output,
    default_storyboard_metadata,
    http_error,
    merge_run_metadata,
    normalize_shot_indexes,
    rank_status,
    run_metadata,
    shot_source_hash,
    short_hash,
    shot_step_key,
    step_kind,
)
from ...services.storyboard.contracts import (
    StoryboardAssetCreateIn,
    StoryboardAssetPatchIn,
    StoryboardCreateIn,
    StoryboardPatchIn,
    StoryboardRunOut,
    StoryboardShotCreateIn,
    StoryboardShotMoveIn,
    StoryboardShotPatchIn,
    StoryboardShotsRebuildIn,
)
from .runtime import StoryboardRuntime


async def create_storyboard(
    *,
    db: AsyncSession,
    user: User,
    body: StoryboardCreateIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = WorkflowRun(
        user_id=user.id,
        type=STORYBOARD_WORKFLOW_TYPE,
        status="draft",
        title=body.title.strip(),
        user_prompt=body.idea.strip(),
        product_image_ids=[],
        current_step="idea",
        quality_mode="premium",
        metadata_jsonb={
            **default_storyboard_metadata(),
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
    await runtime.assembly_step(db, run)
    conversation = await runtime.get_or_create_conversation(db, user=user, run=run)
    run.conversation_id = conversation.id
    await db.commit()
    await db.refresh(run)
    return await runtime.build_run_out(db, run)


async def patch_storyboard(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    body: StoryboardPatchIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    patch = storyboard_patching.apply_storyboard_patch(
        run,
        body,
        now_fn=runtime.now,
    )
    if patch:
        merge_run_metadata(run, patch)
    if run.status == "draft" and (run.user_prompt or patch.get("script")):
        run.status = "in_progress"
    out = await runtime.build_run_out(db, run)
    await db.commit()
    await runtime.publish_event(
        user_id,
        run.id,
        "storyboard.updated",
        {"run": out.model_dump(mode="json")},
    )
    return out


async def delete_storyboard(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    runtime: StoryboardRuntime,
) -> dict[str, bool]:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    deleted_at = runtime.now()
    run.deleted_at = deleted_at
    if run.conversation_id:
        conversation = (
            await db.execute(
                select(Conversation).where(
                    Conversation.id == run.conversation_id,
                    Conversation.user_id == user_id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conversation is not None:
            conversation.deleted_at = deleted_at
    await db.commit()
    await runtime.publish_event(user_id, run.id, "storyboard.deleted", {})
    return {"ok": True}


async def add_asset(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    body: StoryboardAssetCreateIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    step = WorkflowStep(
        workflow_run_id=run.id,
        step_key=asset_step_key(runtime.new_id()),
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
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out


async def patch_asset(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    step_id: str,
    body: StoryboardAssetPatchIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="asset", lock=True)
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
            output = dict(step.output_json or {})
            output.pop("approved_at", None)
            step.output_json = output
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out


async def approve_asset(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    step_id: str,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="asset", lock=True)
    await runtime.sync_outputs(db, run)
    output = dict(step.output_json or {})
    if not output.get("image_id"):
        raise http_error(
            "asset_image_required",
            "generate an asset image before approval",
            422,
        )
    now = runtime.now()
    step.status = "approved"
    step.approved_at = now
    step.approved_by = user_id
    output["approved_at"] = now.isoformat()
    step.output_json = output
    out = await runtime.build_run_out(db, run)
    await db.commit()
    await runtime.publish_event(
        user_id,
        run.id,
        "storyboard.asset_ready",
        {"asset_id": step.id},
    )
    return out


async def delete_asset(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    step_id: str,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="asset", lock=True)
    await db.delete(step)
    shots = [
        item
        for item in await runtime.load_steps(db, run.id, lock=True)
        if step_kind(item) == "shot"
    ]
    for shot in shots:
        data = dict(shot.input_json or {})
        asset_ids = [
            asset_id for asset_id in data.get("asset_ids", []) if asset_id != step_id
        ]
        if asset_ids != data.get("asset_ids", []):
            data["asset_ids"] = asset_ids
            shot.input_json = data
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out


async def rebuild_shots(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    body: StoryboardShotsRebuildIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    existing = [
        step
        for step in await runtime.load_steps(db, run.id)
        if step_kind(step) == "shot"
    ]
    if body.replace:
        for shot in existing:
            await db.delete(shot)
        await db.flush()
        existing = []
    metadata = run_metadata(run)
    shots = (
        body.shots
        if body.shots is not None
        else storyboard_patching.shots_from_script(
            str(metadata.get("script") or run.user_prompt)
        )
    )
    offset = len(existing)
    for index, item in enumerate(shots, start=1 + offset):
        data = storyboard_patching.shot_input_from_body(item, index=index)
        data["asset_ids"] = await storyboard_patching.validate_asset_ids(
            db,
            run,
            data.get("asset_ids") or [],
        )
        db.add(
            WorkflowStep(
                workflow_run_id=run.id,
                step_key=shot_step_key(runtime.new_id()),
                status="draft",
                input_json=data,
                output_json={},
            )
        )
    run.current_step = "shots"
    run.status = "in_progress"
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out


async def add_shot(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    body: StoryboardShotCreateIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    shots = [
        step
        for step in await runtime.load_steps(db, run.id)
        if step_kind(step) == "shot"
    ]
    data = storyboard_patching.shot_input_from_body(body, index=len(shots) + 1)
    data["asset_ids"] = await storyboard_patching.validate_asset_ids(
        db,
        run,
        data.get("asset_ids") or [],
    )
    db.add(
        WorkflowStep(
            workflow_run_id=run.id,
            step_key=shot_step_key(runtime.new_id()),
            status="draft",
            input_json=data,
            output_json={},
        )
    )
    run.current_step = "shots"
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out


async def patch_shot(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    step_id: str,
    body: StoryboardShotPatchIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="shot", lock=True)
    data = storyboard_patching.shot_input_from_body(
        body,
        existing=dict(step.input_json or {}),
    )
    data["asset_ids"] = await storyboard_patching.validate_asset_ids(
        db,
        run,
        data.get("asset_ids") or [],
    )
    before_hash = short_hash(step.input_json or {})
    after_hash = short_hash(data)
    step.input_json = data
    if before_hash != after_hash and step.status in {
        "keyframe_ready",
        "keyframe_approved",
        "generating",
        "done",
    }:
        step.status = "approved"
        output = clear_shot_video_output(dict(step.output_json or {}))
        output.pop("keyframe_approved_at", None)
        step.output_json = output
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out


async def approve_shot(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    step_id: str,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="shot", lock=True)
    if rank_status(step.status) < rank_status("approved"):
        step.status = "approved"
    step.approved_at = runtime.now()
    step.approved_by = user_id
    run.current_step = "shots"
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out


async def approve_keyframe(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    step_id: str,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="shot", lock=True)
    await runtime.sync_outputs(db, run)
    assets = [
        item
        for item in await runtime.load_steps(db, run.id)
        if step_kind(item) == "asset"
    ]
    source_hash = shot_source_hash(
        step,
        {asset.id: asset for asset in assets},
    )
    input_data = dict(step.input_json or {})
    output = dict(step.output_json or {})
    if not output.get("keyframe_image_id"):
        raise http_error(
            "keyframe_required",
            "generate a keyframe before approval",
            422,
        )
    if input_data.get("keyframe_source_hash") != source_hash:
        raise http_error(
            "keyframe_stale",
            "keyframe is stale; regenerate before approval",
            422,
        )
    now = runtime.now()
    step.status = "keyframe_approved"
    step.approved_at = now
    step.approved_by = user_id
    output["keyframe_approved_at"] = now.isoformat()
    step.output_json = output
    out = await runtime.build_run_out(db, run)
    await db.commit()
    await runtime.publish_event(
        user_id,
        run.id,
        "storyboard.keyframe_ready",
        {"shot_id": step.id},
    )
    return out


async def delete_shot(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    step_id: str,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="shot", lock=True)
    await db.delete(step)
    await db.flush()
    shots = [
        item
        for item in await runtime.load_steps(db, run.id)
        if step_kind(item) == "shot"
    ]
    normalize_shot_indexes(shots)
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out


async def move_shot(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    step_id: str,
    body: StoryboardShotMoveIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    target = await runtime.get_step(db, run, step_id, kind="shot", lock=True)
    shots = [
        item
        for item in await runtime.load_steps(db, run.id)
        if step_kind(item) == "shot"
    ]
    ordered = sorted(
        shots,
        key=lambda item: (
            int((item.input_json or {}).get("index") or 0),
            item.created_at,
            item.id,
        ),
    )
    position = next(
        (index for index, shot in enumerate(ordered) if shot.id == target.id),
        -1,
    )
    new_position = position + body.direction
    if position < 0 or new_position < 0 or new_position >= len(ordered):
        out = await runtime.build_run_out(db, run)
        await db.commit()
        return out
    ordered[position], ordered[new_position] = (
        ordered[new_position],
        ordered[position],
    )
    for index, shot in enumerate(ordered, start=1):
        data = dict(shot.input_json or {})
        data["index"] = index
        shot.input_json = data
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out
