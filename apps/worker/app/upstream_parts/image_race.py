"""Image race orchestration, cancellation cleanup, and bonus grace handling."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from typing import Any

from .transport import ImageProgressCallback

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve compatibility dependencies at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


def _drain_task_group_result(task_group: asyncio.Future[Any]) -> None:
    with suppress(BaseException):
        task_group.result()


async def _cancel_and_wait_tasks(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    label: str,
) -> None:
    facade = _facade()
    pending = [task for task in tasks if not task.done()]
    if not pending:
        return
    for task in pending:
        task.cancel()
    grouped = asyncio.gather(*pending, return_exceptions=True)
    try:
        await asyncio.wait_for(
            asyncio.shield(grouped),
            timeout=facade._RACE_CANCEL_WAIT_S,
        )
    except asyncio.TimeoutError:
        grouped.add_done_callback(facade._drain_task_group_result)
        facade.logger.warning(
            "%s cancel cleanup still pending after %.1fs for %d task(s)",
            label,
            facade._RACE_CANCEL_WAIT_S,
            len(pending),
        )
    except asyncio.CancelledError:
        grouped.add_done_callback(facade._drain_task_group_result)
        raise


def _completed_race_batch(
    tasks: list[asyncio.Task[Any]],
    done: set[asyncio.Task[Any]],
) -> tuple[list[asyncio.Task[Any]], list[asyncio.Task[Any]]]:
    ordered = [task for task in tasks if task in done]
    successful = [
        task for task in ordered if not task.cancelled() and task.exception() is None
    ]
    return ordered, successful


def _simultaneous_bonus_tasks(
    successful: list[asyncio.Task[Any]],
    winner: asyncio.Task[Any],
) -> set[asyncio.Task[Any]]:
    return {task for task in successful if task is not winner}


async def _race_responses_image(
    *,
    action: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    quality: str,
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    lanes: int,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
    user_id: str | None = None,
) -> tuple[str, str | None]:
    """Race Responses lanes and cancel losers after the first success."""
    facade = _facade()
    if provider_override is not None:
        lanes = 1
    pixels = facade._parse_size_pixels(size)
    if pixels is not None and pixels > facade._RACE_SINGLE_LANE_PIXELS:
        lanes = 1
    if lanes <= 1:
        return await facade._responses_image_stream_with_failover(
            prompt=prompt,
            size=size,
            action=action,
            images=images,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=progress_callback,
            use_httpx=False,
            provider_override=provider_override,
            user_id=user_id,
        )

    async def _metadata_only_progress(event: dict[str, Any]) -> None:
        if event.get("type") != "provider_used":
            return
        extra = {
            key: event.get(key)
            for key in (
                "attempt",
                "endpoint_attempt",
                "duration_ms",
                "status",
                "reason",
                "error_code",
                "status_code",
                "byok",
            )
            if event.get(key) is not None
        }
        await facade._emit_image_progress(
            progress_callback,
            "provider_used",
            provider=event.get("provider"),
            route=event.get("route"),
            source=event.get("source"),
            endpoint=event.get("endpoint"),
            **extra,
        )

    async def _run_lane(index: int) -> tuple[str, str | None]:
        callback = progress_callback if index == 0 else _metadata_only_progress
        return await facade._responses_image_stream_with_failover(
            prompt=prompt,
            size=size,
            action=action,
            images=images,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=callback,
            use_httpx=index == 1,
            provider_override=provider_override,
            user_id=user_id,
        )

    tasks: list[asyncio.Task[tuple[str, str | None]]] = [
        asyncio.create_task(
            _run_lane(index),
            name=f"{action}-race-lane-{index}",
        )
        for index in range(lanes)
    ]
    errors: list[BaseException] = []
    try:
        pending: set[asyncio.Task[tuple[str, str | None]]] = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for finished in done:
                exc = finished.exception()
                if exc is None:
                    winner_name = finished.get_name()
                    losers = [task for task in pending if not task.done()]
                    if losers:
                        await facade._cancel_and_wait_tasks(
                            losers,
                            label=f"{action} race loser cleanup",
                        )
                    facade.logger.info(
                        "%s race: %s won, cancelled %d lane(s)",
                        action,
                        winner_name,
                        len(losers),
                    )
                    return finished.result()
                if isinstance(exc, facade.UpstreamCancelled):
                    losers = [task for task in pending if not task.done()]
                    if losers:
                        await facade._cancel_and_wait_tasks(
                            losers,
                            label=f"{action} race cancelled cleanup",
                        )
                    facade.logger.info(
                        "%s race: cancelled by caller; aborting %d lane(s)",
                        action,
                        len(losers),
                    )
                    raise exc
                errors.append(exc)
                facade.logger.warning(
                    "%s race: %s failed: %r",
                    action,
                    finished.get_name(),
                    exc,
                )
        facade.logger.warning(
            "%s race: all %d lane(s) failed; summaries=%s",
            action,
            len(errors),
            json.dumps(
                [facade._summarize_exception(error) for error in errors],
                ensure_ascii=False,
            )[:2000],
        )
        raise facade._merge_fallback_errors(
            errors,
            error_code=facade.EC.FALLBACK_LANES_FAILED.value,
            message=f"{action} fallback lanes all failed",
        )
    finally:
        leftovers = [task for task in tasks if not task.done()]
        if leftovers:
            try:
                await facade._cancel_and_wait_tasks(
                    leftovers,
                    label=f"{action} race final cleanup",
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                facade.logger.debug(
                    "%s race final cleanup failed",
                    action,
                    exc_info=True,
                )


async def _dual_race_image_action(
    *,
    action: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    mask: bytes | None = None,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    model: str | None,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None,
    user_id: str | None = None,
    allow_provider_override_race: bool = False,
) -> AsyncIterator[tuple[str, str | None]]:
    """Race direct image2 and Responses while allowing a bonus result."""
    facade = _facade()
    if provider_override is not None and not allow_provider_override_race:
        yield await facade._responses_image_stream_with_failover(
            prompt=prompt,
            size=size,
            action=action,
            images=images,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=progress_callback,
            use_httpx=False,
            provider_override=provider_override,
            user_id=user_id,
        )
        return

    async def _metadata_only_progress(event: dict[str, Any]) -> None:
        if event.get("type") != "provider_used":
            return
        extra = {
            key: event.get(key)
            for key in (
                "attempt",
                "endpoint_attempt",
                "duration_ms",
                "status",
                "reason",
                "error_code",
                "status_code",
                "byok",
            )
            if event.get(key) is not None
        }
        await facade._emit_image_progress(
            progress_callback,
            "provider_used",
            provider=event.get("provider"),
            route=event.get("route"),
            source=event.get("source"),
            endpoint=event.get("endpoint"),
            **extra,
        )

    async def _lane_image2() -> list[tuple[str, str | None]]:
        if action == "edit":
            if not images:
                raise facade.UpstreamError(
                    "edit action requires at least one reference image",
                    error_code=facade.EC.MISSING_INPUT_IMAGES.value,
                    status_code=400,
                )
            return await facade._direct_edit_image_with_failover(
                prompt=prompt,
                size=size,
                images=images,
                mask=mask,
                n=n,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
                progress_callback=progress_callback,
                provider_override=provider_override,
            )
        return await facade._direct_generate_image_with_failover(
            prompt=prompt,
            size=size,
            n=n,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            progress_callback=progress_callback,
            provider_override=provider_override,
        )

    async def _lane_responses() -> list[tuple[str, str | None]]:
        return [
            await facade._responses_image_stream_with_failover(
                prompt=prompt,
                size=size,
                action=action,
                images=images,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
                model=model,
                progress_callback=_metadata_only_progress,
                use_httpx=False,
                provider_override=provider_override,
                user_id=user_id,
            )
        ]

    pixels = facade._parse_size_pixels(size)
    grace_seconds = (
        facade._DUAL_RACE_BONUS_GRACE_4K_S
        if pixels is not None and pixels > facade._IMAGE_4K_PIXELS
        else facade._DUAL_RACE_BONUS_GRACE_S
    )
    tasks: list[asyncio.Task[list[tuple[str, str | None]]]] = [
        asyncio.create_task(
            _lane_image2(),
            name=f"{action}-dual-image2",
        ),
        asyncio.create_task(
            _lane_responses(),
            name=f"{action}-dual-responses",
        ),
    ]
    lane_names: dict[asyncio.Task[Any], str] = {
        tasks[0]: "image2",
        tasks[1]: "responses",
    }
    errors: list[tuple[str, BaseException]] = []
    pending: set[asyncio.Task[list[tuple[str, str | None]]]] = set(tasks)
    winner_yielded = False
    try:
        while pending and not winner_yielded:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            ordered_done, simultaneous_successes = _completed_race_batch(tasks, done)
            for finished in ordered_done:
                lane_name = lane_names[finished]
                exc = finished.exception()
                if exc is None:
                    facade.logger.info(
                        "%s dual_race: %s won, loser keeps running (grace=%.0fs)",
                        action,
                        lane_name,
                        grace_seconds,
                    )
                    winner_yielded = True
                    pending.update(
                        _simultaneous_bonus_tasks(
                            simultaneous_successes,
                            finished,
                        )
                    )
                    for item in finished.result():
                        yield item
                    break
                if isinstance(exc, facade.UpstreamCancelled):
                    raise exc
                if facade._is_direct_image_result_unknown(exc):
                    await facade._cancel_and_wait_tasks(
                        pending,
                        label=f"{action} dual_race result-unknown cleanup",
                    )
                    raise exc
                errors.append((lane_name, exc))
                facade.logger.warning(
                    "%s dual_race: %s failed: %r",
                    action,
                    lane_name,
                    exc,
                )

        if not winner_yielded:
            facade.logger.warning(
                "%s dual_race: both lanes failed; summaries=%s",
                action,
                json.dumps(
                    [
                        facade._truncate_lane_summary(lane, error)
                        for lane, error in errors
                    ],
                    ensure_ascii=False,
                )[:2000],
            )
            merged_message = " | ".join(f"[{lane}] {error!s}" for lane, error in errors)
            raise facade._merge_fallback_errors(
                [error for _, error in errors],
                error_code=facade.EC.FALLBACK_LANES_FAILED.value,
                message=f"{action} dual_race: {merged_message}",
            )

        if pending:
            done, still_pending = await asyncio.wait(
                pending,
                timeout=grace_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if still_pending:
                await facade._cancel_and_wait_tasks(
                    still_pending,
                    label=f"{action} dual_race bonus cleanup",
                )
                facade.logger.info(
                    "%s dual_race: loser exceeded grace=%.0fs, cancelled silently",
                    action,
                    grace_seconds,
                )
                return
            for finished in done:
                lane_name = lane_names[finished]
                exc = finished.exception()
                if exc is None:
                    facade.logger.info(
                        "%s dual_race: bonus from %s succeeded",
                        action,
                        lane_name,
                    )
                    for item in finished.result():
                        yield item
                    return
                if isinstance(exc, facade.UpstreamCancelled):
                    return
                facade.logger.info(
                    "%s dual_race: bonus %s failed silently: %r",
                    action,
                    lane_name,
                    exc,
                )
                return
    finally:
        leftovers = [task for task in tasks if not task.done()]
        if leftovers:
            try:
                await facade._cancel_and_wait_tasks(
                    leftovers,
                    label=f"{action} dual_race final cleanup",
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                facade.logger.debug(
                    "%s dual_race final cleanup failed",
                    action,
                    exc_info=True,
                )


async def _dual_race_image_jobs_action(
    *,
    action: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    mask: bytes | None = None,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    model: str | None,
    progress_callback: ImageProgressCallback | None,
    provider_override: Any | None = None,
    user_id: str | None = None,
) -> AsyncIterator[tuple[str, str | None]]:
    """Race image-job generations and Responses endpoints with bonus grace."""
    facade = _facade()
    pixels = facade._parse_size_pixels(size)
    grace_seconds = (
        facade._DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_4K_S
        if pixels is not None and pixels > facade._IMAGE_4K_PIXELS
        else facade._DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S
    )

    async def _metadata_only_progress(event: dict[str, Any]) -> None:
        if event.get("type") != "provider_used":
            return
        extra = {
            key: event.get(key)
            for key in (
                "attempt",
                "endpoint_attempt",
                "duration_ms",
                "status",
                "reason",
                "error_code",
                "status_code",
                "byok",
            )
            if event.get(key) is not None
        }
        await facade._emit_image_progress(
            progress_callback,
            "provider_used",
            provider=event.get("provider"),
            route=event.get("route"),
            source=event.get("source"),
            endpoint=event.get("endpoint"),
            **extra,
        )

    async def _lane(
        endpoint: str,
        lane_progress: ImageProgressCallback | None,
    ) -> tuple[str, str | None]:
        lane_mask = mask if endpoint == "generations" else None
        return await facade._image_job_with_failover(
            action=action,
            prompt=prompt,
            size=size,
            images=images,
            mask=lane_mask,
            n=n,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            progress_callback=lane_progress,
            provider_override=provider_override,
            user_id=user_id,
            endpoint_override=endpoint,
        )

    tasks: list[asyncio.Task[tuple[str, str | None]]] = [
        asyncio.create_task(
            _lane("generations", progress_callback),
            name=f"{action}-image-jobs-dual-generations",
        ),
        asyncio.create_task(
            _lane("responses", _metadata_only_progress),
            name=f"{action}-image-jobs-dual-responses",
        ),
    ]
    lane_names: dict[asyncio.Task[Any], str] = {
        tasks[0]: "image_jobs:generations",
        tasks[1]: "image_jobs:responses",
    }
    errors: list[tuple[str, BaseException]] = []
    pending: set[asyncio.Task[tuple[str, str | None]]] = set(tasks)
    winner_yielded = False
    try:
        while pending and not winner_yielded:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            ordered_done, simultaneous_successes = _completed_race_batch(tasks, done)
            for finished in ordered_done:
                lane_name = lane_names[finished]
                exc = finished.exception()
                if exc is None:
                    facade.logger.info(
                        "%s image_jobs dual_race: %s won, loser keeps "
                        "running (grace=%.0fs)",
                        action,
                        lane_name,
                        grace_seconds,
                    )
                    winner_yielded = True
                    pending.update(
                        _simultaneous_bonus_tasks(
                            simultaneous_successes,
                            finished,
                        )
                    )
                    yield finished.result()
                    break
                if isinstance(exc, facade.UpstreamCancelled):
                    raise exc
                errors.append((lane_name, exc))
                facade.logger.warning(
                    "%s image_jobs dual_race: %s failed: %r",
                    action,
                    lane_name,
                    exc,
                )

        if not winner_yielded:
            facade.logger.warning(
                "%s image_jobs dual_race: both lanes failed; summaries=%s",
                action,
                json.dumps(
                    [
                        facade._truncate_lane_summary(lane, error)
                        for lane, error in errors
                    ],
                    ensure_ascii=False,
                )[:2000],
            )
            merged_message = " | ".join(f"[{lane}] {error!s}" for lane, error in errors)
            raise facade._merge_fallback_errors(
                [error for _, error in errors],
                error_code=facade.EC.FALLBACK_LANES_FAILED.value,
                message=f"{action} image_jobs dual_race: {merged_message}",
            )

        if pending:
            done, still_pending = await asyncio.wait(
                pending,
                timeout=grace_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if still_pending:
                await facade._cancel_and_wait_tasks(
                    still_pending,
                    label=f"{action} image_jobs dual_race bonus cleanup",
                )
                facade.logger.info(
                    "%s image_jobs dual_race: loser exceeded grace=%.0fs, "
                    "cancelled silently",
                    action,
                    grace_seconds,
                )
                return
            for finished in done:
                lane_name = lane_names[finished]
                exc = finished.exception()
                if exc is None:
                    facade.logger.info(
                        "%s image_jobs dual_race: bonus from %s succeeded",
                        action,
                        lane_name,
                    )
                    yield finished.result()
                    return
                if isinstance(exc, facade.UpstreamCancelled):
                    return
                facade.logger.info(
                    "%s image_jobs dual_race: bonus %s failed silently: %r",
                    action,
                    lane_name,
                    exc,
                )
                return
    finally:
        leftovers = [task for task in tasks if not task.done()]
        if leftovers:
            try:
                await facade._cancel_and_wait_tasks(
                    leftovers,
                    label=f"{action} image_jobs dual_race final cleanup",
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                facade.logger.debug(
                    "%s image_jobs dual_race final cleanup failed",
                    action,
                    exc_info=True,
                )


__all__ = [
    "_cancel_and_wait_tasks",
    "_drain_task_group_result",
    "_dual_race_image_action",
    "_dual_race_image_jobs_action",
    "_race_responses_image",
]
