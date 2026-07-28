from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Generation

from ...provider_runtime.contracts import ResolvedProvider
from .event_delivery import GenerationEventDelivery
from .image_artifact_contracts import PostprocessedGeneratedImage


EventPayload = dict[str, object]
ImageProgressCallback = Callable[[dict[str, object]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class GenerationProviderContext:
    trace_id: str
    retry_attempt: int
    quota_task_id: str
    quota_attempt_epoch: int


@dataclass(frozen=True, slots=True)
class GenerationProviderRequest:
    prompt: str
    size: str
    n: int
    quality: str
    output_format: str | None
    output_compression: int | None
    background: str | None
    moderation: str | None
    model: str | None
    progress_callback: ImageProgressCallback | None
    provider_override: ResolvedProvider | None
    user_id: str | None
    context: GenerationProviderContext


@dataclass(frozen=True, slots=True)
class GenerationProviderEditRequest:
    request: GenerationProviderRequest
    images: Sequence[bytes]
    mask: bytes | None


class GenerationStoreService(Protocol):
    def session(self) -> AbstractAsyncContextManager[AsyncSession]: ...


class GenerationArtifactService(Protocol):
    async def get_bytes(self, key: str) -> bytes: ...

    def public_url(self, key: str) -> str: ...

    async def write_files(
        self,
        files: Sequence[tuple[str, bytes]],
    ) -> list[str]: ...

    async def delete_files(self, keys: Sequence[str]) -> None: ...

    def cleanup_on_error(
        self,
        keys: Sequence[str],
    ) -> AbstractAsyncContextManager[None]: ...


class GenerationBillingService(Protocol):
    async def release(
        self,
        session: AsyncSession,
        generation: Generation,
        *,
        reason: str,
    ) -> None: ...

    async def settle(
        self,
        session: AsyncSession,
        generation: Generation,
        *,
        width: int,
        height: int,
        image_count: int = 1,
    ) -> None: ...

    async def flush_after_commit(self, session: AsyncSession) -> None: ...

    async def settle_unknown_upstream(
        self,
        session: AsyncSession,
        generation: Generation,
        *,
        reason: str,
        knowledge: str,
    ) -> None: ...


class GenerationEventService(Protocol):
    async def publish(
        self,
        redis: object,
        user_id: str,
        channel: str,
        event_name: str,
        data: EventPayload,
    ) -> None: ...

    async def deliver(
        self,
        redis: object,
        delivery: GenerationEventDelivery,
    ) -> None: ...

    async def deliver_many(
        self,
        redis: object,
        deliveries: list[GenerationEventDelivery],
    ) -> None: ...


class GenerationProviderService(Protocol):
    async def resolve_primary_route(self) -> str: ...

    def endpoint_kind_for_engine(self, engine: str) -> str | None: ...

    async def load_reference_images(
        self,
        session: AsyncSession,
        image_ids: list[str],
    ) -> list[tuple[str, bytes]]: ...

    async def load_mask_image(
        self,
        session: AsyncSession,
        mask_image_id: str,
    ) -> bytes: ...

    async def postprocess(
        self,
        raw_image: bytes,
        *,
        prompt: str,
        transparent_requested: bool,
        mode: str | None = None,
    ) -> PostprocessedGeneratedImage: ...

    def resize_mask_to_reference(
        self,
        mask_bytes: bytes,
        reference_bytes: bytes,
    ) -> bytes: ...

    def reference_pixel_size(
        self,
        reference_bytes: bytes,
    ) -> tuple[int, int] | None: ...

    def inpaint_size_from_reference(
        self,
        reference_width: int,
        reference_height: int,
    ) -> str | None: ...

    def generate(
        self,
        request: GenerationProviderRequest,
    ) -> AsyncIterator[tuple[str, str | None]]: ...

    def edit(
        self,
        request: GenerationProviderEditRequest,
    ) -> AsyncIterator[tuple[str, str | None]]: ...


class GenerationQueueService(Protocol):
    @property
    def provider_cooldowns(self) -> dict[str, float]: ...

    @property
    def expose_provider_diagnostics(self) -> bool: ...

    def configured_capacity(self) -> int: ...

    async def resolve_capacity(self) -> int: ...

    def resource_budgets(self) -> tuple[int, int, int]: ...

    async def select_providers(
        self,
        *,
        task_id: str,
        endpoint_kind: str | None,
        requires_mask: bool,
        queue_lane: str | None,
        size_bucket: str | None,
        cost_class: str | None,
    ) -> list[Any]: ...


class GenerationLeaseService(Protocol):
    async def is_cancelled(self, redis: object, task_id: str) -> bool: ...

    async def acquire(
        self,
        redis: object,
        task_id: str,
        worker_token: str,
    ) -> None: ...

    async def release(
        self,
        redis: object,
        task_id: str,
        worker_token: str,
    ) -> None: ...

    async def renew(
        self,
        redis: object,
        task_id: str,
        worker_token: str,
        lease_lost: object | None = None,
        *,
        extra_lease_keys: list[str] | None = None,
        image_provider_name: str | None = None,
    ) -> None: ...

    async def cancel_renewer(self, renewer: object | None) -> None: ...


class GenerationCredentialService(Protocol):
    async def resolve(
        self,
        session: AsyncSession,
        credential_id: str,
    ) -> ResolvedProvider: ...

    async def record_runtime_error(
        self,
        credential_id: str | None,
        exc: BaseException,
    ) -> None: ...

    def classify_error(self, exc: BaseException) -> tuple[bool, str | None]: ...

    def error_message(self, error_code: str) -> str: ...

    def generation_error_code(self, error_code: str) -> str: ...


class GenerationWorkflowService(Protocol):
    async def record_model_library_generate_image(
        self,
        *,
        session: AsyncSession,
        user_id: str,
        generation: Generation,
        image_id: str,
    ) -> None: ...

    async def record_poster_style_library_generate_image(
        self,
        *,
        session: AsyncSession,
        user_id: str,
        generation: Generation,
        image_id: str,
    ) -> None: ...

    async def record_model_library_candidate_image(
        self,
        *,
        session: AsyncSession,
        user_id: str,
        parent_upstream_request: Mapping[str, object],
        bonus_image_id: str,
    ) -> None: ...

    async def record_poster_workflow_image(
        self,
        *,
        session: AsyncSession,
        user_id: str,
        generation: Generation,
        image_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RunGenerationDeps:
    store: GenerationStoreService
    artifacts: GenerationArtifactService
    billing: GenerationBillingService
    events: GenerationEventService
    provider: GenerationProviderService
    queue: GenerationQueueService
    lease: GenerationLeaseService
    credentials: GenerationCredentialService
    workflows: GenerationWorkflowService
