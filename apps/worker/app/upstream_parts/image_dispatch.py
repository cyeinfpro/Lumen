"""Image channel/engine dispatch and public generation entry points."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from . import direct_requests
from .image_execution import (
    ImageExecutionRequest,
    ImageProviderRoute,
    ImageResult,
)


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def _request_services(request: ImageExecutionRequest) -> UpstreamServices:
    return _runtime_services(request.upstream_runtime)


def _image_jobs_endpoint_for_engine(
    engine: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = _runtime_services(runtime)
    if engine == services.core.IMAGE_ROUTE_IMAGE2:
        return "generations"
    return "responses"


def _provider_supports_image_jobs(
    provider: Any,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    services = _runtime_services(runtime)
    try:
        return services.infrastructure.parse_provider_bool(
            getattr(provider, "image_jobs_enabled", False),
            default=False,
        )
    except ValueError:
        return False


def _should_use_image_jobs(
    channel: str,
    provider: Any,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    services = _runtime_services(runtime)
    supports_jobs = _provider_supports_image_jobs(provider, runtime=runtime)
    if channel == services.core.IMAGE_CHANNEL_IMAGE_JOBS_ONLY:
        if not supports_jobs:
            provider_name = getattr(provider, "name", "unknown")
            raise services.infrastructure.UpstreamError(
                f"provider {provider_name} does not support image_jobs "
                "(channel=image_jobs_only)",
                error_code=services.infrastructure.EC.ALL_ACCOUNTS_FAILED.value,
                status_code=503,
                payload={
                    "provider": str(provider_name),
                    "channel": channel,
                    "reason": "image_jobs_not_enabled",
                },
            )
        return True
    if channel == services.core.IMAGE_CHANNEL_STREAM_ONLY:
        return False
    return supports_jobs


def _is_image_job_configuration_error(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    services = _runtime_services(runtime)
    return (
        isinstance(exc, services.infrastructure.UpstreamError)
        and getattr(exc, "payload", {}).get("reason") == "configuration_unavailable"
    )


async def _validate_selected_image_job_configuration(
    provider: Any,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = _runtime_services(runtime)
    services.image_jobs.image_job_sidecar_token()
    provider_base_url = str(getattr(provider, "image_jobs_base_url", "") or "").strip()
    if provider_base_url:
        services.requests.validate_image_job_base_url(provider_base_url)
        return
    await direct_requests._resolve_image_job_base_url(runtime=runtime)


def _image_endpoint_kind_for_engine(
    engine: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    services = _runtime_services(runtime)
    if engine == services.core.IMAGE_ROUTE_IMAGE2:
        return "generations"
    if engine == services.core.IMAGE_ROUTE_RESPONSES:
        return "responses"
    return None


async def _image_dispatch_candidates(
    provider_override: Any | None,
    *,
    engine: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> list[Any]:
    services = _runtime_services(runtime)
    if provider_override is not None:
        endpoint_kind = _image_endpoint_kind_for_engine(engine, runtime=runtime)
        if endpoint_kind is not None:
            unavailable_error = services.providers.provider_endpoint_unavailable_error(
                provider_override,
                endpoint_kind,
            )
            if unavailable_error is not None:
                raise unavailable_error
        return [provider_override]

    pool = await services.infrastructure.provider_pool.get_pool()
    return await services.providers.pool_select_compat(
        pool,
        route="image",
        ignore_cooldown=True,
        endpoint_kind=_image_endpoint_kind_for_engine(engine, runtime=runtime),
    )


async def _prepare_provider_route(
    request: ImageExecutionRequest,
    *,
    channel: str,
    engine: str,
) -> ImageProviderRoute:
    runtime = request.upstream_runtime
    services = _request_services(request)
    provider = request.provider_override
    use_jobs = _should_use_image_jobs(channel, provider, runtime=runtime)
    provider_name = getattr(provider, "name", "unknown")
    if use_jobs:
        try:
            await _validate_selected_image_job_configuration(
                provider,
                runtime=runtime,
            )
        except services.infrastructure.UpstreamError as exc:
            if (
                channel != services.core.IMAGE_CHANNEL_AUTO
                or not _is_image_job_configuration_error(exc, runtime=runtime)
            ):
                raise
            use_jobs = False
            services.infrastructure.logger.warning(
                "%s image dispatch provider=%s channel=auto "
                "image_jobs configuration unavailable; falling back to stream",
                request.action,
                provider_name,
            )
            await services.transport.emit_image_progress(
                request.progress_callback,
                "route_diagnostic",
                provider=provider_name,
                route=f"{channel}:{engine}",
                reason="image_job_configuration_unavailable",
                fallback_route=f"stream_only:{engine}",
                status="routed",
            )
    services.infrastructure.logger.info(
        "%s image dispatch provider=%s channel=%s engine=%s use_jobs=%s mask=%s",
        request.action,
        provider_name,
        channel,
        engine,
        use_jobs,
        request.mask is not None,
    )
    if (
        engine != services.core.IMAGE_ROUTE_DUAL_RACE
        or not services.providers.is_byok_provider(provider)
    ):
        return ImageProviderRoute(channel, engine, use_jobs, provider_name)
    await services.transport.emit_image_progress(
        request.progress_callback,
        "route_diagnostic",
        provider=provider_name,
        route=f"{channel}:{engine}",
        reason="byok_disables_dual_race",
        fallback_route=f"{channel}:{services.core.IMAGE_ROUTE_RESPONSES}",
        byok=True,
        status="routed",
    )
    return ImageProviderRoute(
        channel,
        services.core.IMAGE_ROUTE_RESPONSES,
        use_jobs,
        provider_name,
    )


def _require_edit_images(request: ImageExecutionRequest) -> None:
    services = _request_services(request)
    if request.images:
        return
    raise services.infrastructure.UpstreamError(
        "edit action requires at least one reference image",
        error_code=services.infrastructure.EC.MISSING_INPUT_IMAGES.value,
        status_code=400,
    )


def _require_mask_images(request: ImageExecutionRequest) -> None:
    services = _request_services(request)
    if request.images and any(request.images):
        return
    raise services.infrastructure.UpstreamError(
        "mask requires at least one reference image",
        error_code=services.infrastructure.EC.MISSING_INPUT_IMAGES.value,
        status_code=400,
    )


async def _run_direct_image2_once(
    request: ImageExecutionRequest,
) -> list[ImageResult]:
    services = _request_services(request)
    if request.action == "edit":
        _require_edit_images(request)
        return await services.direct.direct_edit_image_with_failover(request)
    return await services.direct.direct_generate_image_with_failover(request)


async def _run_responses_once(
    request: ImageExecutionRequest,
) -> ImageResult:
    services = _request_services(request)
    return await services.race.race_responses_image(
        request,
        lanes=max(1, int(services.infrastructure.settings.edit_race_lanes)),
    )


def _merge_image_route_errors(
    request: ImageExecutionRequest,
    *,
    primary_path: str,
    primary_error: BaseException,
    fallback_path: str,
    fallback_error: BaseException,
) -> BaseException:
    services = _request_services(request)
    return services.retry.merge_image_path_errors(
        action=request.action,
        primary_path=primary_path,
        primary_error=primary_error,
        fallback_path=fallback_path,
        fallback_error=fallback_error,
        runtime=request.upstream_runtime,
    )


async def _run_image2_with_responses_fallback(
    request: ImageExecutionRequest,
    route: ImageProviderRoute,
) -> list[ImageResult]:
    services = _request_services(request)
    try:
        return await _run_direct_image2_once(request)
    except (
        asyncio.CancelledError,
        services.infrastructure.UpstreamCancelled,
    ):
        raise
    except Exception as primary_error:  # noqa: BLE001
        if direct_requests._is_direct_image_result_unknown(
            primary_error,
            runtime=request.upstream_runtime,
        ):
            raise
        services.infrastructure.logger.warning(
            "%s image2 provider=%s failed; falling back to responses: %r",
            request.action,
            route.provider_name,
            primary_error,
        )
        unavailable = services.providers.provider_endpoint_unavailable_error(
            request.provider_override,
            "responses",
        )
        if unavailable is not None:
            raise _merge_image_route_errors(
                request,
                primary_path="image2",
                primary_error=primary_error,
                fallback_path="responses",
                fallback_error=unavailable,
            ) from primary_error
        try:
            return [await _run_responses_once(request)]
        except (
            asyncio.CancelledError,
            services.infrastructure.UpstreamCancelled,
        ):
            raise
        except Exception as fallback_error:  # noqa: BLE001
            raise _merge_image_route_errors(
                request,
                primary_path="image2",
                primary_error=primary_error,
                fallback_path="responses",
                fallback_error=fallback_error,
            ) from fallback_error


async def _run_responses_with_image2_fallback(
    request: ImageExecutionRequest,
    route: ImageProviderRoute,
) -> list[ImageResult]:
    services = _request_services(request)
    try:
        return [await _run_responses_once(request)]
    except (
        asyncio.CancelledError,
        services.infrastructure.UpstreamCancelled,
    ):
        raise
    except Exception as primary_error:  # noqa: BLE001
        if direct_requests._is_direct_image_result_unknown(
            primary_error,
            runtime=request.upstream_runtime,
        ):
            raise
        services.infrastructure.logger.warning(
            "%s responses provider=%s failed; falling back to image2: %r",
            request.action,
            route.provider_name,
            primary_error,
        )
        unavailable = services.providers.provider_endpoint_unavailable_error(
            request.provider_override,
            "generations",
        )
        if unavailable is not None:
            raise _merge_image_route_errors(
                request,
                primary_path="responses",
                primary_error=primary_error,
                fallback_path="image2",
                fallback_error=unavailable,
            ) from primary_error
        if request.action == "edit" and not request.images:
            try:
                _require_edit_images(request)
            except services.infrastructure.UpstreamError as missing_images:
                raise missing_images from primary_error
        try:
            return await _run_direct_image2_once(request)
        except (
            asyncio.CancelledError,
            services.infrastructure.UpstreamCancelled,
        ):
            raise
        except Exception as fallback_error:  # noqa: BLE001
            raise _merge_image_route_errors(
                request,
                primary_path="responses",
                primary_error=primary_error,
                fallback_path="image2",
                fallback_error=fallback_error,
            ) from fallback_error


async def _run_masked_image_once(
    request: ImageExecutionRequest,
    route: ImageProviderRoute,
) -> list[ImageResult]:
    services = _request_services(request)
    if request.action != "edit":
        raise services.infrastructure.UpstreamError(
            f"mask only supported on edit action (got {request.action})",
            error_code=services.infrastructure.EC.INVALID_REQUEST_ERROR.value,
            status_code=400,
        )
    _require_mask_images(request)
    if route.engine != services.core.IMAGE_ROUTE_IMAGE2 or route.use_jobs:
        await services.transport.emit_image_progress(
            request.progress_callback,
            "route_diagnostic",
            provider=route.provider_name,
            route=f"{route.channel}:{route.engine}",
            reason="mask_requires_generations_endpoint",
            fallback_route=(
                "image_jobs:generations" if route.use_jobs else "image2_edit_direct"
            ),
            byok=services.providers.is_byok_provider(request.provider_override),
            status="routed",
        )
    if not route.use_jobs:
        return await _run_direct_image2_once(request)
    return [
        await services.image_jobs.image_job_with_failover(
            request,
            endpoint_override="generations",
        )
    ]


def _dual_race_image_iter(
    request: ImageExecutionRequest,
    route: ImageProviderRoute,
) -> AsyncIterator[ImageResult]:
    services = _request_services(request)
    race = (
        services.race.dual_race_image_jobs_action
        if route.use_jobs
        else services.race.dual_race_image_action
    )
    if route.use_jobs:
        return race(request)
    return race(request, allow_provider_override_race=True)


async def _run_non_race_image_once(
    request: ImageExecutionRequest,
    route: ImageProviderRoute,
) -> list[ImageResult]:
    runtime = request.upstream_runtime
    services = _request_services(request)
    if route.use_jobs:
        return [
            await services.image_jobs.image_job_with_failover(
                request,
                endpoint_preference=_image_jobs_endpoint_for_engine(
                    route.engine,
                    runtime=runtime,
                ),
            )
        ]
    if route.engine == services.core.IMAGE_ROUTE_IMAGE2:
        return await _run_image2_with_responses_fallback(request, route)
    return await _run_responses_with_image2_fallback(request, route)


async def _run_image_once_for_provider(
    request: ImageExecutionRequest,
    *,
    channel: str,
    engine: str,
) -> AsyncIterator[tuple[str, str | None]]:
    services = _request_services(request)
    route = await _prepare_provider_route(request, channel=channel, engine=engine)
    if request.mask is not None:
        for item in await _run_masked_image_once(request, route):
            yield item
        return
    if route.engine == services.core.IMAGE_ROUTE_DUAL_RACE:
        async with aclosing(_dual_race_image_iter(request, route)) as results:
            async for item in results:
                yield item
        return
    for item in await _run_non_race_image_once(request, route):
        yield item


async def _resume_persisted_image_jobs(
    request: ImageExecutionRequest,
) -> AsyncIterator[ImageResult]:
    services = _request_services(request)
    async with aclosing(services.image_jobs.resume_image_jobs(request)) as results:
        async for item in results:
            yield item


async def _dispatch_fresh_image(
    request: ImageExecutionRequest,
) -> AsyncIterator[tuple[str, str | None]]:
    from ..retry import is_retriable as classify_retriable

    runtime = request.upstream_runtime
    services = _request_services(request)
    channel = await services.core.resolve_image_channel()
    engine = await services.core.resolve_image_engine()
    dispatch_endpoint_kind = _image_endpoint_kind_for_engine(
        engine,
        runtime=runtime,
    )
    providers = await services.dispatch.image_dispatch_candidates(
        request.provider_override,
        engine=engine,
        runtime=runtime,
    )
    errors: list[BaseException] = []
    dispatch_owns_inflight = request.provider_override is None
    pool = (
        await services.infrastructure.provider_pool.get_pool()
        if dispatch_owns_inflight
        else None
    )

    for index, provider in enumerate(providers):
        if dispatch_owns_inflight and index > 0 and pool is not None:
            services.providers.pool_acquire_inflight(
                pool,
                provider.name,
                dispatch_endpoint_kind,
            )
        try:
            any_yielded = False
            try:
                image_iter = _run_image_once_for_provider(
                    request.with_provider(provider),
                    channel=channel,
                    engine=engine,
                )
                async for item in image_iter:
                    any_yielded = True
                    yield item
                return
            except (
                asyncio.CancelledError,
                services.infrastructure.UpstreamCancelled,
            ):
                raise
            except Exception as exc:  # noqa: BLE001
                if any_yielded:
                    raise
                errors.append(exc)
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                should_continue = (
                    services.retry.should_continue_image_provider_failover(
                        exc,
                        retriable=decision.retriable,
                        runtime=runtime,
                    )
                )
                if (
                    channel == services.core.IMAGE_CHANNEL_IMAGE_JOBS_ONLY
                    and not _provider_supports_image_jobs(
                        provider,
                        runtime=runtime,
                    )
                ):
                    raise
                if not should_continue:
                    raise
                remaining = len(providers) - index - 1
                if remaining <= 0:
                    continue
                provider_name = getattr(provider, "name", "unknown")
                services.infrastructure.logger.warning(
                    "%s image dispatch provider_failover: from=%s "
                    "remaining=%d channel=%s engine=%s reason=%s",
                    request.action,
                    provider_name,
                    remaining,
                    channel,
                    engine,
                    decision.reason,
                )
                await services.transport.emit_image_progress(
                    request.progress_callback,
                    "provider_failover",
                    from_provider=provider_name,
                    remaining=remaining,
                    reason=decision.reason,
                    route=f"{channel}:{engine}",
                    **services.providers.provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        status="failed",
                        reason=decision.reason,
                        exc=exc,
                    ),
                )
        finally:
            if dispatch_owns_inflight and pool is not None:
                services.providers.pool_release_inflight(
                    pool,
                    provider.name,
                    dispatch_endpoint_kind,
                )

    merged = services.retry.merge_fallback_errors(
        errors,
        error_code=services.infrastructure.EC.ALL_ACCOUNTS_FAILED.value,
        message=f"all {len(providers)} image dispatch provider(s) failed",
        runtime=runtime,
    )
    merged.payload["provider_errors"] = services.retry.provider_error_details(
        providers,
        errors,
        runtime=runtime,
    )
    merged.payload["channel"] = channel
    merged.payload["engine"] = engine
    raise merged


async def _dispatch_image(
    request: ImageExecutionRequest,
) -> AsyncIterator[tuple[str, str | None]]:
    image_iter = (
        _resume_persisted_image_jobs(request)
        if request.request_context.sidecar_execution is not None
        else _dispatch_fresh_image(request)
    )
    async with aclosing(image_iter) as results:
        async for item in results:
            yield item


async def generate_image(
    request: ImageExecutionRequest,
) -> AsyncIterator[tuple[str, str | None]]:
    """Text-to-image dispatch using image.channel and image.engine."""
    services = _request_services(request)
    if request.action != "generate":
        raise ValueError("generate_image requires action='generate'")
    async for item in services.dispatch.dispatch_image(request):
        yield item


async def edit_image(
    request: ImageExecutionRequest,
) -> AsyncIterator[tuple[str, str | None]]:
    """Image-to-image dispatch with optional generations-only inpaint mask."""
    services = _request_services(request)
    if request.action != "edit":
        raise ValueError("edit_image requires action='edit'")
    if not request.images or not any(request.images):
        raise services.infrastructure.UpstreamError(
            "edit action requires at least one reference image",
            error_code=services.infrastructure.EC.MISSING_INPUT_IMAGES.value,
            status_code=400,
        )
    effective_prompt = (
        direct_requests._wrap_inpaint_prompt(
            request.prompt,
            runtime=request.upstream_runtime,
        )
        if request.mask is not None
        else request.prompt
    )
    async for item in services.dispatch.dispatch_image(
        request.with_prompt(effective_prompt)
    ):
        yield item


image_endpoint_kind_for_engine = _image_endpoint_kind_for_engine


__all__ = [
    "_dispatch_image",
    "_image_dispatch_candidates",
    "_image_endpoint_kind_for_engine",
    "image_endpoint_kind_for_engine",
    "_image_jobs_endpoint_for_engine",
    "_is_image_job_configuration_error",
    "_provider_supports_image_jobs",
    "_run_image_once_for_provider",
    "_should_use_image_jobs",
    "_validate_selected_image_job_configuration",
    "edit_image",
    "generate_image",
]
