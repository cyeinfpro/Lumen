"""Durable ownership fencing for finalized video artifacts."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lumen_core.constants import (
    EV_VIDEO_SUCCEEDED,
    VideoGenerationStage,
    VideoGenerationStatus,
)
from lumen_core.model_entities import Video, VideoGeneration

from ...artifact_commit import (
    ArtifactAdoption,
    commit_error_or_default,
    commit_with_adoption_probe,
)
from ...video_upstream_service import PollResult
from .contracts import StoredVideo
from .runtime import video_ports


_VIDEO_ARTIFACT_FENCE_KEY = "video_artifact_fence"
_VIDEO_ARTIFACT_PENDING = "pending"
_VIDEO_ARTIFACT_ADOPTED = "adopted"
_VIDEO_ARTIFACT_CLEANED = "cleaned"


@dataclass(frozen=True, slots=True)
class _VideoArtifactFence:
    owner_token: str
    execution_epoch: int
    attempt_epoch: int
    artifact_attempt_id: str

    def payload(self, *, state: str) -> dict[str, Any]:
        return {
            "owner_token": self.owner_token,
            "execution_epoch": self.execution_epoch,
            "attempt_epoch": self.attempt_epoch,
            "artifact_attempt_id": self.artifact_attempt_id,
            "state": state,
        }


@dataclass(frozen=True, slots=True)
class VideoSuccessAdoptionOutcome:
    terminal_committed: bool
    cleanup_created_artifacts: bool
    release_provider_slot: bool


def _video_artifact_execution_epoch(generation: Any) -> int:
    return max(0, int(getattr(generation, "submission_epoch", 0) or 0))


def _video_artifact_attempt_epoch(generation: Any) -> int:
    return max(0, int(getattr(generation, "attempt", 0) or 0))


def _video_artifact_fence_payload(generation: Any) -> dict[str, Any] | None:
    raw_diagnostics = getattr(generation, "diagnostics", None)
    diagnostics = raw_diagnostics if isinstance(raw_diagnostics, dict) else {}
    payload = diagnostics.get(_VIDEO_ARTIFACT_FENCE_KEY)
    return payload if isinstance(payload, dict) else None


def _generation_matches_video_artifact_fence(
    generation: Any,
    fence: _VideoArtifactFence,
    *,
    states: set[str],
) -> bool:
    payload = _video_artifact_fence_payload(generation)
    return bool(
        payload is not None
        and payload.get("owner_token") == fence.owner_token
        and payload.get("execution_epoch") == fence.execution_epoch
        and payload.get("attempt_epoch") == fence.attempt_epoch
        and payload.get("artifact_attempt_id") == fence.artifact_attempt_id
        and payload.get("state") in states
        and _video_artifact_execution_epoch(generation) == fence.execution_epoch
        and _video_artifact_attempt_epoch(generation) == fence.attempt_epoch
    )


def _pending_video_artifact_fence(
    generation: Any,
    *,
    artifact_attempt_id: str | None,
) -> _VideoArtifactFence | None:
    if artifact_attempt_id is None:
        return None
    payload = _video_artifact_fence_payload(generation)
    if payload is None:
        return None
    owner_token = payload.get("owner_token")
    execution_epoch = payload.get("execution_epoch")
    attempt_epoch = payload.get("attempt_epoch")
    if (
        not isinstance(owner_token, str)
        or not owner_token
        or not isinstance(execution_epoch, int)
        or not isinstance(attempt_epoch, int)
    ):
        return None
    fence = _VideoArtifactFence(
        owner_token=owner_token,
        execution_epoch=execution_epoch,
        attempt_epoch=attempt_epoch,
        artifact_attempt_id=artifact_attempt_id,
    )
    if not _generation_matches_video_artifact_fence(
        generation,
        fence,
        states={_VIDEO_ARTIFACT_PENDING},
    ):
        return None
    return fence


def video_artifact_attempt_id(generation: VideoGeneration) -> str:
    """Return a stable finalization key for one provider execution."""
    identity = "\0".join(
        (
            str(generation.id),
            str(getattr(generation, "provider_task_id", "") or ""),
            str(max(0, int(getattr(generation, "submission_epoch", 0) or 0))),
            str(max(0, int(getattr(generation, "attempt", 0) or 0))),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


async def claim_video_artifact_fence(
    session: Any,
    generation: VideoGeneration,
    *,
    lease_lost: asyncio.Event | None,
    artifact_attempt_id: str,
) -> _VideoArtifactFence | None:
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before artifact ownership claim",
    )
    await session.refresh(generation, with_for_update=True)
    if (
        generation.status in video_ports()._TERMINAL_STATUSES
        or generation.cancel_requested_at is not None
    ):
        return None
    fence = _VideoArtifactFence(
        owner_token=video_ports().new_uuid7(),
        execution_epoch=_video_artifact_execution_epoch(generation),
        attempt_epoch=_video_artifact_attempt_epoch(generation),
        artifact_attempt_id=artifact_attempt_id,
    )
    diagnostics = (
        dict(generation.diagnostics) if isinstance(generation.diagnostics, dict) else {}
    )
    diagnostics[_VIDEO_ARTIFACT_FENCE_KEY] = fence.payload(
        state=_VIDEO_ARTIFACT_PENDING
    )
    generation.diagnostics = diagnostics
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before artifact ownership commit",
    )
    await session.commit()
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost after artifact ownership commit",
    )
    return fence


async def probe_video_success_adoption(
    *,
    generation_id: str,
    video: Video,
    fence: _VideoArtifactFence,
) -> ArtifactAdoption:
    async with video_ports().SessionLocal() as session:
        generation = await session.get(
            VideoGeneration,
            generation_id,
            with_for_update=True,
        )
        persisted_video = await session.get(Video, video.id)
        if persisted_video is not None:
            video_fence = (
                persisted_video.metadata_jsonb.get(_VIDEO_ARTIFACT_FENCE_KEY)
                if isinstance(persisted_video.metadata_jsonb, dict)
                else None
            )
            exact_video = (
                persisted_video.owner_generation_id == generation_id
                and persisted_video.user_id == video.user_id
                and persisted_video.storage_key == video.storage_key
                and persisted_video.poster_storage_key == video.poster_storage_key
                and persisted_video.sha256 == video.sha256
                and video_fence
                == fence.payload(state=_VIDEO_ARTIFACT_ADOPTED)
            )
            exact_generation = (
                generation is not None
                and generation.status == VideoGenerationStatus.SUCCEEDED.value
                and _generation_matches_video_artifact_fence(
                    generation,
                    fence,
                    states={_VIDEO_ARTIFACT_ADOPTED},
                )
            )
            return (
                ArtifactAdoption.ADOPTED
                if exact_video and exact_generation
                else ArtifactAdoption.UNKNOWN
            )
        if (
            generation is not None
            and generation.status == VideoGenerationStatus.SUCCEEDED.value
        ):
            return ArtifactAdoption.UNKNOWN
        return ArtifactAdoption.NOT_ADOPTED


async def finalize_video_success_adoption(
    session: Any,
    redis: Any,
    generation: VideoGeneration,
    poll: PollResult,
    stored: StoredVideo,
    fence: _VideoArtifactFence,
    *,
    lease_lost: asyncio.Event | None,
    probe_success_adoption: Callable[..., Awaitable[ArtifactAdoption]],
) -> VideoSuccessAdoptionOutcome:
    artifact_attempt_id = fence.artifact_attempt_id
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before success row lock",
    )
    await session.refresh(generation, with_for_update=True)
    if not _generation_matches_video_artifact_fence(
        generation,
        fence,
        states={_VIDEO_ARTIFACT_PENDING},
    ):
        video_ports().logger.warning(
            "video finalization ownership superseded task=%s "
            "epoch=%s attempt=%s owner=%s; artifacts retained",
            generation.id,
            fence.execution_epoch,
            fence.attempt_epoch,
            fence.owner_token,
        )
        return VideoSuccessAdoptionOutcome(
            terminal_committed=False,
            cleanup_created_artifacts=True,
            release_provider_slot=False,
        )
    if generation.status in video_ports()._TERMINAL_STATUSES:
        video_ports().logger.warning(
            "video finalization lost terminal race; deterministic artifacts "
            "retained for aged reconciliation task=%s attempt=%s",
            generation.id,
            artifact_attempt_id,
        )
        return VideoSuccessAdoptionOutcome(
            terminal_committed=False,
            cleanup_created_artifacts=False,
            release_provider_slot=False,
        )
    if generation.cancel_requested_at is not None:
        await video_ports()._finish_terminal_failure(
            session,
            redis,
            generation,
            video_ports()._cancelled_poll_during_finalization(poll),
            fallback_error_message="video task cancelled by user",
            lease_lost=lease_lost,
        )
        return VideoSuccessAdoptionOutcome(
            terminal_committed=False,
            cleanup_created_artifacts=True,
            release_provider_slot=False,
        )

    existing = await video_ports()._video_for_generation(session, generation.id)
    if existing is None:
        session.add(stored.video)
        await session.flush()
        video = stored.video
        adopt_stored_artifacts = True
    else:
        video = existing
        adopt_stored_artifacts = False
    diagnostics = {**(generation.diagnostics or {}), **stored.diagnostics}
    adopted_fence = fence.payload(state=_VIDEO_ARTIFACT_ADOPTED)
    diagnostics[_VIDEO_ARTIFACT_FENCE_KEY] = adopted_fence
    video_metadata = (
        dict(video.metadata_jsonb)
        if isinstance(getattr(video, "metadata_jsonb", None), dict)
        else {}
    )
    video_metadata[_VIDEO_ARTIFACT_FENCE_KEY] = adopted_fence
    video.metadata_jsonb = video_metadata
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before billing settlement",
    )
    resolution = await video_ports().resolve_video_billing(
        session,
        generation,
        poll_result=poll,
        reason="succeeded",
    )
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before success mutation",
    )
    diagnostics["billing_decision"] = resolution.decision
    generation.status = VideoGenerationStatus.SUCCEEDED.value
    generation.progress_stage = VideoGenerationStage.FINISHED.value
    generation.progress_pct = 100
    generation.upstream_response = poll.raw
    generation.diagnostics = diagnostics
    generation.billed_tokens = resolution.actual_tokens
    generation.billed_cost_micro = resolution.actual_micro
    generation.finished_at = video_ports()._now()
    video_ports()._queue_video_event(
        session,
        generation,
        EV_VIDEO_SUCCEEDED,
        video_id=video.id,
    )
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before success commit",
    )
    commit_result = await commit_with_adoption_probe(
        session,
        probe=lambda: probe_success_adoption(
            generation_id=generation.id,
            video=video,
            fence=fence,
        ),
        logger=video_ports().logger,
        label=f"video artifact task={generation.id} attempt={artifact_attempt_id}",
    )
    if commit_result.adopted:
        await video_ports().worker_flush_balance_cache(session)
        return VideoSuccessAdoptionOutcome(
            terminal_committed=True,
            cleanup_created_artifacts=not adopt_stored_artifacts,
            release_provider_slot=True,
        )
    if commit_result.outcome is ArtifactAdoption.NOT_ADOPTED:
        raise commit_error_or_default(
            commit_result,
            label=f"video artifact task={generation.id}",
        )
    video_ports().logger.error(
        "video artifact commit outcome unknown task=%s attempt=%s; "
        "artifacts retained for reconciliation",
        generation.id,
        artifact_attempt_id,
    )
    return VideoSuccessAdoptionOutcome(
        terminal_committed=False,
        cleanup_created_artifacts=False,
        release_provider_slot=True,
    )


async def cleanup_video_artifacts_if_owned(
    keys: tuple[str, ...] | list[str],
    *,
    generation_id: str,
    fence: _VideoArtifactFence,
    lease_lost: asyncio.Event | None,
    video_for_generation: Callable[[Any, str], Awaitable[Video | None]],
) -> bool:
    unique_keys = list(dict.fromkeys(keys))
    if not unique_keys:
        return True
    if lease_lost is not None and lease_lost.is_set():
        video_ports().logger.warning(
            "video artifact cleanup deferred after lease loss; deterministic "
            "keys retained for aged sweep task=%s keys=%s",
            generation_id,
            unique_keys,
        )
        return False
    try:
        async with video_ports().SessionLocal() as session:
            generation = await session.get(
                VideoGeneration,
                generation_id,
                with_for_update=True,
            )
            if generation is None or not _generation_matches_video_artifact_fence(
                generation,
                fence,
                states={
                    _VIDEO_ARTIFACT_PENDING,
                    _VIDEO_ARTIFACT_ADOPTED,
                },
            ):
                video_ports().logger.warning(
                    "video artifact cleanup fenced by durable owner task=%s "
                    "epoch=%s attempt=%s owner=%s keys=%s",
                    generation_id,
                    fence.execution_epoch,
                    fence.attempt_epoch,
                    fence.owner_token,
                    unique_keys,
                )
                return False

            payload = _video_artifact_fence_payload(generation)
            if payload is None:
                return False
            state = payload.get("state")
            adopted_video = await video_for_generation(session, generation_id)
            if state == _VIDEO_ARTIFACT_PENDING:
                if (
                    generation.status == VideoGenerationStatus.SUCCEEDED.value
                    or adopted_video is not None
                ):
                    video_ports().logger.error(
                        "video artifact cleanup retained inconsistent pending "
                        "adoption task=%s keys=%s",
                        generation_id,
                        unique_keys,
                    )
                    return False
                deletable_keys = unique_keys
            else:
                if (
                    state != _VIDEO_ARTIFACT_ADOPTED
                    or generation.status != VideoGenerationStatus.SUCCEEDED.value
                    or adopted_video is None
                ):
                    video_ports().logger.error(
                        "video artifact cleanup retained unconfirmed adoption "
                        "task=%s keys=%s",
                        generation_id,
                        unique_keys,
                    )
                    return False
                protected_keys = {
                    key
                    for key in (
                        adopted_video.storage_key,
                        adopted_video.poster_storage_key,
                    )
                    if isinstance(key, str) and key
                }
                deletable_keys = [
                    key for key in unique_keys if key not in protected_keys
                ]

            if deletable_keys:
                await video_ports()._delete_video_storage_keys(deletable_keys)
            if state == _VIDEO_ARTIFACT_PENDING:
                diagnostics = (
                    dict(generation.diagnostics)
                    if isinstance(generation.diagnostics, dict)
                    else {}
                )
                diagnostics[_VIDEO_ARTIFACT_FENCE_KEY] = fence.payload(
                    state=_VIDEO_ARTIFACT_CLEANED
                )
                generation.diagnostics = diagnostics
            await session.commit()
            return True
    except Exception as exc:  # noqa: BLE001
        video_ports().logger.error(
            "video artifact cleanup ownership check failed closed task=%s "
            "epoch=%s attempt=%s owner=%s keys=%s err=%s",
            generation_id,
            fence.execution_epoch,
            fence.attempt_epoch,
            fence.owner_token,
            unique_keys,
            exc,
        )
        return False
