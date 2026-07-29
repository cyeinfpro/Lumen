"""Transactional storyboard assembly scheduling and outbox handoff."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.arq_jobs import arq_job_id
from lumen_core.models import OutboxEvent

from ...services.storyboard import assembly as assembly_policy
from ...services.storyboard.common import (
    STORYBOARD_ASSEMBLY_WAITING_LEASE_S,
    http_error,
    normalize_shot_indexes,
    step_kind,
)
from ...services.storyboard.contracts import StoryboardRunOut
from .runtime import StoryboardRuntime


logger = logging.getLogger(__name__)


async def assemble_storyboard(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id, lock=True)
    await runtime.sync_outputs(db, run)
    steps = await runtime.load_steps(db, run.id, lock=True)
    shots = [step for step in steps if step_kind(step) == "shot"]
    normalize_shot_indexes(shots)
    if not shots:
        raise http_error("shots_required", "create shots before assembly", 422)
    not_done = [shot.id for shot in shots if shot.status != "done"]
    if not_done:
        raise http_error(
            "shots_not_done",
            "all shots must be done before assembly",
            422,
            shot_ids=not_done,
        )
    ordered = sorted(
        shots,
        key=lambda step: int((step.input_json or {}).get("index") or 0),
    )
    segment_ids = [
        str((shot.output_json or {}).get("video_generation_id"))
        for shot in ordered
        if (shot.output_json or {}).get("video_generation_id")
    ]
    if len(segment_ids) != len(ordered):
        raise http_error(
            "segment_missing",
            "one or more shots are missing generated video ids",
            422,
        )
    assembly = await runtime.assembly_step(db, run, lock=True)
    fingerprint = assembly_policy.storyboard_assembly_fingerprint(segment_ids)
    idempotency_key = assembly_policy.storyboard_assembly_idempotency_key(
        run_id=run.id,
        fingerprint=fingerprint,
    )
    current_output = dict(assembly.output_json or {})
    attempt_now = runtime.now()
    if assembly_policy.assembly_request_is_replay(
        assembly,
        current_output,
        fingerprint,
        now=attempt_now,
    ):
        out = await runtime.build_run_out(db, run)
        await db.commit()
        return out

    stale_recovery = current_output.get(
        "assembly_fingerprint"
    ) == fingerprint and assembly_policy.assembly_attempt_is_stale(
        assembly,
        current_output,
        now=attempt_now,
    )
    previous_attempt_token = current_output.get("assembly_attempt_token")
    if not isinstance(previous_attempt_token, str) or not previous_attempt_token:
        previous_attempt_token = None
    raw_recovery_count = current_output.get("assembly_recovery_count")
    recovery_count = (
        raw_recovery_count
        if isinstance(raw_recovery_count, int) and raw_recovery_count >= 0
        else 0
    )
    if stale_recovery:
        recovery_count += 1

    attempt_token = runtime.new_id()
    if assembly.status != "compositing":
        assembly.status = "waiting"
    lease_expires_at = attempt_now + timedelta(
        seconds=STORYBOARD_ASSEMBLY_WAITING_LEASE_S
    )
    assembly.output_json = {
        **current_output,
        "segment_ids": segment_ids,
        "assembly_fingerprint": fingerprint,
        "assembly_idempotency_key": idempotency_key,
        "assembly_attempt_token": attempt_token,
        "assembly_enqueued_at": attempt_now.isoformat(),
        "assembly_claimed_at": None,
        "assembly_heartbeat_at": None,
        "assembly_lease_expires_at": lease_expires_at.isoformat(),
        "assembly_completed_at": None,
        "assembly_recovery_count": recovery_count,
        "assembly_recovery_reason": "lease_expired" if stale_recovery else None,
        "assembly_superseded_attempt_token": (
            previous_attempt_token if stale_recovery else None
        ),
        "video_id": None,
        "error_code": None,
        "error_message": None,
    }
    payload: dict[str, Any] = {
        "task_id": run.id,
        "run_id": run.id,
        "user_id": user_id,
        "kind": "storyboard_assembly",
        "assembly_fingerprint": fingerprint,
        "assembly_idempotency_key": idempotency_key,
        "assembly_attempt_token": attempt_token,
        "assembly_lease_expires_at": lease_expires_at.isoformat(),
        "assembly_recovered": stale_recovery,
    }
    outbox = OutboxEvent(kind="storyboard_assembly", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    outbox_id = str(outbox.id)
    payload["outbox_id"] = outbox_id
    outbox.payload = dict(payload)
    assembly.task_ids = [outbox.id]
    run.current_step = "assembly"
    out = await runtime.build_run_out(db, run)
    await db.commit()

    try:
        pool = await runtime.arq_pool()
        await pool.enqueue_job(  # type: ignore[attr-defined]
            "run_storyboard_assembly",
            run.id,
            attempt_token,
            _job_id=arq_job_id("storyboard_assembly", run.id, outbox_id),
        )
    except Exception:
        logger.warning(
            "storyboard assembly enqueue failed run=%s",
            run.id,
            exc_info=True,
        )
    await runtime.publish_event(
        user_id,
        run.id,
        "storyboard.assembling",
        {
            "segment_ids": segment_ids,
            "assembly_fingerprint": fingerprint,
            "assembly_attempt_token": attempt_token,
            "recovered": stale_recovery,
        },
    )
    return out
