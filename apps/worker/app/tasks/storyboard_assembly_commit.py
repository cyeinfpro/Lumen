"""Durable reconciliation helpers for storyboard assembly commits."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable

from arq import Retry
from sqlalchemy import select, update

from lumen_core.model_entities.media_workflows import (
    Video,
    WorkflowRun,
    WorkflowStep,
)

from ..artifact_commit import ArtifactAdoption
from ..db import affected_rows


ASSEMBLY_COMMIT_MARKER_FILENAME = "commit-recovery.json"
ASSEMBLY_COMMIT_MARKER_SCHEMA = "lumen.storyboard-assembly-commit-recovery.v1"
ASSEMBLY_RECOVERY_SCAN_LIMIT = 64
ASSEMBLY_ARTIFACT_IDENTITY_KEY = "assembly_artifact_identity"


class AssemblyArtifactVerificationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class AssemblyRecoveryCandidateMissing(RuntimeError):
    def __init__(self, *, claim: Any) -> None:
        self.claim = claim
        super().__init__("storyboard assembly recovery candidate is missing")


class AssemblyRecoveryCandidateInvalid(RuntimeError):
    def __init__(self, *, claim: Any) -> None:
        self.claim = claim
        super().__init__("storyboard assembly recovery candidate is invalid")


AsyncOperation = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class AssemblyCommitDeferralRuntime:
    max_attempts: int
    fail_assembly: AsyncOperation
    record_commit_unknown: AsyncOperation
    enqueue_commit_reconcile: AsyncOperation
    logger: logging.Logger


@dataclass(frozen=True, slots=True)
class AssemblyCommitReconcileRuntime:
    probe_adoption: AsyncOperation
    verify_candidate_artifacts: AsyncOperation
    fail_assembly: AsyncOperation
    defer_reconcile: AsyncOperation
    delete_recovery_marker: AsyncOperation
    publish_success: AsyncOperation
    verify_recovery_marker: AsyncOperation
    complete_assembly: AsyncOperation
    commit_unknown_error: type[BaseException]
    logger: logging.Logger


def assembly_video_candidate(video: Video) -> dict[str, Any]:
    return {
        "id": video.id,
        "user_id": video.user_id,
        "owner_generation_id": video.owner_generation_id,
        "storage_key": video.storage_key,
        "poster_storage_key": video.poster_storage_key,
        "mime": video.mime,
        "width": int(video.width or 0),
        "height": int(video.height or 0),
        "duration_ms": int(video.duration_ms or 0),
        "fps": video.fps,
        "size_bytes": int(video.size_bytes or 0),
        "sha256": video.sha256,
        "etag": video.etag,
        "has_audio": bool(video.has_audio),
        "faststart": bool(video.faststart),
        "visibility": video.visibility,
        "metadata_jsonb": dict(video.metadata_jsonb or {}),
    }


def video_from_assembly_candidate(candidate: dict[str, Any]) -> Video:
    required = ("id", "user_id", "storage_key", "mime", "sha256", "etag")
    if any(
        not isinstance(candidate.get(field), str) or not candidate.get(field)
        for field in required
    ):
        raise ValueError("invalid storyboard assembly commit candidate")
    metadata = candidate.get("metadata_jsonb")
    return Video(
        id=str(candidate["id"]),
        user_id=str(candidate["user_id"]),
        owner_generation_id=(
            str(candidate["owner_generation_id"])
            if isinstance(candidate.get("owner_generation_id"), str)
            and candidate.get("owner_generation_id")
            else None
        ),
        storage_key=str(candidate["storage_key"]),
        poster_storage_key=(
            str(candidate["poster_storage_key"])
            if isinstance(candidate.get("poster_storage_key"), str)
            and candidate.get("poster_storage_key")
            else None
        ),
        mime=str(candidate["mime"]),
        width=max(0, int(candidate.get("width") or 0)),
        height=max(0, int(candidate.get("height") or 0)),
        duration_ms=max(0, int(candidate.get("duration_ms") or 0)),
        fps=(
            float(candidate["fps"])
            if isinstance(candidate.get("fps"), (int, float))
            else None
        ),
        size_bytes=max(0, int(candidate.get("size_bytes") or 0)),
        sha256=str(candidate["sha256"]),
        etag=str(candidate["etag"]),
        has_audio=bool(candidate.get("has_audio")),
        faststart=bool(candidate.get("faststart")),
        visibility=(
            str(candidate["visibility"])
            if isinstance(candidate.get("visibility"), str)
            and candidate.get("visibility")
            else "private"
        ),
        metadata_jsonb=dict(metadata) if isinstance(metadata, dict) else {},
    )


def assembly_recovery_marker_key(video: Video) -> str:
    return f"{video.storage_key.rsplit('/', 1)[0]}/{ASSEMBLY_COMMIT_MARKER_FILENAME}"


def assembly_recovery_artifact_keys(video: Video) -> tuple[str, ...]:
    keys = [video.storage_key]
    if video.poster_storage_key:
        keys.append(video.poster_storage_key)
    keys.append(assembly_recovery_marker_key(video))
    return tuple(keys)


def assembly_recovery_marker_bytes(video: Video) -> bytes:
    payload = {
        "schema": ASSEMBLY_COMMIT_MARKER_SCHEMA,
        "candidate": assembly_video_candidate(video),
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _assembly_recovery_candidate(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid storyboard assembly recovery marker") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != ASSEMBLY_COMMIT_MARKER_SCHEMA
        or not isinstance(payload.get("candidate"), dict)
    ):
        raise ValueError("invalid storyboard assembly recovery marker")
    return dict(payload["candidate"])


def load_assembly_recovery_candidate(
    *,
    storage: Any,
    run_id: str,
    user_id: str,
    attempt_token: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    base_key = f"u/{user_id}/storyboards/{run_id}/assembly"
    try:
        base_path = storage.path_for(base_key)
        version_paths = sorted(
            (
                path
                for path in base_path.iterdir()
                if not path.is_symlink() and path.is_dir()
            ),
            key=lambda path: path.name,
            reverse=True,
        )[:ASSEMBLY_RECOVERY_SCAN_LIMIT]
    except FileNotFoundError:
        return None

    for version_path in version_paths:
        marker_key = f"{base_key}/{version_path.name}/{ASSEMBLY_COMMIT_MARKER_FILENAME}"
        try:
            candidate = _assembly_recovery_candidate(storage.get_bytes(marker_key))
            video = video_from_assembly_candidate(candidate)
        except (FileNotFoundError, ValueError, TypeError):
            continue
        if assembly_recovery_marker_key(
            video
        ) == marker_key and assembly_candidate_matches_attempt(
            video,
            run_id=run_id,
            user_id=user_id,
            attempt_token=attempt_token,
            fingerprint=fingerprint,
        ):
            return candidate
    return None


def _artifact_identity(video: Video) -> dict[str, Any]:
    metadata = dict(video.metadata_jsonb or {})
    identity = metadata.get(ASSEMBLY_ARTIFACT_IDENTITY_KEY)
    if not isinstance(identity, dict):
        raise AssemblyArtifactVerificationError(
            "assembly_artifact_identity_missing",
            "storyboard assembly artifact identity is missing",
        )
    return identity


def _verify_artifact_path(
    *,
    label: str,
    key: str,
    expected_size: int,
    expected_sha256: str,
    path_for: Callable[[str], Path],
) -> None:
    try:
        path = path_for(key)
        size_bytes = path.stat().st_size
    except FileNotFoundError as exc:
        raise AssemblyArtifactVerificationError(
            "assembly_artifact_missing",
            f"storyboard assembly {label} artifact is missing",
        ) from exc
    if size_bytes != expected_size:
        raise AssemblyArtifactVerificationError(
            "assembly_artifact_identity_mismatch",
            f"storyboard assembly {label} artifact identity mismatch",
        )
    digest = hashlib.sha256()
    try:
        with path.open("rb") as artifact:
            while chunk := artifact.read(1024 * 1024):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise AssemblyArtifactVerificationError(
            "assembly_artifact_missing",
            f"storyboard assembly {label} artifact is missing",
        ) from exc
    if digest.hexdigest() != expected_sha256:
        raise AssemblyArtifactVerificationError(
            "assembly_artifact_identity_mismatch",
            f"storyboard assembly {label} artifact identity mismatch",
        )


def verify_assembly_candidate_artifacts(
    video: Video,
    *,
    path_for: Callable[[str], Path],
) -> None:
    identity = _artifact_identity(video)
    video_identity = identity.get("video")
    if not isinstance(video_identity, dict):
        raise AssemblyArtifactVerificationError(
            "assembly_artifact_identity_missing",
            "storyboard assembly video identity is missing",
        )
    expected_video_identity = {
        "storage_key": video.storage_key,
        "size_bytes": int(video.size_bytes or 0),
        "sha256": video.sha256,
    }
    if video.etag != video.sha256 or video_identity != expected_video_identity:
        raise AssemblyArtifactVerificationError(
            "assembly_artifact_identity_mismatch",
            "storyboard assembly video candidate identity mismatch",
        )
    _verify_artifact_path(
        label="video",
        key=video.storage_key,
        expected_size=expected_video_identity["size_bytes"],
        expected_sha256=expected_video_identity["sha256"],
        path_for=path_for,
    )

    poster_identity = identity.get("poster")
    if video.poster_storage_key is None:
        if poster_identity is not None:
            raise AssemblyArtifactVerificationError(
                "assembly_artifact_identity_mismatch",
                "storyboard assembly poster candidate identity mismatch",
            )
        return
    if not isinstance(poster_identity, dict):
        raise AssemblyArtifactVerificationError(
            "assembly_artifact_identity_missing",
            "storyboard assembly poster identity is missing",
        )
    poster_key = poster_identity.get("storage_key")
    poster_size = poster_identity.get("size_bytes")
    poster_sha256 = poster_identity.get("sha256")
    if (
        poster_key != video.poster_storage_key
        or not isinstance(poster_size, int)
        or poster_size < 0
        or not isinstance(poster_sha256, str)
        or not poster_sha256
    ):
        raise AssemblyArtifactVerificationError(
            "assembly_artifact_identity_mismatch",
            "storyboard assembly poster candidate identity mismatch",
        )
    _verify_artifact_path(
        label="poster",
        key=video.poster_storage_key,
        expected_size=poster_size,
        expected_sha256=poster_sha256,
        path_for=path_for,
    )


def verify_assembly_recovery_marker(
    video: Video,
    *,
    read_bytes: Callable[[str], bytes],
) -> None:
    try:
        candidate = _assembly_recovery_candidate(
            read_bytes(assembly_recovery_marker_key(video))
        )
    except FileNotFoundError as exc:
        raise AssemblyArtifactVerificationError(
            "assembly_recovery_marker_missing",
            "storyboard assembly recovery marker is missing",
        ) from exc
    except ValueError as exc:
        raise AssemblyArtifactVerificationError(
            "assembly_recovery_marker_invalid",
            "storyboard assembly recovery marker is invalid",
        ) from exc
    if candidate != assembly_video_candidate(video):
        raise AssemblyArtifactVerificationError(
            "assembly_recovery_marker_mismatch",
            "storyboard assembly recovery marker identity mismatch",
        )


def assembly_candidate_matches_attempt(
    video: Video,
    *,
    run_id: str,
    user_id: str,
    attempt_token: str,
    fingerprint: str,
) -> bool:
    metadata = dict(video.metadata_jsonb or {})
    identity = metadata.get(ASSEMBLY_ARTIFACT_IDENTITY_KEY)
    video_identity = identity.get("video") if isinstance(identity, dict) else None
    poster_identity = identity.get("poster") if isinstance(identity, dict) else object()
    primary_parts = tuple(video.storage_key.split("/"))
    expected_prefix = ("u", user_id, "storyboards", run_id, "assembly")
    if (
        video.user_id != user_id
        or video.owner_generation_id is not None
        or len(primary_parts) != 7
        or primary_parts[:5] != expected_prefix
        or primary_parts[5] in {"", ".", ".."}
        or "\x00" in primary_parts[5]
        or "\\" in primary_parts[5]
        or primary_parts[6] != "output.mp4"
        or metadata.get("workflow_type") != "storyboard"
        or metadata.get("workflow_run_id") != run_id
        or metadata.get("assembly_attempt_token") != attempt_token
        or metadata.get("assembly_fingerprint") != fingerprint
        or video.etag != video.sha256
        or video_identity
        != {
            "storage_key": video.storage_key,
            "size_bytes": int(video.size_bytes or 0),
            "sha256": video.sha256,
        }
    ):
        return False
    if video.poster_storage_key is None:
        return poster_identity is None
    poster_parts = tuple(video.poster_storage_key.split("/"))
    return (
        len(poster_parts) == 7
        and poster_parts[:6] == primary_parts[:6]
        and poster_parts[6] == "poster.jpg"
        and isinstance(poster_identity, dict)
        and poster_identity.get("storage_key") == video.poster_storage_key
        and isinstance(poster_identity.get("size_bytes"), int)
        and poster_identity.get("size_bytes") >= 0
        and isinstance(poster_identity.get("sha256"), str)
        and bool(poster_identity.get("sha256"))
    )


async def load_assembly_commit_reconcile_target(
    run_id: str,
    *,
    expected_attempt_token: str | None,
    candidate: dict[str, Any] | None,
    session_factory: Callable[[], Any],
    claim_factory: Callable[..., Any],
    target_factory: Callable[..., Any],
    recovery_candidate_loader: Callable[..., dict[str, Any] | None] | None = None,
    require_recovery_candidate: bool = False,
) -> Any | None:
    async with session_factory() as session:
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
            return None
        step = (
            await session.execute(
                select(WorkflowStep).where(
                    WorkflowStep.workflow_run_id == run.id,
                    WorkflowStep.step_key == "assembly",
                )
            )
        ).scalar_one_or_none()
        if step is None or step.status not in {"compositing", "done"}:
            return None
        output = dict(step.output_json or {})
        attempt_token = output.get("assembly_attempt_token")
        fingerprint = output.get("assembly_fingerprint")
        if not isinstance(attempt_token, str) or not attempt_token:
            return None
        if expected_attempt_token and expected_attempt_token != attempt_token:
            return None
        if not isinstance(fingerprint, str) or not fingerprint:
            return None
        idempotency_key = output.get("assembly_idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            idempotency_key = None
        segment_ids = tuple(
            value
            for value in output.get("segment_ids") or []
            if isinstance(value, str) and value
        )
        claim = claim_factory(
            run_id=run.id,
            user_id=run.user_id,
            step_id=step.id,
            attempt_token=attempt_token,
            fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            segment_ids=segment_ids,
            output_json=output,
        )
        raw_candidate = candidate
        if raw_candidate is None and output.get("assembly_commit_state") == "unknown":
            stored_candidate = output.get("assembly_commit_candidate")
            raw_candidate = (
                stored_candidate if isinstance(stored_candidate, dict) else None
            )
        if raw_candidate is None and recovery_candidate_loader is not None:
            commit = getattr(session, "commit", None)
            if callable(commit):
                await commit()
            raw_candidate = await asyncio.to_thread(
                recovery_candidate_loader,
                run_id=run.id,
                user_id=run.user_id,
                attempt_token=attempt_token,
                fingerprint=fingerprint,
            )
        if raw_candidate is None:
            if require_recovery_candidate and step.status == "compositing":
                raise AssemblyRecoveryCandidateMissing(claim=claim)
            return None
        try:
            video = video_from_assembly_candidate(raw_candidate)
        except (OverflowError, TypeError, ValueError) as exc:
            raise AssemblyRecoveryCandidateInvalid(claim=claim) from exc
        if not assembly_candidate_matches_attempt(
            video,
            run_id=run.id,
            user_id=run.user_id,
            attempt_token=attempt_token,
            fingerprint=fingerprint,
        ):
            raise AssemblyRecoveryCandidateInvalid(claim=claim)
        return target_factory(
            claim=claim,
            video=video,
        )


async def record_assembly_commit_unknown(
    claim: Any,
    video: Video,
    *,
    reconcile_attempt: int,
    recorded_at: datetime,
    lease_ttl_s: int,
    session_factory: Callable[[], Any],
    attempt_predicates: Callable[..., tuple[Any, ...]],
) -> bool:
    output = {
        **claim.output_json,
        "assembly_commit_state": "unknown",
        "assembly_commit_candidate": assembly_video_candidate(video),
        "assembly_commit_unknown_at": recorded_at.isoformat(),
        "assembly_commit_reconcile_attempt": max(0, reconcile_attempt),
        "assembly_heartbeat_at": recorded_at.isoformat(),
        "assembly_lease_expires_at": (
            recorded_at + timedelta(seconds=lease_ttl_s)
        ).isoformat(),
    }
    async with session_factory() as session:
        result = await session.execute(
            update(WorkflowStep)
            .where(
                *attempt_predicates(
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
        await session.commit()
    claim.output_json.clear()
    claim.output_json.update(output)
    return True


def assembly_commit_reconcile_delay(reconcile_attempt: int) -> int:
    exponent = min(4, max(0, int(reconcile_attempt)))
    return min(60, 5 * (2**exponent))


async def enqueue_assembly_commit_reconcile(
    redis: Any,
    *,
    claim: Any,
    video: Video,
    reconcile_attempt: int,
) -> None:
    next_attempt = max(0, int(reconcile_attempt)) + 1
    delay = assembly_commit_reconcile_delay(reconcile_attempt)
    await redis.enqueue_job(
        "run_storyboard_assembly",
        claim.run_id,
        claim.attempt_token,
        assembly_video_candidate(video),
        next_attempt,
        _defer_by=delay,
        _job_id=(
            f"lumen:storyboard-assembly-commit:{claim.run_id}:"
            f"{claim.attempt_token}:{next_attempt}"
        ),
    )


async def defer_assembly_commit_reconcile(
    redis: Any,
    *,
    claim: Any,
    video: Video,
    reconcile_attempt: int,
    runtime: AssemblyCommitDeferralRuntime,
) -> None:
    reconcile_attempt = max(
        max(0, int(reconcile_attempt)),
        (
            int(claim.output_json.get("assembly_commit_reconcile_attempt"))
            if isinstance(
                claim.output_json.get("assembly_commit_reconcile_attempt"),
                int,
            )
            else 0
        ),
    )
    if reconcile_attempt >= runtime.max_attempts:
        await runtime.fail_assembly(
            redis,
            claim=claim,
            code="assembly_commit_reconcile_exhausted",
            message=(
                "storyboard assembly commit reconciliation exhausted "
                f"after {reconcile_attempt} attempts"
            ),
        )
        return

    persisted = False
    try:
        persisted = await runtime.record_commit_unknown(
            claim,
            video,
            reconcile_attempt=reconcile_attempt,
        )
    except Exception:
        runtime.logger.warning(
            "storyboard assembly commit-unknown persistence failed run=%s attempt=%s",
            claim.run_id,
            claim.attempt_token,
            exc_info=True,
        )
    try:
        await runtime.enqueue_commit_reconcile(
            redis,
            claim=claim,
            video=video,
            reconcile_attempt=reconcile_attempt,
        )
    except Exception:
        runtime.logger.error(
            "storyboard assembly commit reconciliation enqueue failed run=%s "
            "attempt=%s persisted=%s",
            claim.run_id,
            claim.attempt_token,
            persisted,
            exc_info=True,
        )
        raise Retry(defer=assembly_commit_reconcile_delay(reconcile_attempt)) from None


async def publish_assembly_success(
    redis: Any,
    *,
    claim: Any,
    video: Video,
    publish: AsyncOperation,
) -> None:
    await publish(
        redis,
        user_id=claim.user_id,
        run_id=claim.run_id,
        event_name="storyboard.assembled",
        data={
            "video_id": video.id,
            "segment_ids": list(claim.segment_ids),
            "assembly_fingerprint": claim.fingerprint,
            "progress_pct": 100,
        },
    )


async def handle_assembly_failure(
    redis: Any,
    *,
    run_id: str,
    claim: Any | None,
    reconcile_attempt: int,
    max_reconcile_attempts: int,
    failure: Exception,
    fail_assembly: AsyncOperation,
    retry_delay: Callable[[int], int],
    logger: logging.Logger,
) -> None:
    if (
        claim is None
        and reconcile_attempt > 0
        and reconcile_attempt < max_reconcile_attempts
    ):
        raise Retry(defer=retry_delay(reconcile_attempt)) from failure
    message = str(failure)
    logger.warning(
        "storyboard assembly failed run=%s err=%s",
        run_id,
        message,
        exc_info=True,
    )
    if claim is not None:
        await fail_assembly(
            redis,
            claim=claim,
            code=message.split(":", 1)[0] or "assembly_failed",
            message=message,
        )


async def probe_assembly_adoption(
    claim: Any,
    video: Video,
    *,
    session_factory: Callable[[], Any],
    candidate_matches_attempt: Callable[..., bool],
) -> ArtifactAdoption:
    async with session_factory() as session:
        step = await session.get(
            WorkflowStep,
            claim.step_id,
            with_for_update=True,
        )
        persisted_video = await session.get(Video, video.id)
        if persisted_video is not None:
            output = (
                step.output_json
                if step is not None and isinstance(step.output_json, dict)
                else {}
            )
            exact_step = (
                step is not None
                and step.status == "done"
                and output.get("video_id") == video.id
                and output.get("assembly_attempt_token") == claim.attempt_token
                and output.get("assembly_fingerprint") == claim.fingerprint
            )
            exact_video = (
                persisted_video.user_id == claim.user_id
                and persisted_video.storage_key == video.storage_key
                and persisted_video.poster_storage_key == video.poster_storage_key
                and int(persisted_video.size_bytes or 0) == int(video.size_bytes or 0)
                and persisted_video.sha256 == video.sha256
                and persisted_video.etag == video.etag
                and candidate_matches_attempt(
                    persisted_video,
                    run_id=claim.run_id,
                    user_id=claim.user_id,
                    attempt_token=claim.attempt_token,
                    fingerprint=claim.fingerprint,
                )
            )
            return (
                ArtifactAdoption.ADOPTED
                if exact_step and exact_video
                else ArtifactAdoption.UNKNOWN
            )
        if step is not None:
            output = step.output_json if isinstance(step.output_json, dict) else {}
            if step.status == "done" and output.get("video_id") == video.id:
                return ArtifactAdoption.UNKNOWN
        return ArtifactAdoption.NOT_ADOPTED


async def reconcile_assembly_commit(
    redis: Any,
    *,
    target: Any,
    reconcile_attempt: int,
    storage_writes: Any,
    runtime: AssemblyCommitReconcileRuntime,
) -> None:
    claim = target.claim
    video = target.video
    try:
        outcome = await runtime.probe_adoption(claim, video)
    except Exception:
        runtime.logger.warning(
            "storyboard assembly commit reconciliation probe failed run=%s attempt=%s",
            claim.run_id,
            claim.attempt_token,
            exc_info=True,
        )
        await runtime.defer_reconcile(
            redis,
            claim=claim,
            video=video,
            reconcile_attempt=reconcile_attempt,
        )
        return
    try:
        await runtime.verify_candidate_artifacts(video)
    except AssemblyArtifactVerificationError as exc:
        runtime.logger.error(
            "storyboard assembly commit candidate rejected run=%s attempt=%s "
            "code=%s err=%s",
            claim.run_id,
            claim.attempt_token,
            exc.code,
            exc,
        )
        failed = await runtime.fail_assembly(
            redis,
            claim=claim,
            code=exc.code,
            message=str(exc),
        )
        if failed and outcome is ArtifactAdoption.NOT_ADOPTED:
            await storage_writes.delete_files(assembly_recovery_artifact_keys(video))
        return
    except OSError:
        runtime.logger.warning(
            "storyboard assembly artifact verification unavailable run=%s attempt=%s",
            claim.run_id,
            claim.attempt_token,
            exc_info=True,
        )
        await runtime.defer_reconcile(
            redis,
            claim=claim,
            video=video,
            reconcile_attempt=reconcile_attempt,
        )
        return
    if outcome is ArtifactAdoption.ADOPTED:
        await runtime.delete_recovery_marker(storage_writes, video)
        await runtime.publish_success(redis, claim=claim, video=video)
        return
    if outcome is ArtifactAdoption.UNKNOWN:
        await runtime.defer_reconcile(
            redis,
            claim=claim,
            video=video,
            reconcile_attempt=reconcile_attempt,
        )
        return
    try:
        await runtime.verify_recovery_marker(video)
    except AssemblyArtifactVerificationError as exc:
        runtime.logger.error(
            "storyboard assembly recovery marker rejected run=%s attempt=%s "
            "code=%s err=%s",
            claim.run_id,
            claim.attempt_token,
            exc.code,
            exc,
        )
        failed = await runtime.fail_assembly(
            redis,
            claim=claim,
            code=exc.code,
            message=str(exc),
        )
        if failed:
            await storage_writes.delete_files(assembly_recovery_artifact_keys(video))
        return
    except OSError:
        runtime.logger.warning(
            "storyboard assembly recovery marker verification unavailable "
            "run=%s attempt=%s",
            claim.run_id,
            claim.attempt_token,
            exc_info=True,
        )
        await runtime.defer_reconcile(
            redis,
            claim=claim,
            video=video,
            reconcile_attempt=reconcile_attempt,
        )
        return
    try:
        completed = await runtime.complete_assembly(claim, video)
    except runtime.commit_unknown_error as exc:
        await runtime.defer_reconcile(
            redis,
            claim=claim,
            video=exc.video,
            reconcile_attempt=reconcile_attempt,
        )
        return
    if completed:
        await runtime.delete_recovery_marker(storage_writes, video)
        await runtime.publish_success(redis, claim=claim, video=video)
