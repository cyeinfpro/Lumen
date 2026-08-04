"""Paid storyboard asset and keyframe image submission workflows."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import Intent, Role
from lumen_core.model_entities import Message, User, WorkflowRun, WorkflowStep
from lumen_core.schema_models import ChatParamsIn, ImageParamsIn

from ...services.active_user import (
    AccountMode,
    ActiveUserFenceError,
    ActiveUserSnapshot,
    account_mode_from_user,
    active_user_fence_http_error,
    lock_active_user_snapshot,
)
from ...services.storyboard import idempotency as storyboard_idempotency
from ...services.storyboard import patching as storyboard_patching
from ...services.storyboard.common import (
    STORYBOARD_DEFAULT_ASPECT_RATIO,
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
)
from ..messages import create_assistant_task
from .runtime import StoryboardRuntime


async def create_storyboard_image_task(
    *,
    db: AsyncSession,
    user: User,
    account_mode: AccountMode,
    run: WorkflowRun,
    step: WorkflowStep,
    prompt: str,
    attachment_ids: list[str],
    purpose: str,
    task_idempotency_key: str,
    idempotency_metadata: dict[str, str],
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
            **idempotency_metadata,
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
        account_mode=account_mode,
        conv=conversation,
        user_msg=user_message,
        intent=Intent.IMAGE_TO_IMAGE if attachment_ids else Intent.TEXT_TO_IMAGE,
        idempotency_key=task_idempotency_key,
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
            **idempotency_metadata,
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


async def _lock_storyboard_user(
    db: AsyncSession,
    user: User,
    *,
    expected_account_mode: AccountMode,
    session_id: str | None,
) -> ActiveUserSnapshot:
    try:
        return await lock_active_user_snapshot(
            db,
            user.id,
            expected_account_mode,
            session_id=session_id,
        )
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc


def _record_operation(
    run: WorkflowRun,
    operation: storyboard_idempotency.PaidStoryboardOperation,
    *,
    response: StoryboardRunOut,
    task_ids: list[str],
    child_task_keys: dict[str, str],
    runtime: StoryboardRuntime,
) -> None:
    storyboard_idempotency.record_paid_operation(
        run,
        operation,
        response=response,
        task_ids=task_ids,
        child_task_keys=child_task_keys,
        created_at=runtime.now(),
    )


async def generate_asset(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    step_id: str,
    body: StoryboardGenerateIn,
    idempotency_key: str | None,
    runtime: StoryboardRuntime,
    session_id: str | None = None,
) -> StoryboardRunOut:
    expected_account_mode = account_mode_from_user(user)
    client_key = storyboard_idempotency.resolve_client_idempotency_key(
        idempotency_key
    )
    operation = storyboard_idempotency.paid_storyboard_operation(
        user_id=user.id,
        idempotency_key=client_key,
        operation_namespace=storyboard_idempotency.ASSET_GENERATE_OPERATION,
        payload={
            "run_id": run_id,
            "step_id": step_id,
            "body": body.model_dump(mode="json"),
        },
    )

    async def action(
        paid: storyboard_idempotency.PaidStoryboardOperation,
    ) -> StoryboardRunOut:
        snapshot = await _lock_storyboard_user(
            db,
            user,
            expected_account_mode=expected_account_mode,
            session_id=session_id,
        )
        locked_user = snapshot.user
        run = await runtime.get_run(db, user_id=user.id, run_id=run_id, lock=True)
        step = await runtime.get_step(db, run, step_id, kind="asset", lock=True)
        prompt = storyboard_patching.asset_prompt(run, step, body.prompt)
        child_identity = f"asset:{step.id}:image"
        task_key = storyboard_idempotency.child_task_idempotency_key(
            paid,
            child_identity,
        )
        task = await create_storyboard_image_task(
            db=db,
            user=locked_user,
            account_mode=snapshot.account_mode,
            run=run,
            step=step,
            prompt=prompt,
            attachment_ids=[],
            purpose="asset",
            task_idempotency_key=task_key,
            idempotency_metadata=storyboard_idempotency.child_task_metadata(
                paid,
                child_identity,
            ),
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
        _record_operation(
            run,
            paid,
            response=out,
            task_ids=[task.generation_id],
            child_task_keys={child_identity: task_key},
            runtime=runtime,
        )
        await db.commit()
        await runtime.publish_image_task(db=db, user_id=user.id, task=task)
        await runtime.publish_event(
            user.id,
            run.id,
            "storyboard.asset_generating",
            {"asset_id": step.id, "generation_id": task.generation_id},
        )
        return out

    return await storyboard_idempotency.execute_paid_operation(db, operation, action)


async def generate_shot_keyframe(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    step_id: str,
    body: StoryboardGenerateIn,
    idempotency_key: str | None,
    runtime: StoryboardRuntime,
    session_id: str | None = None,
) -> StoryboardRunOut:
    expected_account_mode = account_mode_from_user(user)
    client_key = storyboard_idempotency.resolve_client_idempotency_key(
        idempotency_key
    )
    operation = storyboard_idempotency.paid_storyboard_operation(
        user_id=user.id,
        idempotency_key=client_key,
        operation_namespace=storyboard_idempotency.KEYFRAME_GENERATE_OPERATION,
        payload={
            "run_id": run_id,
            "step_id": step_id,
            "body": body.model_dump(mode="json"),
        },
    )

    async def action(
        paid: storyboard_idempotency.PaidStoryboardOperation,
    ) -> StoryboardRunOut:
        snapshot = await _lock_storyboard_user(
            db,
            user,
            expected_account_mode=expected_account_mode,
            session_id=session_id,
        )
        locked_user = snapshot.user
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
        child_identity = f"shot:{step.id}:keyframe"
        task_key = storyboard_idempotency.child_task_idempotency_key(
            paid,
            child_identity,
        )
        task = await create_storyboard_image_task(
            db=db,
            user=locked_user,
            account_mode=snapshot.account_mode,
            run=run,
            step=step,
            prompt=prompt,
            attachment_ids=attachment_ids,
            purpose="keyframe",
            task_idempotency_key=task_key,
            idempotency_metadata=storyboard_idempotency.child_task_metadata(
                paid,
                child_identity,
            ),
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
        _record_operation(
            run,
            paid,
            response=out,
            task_ids=[task.generation_id],
            child_task_keys={child_identity: task_key},
            runtime=runtime,
        )
        await db.commit()
        await runtime.publish_image_task(db=db, user_id=user.id, task=task)
        await runtime.publish_event(
            user.id,
            run.id,
            "storyboard.keyframe_generating",
            {"shot_id": step.id, "generation_id": task.generation_id},
        )
        return out

    return await storyboard_idempotency.execute_paid_operation(db, operation, action)


async def generate_all_keyframes(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    idempotency_key: str | None,
    runtime: StoryboardRuntime,
    session_id: str | None = None,
) -> StoryboardRunOut:
    expected_account_mode = account_mode_from_user(user)
    client_key = storyboard_idempotency.resolve_client_idempotency_key(
        idempotency_key
    )
    operation = storyboard_idempotency.paid_storyboard_operation(
        user_id=user.id,
        idempotency_key=client_key,
        operation_namespace=storyboard_idempotency.KEYFRAME_GENERATE_ALL_OPERATION,
        payload={"run_id": run_id},
    )

    async def action(
        paid: storyboard_idempotency.PaidStoryboardOperation,
    ) -> StoryboardRunOut:
        snapshot = await _lock_storyboard_user(
            db,
            user,
            expected_account_mode=expected_account_mode,
            session_id=session_id,
        )
        locked_user = snapshot.user
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
        planned: list[tuple[WorkflowStep, list[str], str, str, str, str]] = []
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
            child_identity = f"shot:{shot.id}:keyframe"
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
                    child_identity,
                    storyboard_idempotency.child_task_idempotency_key(
                        paid,
                        child_identity,
                    ),
                )
            )
        tasks: list[tuple[WorkflowStep, StoryboardImageTask]] = []
        child_task_keys: dict[str, str] = {}
        for (
            shot,
            attachment_ids,
            source_hash,
            prompt,
            child_identity,
            task_key,
        ) in planned:
            task = await create_storyboard_image_task(
                db=db,
                user=locked_user,
                account_mode=snapshot.account_mode,
                run=run,
                step=shot,
                prompt=prompt,
                attachment_ids=attachment_ids,
                purpose="keyframe",
                task_idempotency_key=task_key,
                idempotency_metadata=storyboard_idempotency.child_task_metadata(
                    paid,
                    child_identity,
                ),
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
            child_task_keys[child_identity] = task_key
        out = await runtime.build_run_out(db, run)
        _record_operation(
            run,
            paid,
            response=out,
            task_ids=[task.generation_id for _shot, task in tasks],
            child_task_keys=child_task_keys,
            runtime=runtime,
        )
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

    return await storyboard_idempotency.execute_paid_operation(db, operation, action)


__all__ = [
    "create_storyboard_image_task",
    "generate_all_keyframes",
    "generate_asset",
    "generate_shot_keyframe",
]
