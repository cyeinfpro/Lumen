"""Paid storyboard video shot submission workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import MAX_PROMPT_CHARS
from lumen_core.model_entities import (
    User,
    VideoGeneration,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.schema_models import VideoCreateIn

from ...deps import durable_session_id
from ...services.active_user import (
    ActiveUserFenceError,
    ActiveUserSnapshot,
    account_mode_from_user,
    active_user_fence_http_error,
    lock_active_user_snapshot,
)
from ...services.storyboard import assembly as storyboard_assembly
from ...services.storyboard import idempotency as storyboard_idempotency
from ...services.storyboard.common import (
    STORYBOARD_DEFAULT_ASPECT_RATIO,
    STORYBOARD_DEFAULT_DURATION_S,
    STORYBOARD_DEFAULT_MODEL,
    STORYBOARD_DEFAULT_RESOLUTION,
    STORYBOARD_WORKFLOW_TYPE,
    http_error,
    run_metadata,
    shot_source_hash,
    step_kind,
)
from ...services.storyboard.contracts import (
    StoryboardRunOut,
    StoryboardSubmitShotIn,
)
from ...services.video.submission import (
    generation_request_fingerprint as video_generation_request_fingerprint,
    invalidate_video_balance_cache,
    request_fingerprint as video_request_fingerprint,
)
from ...services.video_publish import publish_video_queued
from ..videos import create_video_generation_record
from .runtime import StoryboardRuntime


@dataclass(frozen=True, slots=True)
class _StoryboardVideoPlan:
    step: WorkflowStep
    keyframe_image_id: str
    submission_fingerprint: str
    body: VideoCreateIn


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


def _video_body(
    run: WorkflowRun,
    step: WorkflowStep,
    body: StoryboardSubmitShotIn,
    *,
    keyframe_image_id: str,
    task_idempotency_key: str,
) -> VideoCreateIn:
    metadata = run_metadata(run)
    input_data = dict(step.input_json or {})
    prompt = (
        body.prompt
        or input_data.get("keyframe_prompt")
        or input_data.get("visual")
        or run.user_prompt
    )
    return VideoCreateIn.model_validate(
        {
            "action": "i2v",
            "model": str(metadata.get("model") or STORYBOARD_DEFAULT_MODEL),
            "prompt": str(prompt)[:MAX_PROMPT_CHARS],
            "input_image_id": keyframe_image_id,
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
            "idempotency_key": task_idempotency_key,
        }
    )


def _video_plan(
    run: WorkflowRun,
    step: WorkflowStep,
    assets_by_id: dict[str, WorkflowStep],
    body: StoryboardSubmitShotIn,
    *,
    task_idempotency_key: str,
) -> _StoryboardVideoPlan:
    source_hash = shot_source_hash(step, assets_by_id)
    input_data = dict(step.input_json or {})
    output = dict(step.output_json or {})
    keyframe_id = output.get("keyframe_image_id")
    if step.status != "keyframe_approved" or not isinstance(keyframe_id, str):
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
    submission_fingerprint = (
        storyboard_assembly.storyboard_video_submission_fingerprint(
            step=step,
            keyframe_image_id=keyframe_id,
        )
    )
    return _StoryboardVideoPlan(
        step=step,
        keyframe_image_id=keyframe_id,
        submission_fingerprint=submission_fingerprint,
        body=_video_body(
            run,
            step,
            body,
            keyframe_image_id=keyframe_id,
            task_idempotency_key=task_idempotency_key,
        ),
    )


async def create_storyboard_video_task(
    *,
    db: AsyncSession,
    user: User,
    run: WorkflowRun,
    plan: _StoryboardVideoPlan,
    request: Request,
    idempotency_metadata: dict[str, str],
    active_user_snapshot: ActiveUserSnapshot,
) -> tuple[str, dict[str, Any]]:
    publish_payload: dict[str, Any] = {}
    async with db.begin_nested():
        generation = await create_video_generation_record(
            db,
            plan.body,
            user,
            request=request,
            active_user_snapshot=active_user_snapshot,
            idempotency_serialized=True,
            workflow_metadata={
                "workflow_type": STORYBOARD_WORKFLOW_TYPE,
                "workflow_run_id": run.id,
                "workflow_step_key": plan.step.step_key,
                "storyboard_purpose": "shot_video",
                "storyboard_keyframe_image_id": plan.keyframe_image_id,
                "storyboard_video_submission_fingerprint": (
                    plan.submission_fingerprint
                ),
                "source": "storyboard",
                "action_source": "storyboard_video",
                **idempotency_metadata,
            },
            defer_commit=True,
            deferred_publish_payload=publish_payload,
        )
    return generation.id, publish_payload


def _mark_video_submitted(
    run: WorkflowRun,
    plan: _StoryboardVideoPlan,
    *,
    generation_id: str,
    task_idempotency_key: str,
    operation: storyboard_idempotency.PaidStoryboardOperation,
    runtime: StoryboardRuntime,
) -> None:
    output = dict(plan.step.output_json or {})
    output["video_submission"] = {
        "fingerprint": plan.submission_fingerprint,
        "idempotency_key": task_idempotency_key,
        "client_key_hash": operation.client_key_hash,
        "operation_namespace": operation.operation_namespace,
        "request_fingerprint": operation.request_fingerprint,
        "created_at": runtime.now().isoformat(),
    }
    output.update(
        {
            "video_generation_id": generation_id,
            "error_code": None,
            "error_message": None,
        }
    )
    plan.step.output_json = output
    plan.step.status = "generating"
    plan.step.task_ids = [generation_id]
    run.current_step = "videos"


async def _publish_video_tasks(
    *,
    user_id: str,
    payloads: list[dict[str, Any]],
) -> None:
    if not payloads:
        return
    await invalidate_video_balance_cache(user_id)
    for payload in payloads:
        await publish_video_queued(payload)


async def _legacy_submit_shot_replay(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    step_id: str,
    body: StoryboardSubmitShotIn,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await runtime.get_step(db, run, step_id, kind="shot", lock=True)
    await runtime.sync_outputs(db, run)
    output = dict(step.output_json or {})
    raw_submission = output.get("video_submission")
    submission = raw_submission if isinstance(raw_submission, dict) else {}
    task_key = submission.get("idempotency_key")
    keyframe_image_id = output.get("keyframe_image_id")
    if (
        not isinstance(task_key, str)
        or not task_key
        or not isinstance(keyframe_image_id, str)
        or not keyframe_image_id
    ):
        raise http_error(
            "idempotency_key_required",
            "Idempotency-Key is required for paid storyboard operations",
            422,
        )
    expected_submission_fingerprint = (
        storyboard_assembly.storyboard_video_submission_fingerprint(
            step=step,
            keyframe_image_id=keyframe_image_id,
        )
    )
    if submission.get("fingerprint") != expected_submission_fingerprint:
        raise http_error(
            "idempotency_key_required",
            "Idempotency-Key is required for paid storyboard operations",
            422,
        )
    expected_body = _video_body(
        run,
        step,
        body,
        keyframe_image_id=keyframe_image_id,
        task_idempotency_key=task_key,
    )
    generation = (
        await db.execute(
            select(VideoGeneration).where(
                VideoGeneration.user_id == user.id,
                VideoGeneration.idempotency_key == task_key,
            )
        )
    ).scalar_one_or_none()
    request_metadata = (
        generation.upstream_request
        if generation is not None and isinstance(generation.upstream_request, dict)
        else {}
    )
    if (
        generation is None
        or video_generation_request_fingerprint(generation)
        != video_request_fingerprint(expected_body)
        or request_metadata.get("workflow_run_id") != run.id
        or request_metadata.get("workflow_step_key") != step.step_key
        or request_metadata.get("storyboard_video_submission_fingerprint")
        != expected_submission_fingerprint
    ):
        raise http_error(
            "idempotency_key_required",
            "Idempotency-Key is required for paid storyboard operations",
            422,
        )
    step.task_ids = [generation.id]
    step.output_json = {
        **output,
        "video_generation_id": generation.id,
        "error_code": None,
        "error_message": None,
    }
    if step.status == "keyframe_approved":
        step.status = "generating"
    run.current_step = "videos"
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out


async def submit_shot(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    step_id: str,
    body: StoryboardSubmitShotIn,
    request: Request,
    idempotency_key: str | None,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    if idempotency_key is None and body.idempotency_key is None:
        return await _legacy_submit_shot_replay(
            db=db,
            user=user,
            run_id=run_id,
            step_id=step_id,
            body=body,
            runtime=runtime,
        )
    client_key = storyboard_idempotency.resolve_client_idempotency_key(
        idempotency_key,
        body.idempotency_key,
    )
    operation = storyboard_idempotency.paid_storyboard_operation(
        user_id=user.id,
        idempotency_key=client_key,
        operation_namespace=storyboard_idempotency.SHOT_SUBMIT_OPERATION,
        payload={
            "run_id": run_id,
            "step_id": step_id,
            "body": body.model_dump(mode="json", exclude={"idempotency_key"}),
        },
    )
    expected_account_mode = account_mode_from_user(user)

    async def action(
        paid: storyboard_idempotency.PaidStoryboardOperation,
    ) -> StoryboardRunOut:
        try:
            snapshot = await lock_active_user_snapshot(
                db,
                user.id,
                expected_account_mode,
                session_id=durable_session_id(request),
            )
        except ActiveUserFenceError as exc:
            raise active_user_fence_http_error(exc) from exc
        locked_user = snapshot.user
        if snapshot.account_mode != "wallet":
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
        child_identity = f"shot:{step.id}:video"
        task_key = storyboard_idempotency.child_task_idempotency_key(
            paid,
            child_identity,
        )
        plan = _video_plan(
            run,
            step,
            {asset.id: asset for asset in assets},
            body,
            task_idempotency_key=task_key,
        )
        generation_id, publish_payload = await create_storyboard_video_task(
            db=db,
            user=locked_user,
            run=run,
            plan=plan,
            request=request,
            idempotency_metadata=storyboard_idempotency.child_task_metadata(
                paid,
                child_identity,
            ),
            active_user_snapshot=snapshot,
        )
        _mark_video_submitted(
            run,
            plan,
            generation_id=generation_id,
            task_idempotency_key=task_key,
            operation=paid,
            runtime=runtime,
        )
        out = await runtime.build_run_out(db, run)
        _record_operation(
            run,
            paid,
            response=out,
            task_ids=[generation_id],
            child_task_keys={child_identity: task_key},
            runtime=runtime,
        )
        await db.commit()
        await _publish_video_tasks(user_id=user.id, payloads=[publish_payload])
        await runtime.publish_event(
            user.id,
            run.id,
            "storyboard.shot_submitted",
            {"shot_id": step.id, "video_generation_id": generation_id},
        )
        return out

    return await storyboard_idempotency.execute_paid_operation(db, operation, action)


async def submit_all_shots(
    *,
    db: AsyncSession,
    user: User,
    run_id: str,
    request: Request,
    idempotency_key: str | None,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    client_key = storyboard_idempotency.resolve_client_idempotency_key(
        idempotency_key
    )
    operation = storyboard_idempotency.paid_storyboard_operation(
        user_id=user.id,
        idempotency_key=client_key,
        operation_namespace=storyboard_idempotency.SHOTS_SUBMIT_ALL_OPERATION,
        payload={"run_id": run_id},
    )
    expected_account_mode = account_mode_from_user(user)

    async def action(
        paid: storyboard_idempotency.PaidStoryboardOperation,
    ) -> StoryboardRunOut:
        try:
            snapshot = await lock_active_user_snapshot(
                db,
                user.id,
                expected_account_mode,
                session_id=durable_session_id(request),
            )
        except ActiveUserFenceError as exc:
            raise active_user_fence_http_error(exc) from exc
        locked_user = snapshot.user
        if snapshot.account_mode != "wallet":
            raise http_error(
                "account_mode_forbidden",
                "video generation requires wallet mode",
                403,
            )
        run = await runtime.get_run(db, user_id=user.id, run_id=run_id, lock=True)
        await runtime.sync_outputs(db, run)
        steps = await runtime.load_steps(db, run.id, lock=True)
        assets_by_id = {
            step.id: step for step in steps if step_kind(step) == "asset"
        }
        candidates = [
            step
            for step in steps
            if step_kind(step) == "shot"
            and step.status == "keyframe_approved"
            and isinstance(
                dict(step.output_json or {}).get("keyframe_image_id"),
                str,
            )
            and dict(step.input_json or {}).get("keyframe_source_hash")
            == shot_source_hash(step, assets_by_id)
        ]
        plans: list[
            tuple[_StoryboardVideoPlan, str, str, dict[str, str]]
        ] = []
        for shot in candidates:
            child_identity = f"shot:{shot.id}:video"
            task_key = storyboard_idempotency.child_task_idempotency_key(
                paid,
                child_identity,
            )
            plans.append(
                (
                    _video_plan(
                        run,
                        shot,
                        assets_by_id,
                        StoryboardSubmitShotIn(),
                        task_idempotency_key=task_key,
                    ),
                    child_identity,
                    task_key,
                    storyboard_idempotency.child_task_metadata(
                        paid,
                        child_identity,
                    ),
                )
            )
        generation_ids: list[str] = []
        publish_payloads: list[dict[str, Any]] = []
        child_task_keys: dict[str, str] = {}
        submitted: list[tuple[WorkflowStep, str]] = []
        for plan, child_identity, task_key, idempotency_metadata in plans:
            generation_id, publish_payload = await create_storyboard_video_task(
                db=db,
                user=locked_user,
                run=run,
                plan=plan,
                request=request,
                idempotency_metadata=idempotency_metadata,
                active_user_snapshot=snapshot,
            )
            _mark_video_submitted(
                run,
                plan,
                generation_id=generation_id,
                task_idempotency_key=task_key,
                operation=paid,
                runtime=runtime,
            )
            generation_ids.append(generation_id)
            publish_payloads.append(publish_payload)
            child_task_keys[child_identity] = task_key
            submitted.append((plan.step, generation_id))
        out = await runtime.build_run_out(db, run)
        _record_operation(
            run,
            paid,
            response=out,
            task_ids=generation_ids,
            child_task_keys=child_task_keys,
            runtime=runtime,
        )
        await db.commit()
        await _publish_video_tasks(user_id=user.id, payloads=publish_payloads)
        for step, generation_id in submitted:
            await runtime.publish_event(
                user.id,
                run.id,
                "storyboard.shot_submitted",
                {"shot_id": step.id, "video_generation_id": generation_id},
            )
        return out

    return await storyboard_idempotency.execute_paid_operation(db, operation, action)


def resolve_storyboard_video_idempotency_key(
    *,
    run_id: str,
    step: WorkflowStep,
    keyframe_image_id: str,
    requested_key: str | None,
    runtime: StoryboardRuntime,
) -> tuple[str, str]:
    return storyboard_assembly.resolve_storyboard_video_idempotency_key(
        run_id=run_id,
        step=step,
        keyframe_image_id=keyframe_image_id,
        requested_key=requested_key,
        nonce_factory=runtime.new_id,
    )


__all__ = [
    "create_storyboard_video_task",
    "resolve_storyboard_video_idempotency_key",
    "submit_all_shots",
    "submit_shot",
]
