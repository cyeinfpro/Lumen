"""Shared runtime bookkeeping for remote library synchronization."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, TypeVar

from ..application.errors import WorkflowRequestError


SyncResponseT = TypeVar("SyncResponseT")


def is_request_error(exc: BaseException) -> bool:
    return isinstance(exc, WorkflowRequestError) or (
        isinstance(getattr(exc, "status_code", None), int)
        and isinstance(getattr(exc, "detail", None), dict)
    )


@dataclass(slots=True)
class LibrarySyncOperation:
    """Shared lease, budget, counters, and error bookkeeping."""

    lease_token: str
    renew_lease: Callable[[str], Awaitable[bool]]
    lease_lost_error: type[Exception]
    lease_lost_message: str
    lease_renew_seconds: float
    download_limit_for: Callable[[int, int | None], int]
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    downloaded_bytes: int = 0
    _last_lease_renewal: float = field(default_factory=time.monotonic)

    def record_error(self, message: str) -> None:
        if len(self.errors) < 20:
            self.errors.append(message[:300])

    async def heartbeat(self, *, force: bool = False) -> None:
        now_mono = time.monotonic()
        if not force and now_mono - self._last_lease_renewal < self.lease_renew_seconds:
            return
        if not await self.renew_lease(self.lease_token):
            raise self.lease_lost_error(self.lease_lost_message)
        self._last_lease_renewal = time.monotonic()

    def download_limit(self, expected_size: int | None) -> int:
        return self.download_limit_for(self.downloaded_bytes, expected_size)

    def record_download(self, data: bytes) -> str:
        self.downloaded_bytes += len(data)
        return hashlib.sha256(data).hexdigest()

    def result(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }

    def failure_result(self, message: str) -> dict[str, Any]:
        failure_errors = list(self.errors[:19])
        short_message = message[:300]
        if not failure_errors or failure_errors[-1] != short_message:
            failure_errors.append(short_message)
        return {
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": failure_errors,
        }


async def run_library_sync_operation(
    operation: LibrarySyncOperation,
    *,
    build_index: Callable[[LibrarySyncOperation], Awaitable[dict[str, Any]]],
    complete_sync: Callable[
        [str, dict[str, Any], dict[str, Any], datetime],
        Awaitable[None],
    ],
    fail_sync: Callable[..., Awaitable[bool]],
    now: Callable[[], datetime],
    success_response: Callable[[LibrarySyncOperation, datetime], SyncResponseT],
    map_error: Callable[[Exception, str], Exception | None],
) -> SyncResponseT:
    """Execute one library sync and preserve domain-specific error mapping."""
    try:
        index = await build_index(operation)
        await operation.heartbeat(force=True)
        completed_at = now()
        await complete_sync(
            operation.lease_token,
            index,
            operation.result(),
            completed_at,
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        await fail_sync(
            operation.lease_token,
            message=message,
            result=operation.failure_result(message),
        )
        mapped = map_error(exc, message)
        if mapped is None:
            raise
        raise mapped from exc
    return success_response(operation, completed_at)
