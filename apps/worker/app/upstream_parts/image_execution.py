"""Shared request contracts for image dispatch, failover, and races."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from ..provider_runtime.upstream_services import ImageUpstreamRuntime
from .generated_payload import GeneratedImageResult
from .transport import ImageProgressCallback

ImageResult = GeneratedImageResult


@dataclass(slots=True)
class ImageQuotaMemberScope:
    task_id: str
    attempt_epoch: int
    logical_call_index: int = 0

    def next_member(self, provider_name: str, route: str) -> str:
        self.logical_call_index += 1
        return (
            f"{self.task_id}:{self.attempt_epoch}:{self.logical_call_index}:"
            f"{provider_name}:{route}"
        )


@dataclass(frozen=True, slots=True)
class ImageRequestContext:
    trace_id: str
    retry_attempt: int = 1
    quota_scope: ImageQuotaMemberScope | None = None
    upstream_runtime: ImageUpstreamRuntime | None = None

    @classmethod
    def create(
        cls,
        *,
        trace_id: str | None = None,
        retry_attempt: int = 1,
        quota_task_id: str | None = None,
        quota_attempt_epoch: int | None = None,
        upstream_runtime: ImageUpstreamRuntime | None = None,
    ) -> ImageRequestContext:
        normalized_trace_id = (
            trace_id if isinstance(trace_id, str) and trace_id else uuid.uuid4().hex
        )
        normalized_retry_attempt = max(1, int(retry_attempt or 1))
        quota_scope = None
        if quota_task_id is not None:
            quota_scope = ImageQuotaMemberScope(
                task_id=str(quota_task_id),
                attempt_epoch=max(
                    1,
                    int(quota_attempt_epoch or normalized_retry_attempt),
                ),
            )
        return cls(
            trace_id=normalized_trace_id,
            retry_attempt=normalized_retry_attempt,
            quota_scope=quota_scope,
            upstream_runtime=upstream_runtime,
        )

    def with_retry_attempt(self, retry_attempt: int) -> ImageRequestContext:
        return replace(
            self,
            retry_attempt=max(1, int(retry_attempt or 1)),
        )

    def next_quota_member(self, provider_name: str, route: str) -> str:
        if self.quota_scope is not None:
            return self.quota_scope.next_member(provider_name, route)
        return f"{self.trace_id}:1:{provider_name}:{route}"


def ensure_image_request_context(
    request_context: ImageRequestContext | None,
) -> ImageRequestContext:
    return request_context or ImageRequestContext.create()


@dataclass(frozen=True, slots=True)
class ImageExecutionRequest:
    action: str
    prompt: str
    size: str
    images: list[bytes] | None
    mask: bytes | None
    n: int
    quality: str
    output_format: str | None
    output_compression: int | None
    background: str | None
    moderation: str | None
    model: str | None
    progress_callback: ImageProgressCallback | None
    provider_override: Any | None
    user_id: str | None
    request_context: ImageRequestContext = field(
        default_factory=ImageRequestContext.create
    )
    upstream_runtime: ImageUpstreamRuntime | None = None

    def with_progress(
        self,
        progress_callback: ImageProgressCallback | None,
    ) -> ImageExecutionRequest:
        return replace(self, progress_callback=progress_callback)

    def with_mask(self, mask: bytes | None) -> ImageExecutionRequest:
        return replace(self, mask=mask)

    def with_prompt(self, prompt: str) -> ImageExecutionRequest:
        return replace(self, prompt=prompt)

    def with_provider(self, provider: Any) -> ImageExecutionRequest:
        return replace(self, provider_override=provider)

@dataclass(frozen=True)
class ImageProviderRoute:
    channel: str
    engine: str
    use_jobs: bool
    provider_name: str


__all__ = [
    "ImageExecutionRequest",
    "ImageProviderRoute",
    "ImageQuotaMemberScope",
    "ImageRequestContext",
    "ImageResult",
    "ensure_image_request_context",
]
