"""Stable image-job processing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ERROR_CLASS_NETWORK = "network"
ERROR_CLASS_UPSTREAM_4XX = "upstream_4xx"
ERROR_CLASS_UPSTREAM_5XX = "upstream_5xx"
ERROR_CLASS_NO_IMAGE = "no_image"
ERROR_CLASS_IMAGE_SAVE = "image_save"
ERROR_CLASS_INTERNAL = "internal"
ERROR_CLASS_VALIDATION = "validation"

ALLOWED_FIXED_ENDPOINTS = (
    "/v1/images/generations",
    "/v1/images/edits",
    "/v1/responses",
)
ALLOWED_PREFIX_ENDPOINTS = ("/v1beta/models/",)
IMAGE_OUTPUT_FORMATS = ("png", "jpeg", "webp")
DEFAULT_IMAGE_OUTPUT_FORMAT = "jpeg"
DEFAULT_IMAGE_OUTPUT_COMPRESSION = 0


class JobFailure(Exception):
    def __init__(
        self,
        error: str,
        *,
        upstream_status: int | None = None,
        upstream_body: Any | None = None,
        retryable: bool = False,
        retry_requires_idempotency: bool = False,
        outcome_uncertain: bool = False,
        error_class: str = ERROR_CLASS_INTERNAL,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.upstream_status = upstream_status
        self.upstream_body = upstream_body
        self.retryable = retryable
        self.retry_requires_idempotency = retry_requires_idempotency
        self.outcome_uncertain = outcome_uncertain
        self.retry_suppressed = False
        self.error_class = error_class


@dataclass
class ImageCandidateBudget:
    max_count: int
    max_image_bytes: int
    max_total_bytes: int
    count: int = 0
    total_bytes: int = 0

    def next_max_bytes(self) -> int:
        if self.count >= self.max_count:
            raise JobFailure(
                f"上游图片候选数超过限制（max {self.max_count}）",
                error_class=ERROR_CLASS_IMAGE_SAVE,
            )
        remaining = self.max_total_bytes - self.total_bytes
        if remaining <= 0:
            raise JobFailure(
                f"上游图片总字节超过限制（max {self.max_total_bytes}）",
                error_class=ERROR_CLASS_IMAGE_SAVE,
            )
        return min(self.max_image_bytes, remaining)

    def record(self, candidate: Any) -> Any:
        size = len(candidate.data)
        if size > self.max_image_bytes:
            raise JobFailure(
                f"上游单图超过大小限制（max {self.max_image_bytes}）",
                error_class=ERROR_CLASS_IMAGE_SAVE,
            )
        if self.count >= self.max_count:
            raise JobFailure(
                f"上游图片候选数超过限制（max {self.max_count}）",
                error_class=ERROR_CLASS_IMAGE_SAVE,
            )
        if self.total_bytes + size > self.max_total_bytes:
            raise JobFailure(
                f"上游图片总字节超过限制（max {self.max_total_bytes}）",
                error_class=ERROR_CLASS_IMAGE_SAVE,
            )
        self.count += 1
        self.total_bytes += size
        return candidate
