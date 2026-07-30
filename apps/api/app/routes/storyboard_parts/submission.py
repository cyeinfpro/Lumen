"""Storyboard image and video task submission workflows."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import Intent, MAX_PROMPT_CHARS, Role
from lumen_core.model_entities import (
    Message,
    User,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.schema_models import (
    ChatParamsIn,
    ImageParamsIn,
    VideoCreateIn,
)

from ...services.storyboard import patching as storyboard_patching
from ...services.storyboard.common import (
    STORYBOARD_DEFAULT_ASPECT_RATIO,
    STORYBOARD_DEFAULT_DURATION_S,
    STORYBOARD_DEFAULT_MODEL,
    STORYBOARD_DEFAULT_RESOLUTION,
    STORYBOARD_WORKFLOW_TYPE,
    clean_string_list,
    clear_shot_video_output,
    http_error,
    rank_status,
    run_metadata,
    shot_source_hash,
    step_kind,
)
from ...services.storyboard.contracts import (
    StoryboardGenerateIn,
    StoryboardImageTask,
    StoryboardRunOut,
    StoryboardSubmitShotIn,
)
from ..messages import create_assistant_task
from ..videos import create_video_generation_record
from .runtime import StoryboardRuntime


async def create_storyboard_image_task(
    *,
    db: AsyncSession,
    user: User,
    run: WorkflowRun,
    step: WorkflowStep,
    prompt: str,
    attachment_ids: list[str],
    purpose: str,
    runtime: StoryboardRuntime,
) -> StoryboardImageTask:
    conversation = await runtime.get_or_create_conversation(db, user=user, run=run)
    user_message = Message(
        conversation_id=conversation.id,
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
    db.add(user_message)
    await db.flush()
    metadata = run_metadata(run)
    result = await create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=getattr(user, "account_mode", "wallet"),
        conv=conversation,
        user_msg=user_message,
        intent=Intent.IMAGE_TO_IMAGE if attachment_ids else Intent.TEXT_TO_IMAGE,
        idempotency_key=(f"storyboard:{run.id}:{step.id}:{purpose}:{runtime.new_id()}"),
        image_params=ImageParamsIn.model_validate(
            {
                "aspect_ratio": str(
                    metadata.get("aspect_ratio") or STORYBOARD_DEFAULT_ASPECT_RATIO
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
        raise http_error(
            "task_not_created",
            "image generation task was not created",
            500,
        )
    return StoryboardImageTask(
        generation_id=result.generation_ids[0],
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_message_id=result.assistant_msg.id,
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )


async def generate_asset(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    step_id: str,
    body: StoryboardGenerateIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="asset", lock=True)
    prompt = storyboard_patching.asset_prompt(run, step, body.prompt)
    task = await create_storyboard_image_task(
        db=db,
        user=user,
        run=run,
        step=step,
        prompt=prompt,
        attachment_ids=[],
        purpose="asset",
        runtime=runtime,
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
    out = await runtime.build_run_out(db, run)
    await db.commit()
    await runtime.publish_image_task(db=db, user_id=user.id, task=task)
    await runtime.publish_event(
        user.id,
        run.id,
        "storyboard.asset_generating",
        {"asset_id": step.id, "generation_id": task.generation_id},
    )
    return out


async def generate_shot_keyframe(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    step_id: str,
    body: StoryboardGenerateIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="shot", lock=True)
    if rank_status(step.status) < rank_status("approved"):
        raise http_error(
            "shot_not_approved",
            "approve the shot before keyframe generation",
            422,
        )
    asset_ids = await storyboard_patching.validate_asset_ids(
        db,
        run,
        (step.input_json or {}).get("asset_ids") or [],
        require_approved=True,
    )
    assets = [
        item
        for item in await runtime.load_steps(db, run.id)
        if step_kind(item) == "asset"
    ]
    assets_by_id = {asset.id: asset for asset in assets}
    attachment_ids = clean_string_list(
        [
            dict(assets_by_id[asset_id].output_json or {}).get("image_id")
            for asset_id in asset_ids
            if asset_id in assets_by_id
        ]
    )
    source_hash = shot_source_hash(step, assets_by_id)
    prompt = storyboard_patching.shot_keyframe_prompt(
        run,
        step,
        assets_by_id,
        body.prompt,
    )
    task = await create_storyboard_image_task(
        db=db,
        user=user,
        run=run,
        step=step,
        prompt=prompt,
        attachment_ids=attachment_ids,
        purpose="keyframe",
        runtime=runtime,
    )
    input_data = dict(step.input_json or {})
    input_data["keyframe_prompt"] = prompt
    input_data["keyframe_source_hash"] = source_hash
    step.input_json = input_data
    step.status = "keyframe_generating"
    step.task_ids = [task.generation_id]
    step.output_json = {
        **clear_shot_video_output(dict(step.output_json or {})),
        "keyframe_generation_id": task.generation_id,
        "keyframe_image_id": None,
        "keyframe_approved_at": None,
        "error_code": None,
        "error_message": None,
    }
    out = await runtime.build_run_out(db, run)
    await db.commit()
    await runtime.publish_image_task(db=db, user_id=user.id, task=task)
    await runtime.publish_event(
        user.id,
        run.id,
        "storyboard.keyframe_generating",
        {"shot_id": step.id, "generation_id": task.generation_id},
    )
    return out


async def generate_all_keyframes(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user.id, run_id=run_id, lock=True)
    await runtime.sync_outputs(db, run)
    steps = await runtime.load_steps(db, run.id, lock=True)
    assets = [step for step in steps if step_kind(step) == "asset"]
    shots = [step for step in steps if step_kind(step) == "shot"]
    assets_by_id = {asset.id: asset for asset in assets}
    not_approved = [
        shot.id
        for shot in shots
        if rank_status(shot.status) < rank_status("approved")
        or shot.status in {"keyframe_generating", "generating"}
    ]
    if not_approved:
        raise http_error(
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
            != shot_source_hash(shot, assets_by_id)
        )
    ]
    planned: list[tuple[WorkflowStep, list[str], str, str]] = []
    for shot in candidates:
        asset_ids = await storyboard_patching.validate_asset_ids(
            db,
            run,
            (shot.input_json or {}).get("asset_ids") or [],
            require_approved=True,
        )
        attachment_ids = clean_string_list(
            [
                dict(assets_by_id[asset_id].output_json or {}).get("image_id")
                for asset_id in asset_ids
                if asset_id in assets_by_id
            ]
        )
        planned.append(
            (
                shot,
                attachment_ids,
                shot_source_hash(shot, assets_by_id),
                storyboard_patching.shot_keyframe_prompt(
                    run,
                    shot,
                    assets_by_id,
                    None,
                ),
            )
        )
    tasks: list[tuple[WorkflowStep, StoryboardImageTask]] = []
    for shot, attachment_ids, source_hash, prompt in planned:
        task = await create_storyboard_image_task(
            db=db,
            user=user,
            run=run,
            step=shot,
            prompt=prompt,
            attachment_ids=attachment_ids,
            purpose="keyframe",
            runtime=runtime,
        )
        input_data = dict(shot.input_json or {})
        input_data["keyframe_prompt"] = prompt
        input_data["keyframe_source_hash"] = source_hash
        shot.input_json = input_data
        shot.status = "keyframe_generating"
        shot.task_ids = [task.generation_id]
        shot.output_json = {
            **clear_shot_video_output(dict(shot.output_json or {})),
            "keyframe_generation_id": task.generation_id,
            "keyframe_image_id": None,
            "keyframe_approved_at": None,
            "error_code": None,
            "error_message": None,
        }
        tasks.append((shot, task))
    run = await runtime.get_run(db, user_id=user.id, run_id=run_id)
    out = await runtime.build_run_out(db, run)
    await db.commit()
    await runtime.publish_image_tasks(
        db=db,
        user_id=user.id,
        tasks=[task for _shot, task in tasks],
    )
    for shot, task in tasks:
        await runtime.publish_event(
            user.id,
            run.id,
            "storyboard.keyframe_generating",
            {"shot_id": shot.id, "generation_id": task.generation_id},
        )
    return out


async def submit_shot(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    step_id: str,
    body: StoryboardSubmitShotIn,
    request: Request,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    if getattr(user, "account_mode", "wallet") != "wallet":
        raise http_error(
            "account_mode_forbidden",
            "video generation requires wallet mode",
            403,
        )
    run = await runtime.get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="shot", lock=True)
    await runtime.sync_outputs(db, run)
    assets = [
        item
        for item in await runtime.load_steps(db, run.id)
        if step_kind(item) == "asset"
    ]
    source_hash = shot_source_hash(step, {asset.id: asset for asset in assets})
    input_data = dict(step.input_json or {})
    output = dict(step.output_json or {})
    keyframe_id = output.get("keyframe_image_id")
    if step.status != "keyframe_approved" or not keyframe_id:
        raise http_error(
            "keyframe_not_approved",
            "approve the keyframe before video submission",
            422,
        )
    if input_data.get("keyframe_source_hash") != source_hash:
        raise http_error(
            "keyframe_stale",
            "keyframe is stale; regenerate before submission",
            422,
        )
    metadata = run_metadata(run)
    prompt = (
        body.prompt
        or input_data.get("keyframe_prompt")
        or input_data.get("visual")
        or run.user_prompt
    )
    idempotency_key, submission_fingerprint = resolve_storyboard_video_idempotency_key(
        run_id=run.id,
        step=step,
        keyframe_image_id=str(keyframe_id),
        requested_key=body.idempotency_key,
        runtime=runtime,
    )
    output["video_submission"] = {
        "fingerprint": submission_fingerprint,
        "idempotency_key": idempotency_key,
        "created_at": runtime.now().isoformat(),
    }
    output.update({"error_code": None, "error_message": None})
    step.output_json = output
    step.status = "generating"
    run.current_step = "videos"
    await db.flush()
    video_body = VideoCreateIn.model_validate(
        {
            "action": "i2v",
            "model": str(metadata.get("model") or STORYBOARD_DEFAULT_MODEL),
            "prompt": str(prompt)[:MAX_PROMPT_CHARS],
            "input_image_id": str(keyframe_id),
            "duration_s": body.duration_s
            or int(input_data.get("duration_s") or STORYBOARD_DEFAULT_DURATION_S),
            "resolution": str(
                metadata.get("resolution") or STORYBOARD_DEFAULT_RESOLUTION
            ),
            "aspect_ratio": str(
                metadata.get("aspect_ratio") or STORYBOARD_DEFAULT_ASPECT_RATIO
            ),
            "generate_audio": bool(metadata.get("generate_audio", True)),
            "seed": (
                metadata.get("seed") if isinstance(metadata.get("seed"), int) else None
            ),
            "watermark": False,
            "idempotency_key": idempotency_key,
        }
    )
    generation = await create_video_generation_record(
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
    run = await runtime.get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="shot", lock=True)
    step.status = "generating"
    step.task_ids = [generation.id]
    step.output_json = {
        **(step.output_json or {}),
        "video_generation_id": generation.id,
        "error_code": None,
        "error_message": None,
    }
    run.current_step = "videos"
    out = await runtime.build_run_out(db, run)
    await db.commit()
    await runtime.publish_event(
        user.id,
        run.id,
        "storyboard.shot_submitted",
        {"shot_id": step.id, "video_generation_id": generation.id},
    )
    return out


async def submit_all_shots(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    request: Request,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user.id, run_id=run_id)
    out = await runtime.build_run_out(db, run)
    candidates = [
        shot
        for shot in out.shots
        if shot.status == "keyframe_approved"
        and shot.keyframe_image_id
        and not shot.keyframe_stale
    ]
    for shot in candidates:
        await submit_shot(
            db=db,
            user=user,
            run_id=run_id,
            step_id=shot.id,
            body=StoryboardSubmitShotIn(),
            request=request,
            runtime=runtime,
        )
    run = await runtime.get_run(db, user_id=user.id, run_id=run_id)
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out


def resolve_storyboard_video_idempotency_key(
    *,
    run_id: str,
    step: WorkflowStep,
    keyframe_image_id: str,
    requested_key: str | None,
    runtime: StoryboardRuntime,
) -> tuple[str, str]:
    from ...services.storyboard import assembly as storyboard_assembly

    return storyboard_assembly.resolve_storyboard_video_idempotency_key(
        run_id=run_id,
        step=step,
        keyframe_image_id=keyframe_image_id,
        requested_key=requested_key,
        nonce_factory=runtime.new_id,
    )
