"""Image race orchestration, cancellation cleanup, and bonus grace handling."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from .image_execution import (
    ImageExecutionRequest,
    ImageResult,
)
from ..upstream_clients.image_job_models import (
    ImageJobCostKnowledge,
    ImageJobExecutionHandle,
)
from .image_race_support import (
    await_irrevocable_task as _await_irrevocable_task,
)
from .image_race_support import cancel_and_wait_tasks as _cancel_and_wait_tasks
from .image_race_support import cleanup_race_tasks as _cleanup_race_tasks
from .image_race_support import completed_race_batch as _completed_race_batch
from .image_race_support import drain_task_group_result as _drain_task_group_result
from .image_race_support import has_successful_task as _has_successful_task
from .image_race_support import invoke_progress_callback as _invoke_progress_callback
from .image_race_support import metadata_only_progress as _metadata_only_progress
from .image_race_support import runtime_services as _runtime_services
from .image_race_support import (
    simultaneous_bonus_tasks as _simultaneous_bonus_tasks,
)
from .transport import ImageProgressCallback


_BONUS_NOT_OBSERVED = object()


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
        return await services.direct.direct_edit_image_with_failover(request)
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


@dataclass(slots=True)
class _ImageJobLaneObservation:
    lane_name: str
    publish_dispatch_cancel_obligation: bool = False
    dispatch_ready: bool = False
    latest_execution: ImageJobExecutionHandle | None = None
    obligation_published: bool = False
    obligation_error: Exception | None = None


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
            if abort_result_unknown and services.direct.is_direct_image_result_unknown(
                exc
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
    lane_observations: (
        dict[asyncio.Task[Any], _ImageJobLaneObservation] | None
    ) = None,
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
        await _publish_dispatch_cancel_obligations(
            request,
            still_pending,
            lane_observations,
            race_name=race_name,
        )
        _raise_obligation_publish_error(lane_observations)
        services.infrastructure.logger.info(
            "%s %s: loser exceeded grace=%.0fs, cancelled silently",
            request.action,
            race_name,
            grace_seconds,
        )
        return None
    finished = next(iter(done))
    lane_name = lane_names[finished]
    _raise_obligation_publish_error(lane_observations)
    if finished.cancelled():
        return None
    exc = finished.exception()
    if exc is None:
        await _publish_dual_race_bonus_ready(
            request,
            lane_name=lane_name,
            race_name=race_name,
            artifact_ready=True,
            obligation_reason="loser_completed",
        )
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


async def _publish_dual_race_bonus_ready(
    request: ImageExecutionRequest,
    *,
    lane_name: str,
    race_name: str,
    artifact_ready: bool = True,
    obligation_reason: str = "loser_completed",
    execution: ImageJobExecutionHandle | None = None,
) -> None:
    callback = request.progress_callback
    if callback is None:
        return

    async def _publish() -> None:
        event: dict[str, Any] = {
            "type": "dual_race_bonus_ready",
            "lane": lane_name,
            "race_name": race_name,
            "size": request.size,
            "artifact_ready": artifact_ready,
            "obligation_reason": obligation_reason,
        }
        if execution is not None:
            event["execution"] = execution.to_dict()
        await _invoke_progress_callback(
            callback,
            event,
        )

    task = asyncio.create_task(
        _publish(),
        name=f"{request.action}-dual-race-bonus-obligation",
    )
    await _await_irrevocable_task(task)


def _cancel_execution_requires_bonus_obligation(
    execution: ImageJobExecutionHandle,
) -> bool:
    return bool(
        execution.cancel_outcome is not None
        and execution.cost_knowledge
        in {
            ImageJobCostKnowledge.UNKNOWN,
            ImageJobCostKnowledge.INCURRED,
        }
    )


def _image_job_lane_progress(
    request: ImageExecutionRequest,
    *,
    lane_name: str,
    race_name: str,
    metadata_only: bool,
) -> tuple[ImageProgressCallback, _ImageJobLaneObservation]:
    observation = _ImageJobLaneObservation(lane_name=lane_name)
    downstream = (
        _metadata_only_progress(
            request.progress_callback,
            runtime=request.upstream_runtime,
        )
        if metadata_only
        else request.progress_callback
    )

    async def _forward(event: dict[str, Any]) -> None:
        if event.get("type") == "dispatch_ready":
            observation.dispatch_ready = True
        execution = ImageJobExecutionHandle.from_mapping(event.get("execution"))
        if execution is not None:
            observation.latest_execution = execution
            if (
                not observation.obligation_published
                and _cancel_execution_requires_bonus_obligation(execution)
            ):
                try:
                    await _publish_dual_race_bonus_ready(
                        request,
                        lane_name=lane_name,
                        race_name=race_name,
                        artifact_ready=False,
                        obligation_reason="grace_cancel_cost",
                        execution=execution,
                    )
                except Exception as exc:
                    observation.obligation_error = exc
                    raise
                observation.obligation_published = True
        await _invoke_progress_callback(downstream, event)

    return _forward, observation


def _direct_lane_progress(
    request: ImageExecutionRequest,
    *,
    lane_name: str,
    metadata_only: bool,
) -> tuple[ImageProgressCallback, _ImageJobLaneObservation]:
    observation = _ImageJobLaneObservation(
        lane_name=lane_name,
        publish_dispatch_cancel_obligation=True,
    )
    downstream = (
        _metadata_only_progress(
            request.progress_callback,
            runtime=request.upstream_runtime,
        )
        if metadata_only
        else request.progress_callback
    )

    async def _forward(event: dict[str, Any]) -> None:
        if event.get("type") == "dispatch_ready":
            observation.dispatch_ready = True
        await _invoke_progress_callback(downstream, event)

    return _forward, observation


async def _publish_dispatch_cancel_obligations(
    request: ImageExecutionRequest,
    cancelled: set[asyncio.Task[list[ImageResult]]],
    observations: dict[asyncio.Task[Any], _ImageJobLaneObservation] | None,
    *,
    race_name: str,
) -> None:
    if not observations:
        return
    for task in cancelled:
        observation = observations.get(task)
        if (
            observation is None
            or not observation.publish_dispatch_cancel_obligation
            or not observation.dispatch_ready
            or observation.obligation_published
        ):
            continue
        try:
            await _publish_dual_race_bonus_ready(
                request,
                lane_name=observation.lane_name,
                race_name=race_name,
                artifact_ready=False,
                obligation_reason="grace_cancel_result_unknown",
            )
        except Exception as exc:
            observation.obligation_error = exc
            raise
        observation.obligation_published = True


def _raise_obligation_publish_error(
    observations: dict[asyncio.Task[Any], _ImageJobLaneObservation] | None,
) -> None:
    if not observations:
        return
    for observation in observations.values():
        if observation.obligation_error is not None:
            raise observation.obligation_error


async def _drain_dual_race_bonus_observer(
    request: ImageExecutionRequest,
    observer: asyncio.Task[list[ImageResult] | None] | None,
    winner: _DualRaceWinner | None,
    *,
    race_name: str,
) -> None:
    if observer is None:
        return
    if (
        not observer.done()
        and winner is not None
        and not _has_successful_task(winner.pending)
    ):
        observer.cancel()
    try:
        await _await_irrevocable_task(observer)
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001
        services = _runtime_services(request.upstream_runtime)
        services.infrastructure.logger.error(
            "%s %s: bonus obligation observer failed during cleanup",
            request.action,
            race_name,
            exc_info=True,
        )


async def _iter_dual_race_results(
    request: ImageExecutionRequest,
    tasks: list[asyncio.Task[list[ImageResult]]],
    lane_names: dict[asyncio.Task[Any], str],
    *,
    grace_seconds: float,
    race_name: str,
    abort_result_unknown: bool,
    lane_observations: (
        dict[asyncio.Task[Any], _ImageJobLaneObservation] | None
    ) = None,
) -> AsyncIterator[ImageResult]:
    runtime = request.upstream_runtime
    winner: _DualRaceWinner | None = None
    bonus_observer: asyncio.Task[list[ImageResult] | None] | None = None
    bonus_observer_consumed = False
    observed_bonus: list[ImageResult] | None | object = _BONUS_NOT_OBSERVED
    try:
        winner = await _select_dual_race_winner(
            request,
            tasks,
            lane_names,
            grace_seconds=grace_seconds,
            race_name=race_name,
            abort_result_unknown=abort_result_unknown,
        )
        bonus_observer = asyncio.create_task(
            _await_dual_race_bonus(
                request,
                winner,
                lane_names,
                grace_seconds=grace_seconds,
                race_name=race_name,
                lane_observations=lane_observations,
            ),
            name=f"{request.action}-{race_name.replace(' ', '-')}-bonus-observer",
        )
        if _has_successful_task(winner.pending):
            try:
                observed_bonus = await _await_irrevocable_task(bonus_observer)
            finally:
                bonus_observer_consumed = True
        for item in winner.results:
            yield item
        if observed_bonus is _BONUS_NOT_OBSERVED:
            try:
                bonus = await bonus_observer
            finally:
                bonus_observer_consumed = True
        else:
            bonus = observed_bonus
        for item in bonus or []:
            yield item
    finally:
        try:
            await _cleanup_race_tasks(
                tasks,
                label=f"{request.action} {race_name} final cleanup",
                runtime=runtime,
            )
        finally:
            if not bonus_observer_consumed:
                await _drain_dual_race_bonus_observer(
                    request,
                    bonus_observer,
                    winner,
                    race_name=race_name,
                )
            _raise_obligation_publish_error(lane_observations)


async def _dual_race_image_action(
    request: ImageExecutionRequest,
    *,
    allow_provider_override_race: bool = False,
) -> AsyncIterator[tuple[str, str | None]]:
    """Race direct image2 and Responses while allowing a bonus result."""
    if request.provider_override is not None and not allow_provider_override_race:
        yield await _run_responses_lane(request, use_httpx=False)
        return
    image2_progress, image2_observation = _direct_lane_progress(
        request,
        lane_name="image2",
        metadata_only=False,
    )
    responses_progress, responses_observation = _direct_lane_progress(
        request,
        lane_name="responses",
        metadata_only=True,
    )
    image2 = request.with_progress(image2_progress)
    secondary = request.with_progress(responses_progress)
    tasks: list[asyncio.Task[list[tuple[str, str | None]]]] = [
        asyncio.create_task(
            _run_direct_image2_lane(image2),
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
    lane_observations = {
        tasks[0]: image2_observation,
        tasks[1]: responses_observation,
    }
    async with aclosing(
        _iter_dual_race_results(
            request,
            tasks,
            lane_names,
            grace_seconds=_dual_race_grace_seconds(request, image_jobs=False),
            race_name="dual_race",
            abort_result_unknown=True,
            lane_observations=lane_observations,
        )
    ) as results:
        async for item in results:
            yield item


async def _dual_race_image_jobs_action(
    request: ImageExecutionRequest,
) -> AsyncIterator[tuple[str, str | None]]:
    """Race image-job generations and Responses endpoints with bonus grace."""
    race_name = "image_jobs dual_race"
    generations_progress, generations_observation = _image_job_lane_progress(
        request,
        lane_name="image_jobs:generations",
        race_name=race_name,
        metadata_only=False,
    )
    responses_progress, responses_observation = _image_job_lane_progress(
        request,
        lane_name="image_jobs:responses",
        race_name=race_name,
        metadata_only=True,
    )
    generations = request.with_progress(generations_progress)
    responses = request.with_progress(responses_progress)
    tasks: list[asyncio.Task[list[ImageResult]]] = [
        asyncio.create_task(
            _run_dual_image_job_lane(generations, endpoint="generations"),
            name=f"{request.action}-image-jobs-dual-generations",
        ),
        asyncio.create_task(
            _run_dual_image_job_lane(responses, endpoint="responses"),
            name=f"{request.action}-image-jobs-dual-responses",
        ),
    ]
    lane_names: dict[asyncio.Task[Any], str] = {
        tasks[0]: "image_jobs:generations",
        tasks[1]: "image_jobs:responses",
    }
    lane_observations = {
        tasks[0]: generations_observation,
        tasks[1]: responses_observation,
    }
    async with aclosing(
        _iter_dual_race_results(
            request,
            tasks,
            lane_names,
            grace_seconds=_dual_race_grace_seconds(request, image_jobs=True),
            race_name=race_name,
            abort_result_unknown=False,
            lane_observations=lane_observations,
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
