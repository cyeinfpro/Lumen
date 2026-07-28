"""Image race orchestration, cancellation cleanup, and bonus grace handling."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import Any

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from .image_execution import (
    ImageExecutionRequest,
    ImageResult,
)
from .transport import ImageProgressCallback


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def _drain_task_group_result(task_group: asyncio.Future[Any]) -> None:
    with suppress(BaseException):
        task_group.result()


async def _cancel_and_wait_tasks(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    label: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = _runtime_services(runtime)
    pending = [task for task in tasks if not task.done()]
    if not pending:
        return
    for task in pending:
        task.cancel()
    grouped = asyncio.gather(*pending, return_exceptions=True)
    try:
        await asyncio.wait_for(
            asyncio.shield(grouped),
            timeout=services.core.RACE_CANCEL_WAIT_S,
        )
    except asyncio.TimeoutError:
        grouped.add_done_callback(services.race.drain_task_group_result)
        services.infrastructure.logger.warning(
            "%s cancel cleanup still pending after %.1fs for %d task(s)",
            label,
            services.core.RACE_CANCEL_WAIT_S,
            len(pending),
        )
    except asyncio.CancelledError:
        grouped.add_done_callback(services.race.drain_task_group_result)
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


def _metadata_only_progress(
    progress_callback: ImageProgressCallback | None,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> ImageProgressCallback:
    services = _runtime_services(runtime)

    async def _forward(event: dict[str, Any]) -> None:
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
        await services.transport.emit_image_progress(
            progress_callback,
            "provider_used",
            provider=event.get("provider"),
            route=event.get("route"),
            source=event.get("source"),
            endpoint=event.get("endpoint"),
            **extra,
        )

    return _forward


async def _cleanup_race_tasks(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    label: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = _runtime_services(runtime)
    leftovers = [task for task in tasks if not task.done()]
    if not leftovers:
        return
    try:
        await services.race.cancel_and_wait_tasks(
            leftovers,
            label=label,
            runtime=runtime,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        services.infrastructure.logger.debug(
            "%s failed", label, exc_info=True
        )


def _responses_race_lane_count(
    request: ImageExecutionRequest,
    lanes: int,
) -> int:
    services = _runtime_services(request.upstream_runtime)
    if request.provider_override is not None:
        return 1
    pixels = services.requests.parse_size_pixels(request.size)
    if pixels is not None and pixels > services.core.RACE_SINGLE_LANE_PIXELS:
        return 1
    return lanes


async def _run_responses_lane(
    request: ImageExecutionRequest,
    *,
    use_httpx: bool,
) -> ImageResult:
    services = _runtime_services(request.upstream_runtime)
    return await services.direct.responses_image_stream_with_failover(
        request,
        use_httpx=use_httpx,
    )


def _create_responses_race_tasks(
    request: ImageExecutionRequest,
    *,
    lanes: int,
) -> list[asyncio.Task[ImageResult]]:
    runtime = request.upstream_runtime
    secondary = request.with_progress(
        _metadata_only_progress(
            request.progress_callback,
            runtime=runtime,
        )
    )
    return [
        asyncio.create_task(
            _run_responses_lane(
                request if index == 0 else secondary,
                use_httpx=index == 1,
            ),
            name=f"{request.action}-race-lane-{index}",
        )
        for index in range(lanes)
    ]


async def _select_responses_race_winner(
    request: ImageExecutionRequest,
    tasks: list[asyncio.Task[ImageResult]],
) -> ImageResult:
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    pending = set(tasks)
    errors: list[BaseException] = []
    while pending:
        done, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for finished in done:
            exc = finished.exception()
            if exc is None:
                losers = [task for task in pending if not task.done()]
                if losers:
                    await services.race.cancel_and_wait_tasks(
                        losers,
                        label=f"{request.action} race loser cleanup",
                        runtime=runtime,
                    )
                services.infrastructure.logger.info(
                    "%s race: %s won, cancelled %d lane(s)",
                    request.action,
                    finished.get_name(),
                    len(losers),
                )
                return finished.result()
            if isinstance(exc, services.infrastructure.UpstreamCancelled):
                losers = [task for task in pending if not task.done()]
                if losers:
                    await services.race.cancel_and_wait_tasks(
                        losers,
                        label=f"{request.action} race cancelled cleanup",
                        runtime=runtime,
                    )
                services.infrastructure.logger.info(
                    "%s race: cancelled by caller; aborting %d lane(s)",
                    request.action,
                    len(losers),
                )
                raise exc
            errors.append(exc)
            services.infrastructure.logger.warning(
                "%s race: %s failed: %r",
                request.action,
                finished.get_name(),
                exc,
            )
    services.infrastructure.logger.warning(
        "%s race: all %d lane(s) failed; summaries=%s",
        request.action,
        len(errors),
        json.dumps(
            [services.retry.summarize_exception(error) for error in errors],
            ensure_ascii=False,
        )[:2000],
    )
    raise services.retry.merge_fallback_errors(
        errors,
        error_code=services.infrastructure.EC.FALLBACK_LANES_FAILED.value,
        message=f"{request.action} fallback lanes all failed",
        runtime=runtime,
    )


async def _race_responses_image(
    request: ImageExecutionRequest,
    *,
    lanes: int,
) -> tuple[str, str | None]:
    """Race Responses lanes and cancel losers after the first success."""
    runtime = request.upstream_runtime
    lanes = _responses_race_lane_count(request, lanes)
    if lanes <= 1:
        return await _run_responses_lane(request, use_httpx=False)
    tasks = _create_responses_race_tasks(request, lanes=lanes)
    try:
        return await _select_responses_race_winner(request, tasks)
    finally:
        await _cleanup_race_tasks(
            tasks,
            label=f"{request.action} race final cleanup",
            runtime=runtime,
        )


async def _run_direct_image2_lane(
    request: ImageExecutionRequest,
) -> list[ImageResult]:
    services = _runtime_services(request.upstream_runtime)
    if request.action == "edit":
        if not request.images:
            raise services.infrastructure.UpstreamError(
                "edit action requires at least one reference image",
                error_code=services.infrastructure.EC.MISSING_INPUT_IMAGES.value,
                status_code=400,
            )
        return await services.direct.direct_edit_image_with_failover(
            request
        )
    return await services.direct.direct_generate_image_with_failover(request)


async def _run_dual_responses_lane(
    request: ImageExecutionRequest,
) -> list[ImageResult]:
    return [await _run_responses_lane(request, use_httpx=False)]


async def _run_dual_image_job_lane(
    request: ImageExecutionRequest,
    *,
    endpoint: str,
) -> list[ImageResult]:
    services = _runtime_services(request.upstream_runtime)
    lane_request = request if endpoint == "generations" else request.with_mask(None)
    result = await services.image_jobs.image_job_with_failover(
        lane_request,
        endpoint_override=endpoint,
    )
    return [result]


def _dual_race_grace_seconds(
    request: ImageExecutionRequest,
    *,
    image_jobs: bool,
) -> float:
    services = _runtime_services(request.upstream_runtime)
    pixels = services.requests.parse_size_pixels(request.size)
    is_4k = pixels is not None and pixels > services.core.IMAGE_4K_PIXELS
    if image_jobs:
        return (
            services.core.DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_4K_S
            if is_4k
            else services.core.DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S
        )
    return (
        services.core.DUAL_RACE_BONUS_GRACE_4K_S
        if is_4k
        else services.core.DUAL_RACE_BONUS_GRACE_S
    )


@dataclass(frozen=True)
class _DualRaceWinner:
    results: list[ImageResult]
    pending: set[asyncio.Task[list[ImageResult]]]


def _raise_dual_race_failure(
    request: ImageExecutionRequest,
    errors: list[tuple[str, BaseException]],
    *,
    race_name: str,
) -> None:
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    services.infrastructure.logger.warning(
        "%s %s: both lanes failed; summaries=%s",
        request.action,
        race_name,
        json.dumps(
            [
                services.retry.truncate_lane_summary(lane, error)
                for lane, error in errors
            ],
            ensure_ascii=False,
        )[:2000],
    )
    merged_message = " | ".join(f"[{lane}] {error!s}" for lane, error in errors)
    raise services.retry.merge_fallback_errors(
        [error for _, error in errors],
        error_code=services.infrastructure.EC.FALLBACK_LANES_FAILED.value,
        message=f"{request.action} {race_name}: {merged_message}",
        runtime=runtime,
    )


async def _select_dual_race_winner(
    request: ImageExecutionRequest,
    tasks: list[asyncio.Task[list[ImageResult]]],
    lane_names: dict[asyncio.Task[Any], str],
    *,
    grace_seconds: float,
    race_name: str,
    abort_result_unknown: bool,
) -> _DualRaceWinner:
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    pending = set(tasks)
    errors: list[tuple[str, BaseException]] = []
    while pending:
        done, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )
        ordered_done, simultaneous = _completed_race_batch(tasks, done)
        for finished in ordered_done:
            lane_name = lane_names[finished]
            exc = finished.exception()
            if exc is None:
                services.infrastructure.logger.info(
                    "%s %s: %s won, loser keeps running (grace=%.0fs)",
                    request.action,
                    race_name,
                    lane_name,
                    grace_seconds,
                )
                pending.update(_simultaneous_bonus_tasks(simultaneous, finished))
                return _DualRaceWinner(finished.result(), pending)
            if isinstance(exc, services.infrastructure.UpstreamCancelled):
                raise exc
            if (
                abort_result_unknown
                and services.direct.is_direct_image_result_unknown(exc)
            ):
                await services.race.cancel_and_wait_tasks(
                    pending,
                    label=f"{request.action} {race_name} result-unknown cleanup",
                    runtime=runtime,
                )
                raise exc
            errors.append((lane_name, exc))
            services.infrastructure.logger.warning(
                "%s %s: %s failed: %r",
                request.action,
                race_name,
                lane_name,
                exc,
            )
    _raise_dual_race_failure(request, errors, race_name=race_name)


async def _await_dual_race_bonus(
    request: ImageExecutionRequest,
    winner: _DualRaceWinner,
    lane_names: dict[asyncio.Task[Any], str],
    *,
    grace_seconds: float,
    race_name: str,
) -> list[ImageResult] | None:
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    if not winner.pending:
        return None
    done, still_pending = await asyncio.wait(
        winner.pending,
        timeout=grace_seconds,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if still_pending:
        await services.race.cancel_and_wait_tasks(
            still_pending,
            label=f"{request.action} {race_name} bonus cleanup",
            runtime=runtime,
        )
        services.infrastructure.logger.info(
            "%s %s: loser exceeded grace=%.0fs, cancelled silently",
            request.action,
            race_name,
            grace_seconds,
        )
        return None
    finished = next(iter(done))
    lane_name = lane_names[finished]
    exc = finished.exception()
    if exc is None:
        services.infrastructure.logger.info(
            "%s %s: bonus from %s succeeded",
            request.action,
            race_name,
            lane_name,
        )
        return finished.result()
    if isinstance(exc, services.infrastructure.UpstreamCancelled):
        return None
    services.infrastructure.logger.info(
        "%s %s: bonus %s failed silently: %r",
        request.action,
        race_name,
        lane_name,
        exc,
    )
    return None


async def _iter_dual_race_results(
    request: ImageExecutionRequest,
    tasks: list[asyncio.Task[list[ImageResult]]],
    lane_names: dict[asyncio.Task[Any], str],
    *,
    grace_seconds: float,
    race_name: str,
    abort_result_unknown: bool,
) -> AsyncIterator[ImageResult]:
    runtime = request.upstream_runtime
    try:
        winner = await _select_dual_race_winner(
            request,
            tasks,
            lane_names,
            grace_seconds=grace_seconds,
            race_name=race_name,
            abort_result_unknown=abort_result_unknown,
        )
        for item in winner.results:
            yield item
        bonus = await _await_dual_race_bonus(
            request,
            winner,
            lane_names,
            grace_seconds=grace_seconds,
            race_name=race_name,
        )
        for item in bonus or []:
            yield item
    finally:
        await _cleanup_race_tasks(
            tasks,
            label=f"{request.action} {race_name} final cleanup",
            runtime=runtime,
        )


async def _dual_race_image_action(
    request: ImageExecutionRequest,
    *,
    allow_provider_override_race: bool = False,
) -> AsyncIterator[tuple[str, str | None]]:
    """Race direct image2 and Responses while allowing a bonus result."""
    runtime = request.upstream_runtime
    if request.provider_override is not None and not allow_provider_override_race:
        yield await _run_responses_lane(request, use_httpx=False)
        return
    secondary = request.with_progress(
        _metadata_only_progress(
            request.progress_callback,
            runtime=runtime,
        )
    )
    tasks: list[asyncio.Task[list[tuple[str, str | None]]]] = [
        asyncio.create_task(
            _run_direct_image2_lane(request),
            name=f"{request.action}-dual-image2",
        ),
        asyncio.create_task(
            _run_dual_responses_lane(secondary),
            name=f"{request.action}-dual-responses",
        ),
    ]
    lane_names: dict[asyncio.Task[Any], str] = {
        tasks[0]: "image2",
        tasks[1]: "responses",
    }
    async with aclosing(
        _iter_dual_race_results(
            request,
            tasks,
            lane_names,
            grace_seconds=_dual_race_grace_seconds(request, image_jobs=False),
            race_name="dual_race",
            abort_result_unknown=True,
        )
    ) as results:
        async for item in results:
            yield item


async def _dual_race_image_jobs_action(
    request: ImageExecutionRequest,
) -> AsyncIterator[tuple[str, str | None]]:
    """Race image-job generations and Responses endpoints with bonus grace."""
    runtime = request.upstream_runtime
    secondary = request.with_progress(
        _metadata_only_progress(
            request.progress_callback,
            runtime=runtime,
        )
    )
    tasks: list[asyncio.Task[list[ImageResult]]] = [
        asyncio.create_task(
            _run_dual_image_job_lane(request, endpoint="generations"),
            name=f"{request.action}-image-jobs-dual-generations",
        ),
        asyncio.create_task(
            _run_dual_image_job_lane(secondary, endpoint="responses"),
            name=f"{request.action}-image-jobs-dual-responses",
        ),
    ]
    lane_names: dict[asyncio.Task[Any], str] = {
        tasks[0]: "image_jobs:generations",
        tasks[1]: "image_jobs:responses",
    }
    async with aclosing(
        _iter_dual_race_results(
            request,
            tasks,
            lane_names,
            grace_seconds=_dual_race_grace_seconds(request, image_jobs=True),
            race_name="image_jobs dual_race",
            abort_result_unknown=False,
        )
    ) as results:
        async for item in results:
            yield item


__all__ = [
    "_cancel_and_wait_tasks",
    "_drain_task_group_result",
    "_dual_race_image_action",
    "_dual_race_image_jobs_action",
    "_race_responses_image",
]
