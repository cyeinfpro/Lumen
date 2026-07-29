"""Concrete adapters for the Generation application service interfaces."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen_core.models import Generation

from ... import billing as worker_billing
from ... import runtime_settings
from ...byok_runtime import (
    byok_error_message,
    byok_error_to_generation_code,
    classify_user_credential_error,
    record_user_credential_runtime_error,
    resolve_user_credential_runtime,
)
from ...config import Settings
from ...provider_runtime.contracts import ResolvedProvider
from ...provider_runtime.upstream_services import ImageUpstreamRuntime
from ...upstream_parts import GeneratedImageResult
from ...storage import LocalStorage
from ...storage_writes import StorageWriteCoordinator
from ...upstream_parts.image_dispatch import edit_image, generate_image
from ...upstream_parts.image_execution import ImageExecutionRequest, ImageRequestContext
from .event_delivery import (
    GenerationEventDelivery,
    deliver_generation_event,
    deliver_generation_events,
    publish_event,
)
from .image_artifact_contracts import PostprocessedGeneratedImage
from .services import (
    EventPayload,
    GenerationProviderEditRequest,
    GenerationProviderRequest,
)


logger = logging.getLogger(__name__)


async def _wait_for_started_task(task: asyncio.Future[object]) -> object:
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


@dataclass(frozen=True, slots=True)
class DefaultGenerationStore:
    session_factory: async_sessionmaker[AsyncSession]

    def session(self) -> AbstractAsyncContextManager[AsyncSession]:
        return self.session_factory()


@dataclass(frozen=True, slots=True)
class DefaultGenerationArtifacts:
    backend: LocalStorage
    coordinator: StorageWriteCoordinator | None = None

    async def get_bytes(self, key: str) -> bytes:
        return await self.backend.aget_bytes(key)

    def public_url(self, key: str) -> str:
        return self.backend.public_url(key)

    async def write_files(
        self,
        files: Sequence[tuple[str, bytes]],
    ) -> list[str]:
        if self.coordinator is not None:
            return await self.coordinator.write_files(files)

        async def write_one(key: str, data: bytes) -> tuple[str, bool]:
            result = await asyncio.to_thread(
                self.backend.put_bytes_result,
                key,
                data,
            )
            return key, bool(result.created)

        started = asyncio.ensure_future(
            asyncio.gather(
                *(write_one(key, data) for key, data in files),
                return_exceptions=True,
            )
        )
        try:
            results = await asyncio.shield(started)
        except BaseException:
            results = await _wait_for_started_task(started)
            created = [
                key
                for result in results
                if not isinstance(result, BaseException)
                for key, was_created in (result,)
                if was_created
            ]
            await self.delete_files(created)
            raise

        created: list[str] = []
        first_error: BaseException | None = None
        for result in results:
            if isinstance(result, BaseException):
                first_error = first_error or result
                continue
            key, was_created = result
            if was_created:
                created.append(key)
        if first_error is not None:
            await self.delete_files(created)
            raise first_error
        return created

    async def delete_files(self, keys: Sequence[str]) -> None:
        unique_keys = list(dict.fromkeys(keys))
        if not unique_keys:
            return
        started = asyncio.ensure_future(
            asyncio.gather(
                *(asyncio.to_thread(self.backend.delete, key) for key in unique_keys),
                return_exceptions=True,
            )
        )
        results = await _wait_for_started_task(started)
        for key, result in zip(unique_keys, results, strict=False):
            if isinstance(result, BaseException):
                logger.warning(
                    "generation storage cleanup failed key=%s err=%s",
                    key,
                    result,
                )

    @asynccontextmanager
    async def cleanup_on_error(
        self,
        keys: Sequence[str],
    ) -> AsyncIterator[None]:
        try:
            yield
        except BaseException:
            await self.delete_files(keys)
            raise


@dataclass(frozen=True, slots=True)
class DefaultGenerationBilling:
    async def release(
        self,
        session: AsyncSession,
        generation: Generation,
        *,
        reason: str,
    ) -> None:
        await worker_billing.release_generation(
            session,
            generation,
            reason=reason,
        )

    async def settle(
        self,
        session: AsyncSession,
        generation: Generation,
        *,
        width: int,
        height: int,
        image_count: int = 1,
    ) -> None:
        await worker_billing.settle_generation(
            session,
            generation,
            width=width,
            height=height,
            image_count=image_count,
        )

    async def flush_after_commit(self, session: AsyncSession) -> None:
        await worker_billing.flush_balance_cache_refreshes(session)

    async def settle_unknown_upstream(
        self,
        session: AsyncSession,
        generation: Generation,
        *,
        reason: str,
        knowledge: str,
    ) -> None:
        await worker_billing.settle_generation_unknown_upstream(
            session,
            generation,
            reason=reason,
            knowledge=knowledge,
        )


@dataclass(frozen=True, slots=True)
class DefaultGenerationEvents:
    async def publish(
        self,
        redis: object,
        user_id: str,
        channel: str,
        event_name: str,
        data: EventPayload,
    ) -> None:
        await publish_event(redis, user_id, channel, event_name, data)

    async def deliver(
        self,
        redis: object,
        delivery: GenerationEventDelivery,
    ) -> None:
        await deliver_generation_event(redis, delivery)

    async def deliver_many(
        self,
        redis: object,
        deliveries: list[GenerationEventDelivery],
    ) -> None:
        await deliver_generation_events(redis, deliveries)


@dataclass(slots=True)
class DefaultGenerationQueue:
    settings: Settings
    provider_cooldowns: dict[str, float] = field(default_factory=dict)
    _provider_selector: object | None = field(default=None, init=False)
    _provider_pool_identity: int | None = field(default=None, init=False)

    @property
    def expose_provider_diagnostics(self) -> bool:
        return bool(self.settings.expose_provider_diagnostics)

    def configured_capacity(self) -> int:
        from .queue import coerce_image_queue_capacity

        return coerce_image_queue_capacity(self.settings.image_generation_concurrency)

    def resource_budgets(self) -> tuple[int, int, int]:
        return (
            max(1, int(self.settings.image_generation_resource_units)),
            max(1, int(self.settings.image_generation_external_lane_units)),
            max(1, int(self.settings.image_generation_user_resource_units)),
        )

    async def resolve_capacity(self) -> int:
        from .queue import (
            IMAGE_GENERATION_CONCURRENCY_SETTING,
            coerce_image_queue_capacity,
        )

        raw = await runtime_settings.resolve(IMAGE_GENERATION_CONCURRENCY_SETTING)
        if raw is None:
            return self.configured_capacity()
        return coerce_image_queue_capacity(raw)

    async def select_providers(
        self,
        *,
        task_id: str,
        endpoint_kind: str | None,
        requires_mask: bool,
        queue_lane: str | None,
        size_bucket: str | None,
        cost_class: str | None,
    ) -> list[object]:
        from ...provider_pool import get_pool
        from .provider_selector import (
            GenerationDispatchTask,
            PoolProviderSelector,
            ProviderConstraints,
        )

        pool = await get_pool()
        pool_identity = id(pool)
        if (
            not isinstance(self._provider_selector, PoolProviderSelector)
            or self._provider_pool_identity != pool_identity
        ):
            self._provider_selector = PoolProviderSelector(pool)
            self._provider_pool_identity = pool_identity
        return await self._provider_selector.select(
            task=GenerationDispatchTask(
                task_id=task_id,
                endpoint_kind=endpoint_kind,
            ),
            constraints=ProviderConstraints(
                requires_mask=requires_mask,
                queue_lane=queue_lane,
                size_bucket=size_bucket,
                cost_class=cost_class,
            ),
        )


@dataclass(frozen=True, slots=True)
class DefaultGenerationCredentials:
    async def resolve(
        self,
        session: AsyncSession,
        credential_id: str,
    ) -> ResolvedProvider:
        return await resolve_user_credential_runtime(session, credential_id)

    async def record_runtime_error(
        self,
        credential_id: str | None,
        exc: BaseException,
    ) -> None:
        await record_user_credential_runtime_error(credential_id, exc)

    def classify_error(self, exc: BaseException) -> tuple[bool, str | None]:
        return classify_user_credential_error(exc)

    def error_message(self, error_code: str) -> str:
        return byok_error_message(error_code)

    def generation_error_code(self, error_code: str) -> str:
        return byok_error_to_generation_code(error_code)


@dataclass(frozen=True, slots=True)
class DefaultGenerationWorkflows:
    async def record_model_library_generate_image(
        self,
        *,
        session: AsyncSession,
        user_id: str,
        generation: Generation,
        image_id: str,
    ) -> None:
        from .workflow_service import record_model_library_generate_image

        await record_model_library_generate_image(
            session=session,
            user_id=user_id,
            generation=generation,
            image_id=image_id,
        )

    async def record_poster_style_library_generate_image(
        self,
        *,
        session: AsyncSession,
        user_id: str,
        generation: Generation,
        image_id: str,
    ) -> None:
        from .workflow_service import record_poster_style_library_generate_image

        await record_poster_style_library_generate_image(
            session=session,
            user_id=user_id,
            generation=generation,
            image_id=image_id,
        )

    async def record_model_library_candidate_image(
        self,
        *,
        session: AsyncSession,
        user_id: str,
        parent_upstream_request: Mapping[str, object],
        bonus_image_id: str,
    ) -> None:
        from .workflow_service import record_model_library_candidate_image

        await record_model_library_candidate_image(
            session=session,
            user_id=user_id,
            parent_upstream_request=dict(parent_upstream_request),
            bonus_image_id=bonus_image_id,
        )

    async def record_poster_workflow_image(
        self,
        *,
        session: AsyncSession,
        user_id: str,
        generation: Generation,
        image_id: str,
    ) -> None:
        from .workflow_service import record_poster_workflow_image

        await record_poster_workflow_image(
            session=session,
            user_id=user_id,
            generation=generation,
            image_id=image_id,
        )


@dataclass(frozen=True, slots=True)
class DefaultGenerationLease:
    async def is_cancelled(self, redis: object, task_id: str) -> bool:
        from .lease import is_cancelled

        return await is_cancelled(redis, task_id)

    async def acquire(
        self,
        redis: object,
        task_id: str,
        worker_token: str,
    ) -> None:
        from .lease import acquire_lease

        await acquire_lease(redis, task_id, worker_token)

    async def release(
        self,
        redis: object,
        task_id: str,
        worker_token: str,
    ) -> None:
        from .lease import release_lease

        await release_lease(redis, task_id, worker_token)

    async def renew(
        self,
        redis: object,
        task_id: str,
        worker_token: str,
        lease_lost: object | None = None,
        *,
        extra_lease_keys: list[str] | None = None,
        image_provider_name: str | None = None,
    ) -> None:
        import asyncio

        from .lease import lease_renewer

        event = lease_lost if isinstance(lease_lost, asyncio.Event) else None
        await lease_renewer(
            redis,
            task_id,
            worker_token,
            event,
            extra_lease_keys=extra_lease_keys,
            image_provider_name=image_provider_name,
        )

    async def cancel_renewer(self, renewer: object | None) -> None:
        import asyncio

        from .lease import cancel_renewer_task

        task = renewer if isinstance(renewer, asyncio.Task) else None
        await cancel_renewer_task(task)


class DefaultGenerationProvider:
    """Upstream and image-processing adapter implemented by composition support."""

    def __init__(
        self,
        postprocess_runtime: object,
        reference_storage: LocalStorage,
        upstream_runtime: ImageUpstreamRuntime,
    ) -> None:
        self._postprocess_runtime = postprocess_runtime
        self._reference_storage = reference_storage
        self._upstream_runtime = upstream_runtime

    async def resolve_primary_route(self) -> str:
        from .composition_support import resolve_image_primary_route

        return await resolve_image_primary_route(self._upstream_runtime)

    def endpoint_kind_for_engine(self, engine: str) -> str | None:
        from ...upstream_parts.image_dispatch import image_endpoint_kind_for_engine

        return image_endpoint_kind_for_engine(
            engine,
            runtime=self._upstream_runtime,
        )

    async def load_reference_images(
        self,
        session: AsyncSession,
        image_ids: list[str],
    ) -> list[tuple[str, bytes]]:
        from .composition_support import load_reference_images

        return await load_reference_images(
            session,
            image_ids,
            storage_backend=self._reference_storage,
        )

    async def load_mask_image(
        self,
        session: AsyncSession,
        mask_image_id: str,
    ) -> bytes:
        from .composition_support import load_mask_image

        return await load_mask_image(
            session,
            mask_image_id,
            storage_backend=self._reference_storage,
        )

    async def postprocess(
        self,
        raw_image: bytes,
        *,
        prompt: str,
        transparent_requested: bool,
        mode: str | None = None,
    ) -> PostprocessedGeneratedImage:
        from .composition_support import postprocess_raw_generated_image
        from .runtime import ImagePostprocessRuntime

        runtime = self._postprocess_runtime
        if not isinstance(runtime, ImagePostprocessRuntime):
            raise TypeError("invalid image postprocess runtime")
        return await postprocess_raw_generated_image(
            raw_image,
            prompt=prompt,
            transparent_requested=transparent_requested,
            mode=mode,
            runtime=runtime,
        )

    def resize_mask_to_reference(
        self,
        mask_bytes: bytes,
        reference_bytes: bytes,
    ) -> bytes:
        from .composition_support import resize_mask_to_reference

        return resize_mask_to_reference(mask_bytes, reference_bytes)

    def reference_pixel_size(
        self,
        reference_bytes: bytes,
    ) -> tuple[int, int] | None:
        from .composition_support import reference_pixel_size

        return reference_pixel_size(reference_bytes)

    def inpaint_size_from_reference(
        self,
        reference_width: int,
        reference_height: int,
    ) -> str | None:
        from .composition_support import inpaint_size_from_reference

        return inpaint_size_from_reference(
            reference_width,
            reference_height,
        )

    def generate(
        self,
        request: GenerationProviderRequest,
    ) -> AsyncIterator[GeneratedImageResult]:
        request_context = ImageRequestContext.create(
            trace_id=request.context.trace_id,
            retry_attempt=request.context.retry_attempt,
            quota_task_id=request.context.quota_task_id,
            quota_attempt_epoch=request.context.quota_attempt_epoch,
            sidecar_execution=request.context.sidecar_execution,
            upstream_runtime=self._upstream_runtime,
        )
        return generate_image(
            ImageExecutionRequest(
                action="generate",
                prompt=request.prompt,
                size=request.size,
                images=None,
                mask=None,
                n=request.n,
                quality=request.quality,
                output_format=request.output_format,
                output_compression=request.output_compression,
                background=request.background,
                moderation=request.moderation,
                model=request.model,
                progress_callback=request.progress_callback,
                provider_override=request.provider_override,
                user_id=request.user_id,
                request_context=request_context,
                upstream_runtime=self._upstream_runtime,
            )
        )

    def edit(
        self,
        request: GenerationProviderEditRequest,
    ) -> AsyncIterator[GeneratedImageResult]:
        common = request.request
        request_context = ImageRequestContext.create(
            trace_id=common.context.trace_id,
            retry_attempt=common.context.retry_attempt,
            quota_task_id=common.context.quota_task_id,
            quota_attempt_epoch=common.context.quota_attempt_epoch,
            sidecar_execution=common.context.sidecar_execution,
            upstream_runtime=self._upstream_runtime,
        )
        return edit_image(
            ImageExecutionRequest(
                action="edit",
                prompt=common.prompt,
                size=common.size,
                images=list(request.images),
                mask=request.mask,
                n=common.n,
                quality=common.quality,
                output_format=common.output_format,
                output_compression=common.output_compression,
                background=common.background,
                moderation=common.moderation,
                model=common.model,
                progress_callback=common.progress_callback,
                provider_override=common.provider_override,
                user_id=common.user_id,
                request_context=request_context,
                upstream_runtime=self._upstream_runtime,
            )
        )
