"""Video artifact storage and terminal state persistence."""

from __future__ import annotations

import asyncio
import errno
import hashlib
from typing import Any

from sqlalchemy import select

from lumen_core.constants import (
    EV_VIDEO_CANCELED,
    EV_VIDEO_FAILED,
    EV_VIDEO_FETCHING,
    VideoGenerationStage,
    VideoGenerationStatus,
)
from lumen_core.models import Video, VideoGeneration

from ...artifact_commit import rollback_artifact_transaction
from ...storage import StorageDiskFullError
from ...storage_writes import StorageWriteOperation
from ...video_artifacts import (
    DownloadedVideo,
    ProcessedVideoFile,
)
from ...video_upstream_service import PollResult, VideoProviderAdapter
from . import artifact_fence as artifact_fence_helpers
from .contracts import StoredVideo
from .runtime import video_ports


_VIDEO_ARTIFACT_ADOPTED = artifact_fence_helpers._VIDEO_ARTIFACT_ADOPTED
_VIDEO_ARTIFACT_FENCE_KEY = artifact_fence_helpers._VIDEO_ARTIFACT_FENCE_KEY
_VIDEO_ARTIFACT_PENDING = artifact_fence_helpers._VIDEO_ARTIFACT_PENDING
_VideoArtifactFence = artifact_fence_helpers._VideoArtifactFence
_generation_matches_video_artifact_fence = (
    artifact_fence_helpers._generation_matches_video_artifact_fence
)
_pending_video_artifact_fence = (
    artifact_fence_helpers._pending_video_artifact_fence
)
_claim_video_artifact_fence_impl = artifact_fence_helpers.claim_video_artifact_fence
_cleanup_video_artifacts_if_owned_impl = (
    artifact_fence_helpers.cleanup_video_artifacts_if_owned
)
_finalize_video_success_adoption_impl = (
    artifact_fence_helpers.finalize_video_success_adoption
)
_probe_video_success_adoption = artifact_fence_helpers.probe_video_success_adoption
video_artifact_attempt_id = artifact_fence_helpers.video_artifact_attempt_id


async def _claim_video_artifact_fence(
    session: Any,
    generation: VideoGeneration,
    *,
    lease_lost: asyncio.Event | None,
) -> _VideoArtifactFence | None:
    return await _claim_video_artifact_fence_impl(
        session,
        generation,
        lease_lost=lease_lost,
        artifact_attempt_id=video_artifact_attempt_id(generation),
    )


async def _transition_to_fetching(
    session: Any,
    redis: Any,
    generation: VideoGeneration,
    poll: PollResult,
    *,
    lease_lost: asyncio.Event | None,
) -> None:
    generation.status = VideoGenerationStatus.RUNNING.value
    generation.progress_stage = VideoGenerationStage.FETCHING.value
    generation.progress_pct = max(generation.progress_pct, 96)
    generation.upstream_response = poll.raw
    await session.commit()
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before fetching event",
    )
    await video_ports()._publish(redis, generation, EV_VIDEO_FETCHING)


async def finish_success(
    session: Any,
    redis: Any,
    generation: VideoGeneration,
    poll: PollResult,
    *,
    adapter: VideoProviderAdapter | None = None,
    lease_lost: asyncio.Event | None = None,
) -> None:
    release_provider_name = generation.provider_name
    release_provider_slot = False
    terminal_committed = False
    stored: StoredVideo | None = None
    cleanup_created_artifacts = False
    artifact_fence: _VideoArtifactFence | None = None
    try:
        if generation.cancel_requested_at is not None:
            await video_ports()._finish_terminal_failure(
                session,
                redis,
                generation,
                video_ports()._cancelled_poll_during_finalization(poll),
                fallback_error_message="video task cancelled by user",
                lease_lost=lease_lost,
            )
            return
        active_adapter = adapter
        if active_adapter is None:
            provider = await video_ports()._provider_for_generation(generation)
            release_provider_name = release_provider_name or provider.name
            active_adapter = video_ports().adapter_for_provider(provider)
        video_ports()._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before fetching-state commit",
        )
        release_provider_slot = True
        await _transition_to_fetching(
            session,
            redis,
            generation,
            poll,
            lease_lost=lease_lost,
        )

        def ensure_active() -> None:
            video_ports()._raise_if_video_lease_lost(
                lease_lost,
                "video poll lease lost during result download",
            )

        downloaded = await active_adapter.download_result(
            poll.video_url or "",
            ensure_active=ensure_active,
        )
        artifact_fence = await _claim_video_artifact_fence(
            session,
            generation,
            lease_lost=lease_lost,
        )
        if artifact_fence is None:
            release_provider_slot = False
            if generation.cancel_requested_at is not None:
                await video_ports()._finish_terminal_failure(
                    session,
                    redis,
                    generation,
                    video_ports()._cancelled_poll_during_finalization(poll),
                    fallback_error_message="video task cancelled by user",
                    lease_lost=lease_lost,
                )
            return
        artifact_attempt_id = artifact_fence.artifact_attempt_id
        stored = await video_ports()._store_video_asset(
            generation,
            downloaded,
            lease_lost=lease_lost,
            artifact_attempt_id=artifact_attempt_id,
        )
        cleanup_created_artifacts = bool(stored.created_storage_keys)
        outcome = await _finalize_video_success_adoption_impl(
            session,
            redis,
            generation,
            poll,
            stored,
            artifact_fence,
            lease_lost=lease_lost,
            probe_success_adoption=_probe_video_success_adoption,
        )
        terminal_committed = outcome.terminal_committed
        cleanup_created_artifacts = (
            cleanup_created_artifacts and outcome.cleanup_created_artifacts
        )
        release_provider_slot = outcome.release_provider_slot
    finally:
        lease_still_owned = lease_lost is None or not lease_lost.is_set()
        if stored is not None and cleanup_created_artifacts:
            rolled_back = await rollback_artifact_transaction(
                session,
                logger=video_ports().logger,
                label=f"video artifact cleanup task={generation.id}",
            )
            if rolled_back and artifact_fence is not None:
                await _cleanup_video_artifacts_if_owned(
                    stored.created_storage_keys,
                    generation_id=generation.id,
                    fence=artifact_fence,
                    lease_lost=lease_lost,
                )
            elif not rolled_back:
                video_ports().logger.error(
                    "video artifact cleanup deferred because rollback was not "
                    "confirmed task=%s keys=%s",
                    generation.id,
                    stored.created_storage_keys,
                )
            else:
                video_ports().logger.error(
                    "video artifact cleanup deferred because durable ownership "
                    "was not established task=%s keys=%s",
                    generation.id,
                    stored.created_storage_keys,
                )
        if (
            release_provider_slot
            and release_provider_name
            and (terminal_committed or lease_still_owned)
        ):
            # Compatibility audit marker:
            # _release_provider_slot(redis, release_provider_name, generation.id)
            await video_ports()._release_provider_slot(
                redis,
                release_provider_name,
                generation.id,
            )


async def worker_flush_balance_cache(session: Any) -> None:
    from ... import billing as worker_billing

    await worker_billing.flush_balance_cache_refreshes(session)


async def finish_terminal_failure(
    session: Any,
    redis: Any,
    generation: VideoGeneration,
    poll: PollResult,
    *,
    fallback_error_message: str | None,
    lease_lost: asyncio.Event | None = None,
    billing_reason: str | None = None,
) -> None:
    release_provider_name = generation.provider_name
    release_provider_slot = False
    terminal_committed = False
    try:
        if generation.status in video_ports()._TERMINAL_STATUSES:
            return
        video_ports()._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before terminal billing",
        )
        release_provider_slot = True
        resolution = await video_ports().resolve_video_billing(
            session,
            generation,
            poll_result=poll,
            reason=billing_reason or poll.status,
        )
        video_ports()._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before terminal mutation",
        )
        internal_status = (
            VideoGenerationStatus.CANCELED.value
            if poll.status == "cancelled"
            else (
                VideoGenerationStatus.EXPIRED.value
                if poll.status == "expired"
                else VideoGenerationStatus.FAILED.value
            )
        )
        generation.status = internal_status
        generation.progress_stage = VideoGenerationStage.FINISHED.value
        generation.progress_pct = 100
        generation.upstream_response = poll.raw
        generation.error_code = poll.failure_class or poll.status
        generation.error_message = (
            fallback_error_message or video_ports()._error_message(poll)
        )
        generation.billed_tokens = resolution.actual_tokens
        generation.billed_cost_micro = resolution.actual_micro
        generation.diagnostics = {
            **(generation.diagnostics or {}),
            "billing_decision": resolution.decision,
        }
        generation.finished_at = video_ports()._now()
        video_ports()._queue_video_event(
            session,
            generation,
            (
                EV_VIDEO_CANCELED
                if internal_status == VideoGenerationStatus.CANCELED.value
                else EV_VIDEO_FAILED
            ),
        )
        video_ports()._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before terminal commit",
        )
        await session.commit()
        terminal_committed = True
        await video_ports().worker_flush_balance_cache(session)
    finally:
        lease_still_owned = lease_lost is None or not lease_lost.is_set()
        if (
            release_provider_slot
            and release_provider_name
            and (terminal_committed or lease_still_owned)
        ):
            # Compatibility audit marker:
            # _release_provider_slot(redis, release_provider_name, generation.id)
            await video_ports()._release_provider_slot(
                redis,
                release_provider_name,
                generation.id,
            )


def error_message(poll: PollResult) -> str:
    raw_msg = None
    if isinstance(poll.raw, dict):
        raw_msg = poll.raw.get("message")
        if not raw_msg:
            raw_error = poll.raw.get("error")
            if isinstance(raw_error, dict):
                raw_msg = raw_error.get("message")
            else:
                raw_msg = raw_error
    if isinstance(raw_msg, str) and raw_msg:
        return raw_msg[:1000]
    return f"video task {poll.status}"


async def video_for_generation(
    session: Any,
    generation_id: str,
) -> Video | None:
    return (
        await session.execute(
            select(Video).where(
                Video.owner_generation_id == generation_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


def video_artifact_keys(
    generation: VideoGeneration,
    extension: str,
    *,
    artifact_attempt_id: str | None,
) -> tuple[str, str]:
    base = f"u/{generation.user_id}/v/{generation.id}"
    if artifact_attempt_id is not None:
        attempt_id = artifact_attempt_id.strip()
        if not attempt_id or "/" in attempt_id or "\x00" in attempt_id:
            raise ValueError("invalid video artifact attempt id")
        base = f"{base}/final/{attempt_id}"
    return f"{base}/output{extension}", f"{base}/poster.jpg"


async def delete_video_storage_keys(
    keys: tuple[str, ...] | list[str],
) -> None:
    unique_keys = list(dict.fromkeys(keys))
    if not unique_keys:
        return
    results = await asyncio.gather(
        *(asyncio.to_thread(video_ports().storage.delete, key) for key in unique_keys),
        return_exceptions=True,
    )
    for key, result in zip(unique_keys, results, strict=False):
        if isinstance(result, BaseException):
            video_ports().logger.warning(
                "video artifact cleanup failed key=%s err=%s",
                key,
                result,
            )
    failures = [
        key
        for key, result in zip(unique_keys, results, strict=False)
        if isinstance(result, BaseException)
    ]
    if failures:
        raise RuntimeError(
            f"video artifact cleanup failed for {len(failures)} key(s)"
        )


async def _cleanup_unadopted_video_storage_keys(
    keys: tuple[str, ...] | list[str],
    *,
    generation_id: str,
    lease_lost: asyncio.Event | None,
    fence: _VideoArtifactFence | None,
) -> None:
    if not keys:
        return
    if fence is None:
        video_ports().logger.error(
            "video artifact cleanup deferred without durable ownership "
            "task=%s keys=%s",
            generation_id,
            keys,
        )
        return
    await _cleanup_video_artifacts_if_owned(
        keys,
        generation_id=generation_id,
        fence=fence,
        lease_lost=lease_lost,
    )


async def _cleanup_video_artifacts_if_owned(
    keys: tuple[str, ...] | list[str],
    *,
    generation_id: str,
    fence: _VideoArtifactFence,
    lease_lost: asyncio.Event | None,
) -> bool:
    return await _cleanup_video_artifacts_if_owned_impl(
        keys,
        generation_id=generation_id,
        fence=fence,
        lease_lost=lease_lost,
        video_for_generation=video_for_generation,
    )


async def put_video_storage_bytes(
    key: str,
    data: bytes,
    *,
    track_created: bool,
) -> bool:
    if not track_created:
        await video_ports().storage.aput_bytes(key, data)
        return False
    result = await asyncio.to_thread(video_ports().storage.put_bytes_result, key, data)
    return bool(result.created)


def _byte_write_operation(
    key: str,
    data: bytes,
) -> StorageWriteOperation:
    storage = video_ports().storage

    def write() -> bool:
        result = storage.put_bytes_result(
            key,
            data,
            max_bytes=len(data),
        )
        return bool(result.created)

    return StorageWriteOperation(
        key=key,
        size_bytes=len(data),
        write=write,
    )


def _poster_write_operation(
    key: str,
    data: bytes,
    *,
    diagnostics: dict[str, Any],
    stored: list[bool],
) -> StorageWriteOperation:
    storage = video_ports().storage

    def write() -> bool:
        try:
            result = storage.put_bytes_result(
                key,
                data,
                max_bytes=len(data),
            )
        except StorageDiskFullError:
            raise
        except Exception as exc:  # noqa: BLE001
            diagnostics["poster_store_error"] = str(exc)[:500]
            return False
        stored.append(True)
        return bool(result.created)

    return StorageWriteOperation(
        key=key,
        size_bytes=len(data),
        write=write,
    )


async def _store_byte_artifacts_with_capacity(
    *,
    video_key: str,
    poster_key: str,
    video_bytes: bytes,
    poster_bytes: bytes | None,
    diagnostics: dict[str, Any],
) -> tuple[list[str], str | None]:
    operations = [_byte_write_operation(video_key, video_bytes)]
    poster_stored: list[bool] = []
    if poster_bytes:
        operations.append(
            _poster_write_operation(
                poster_key,
                poster_bytes,
                diagnostics=diagnostics,
                stored=poster_stored,
            )
        )
    created_keys = await video_ports().storage_writes.write_operations(operations)
    return created_keys, poster_key if poster_stored else None


def _stored_video_from_bytes(
    generation: VideoGeneration,
    *,
    processed: dict[str, Any],
    diagnostics: dict[str, Any],
    video_key: str,
    poster_storage_key: str | None,
    video_bytes: bytes,
    created_keys: list[str],
) -> StoredVideo:
    sha = hashlib.sha256(video_bytes).hexdigest()
    video = Video(
        id=video_ports().new_uuid7(),
        user_id=generation.user_id,
        owner_generation_id=generation.id,
        storage_key=video_key,
        poster_storage_key=poster_storage_key,
        mime=str(processed.get("mime") or "video/mp4"),
        width=int(processed.get("width") or 0),
        height=int(processed.get("height") or 0),
        duration_ms=int(processed.get("duration_ms") or 0),
        fps=processed.get("fps"),
        size_bytes=len(video_bytes),
        sha256=sha,
        etag=sha,
        has_audio=bool(processed.get("has_audio")),
        faststart=bool(processed.get("faststart")),
        visibility="private",
        metadata_jsonb=diagnostics,
    )
    return StoredVideo(
        video=video,
        diagnostics=diagnostics,
        created_storage_keys=tuple(created_keys),
    )


async def store_video_asset(
    generation: VideoGeneration,
    data: bytes | DownloadedVideo,
    *,
    lease_lost: asyncio.Event | None = None,
    artifact_attempt_id: str | None = None,
) -> StoredVideo:
    artifact_fence = _pending_video_artifact_fence(
        generation,
        artifact_attempt_id=artifact_attempt_id,
    )
    if isinstance(data, DownloadedVideo):
        return await video_ports()._store_downloaded_video_asset(
            generation,
            data,
            lease_lost=lease_lost,
            artifact_attempt_id=artifact_attempt_id,
        )
    processed, diagnostics = await asyncio.to_thread(
        video_ports()._postprocess_video_bytes,
        data,
    )
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost after byte video postprocess",
    )
    extension = str(processed.get("extension") or ".mp4")
    video_key, poster_key = video_ports()._video_artifact_keys(
        generation,
        extension,
        artifact_attempt_id=artifact_attempt_id,
    )
    video_bytes = processed["video_bytes"]
    track_created = artifact_attempt_id is not None
    created_keys: list[str] = []
    try:
        video_ports()._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before byte artifact storage",
        )
        poster_bytes = processed.get("poster_bytes")
        if video_ports().storage_writes is not None:
            (
                created_keys,
                poster_storage_key,
            ) = await _store_byte_artifacts_with_capacity(
                video_key=video_key,
                poster_key=poster_key,
                video_bytes=video_bytes,
                poster_bytes=poster_bytes,
                diagnostics=diagnostics,
            )
        else:
            if await video_ports()._put_video_storage_bytes(
                video_key,
                video_bytes,
                track_created=track_created,
            ):
                created_keys.append(video_key)
            poster_storage_key = None
            if poster_bytes:
                try:
                    if await video_ports()._put_video_storage_bytes(
                        poster_key,
                        poster_bytes,
                        track_created=track_created,
                    ):
                        created_keys.append(poster_key)
                    poster_storage_key = poster_key
                except StorageDiskFullError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    diagnostics["poster_store_error"] = str(exc)[:500]
        video_ports()._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost after byte artifact storage",
        )
        if artifact_attempt_id is not None:
            diagnostics["artifact_attempt_id"] = artifact_attempt_id
        if artifact_fence is not None:
            diagnostics[_VIDEO_ARTIFACT_FENCE_KEY] = artifact_fence.payload(
                state=_VIDEO_ARTIFACT_PENDING
            )
        return _stored_video_from_bytes(
            generation,
            processed=processed,
            diagnostics=diagnostics,
            video_key=video_key,
            poster_storage_key=poster_storage_key,
            video_bytes=video_bytes,
            created_keys=created_keys,
        )
    except BaseException:
        await _cleanup_unadopted_video_storage_keys(
            created_keys,
            generation_id=generation.id,
            lease_lost=lease_lost,
            fence=artifact_fence,
        )
        raise


async def _copy_processed_video(
    processed: ProcessedVideoFile,
    *,
    video_key: str,
) -> bool:
    try:
        write_result = await asyncio.to_thread(
            video_ports().copy_video_file_exclusive_result,
            processed.path,
            video_ports().storage.path_for(video_key),
            expected_sha256=processed.sha256,
            expected_size=processed.size_bytes,
        )
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise StorageDiskFullError(video_key) from exc
        raise
    return bool(write_result.created)


def _file_copy_operation(
    processed: ProcessedVideoFile,
    *,
    video_key: str,
) -> StorageWriteOperation:
    storage = video_ports().storage
    copy_file = video_ports().copy_video_file_exclusive_result

    def write() -> bool:
        try:
            result = copy_file(
                processed.path,
                storage.path_for(video_key),
                expected_sha256=processed.sha256,
                expected_size=processed.size_bytes,
            )
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise StorageDiskFullError(video_key) from exc
            raise
        return bool(result.created)

    return StorageWriteOperation(
        key=video_key,
        size_bytes=processed.size_bytes,
        write=write,
    )


async def _store_file_artifacts_with_capacity(
    processed: ProcessedVideoFile,
    *,
    video_key: str,
    poster_key: str,
    diagnostics: dict[str, Any],
) -> tuple[list[str], str | None]:
    operations = [_file_copy_operation(processed, video_key=video_key)]
    poster_stored: list[bool] = []
    if processed.poster_bytes:
        operations.append(
            _poster_write_operation(
                poster_key,
                processed.poster_bytes,
                diagnostics=diagnostics,
                stored=poster_stored,
            )
        )
    created_keys = await video_ports().storage_writes.write_operations(operations)
    return created_keys, poster_key if poster_stored else None


async def _store_processed_poster(
    processed: ProcessedVideoFile,
    *,
    poster_key: str,
    diagnostics: dict[str, Any],
    created_keys: list[str],
) -> str | None:
    if not processed.poster_bytes:
        return None
    try:
        if await video_ports()._put_video_storage_bytes(
            poster_key,
            processed.poster_bytes,
            track_created=True,
        ):
            created_keys.append(poster_key)
        return poster_key
    except StorageDiskFullError:
        raise
    except Exception as exc:  # noqa: BLE001
        diagnostics["poster_store_error"] = str(exc)[:500]
        return None


def _stored_video_from_file(
    generation: VideoGeneration,
    *,
    processed: ProcessedVideoFile,
    video_key: str,
    poster_storage_key: str | None,
    diagnostics: dict[str, Any],
    created_keys: list[str],
) -> StoredVideo:
    video = Video(
        id=video_ports().new_uuid7(),
        user_id=generation.user_id,
        owner_generation_id=generation.id,
        storage_key=video_key,
        poster_storage_key=poster_storage_key,
        mime=processed.mime,
        width=int(processed.metadata.get("width") or 0),
        height=int(processed.metadata.get("height") or 0),
        duration_ms=int(processed.metadata.get("duration_ms") or 0),
        fps=processed.metadata.get("fps"),
        size_bytes=processed.size_bytes,
        sha256=processed.sha256,
        etag=processed.sha256,
        has_audio=bool(processed.metadata.get("has_audio")),
        faststart=processed.faststart,
        visibility="private",
        metadata_jsonb=diagnostics,
    )
    return StoredVideo(
        video=video,
        diagnostics=diagnostics,
        created_storage_keys=tuple(created_keys),
    )


async def store_downloaded_video_asset(
    generation: VideoGeneration,
    downloaded: DownloadedVideo,
    *,
    lease_lost: asyncio.Event | None,
    artifact_attempt_id: str | None,
) -> StoredVideo:
    processed: ProcessedVideoFile | None = None
    created_keys: list[str] = []
    artifact_fence = _pending_video_artifact_fence(
        generation,
        artifact_attempt_id=artifact_attempt_id,
    )
    try:
        video_ports()._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before video postprocess",
        )
        processed = await asyncio.to_thread(
            video_ports()._postprocess_video_file,
            downloaded,
        )
        video_ports()._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost after video postprocess",
        )
        video_key, poster_key = video_ports()._video_artifact_keys(
            generation,
            processed.extension,
            artifact_attempt_id=artifact_attempt_id,
        )
        diagnostics = dict(processed.metadata)
        if video_ports().storage_writes is not None:
            (
                created_keys,
                poster_storage_key,
            ) = await _store_file_artifacts_with_capacity(
                processed,
                video_key=video_key,
                poster_key=poster_key,
                diagnostics=diagnostics,
            )
        else:
            if await _copy_processed_video(processed, video_key=video_key):
                created_keys.append(video_key)
            poster_storage_key = await _store_processed_poster(
                processed,
                poster_key=poster_key,
                diagnostics=diagnostics,
                created_keys=created_keys,
            )
        video_ports()._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost after video artifact storage",
        )
        if artifact_attempt_id is not None:
            diagnostics["artifact_attempt_id"] = artifact_attempt_id
        if artifact_fence is not None:
            diagnostics[_VIDEO_ARTIFACT_FENCE_KEY] = artifact_fence.payload(
                state=_VIDEO_ARTIFACT_PENDING
            )
        return _stored_video_from_file(
            generation,
            processed=processed,
            video_key=video_key,
            poster_storage_key=poster_storage_key,
            diagnostics=diagnostics,
            created_keys=created_keys,
        )
    except BaseException:
        await _cleanup_unadopted_video_storage_keys(
            created_keys,
            generation_id=generation.id,
            lease_lost=lease_lost,
            fence=artifact_fence,
        )
        raise
    finally:
        if processed is not None:
            processed.cleanup()
        downloaded.cleanup()


__all__ = [
    "delete_video_storage_keys",
    "error_message",
    "finish_success",
    "finish_terminal_failure",
    "put_video_storage_bytes",
    "store_downloaded_video_asset",
    "store_video_asset",
    "video_artifact_attempt_id",
    "video_artifact_keys",
    "video_for_generation",
    "worker_flush_balance_cache",
]
