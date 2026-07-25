"""Image channel/engine dispatch and public generation entry points."""

from __future__ import annotations

from ..provider_runtime.upstream_services import upstream_services

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from .image_execution import ImageExecutionRequest, ImageProviderRoute, ImageResult
from .transport import ImageProgressCallback


def _image_jobs_endpoint_for_engine(engine: str) -> str:
    if engine == upstream_services().core.IMAGE_ROUTE_IMAGE2:
        return "generations"
    return "responses"


def _provider_supports_image_jobs(provider: Any) -> bool:
    try:
        return upstream_services().infrastructure.parse_provider_bool(
            getattr(provider, "image_jobs_enabled", False),
            default=False,
        )
    except ValueError:
        return False


def _should_use_image_jobs(channel: str, provider: Any) -> bool:
    supports_jobs = upstream_services().dispatch.provider_supports_image_jobs(provider)
    if channel == upstream_services().core.IMAGE_CHANNEL_IMAGE_JOBS_ONLY:
        if not supports_jobs:
            provider_name = getattr(provider, "name", "unknown")
            raise upstream_services().infrastructure.UpstreamError(
                f"provider {provider_name} does not support image_jobs "
                "(channel=image_jobs_only)",
                error_code=upstream_services().infrastructure.EC.ALL_ACCOUNTS_FAILED.value,
                status_code=503,
                payload={
                    "provider": str(provider_name),
                    "channel": channel,
                    "reason": "image_jobs_not_enabled",
                },
            )
        return True
    if channel == upstream_services().core.IMAGE_CHANNEL_STREAM_ONLY:
        return False
    return supports_jobs


def _is_image_job_configuration_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, upstream_services().infrastructure.UpstreamError)
        and getattr(exc, "payload", {}).get("reason") == "configuration_unavailable"
    )


async def _validate_selected_image_job_configuration(provider: Any) -> None:
    upstream_services().image_jobs.image_job_sidecar_token()
    provider_base_url = str(getattr(provider, "image_jobs_base_url", "") or "").strip()
    if provider_base_url:
        upstream_services().requests.validate_image_job_base_url(provider_base_url)
        return
    await upstream_services().direct.resolve_image_job_base_url()


def _image_endpoint_kind_for_engine(engine: str) -> str | None:
    if engine == upstream_services().core.IMAGE_ROUTE_IMAGE2:
        return "generations"
    if engine == upstream_services().core.IMAGE_ROUTE_RESPONSES:
        return "responses"
    return None


async def _image_dispatch_candidates(
    provider_override: Any | None,
    *,
    engine: str,
) -> list[Any]:
    if provider_override is not None:
        endpoint_kind = upstream_services().dispatch.image_endpoint_kind_for_engine(
            engine
        )
        if endpoint_kind is not None:
            unavailable_error = (
                upstream_services().providers.provider_endpoint_unavailable_error(
                    provider_override,
                    endpoint_kind,
                )
            )
            if unavailable_error is not None:
                raise unavailable_error
        return [provider_override]

    pool = await upstream_services().infrastructure.provider_pool.get_pool()
    return await upstream_services().providers.pool_select_compat(
        pool,
        route="image",
        ignore_cooldown=True,
        endpoint_kind=upstream_services().dispatch.image_endpoint_kind_for_engine(
            engine
        ),
    )


async def _prepare_provider_route(
    request: ImageExecutionRequest,
    *,
    channel: str,
    engine: str,
) -> ImageProviderRoute:
    provider = request.provider_override
    use_jobs = upstream_services().dispatch.should_use_image_jobs(channel, provider)
    provider_name = getattr(provider, "name", "unknown")
    if use_jobs:
        try:
            await _validate_selected_image_job_configuration(provider)
        except upstream_services().infrastructure.UpstreamError as exc:
            if (
                channel != upstream_services().core.IMAGE_CHANNEL_AUTO
                or not _is_image_job_configuration_error(exc)
            ):
                raise
            use_jobs = False
            upstream_services().infrastructure.logger.warning(
                "%s image dispatch provider=%s channel=auto "
                "image_jobs configuration unavailable; falling back to stream",
                request.action,
                provider_name,
            )
            await upstream_services().transport.emit_image_progress(
                request.progress_callback,
                "route_diagnostic",
                provider=provider_name,
                route=f"{channel}:{engine}",
                reason="image_job_configuration_unavailable",
                fallback_route=f"stream_only:{engine}",
                status="routed",
            )
    upstream_services().infrastructure.logger.info(
        "%s image dispatch provider=%s channel=%s engine=%s use_jobs=%s mask=%s",
        request.action,
        provider_name,
        channel,
        engine,
        use_jobs,
        request.mask is not None,
    )
    if (
        engine != upstream_services().core.IMAGE_ROUTE_DUAL_RACE
        or not upstream_services().providers.is_byok_provider(provider)
    ):
        return ImageProviderRoute(channel, engine, use_jobs, provider_name)
    await upstream_services().transport.emit_image_progress(
        request.progress_callback,
        "route_diagnostic",
        provider=provider_name,
        route=f"{channel}:{engine}",
        reason="byok_disables_dual_race",
        fallback_route=f"{channel}:{upstream_services().core.IMAGE_ROUTE_RESPONSES}",
        byok=True,
        status="routed",
    )
    return ImageProviderRoute(
        channel,
        upstream_services().core.IMAGE_ROUTE_RESPONSES,
        use_jobs,
        provider_name,
    )


def _require_edit_images(request: ImageExecutionRequest) -> None:
    if request.images:
        return
    raise upstream_services().infrastructure.UpstreamError(
        "edit action requires at least one reference image",
        error_code=upstream_services().infrastructure.EC.MISSING_INPUT_IMAGES.value,
        status_code=400,
    )


def _require_mask_images(request: ImageExecutionRequest) -> None:
    if request.images and any(request.images):
        return
    raise upstream_services().infrastructure.UpstreamError(
        "mask requires at least one reference image",
        error_code=upstream_services().infrastructure.EC.MISSING_INPUT_IMAGES.value,
        status_code=400,
    )


async def _run_direct_image2_once(
    request: ImageExecutionRequest,
) -> list[ImageResult]:
    if request.action == "edit":
        _require_edit_images(request)
        return await upstream_services().direct.direct_edit_image_with_failover(
            **request.direct_edit_kwargs()
        )
    return await upstream_services().direct.direct_generate_image_with_failover(
        **request.direct_generate_kwargs()
    )


async def _run_responses_once(
    request: ImageExecutionRequest,
) -> ImageResult:
    return await upstream_services().race.race_responses_image(
        **request.responses_kwargs(),
        lanes=max(1, int(upstream_services().infrastructure.settings.edit_race_lanes)),
    )


def _merge_image_route_errors(
    request: ImageExecutionRequest,
    *,
    primary_path: str,
    primary_error: BaseException,
    fallback_path: str,
    fallback_error: BaseException,
) -> BaseException:
    return upstream_services().retry.merge_image_path_errors(
        action=request.action,
        primary_path=primary_path,
        primary_error=primary_error,
        fallback_path=fallback_path,
        fallback_error=fallback_error,
    )


async def _run_image2_with_responses_fallback(
    request: ImageExecutionRequest,
    route: ImageProviderRoute,
) -> list[ImageResult]:
    try:
        return await _run_direct_image2_once(request)
    except (
        asyncio.CancelledError,
        upstream_services().infrastructure.UpstreamCancelled,
    ):
        raise
    except Exception as primary_error:  # noqa: BLE001
        if upstream_services().direct.is_direct_image_result_unknown(primary_error):
            raise
        upstream_services().infrastructure.logger.warning(
            "%s image2 provider=%s failed; falling back to responses: %r",
            request.action,
            route.provider_name,
            primary_error,
        )
        unavailable = upstream_services().providers.provider_endpoint_unavailable_error(
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
            upstream_services().infrastructure.UpstreamCancelled,
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
    try:
        return [await _run_responses_once(request)]
    except (
        asyncio.CancelledError,
        upstream_services().infrastructure.UpstreamCancelled,
    ):
        raise
    except Exception as primary_error:  # noqa: BLE001
        upstream_services().infrastructure.logger.warning(
            "%s responses provider=%s failed; falling back to image2: %r",
            request.action,
            route.provider_name,
            primary_error,
        )
        unavailable = upstream_services().providers.provider_endpoint_unavailable_error(
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
            except upstream_services().infrastructure.UpstreamError as missing_images:
                raise missing_images from primary_error
        try:
            return await _run_direct_image2_once(request)
        except (
            asyncio.CancelledError,
            upstream_services().infrastructure.UpstreamCancelled,
        ):
            raise
        except Exception as fallback_error:  # noqa: BLE001
            if upstream_services().direct.is_direct_image_result_unknown(
                fallback_error
            ):
                raise
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
    if request.action != "edit":
        raise upstream_services().infrastructure.UpstreamError(
            f"mask only supported on edit action (got {request.action})",
            error_code=upstream_services().infrastructure.EC.INVALID_REQUEST_ERROR.value,
            status_code=400,
        )
    _require_mask_images(request)
    if route.engine != upstream_services().core.IMAGE_ROUTE_IMAGE2 or route.use_jobs:
        await upstream_services().transport.emit_image_progress(
            request.progress_callback,
            "route_diagnostic",
            provider=route.provider_name,
            route=f"{route.channel}:{route.engine}",
            reason="mask_requires_generations_endpoint",
            fallback_route=(
                "image_jobs:generations" if route.use_jobs else "image2_edit_direct"
            ),
            byok=upstream_services().providers.is_byok_provider(
                request.provider_override
            ),
            status="routed",
        )
    if not route.use_jobs:
        return await _run_direct_image2_once(request)
    return [
        await upstream_services().image_jobs.image_job_with_failover(
            **request.action_kwargs(),
            endpoint_override="generations",
        )
    ]


def _dual_race_image_iter(
    request: ImageExecutionRequest,
    route: ImageProviderRoute,
) -> AsyncIterator[ImageResult]:
    race = (
        upstream_services().race.dual_race_image_jobs_action
        if route.use_jobs
        else upstream_services().race.dual_race_image_action
    )
    kwargs = request.action_kwargs()
    if not route.use_jobs:
        kwargs["allow_provider_override_race"] = True
    return race(**kwargs)


async def _run_non_race_image_once(
    request: ImageExecutionRequest,
    route: ImageProviderRoute,
) -> list[ImageResult]:
    if route.use_jobs:
        return [
            await upstream_services().image_jobs.image_job_with_failover(
                **request.action_kwargs(),
                endpoint_preference=upstream_services().dispatch.image_jobs_endpoint_for_engine(
                    route.engine
                ),
            )
        ]
    if route.engine == upstream_services().core.IMAGE_ROUTE_IMAGE2:
        return await _run_image2_with_responses_fallback(request, route)
    return await _run_responses_with_image2_fallback(request, route)


async def _run_image_once_for_provider(
    *,
    action: str,
    provider: Any,
    channel: str,
    engine: str,
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
    user_id: str | None = None,
) -> AsyncIterator[tuple[str, str | None]]:
    request = ImageExecutionRequest(
        action,
        prompt,
        size,
        images,
        mask,
        n,
        quality,
        output_format,
        output_compression,
        background,
        moderation,
        model,
        progress_callback,
        provider,
        user_id,
    )
    route = await _prepare_provider_route(request, channel=channel, engine=engine)
    if request.mask is not None:
        for item in await _run_masked_image_once(request, route):
            yield item
        return
    if route.engine == upstream_services().core.IMAGE_ROUTE_DUAL_RACE:
        async with aclosing(_dual_race_image_iter(request, route)) as results:
            async for item in results:
                yield item
        return
    for item in await _run_non_race_image_once(request, route):
        yield item


async def _dispatch_image(
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
) -> AsyncIterator[tuple[str, str | None]]:
    from ..retry import is_retriable as classify_retriable

    channel = await upstream_services().core.resolve_image_channel()
    engine = await upstream_services().core.resolve_image_engine()
    dispatch_endpoint_kind = (
        upstream_services().dispatch.image_endpoint_kind_for_engine(engine)
    )
    try:
        providers = await upstream_services().dispatch.image_dispatch_candidates(
            provider_override,
            engine=engine,
        )
    except TypeError as exc:
        if "engine" not in str(exc):
            raise
        providers = await upstream_services().dispatch.image_dispatch_candidates(
            provider_override
        )
        endpoint_kind = upstream_services().dispatch.image_endpoint_kind_for_engine(
            engine
        )
        if endpoint_kind is not None:
            providers = [
                provider
                for provider in providers
                if upstream_services().providers.provider_allows_image_endpoint(
                    provider,
                    endpoint_kind,
                )
            ]
    errors: list[BaseException] = []
    dispatch_owns_inflight = provider_override is None
    pool = (
        await upstream_services().infrastructure.provider_pool.get_pool()
        if dispatch_owns_inflight
        else None
    )

    for index, provider in enumerate(providers):
        if dispatch_owns_inflight and index > 0 and pool is not None:
            upstream_services().providers.pool_acquire_inflight(
                pool,
                provider.name,
                dispatch_endpoint_kind,
            )
        try:
            any_yielded = False
            try:
                image_iter = upstream_services().dispatch.run_image_once_for_provider(
                    action=action,
                    provider=provider,
                    channel=channel,
                    engine=engine,
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
                    model=model,
                    progress_callback=progress_callback,
                    user_id=user_id,
                )
                async for item in image_iter:
                    any_yielded = True
                    yield item
                return
            except (
                asyncio.CancelledError,
                upstream_services().infrastructure.UpstreamCancelled,
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
                    upstream_services().retry.should_continue_image_provider_failover(
                        exc,
                        retriable=decision.retriable,
                    )
                )
                if (
                    channel == upstream_services().core.IMAGE_CHANNEL_IMAGE_JOBS_ONLY
                    and not upstream_services().dispatch.provider_supports_image_jobs(
                        provider
                    )
                ):
                    raise
                if not should_continue:
                    raise
                remaining = len(providers) - index - 1
                if remaining <= 0:
                    continue
                provider_name = getattr(provider, "name", "unknown")
                upstream_services().infrastructure.logger.warning(
                    "%s image dispatch provider_failover: from=%s "
                    "remaining=%d channel=%s engine=%s reason=%s",
                    action,
                    provider_name,
                    remaining,
                    channel,
                    engine,
                    decision.reason,
                )
                await upstream_services().transport.emit_image_progress(
                    progress_callback,
                    "provider_failover",
                    from_provider=provider_name,
                    remaining=remaining,
                    reason=decision.reason,
                    route=f"{channel}:{engine}",
                    **upstream_services().providers.provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        status="failed",
                        reason=decision.reason,
                        exc=exc,
                    ),
                )
        finally:
            if dispatch_owns_inflight and pool is not None:
                upstream_services().providers.pool_release_inflight(
                    pool,
                    provider.name,
                    dispatch_endpoint_kind,
                )

    merged = upstream_services().retry.merge_fallback_errors(
        errors,
        error_code=upstream_services().infrastructure.EC.ALL_ACCOUNTS_FAILED.value,
        message=f"all {len(providers)} image dispatch provider(s) failed",
    )
    merged.payload["provider_errors"] = (
        upstream_services().retry.provider_error_details(
            providers,
            errors,
        )
    )
    merged.payload["channel"] = channel
    merged.payload["engine"] = engine
    raise merged


async def generate_image(
    *,
    prompt: str,
    size: str,
    n: int = 1,
    quality: str = "high",
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None = None,
    provider_override: Any | None = None,
    user_id: str | None = None,
) -> AsyncIterator[tuple[str, str | None]]:
    """Text-to-image dispatch using image.channel and image.engine."""
    async for item in upstream_services().dispatch.dispatch_image(
        action="generate",
        prompt=prompt,
        size=size,
        images=None,
        mask=None,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
        progress_callback=progress_callback,
        provider_override=provider_override,
        user_id=user_id,
    ):
        yield item


async def edit_image(
    *,
    prompt: str,
    size: str,
    images: list[bytes],
    mask: bytes | None = None,
    n: int = 1,
    quality: str = "high",
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None = None,
    provider_override: Any | None = None,
    user_id: str | None = None,
) -> AsyncIterator[tuple[str, str | None]]:
    """Image-to-image dispatch with optional generations-only inpaint mask."""
    if not images or not any(images):
        raise upstream_services().infrastructure.UpstreamError(
            "edit action requires at least one reference image",
            error_code=upstream_services().infrastructure.EC.MISSING_INPUT_IMAGES.value,
            status_code=400,
        )
    effective_prompt = (
        upstream_services().direct.wrap_inpaint_prompt(prompt)
        if mask is not None
        else prompt
    )
    async for item in upstream_services().dispatch.dispatch_image(
        action="edit",
        prompt=effective_prompt,
        size=size,
        images=images,
        mask=mask,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
        progress_callback=progress_callback,
        provider_override=provider_override,
        user_id=user_id,
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
