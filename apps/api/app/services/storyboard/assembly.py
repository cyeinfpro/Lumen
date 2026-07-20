"""Storyboard video submission and assembly idempotency policies."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Any

from lumen_core.models import WorkflowStep, new_uuid7

from .common import (
    STORYBOARD_ASSEMBLY_WAITING_LEASE_S,
    STORYBOARD_ASSEMBLY_WORKER_LEASE_S,
    short_hash,
    utc_now,
)


def storyboard_video_submission_fingerprint(
    *,
    step: WorkflowStep,
    keyframe_image_id: str,
) -> str:
    inp = dict(step.input_json or {})
    out = dict(step.output_json or {})
    return short_hash(
        {
            "keyframe_generation_id": out.get("keyframe_generation_id"),
            "keyframe_image_id": keyframe_image_id,
            "keyframe_source_hash": inp.get("keyframe_source_hash"),
        }
    )


def new_storyboard_video_idempotency_key(
    *,
    run_id: str,
    step_id: str,
    submission_fingerprint: str,
    nonce_factory: Callable[[], str] = new_uuid7,
) -> str:
    token = short_hash(
        {
            "run_id": run_id,
            "step_id": step_id,
            "submission_fingerprint": submission_fingerprint,
            "nonce": nonce_factory(),
        }
    )[:16]
    return f"sb:{run_id}:{step_id}:v:{token}"[:96]


def resolve_storyboard_video_idempotency_key(
    *,
    run_id: str,
    step: WorkflowStep,
    keyframe_image_id: str,
    requested_key: str | None,
    nonce_factory: Callable[[], str] = new_uuid7,
) -> tuple[str, str]:
    submission_fingerprint = storyboard_video_submission_fingerprint(
        step=step,
        keyframe_image_id=keyframe_image_id,
    )
    if requested_key:
        return requested_key[:96], submission_fingerprint
    output = dict(step.output_json or {})
    raw_submission = output.get("video_submission")
    submission = raw_submission if isinstance(raw_submission, dict) else {}
    existing_key = submission.get("idempotency_key")
    if (
        submission.get("fingerprint") == submission_fingerprint
        and isinstance(existing_key, str)
        and existing_key
    ):
        return existing_key[:96], submission_fingerprint
    return (
        new_storyboard_video_idempotency_key(
            run_id=run_id,
            step_id=step.id,
            submission_fingerprint=submission_fingerprint,
            nonce_factory=nonce_factory,
        ),
        submission_fingerprint,
    )


def storyboard_assembly_fingerprint(segment_ids: Iterable[str]) -> str:
    return short_hash({"segment_ids": list(segment_ids)})


def storyboard_assembly_idempotency_key(
    *,
    run_id: str,
    fingerprint: str,
) -> str:
    return f"sb:{run_id}:assembly:{fingerprint}"[:96]


def parse_assembly_datetime(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str) and raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def assembly_lease_expiry(
    assembly: WorkflowStep,
    output: dict[str, Any],
) -> datetime | None:
    explicit_expiry = parse_assembly_datetime(output.get("assembly_lease_expires_at"))
    if explicit_expiry is not None:
        return explicit_expiry

    claimed_at = parse_assembly_datetime(output.get("assembly_claimed_at"))
    if assembly.status == "compositing" and claimed_at is not None:
        heartbeat_at = parse_assembly_datetime(output.get("assembly_heartbeat_at"))
        base = heartbeat_at or claimed_at
        return base + timedelta(seconds=STORYBOARD_ASSEMBLY_WORKER_LEASE_S)

    enqueued_at = parse_assembly_datetime(output.get("assembly_enqueued_at"))
    updated_at = parse_assembly_datetime(getattr(assembly, "updated_at", None))
    waiting_base = enqueued_at or updated_at
    if waiting_base is None:
        return None
    return waiting_base + timedelta(seconds=STORYBOARD_ASSEMBLY_WAITING_LEASE_S)


def assembly_attempt_is_stale(
    assembly: WorkflowStep,
    output: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    if assembly.status not in {"waiting", "compositing"}:
        return False
    expires_at = assembly_lease_expiry(assembly, output)
    if expires_at is None:
        return False
    current = parse_assembly_datetime(now) or utc_now()
    return expires_at <= current


def assembly_request_is_replay(
    assembly: WorkflowStep,
    output: dict[str, Any],
    fingerprint: str,
    *,
    now: datetime | None = None,
) -> bool:
    if output.get("assembly_fingerprint") != fingerprint:
        return False
    if assembly.status == "done":
        return True
    if assembly.status not in {"waiting", "compositing"}:
        return False
    return not assembly_attempt_is_stale(assembly, output, now=now)


def assembly_status_for_response(
    assembly: WorkflowStep,
    output: dict[str, Any],
) -> str:
    attempt_token = output.get("assembly_attempt_token")
    if (
        assembly.status == "waiting"
        and isinstance(attempt_token, str)
        and attempt_token
    ):
        return "compositing"
    return assembly.status
