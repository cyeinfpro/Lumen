"""Image channel/engine dispatch and public generation entry points."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from typing import Any

from .transport import ImageProgressCallback

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve compatibility dependencies at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


def _image_jobs_endpoint_for_engine(engine: str) -> str:
    facade = _facade()
    if engine == facade._IMAGE_ROUTE_IMAGE2:
        return "generations"
    return "responses"


def _provider_supports_image_jobs(provider: Any) -> bool:
    facade = _facade()
    try:
        return facade.parse_provider_bool(
            getattr(provider, "image_jobs_enabled", False),
            default=False,
        )
    except ValueError:
        return False


def _should_use_image_jobs(channel: str, provider: Any) -> bool:
    facade = _facade()
    supports_jobs = facade._provider_supports_image_jobs(provider)
    if channel == facade._IMAGE_CHANNEL_IMAGE_JOBS_ONLY:
        if not supports_jobs:
            provider_name = getattr(provider, "name", "unknown")
            raise facade.UpstreamError(
                f"provider {provider_name} does not support image_jobs "
                "(channel=image_jobs_only)",
                error_code=facade.EC.ALL_ACCOUNTS_FAILED.value,
                status_code=503,
                payload={
                    "provider": str(provider_name),
                    "channel": channel,
                    "reason": "image_jobs_not_enabled",
                },
            )
        return True
    if channel == facade._IMAGE_CHANNEL_STREAM_ONLY:
        return False
    return supports_jobs


def _image_endpoint_kind_for_engine(engine: str) -> str | None:
    facade = _facade()
    if engine == facade._IMAGE_ROUTE_IMAGE2:
        return "generations"
    if engine == facade._IMAGE_ROUTE_RESPONSES:
        return "responses"
    return None


async def _image_dispatch_candidates(
    provider_override: Any | None,
    *,
    engine: str,
) -> list[Any]:
    facade = _facade()
    if provider_override is not None:
        endpoint_kind = facade._image_endpoint_kind_for_engine(engine)
        if endpoint_kind is not None:
            unavailable_error = facade._provider_endpoint_unavailable_error(
                provider_override,
                endpoint_kind,
            )
            if unavailable_error is not None:
                raise unavailable_error
        return [provider_override]

    pool = await facade.provider_pool.get_pool()
    return await facade._pool_select_compat(
        pool,
        route="image",
        ignore_cooldown=True,
        endpoint_kind=facade._image_endpoint_kind_for_engine(engine),
    )


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
    facade = _facade()
    use_jobs = facade._should_use_image_jobs(channel, provider)
    provider_name = getattr(provider, "name", "unknown")
    facade.logger.info(
        "%s image dispatch provider=%s channel=%s engine=%s use_jobs=%s mask=%s",
        action,
        provider_name,
        channel,
        engine,
        use_jobs,
        mask is not None,
    )

    if engine == facade._IMAGE_ROUTE_DUAL_RACE and facade._is_byok_provider(provider):
        await facade._emit_image_progress(
            progress_callback,
            "route_diagnostic",
            provider=provider_name,
            route=f"{channel}:{engine}",
            reason="byok_disables_dual_race",
            fallback_route=(f"{channel}:{facade._IMAGE_ROUTE_RESPONSES}"),
            byok=True,
            status="routed",
        )
        engine = facade._IMAGE_ROUTE_RESPONSES

    if mask is not None:
        if action != "edit":
            raise facade.UpstreamError(
                f"mask only supported on edit action (got {action})",
                error_code=facade.EC.INVALID_REQUEST_ERROR.value,
                status_code=400,
            )
        if not images or not any(images):
            raise facade.UpstreamError(
                "mask requires at least one reference image",
                error_code=facade.EC.MISSING_INPUT_IMAGES.value,
                status_code=400,
            )
        if engine != facade._IMAGE_ROUTE_IMAGE2 or use_jobs:
            await facade._emit_image_progress(
                progress_callback,
                "route_diagnostic",
                provider=provider_name,
                route=f"{channel}:{engine}",
                reason="mask_requires_generations_endpoint",
                fallback_route=(
                    "image_jobs:generations" if use_jobs else "image2_edit_direct"
                ),
                byok=facade._is_byok_provider(provider),
                status="routed",
            )
        if use_jobs:
            yield await facade._image_job_with_failover(
                action="edit",
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
                provider_override=provider,
                endpoint_override="generations",
                user_id=user_id,
            )
            return
        for item in await facade._direct_edit_image_with_failover(
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
            provider_override=provider,
        ):
            yield item
        return

    if engine == facade._IMAGE_ROUTE_DUAL_RACE:
        if use_jobs:
            async for item in facade._dual_race_image_jobs_action(
                action=action,
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
                provider_override=provider,
                user_id=user_id,
            ):
                yield item
            return
        async for item in facade._dual_race_image_action(
            action=action,
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
            provider_override=provider,
            user_id=user_id,
            allow_provider_override_race=True,
        ):
            yield item
        return

    if use_jobs:
        yield await facade._image_job_with_failover(
            action=action,
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
            provider_override=provider,
            endpoint_preference=facade._image_jobs_endpoint_for_engine(engine),
            user_id=user_id,
        )
        return

    if engine == facade._IMAGE_ROUTE_IMAGE2:
        try:
            if action == "edit":
                if not images:
                    raise facade.UpstreamError(
                        "edit action requires at least one reference image",
                        error_code=facade.EC.MISSING_INPUT_IMAGES.value,
                        status_code=400,
                    )
                for item in await facade._direct_edit_image_with_failover(
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
                    provider_override=provider,
                ):
                    yield item
                return
            for item in await facade._direct_generate_image_with_failover(
                prompt=prompt,
                size=size,
                n=n,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
                progress_callback=progress_callback,
                provider_override=provider,
            ):
                yield item
            return
        except (asyncio.CancelledError, facade.UpstreamCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            if facade._is_direct_image_result_unknown(exc):
                raise
            facade.logger.warning(
                "%s image2 provider=%s failed; falling back to responses: %r",
                action,
                provider_name,
                exc,
            )
            responses_unavailable = facade._provider_endpoint_unavailable_error(
                provider,
                "responses",
            )
            if responses_unavailable is not None:
                raise facade._merge_image_path_errors(
                    action=action,
                    primary_path="image2",
                    primary_error=exc,
                    fallback_path="responses",
                    fallback_error=responses_unavailable,
                ) from exc
            try:
                yield await facade._race_responses_image(
                    action=action,
                    prompt=prompt,
                    size=size,
                    images=images,
                    quality=quality,
                    output_format=output_format,
                    output_compression=output_compression,
                    background=background,
                    moderation=moderation,
                    model=model,
                    lanes=max(1, int(facade.settings.edit_race_lanes)),
                    progress_callback=progress_callback,
                    provider_override=provider,
                    user_id=user_id,
                )
            except (asyncio.CancelledError, facade.UpstreamCancelled):
                raise
            except Exception as fallback_exc:  # noqa: BLE001
                raise facade._merge_image_path_errors(
                    action=action,
                    primary_path="image2",
                    primary_error=exc,
                    fallback_path="responses",
                    fallback_error=fallback_exc,
                ) from fallback_exc
            return

    lanes = max(1, int(facade.settings.edit_race_lanes))
    try:
        yield await facade._race_responses_image(
            action=action,
            prompt=prompt,
            size=size,
            images=images,
            quality=quality,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            model=model,
            lanes=lanes,
            progress_callback=progress_callback,
            provider_override=provider,
            user_id=user_id,
        )
        return
    except (asyncio.CancelledError, facade.UpstreamCancelled):
        raise
    except Exception as exc:  # noqa: BLE001
        facade.logger.warning(
            "%s responses provider=%s failed; falling back to image2: %r",
            action,
            provider_name,
            exc,
        )
        generations_unavailable = facade._provider_endpoint_unavailable_error(
            provider,
            "generations",
        )
        if generations_unavailable is not None:
            raise facade._merge_image_path_errors(
                action=action,
                primary_path="responses",
                primary_error=exc,
                fallback_path="image2",
                fallback_error=generations_unavailable,
            ) from exc
        if action == "edit":
            if not images:
                raise facade.UpstreamError(
                    "edit action requires at least one reference image",
                    error_code=facade.EC.MISSING_INPUT_IMAGES.value,
                    status_code=400,
                ) from exc
            try:
                for item in await facade._direct_edit_image_with_failover(
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
                    provider_override=provider,
                ):
                    yield item
            except (asyncio.CancelledError, facade.UpstreamCancelled):
                raise
            except Exception as fallback_exc:  # noqa: BLE001
                if facade._is_direct_image_result_unknown(fallback_exc):
                    raise
                raise facade._merge_image_path_errors(
                    action=action,
                    primary_path="responses",
                    primary_error=exc,
                    fallback_path="image2",
                    fallback_error=fallback_exc,
                ) from fallback_exc
            return
        try:
            for item in await facade._direct_generate_image_with_failover(
                prompt=prompt,
                size=size,
                n=n,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
                progress_callback=progress_callback,
                provider_override=provider,
            ):
                yield item
        except (asyncio.CancelledError, facade.UpstreamCancelled):
            raise
        except Exception as fallback_exc:  # noqa: BLE001
            if facade._is_direct_image_result_unknown(fallback_exc):
                raise
            raise facade._merge_image_path_errors(
                action=action,
                primary_path="responses",
                primary_error=exc,
                fallback_path="image2",
                fallback_error=fallback_exc,
            ) from fallback_exc
        return


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
    facade = _facade()
    from ..retry import is_retriable as classify_retriable

    channel = await facade._resolve_image_channel()
    engine = await facade._resolve_image_engine()
    dispatch_endpoint_kind = facade._image_endpoint_kind_for_engine(engine)
    try:
        providers = await facade._image_dispatch_candidates(
            provider_override,
            engine=engine,
        )
    except TypeError as exc:
        if "engine" not in str(exc):
            raise
        providers = await facade._image_dispatch_candidates(provider_override)
        endpoint_kind = facade._image_endpoint_kind_for_engine(engine)
        if endpoint_kind is not None:
            providers = [
                provider
                for provider in providers
                if facade._provider_allows_image_endpoint(
                    provider,
                    endpoint_kind,
                )
            ]
    errors: list[BaseException] = []
    dispatch_owns_inflight = provider_override is None
    pool = await facade.provider_pool.get_pool() if dispatch_owns_inflight else None

    for index, provider in enumerate(providers):
        if dispatch_owns_inflight and index > 0 and pool is not None:
            facade._pool_acquire_inflight(
                pool,
                provider.name,
                dispatch_endpoint_kind,
            )
        try:
            any_yielded = False
            try:
                image_iter = facade._run_image_once_for_provider(
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
            except (asyncio.CancelledError, facade.UpstreamCancelled):
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
                should_continue = facade._should_continue_image_provider_failover(
                    exc,
                    retriable=decision.retriable,
                )
                if (
                    channel == facade._IMAGE_CHANNEL_IMAGE_JOBS_ONLY
                    and not facade._provider_supports_image_jobs(provider)
                ):
                    raise
                if not should_continue:
                    raise
                remaining = len(providers) - index - 1
                if remaining <= 0:
                    continue
                provider_name = getattr(provider, "name", "unknown")
                facade.logger.warning(
                    "%s image dispatch provider_failover: from=%s "
                    "remaining=%d channel=%s engine=%s reason=%s",
                    action,
                    provider_name,
                    remaining,
                    channel,
                    engine,
                    decision.reason,
                )
                await facade._emit_image_progress(
                    progress_callback,
                    "provider_failover",
                    from_provider=provider_name,
                    remaining=remaining,
                    reason=decision.reason,
                    route=f"{channel}:{engine}",
                    **facade._provider_attempt_context(
                        provider,
                        attempt=index + 1,
                        status="failed",
                        reason=decision.reason,
                        exc=exc,
                    ),
                )
        finally:
            if dispatch_owns_inflight and pool is not None:
                facade._pool_release_inflight(
                    pool,
                    provider.name,
                    dispatch_endpoint_kind,
                )

    merged = facade._merge_fallback_errors(
        errors,
        error_code=facade.EC.ALL_ACCOUNTS_FAILED.value,
        message=f"all {len(providers)} image dispatch provider(s) failed",
    )
    merged.payload["provider_errors"] = facade._provider_error_details(
        providers,
        errors,
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
    facade = _facade()
    async for item in facade._dispatch_image(
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
    facade = _facade()
    if not images or not any(images):
        raise facade.UpstreamError(
            "edit action requires at least one reference image",
            error_code=facade.EC.MISSING_INPUT_IMAGES.value,
            status_code=400,
        )
    effective_prompt = (
        facade._wrap_inpaint_prompt(prompt) if mask is not None else prompt
    )
    async for item in facade._dispatch_image(
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


__all__ = [
    "_dispatch_image",
    "_image_dispatch_candidates",
    "_image_endpoint_kind_for_engine",
    "_image_jobs_endpoint_for_engine",
    "_provider_supports_image_jobs",
    "_run_image_once_for_provider",
    "_should_use_image_jobs",
    "edit_image",
    "generate_image",
]
