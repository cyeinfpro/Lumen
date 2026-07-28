"""Typed contracts for completion task orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]


class CompletionPhase(StrEnum):
    CLAIM = "claim"
    PREPARATION = "preparation"
    STREAMING = "streaming"
    SETTLEMENT = "settlement"
    COMPLETE = "complete"


class CompletionOutcome(StrEnum):
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY = "retry"
    CANCELLED = "cancelled"
    LEASE_LOST = "lease_lost"
    SUPERSEDED = "superseded"
    TERMINAL = "terminal"
    NOT_FOUND = "not_found"


class CompletionRedis(Protocol):
    """Opaque queue/cache capability owned by completion services."""


class CompletionExecutionView(Protocol):
    """Mutable execution view consumed by behavior services."""


@dataclass(frozen=True, slots=True)
class CompletionCommand:
    task_id: str
    redis: CompletionRedis
    worker_id: str

    @classmethod
    def from_arq(
        cls,
        ctx: Mapping[str, CompletionRedis | str | None],
        task_id: str,
    ) -> "CompletionCommand":
        redis = ctx.get("redis")
        if redis is None or isinstance(redis, str):
            raise TypeError("ctx['redis'] must provide the completion redis capability")
        worker_id = ctx.get("worker_id") or ctx.get("job_id") or "worker"
        if not isinstance(worker_id, str):
            raise TypeError("worker_id must be a string")
        return cls(task_id=task_id, redis=redis, worker_id=worker_id)


@dataclass(frozen=True, slots=True)
class CompletionResult:
    task_id: str
    phase: CompletionPhase
    outcome: CompletionOutcome
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class ClaimResult:
    claimed: bool
    outcome: CompletionOutcome


@dataclass(frozen=True, slots=True)
class RetryResolution:
    retriable: bool
    reason: str
    error_code: str
    error_message: str
    delay_seconds: float | None


class CompletionRepository(Protocol):
    async def claim(self, execution: CompletionExecutionView) -> ClaimResult: ...

    async def flush_stream(self, execution: CompletionExecutionView) -> None: ...

    async def record_upstream_marker(
        self,
        execution: CompletionExecutionView,
        *,
        response_received: bool,
    ) -> None: ...

    async def queue_retry(self, execution: CompletionExecutionView) -> bool: ...

    async def cleanup(self, execution: CompletionExecutionView) -> None: ...


class CompletionContextBuilder(Protocol):
    async def prepare(self, execution: CompletionExecutionView) -> None: ...


class CompletionToolExecutor(Protocol):
    def initialize(self, execution: CompletionExecutionView) -> None: ...

    async def consume_event(
        self,
        execution: CompletionExecutionView,
        event: JsonObject,
    ) -> bool: ...

    async def finalize(self, execution: CompletionExecutionView) -> None: ...


class CompletionUpstreamClient(Protocol):
    async def consume(self, execution: CompletionExecutionView) -> None: ...


class CompletionBillingService(Protocol):
    async def settle_success(self, execution: CompletionExecutionView) -> None: ...

    async def settle_cancelled(self, execution: CompletionExecutionView) -> None: ...

    async def settle_failure(
        self,
        execution: CompletionExecutionView,
        failure: BaseException,
    ) -> RetryResolution: ...


class CompletionEventSink(Protocol):
    async def publish_started(self, execution: CompletionExecutionView) -> None: ...

    def record_outcome(
        self,
        execution: CompletionExecutionView,
        outcome: CompletionOutcome,
    ) -> None: ...


class CompletionLeaseRetryPolicy(Protocol):
    async def start(self, execution: CompletionExecutionView) -> None: ...

    def is_lease_lost(self, failure: BaseException) -> bool: ...

    def is_cancelled(self, failure: BaseException) -> bool: ...

    def is_superseded(self, failure: BaseException) -> bool: ...

    async def enqueue_retry(
        self,
        execution: CompletionExecutionView,
        resolution: RetryResolution,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class CompletionServices:
    repository: CompletionRepository
    context_builder: CompletionContextBuilder
    tool_executor: CompletionToolExecutor
    upstream_client: CompletionUpstreamClient
    billing: CompletionBillingService
    events: CompletionEventSink
    lease_retry: CompletionLeaseRetryPolicy
