from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from lumen_core.capacity_leases import (
    CapacityLeaseLost,
    maintained_capacity_lease,
    race_with_capacity_lease,
)
from lumen_core.storage_capacity import (
    StorageCapacityExceeded,
    StorageCapacityPort,
    StorageCapacityUnavailable,
)

from .storage import LocalStorage, StorageDiskFullError


logger = logging.getLogger(__name__)
DEFAULT_STORAGE_OPERATION_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class StorageWrite:
    key: str
    data: bytes


@dataclass(frozen=True)
class StorageWriteOperation:
    key: str
    size_bytes: int
    write: Callable[[], bool]


async def wait_for_started_task(
    task: asyncio.Future[object],
    *,
    timeout_seconds: float,
) -> object:
    deadline = time.monotonic() + max(0.001, timeout_seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("storage operation confirmation timed out")
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=remaining,
            )
        except asyncio.CancelledError:
            if task.done():
                return task.result()


def _write_outcome(
    results: Sequence[tuple[str, bool] | BaseException],
) -> tuple[list[str], BaseException | None]:
    created_keys: list[str] = []
    first_error: BaseException | None = None
    for result in results:
        if isinstance(result, BaseException):
            first_error = first_error or result
            continue
        key, created = result
        if created:
            created_keys.append(key)
    return created_keys, first_error


class StorageWriteCoordinator:
    def __init__(
        self,
        *,
        storage: LocalStorage,
        capacity: StorageCapacityPort,
        lease_ttl_seconds: float,
        operation_timeout_seconds: float = DEFAULT_STORAGE_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("storage write lease TTL must be positive")
        self.storage = storage
        self.capacity = capacity
        self.lease_ttl_seconds = lease_ttl_seconds
        self.operation_timeout_seconds = max(0.001, operation_timeout_seconds)
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def track_background_task(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.add(task)

        def consume(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            try:
                done.result()
            except BaseException:
                pass

        task.add_done_callback(consume)

    @staticmethod
    def _reservation_bytes(
        operations: Sequence[StorageWriteOperation],
    ) -> int:
        # LocalStorage writes a temporary file before publishing the final path.
        return 2 * sum(max(0, operation.size_bytes) for operation in operations)

    async def _delete_created_task(self, keys: Sequence[str]) -> None:
        unique_keys = list(dict.fromkeys(keys))
        if not unique_keys:
            return
        results = await asyncio.gather(
            *(asyncio.to_thread(self.storage.delete, key) for key in unique_keys),
            return_exceptions=True,
        )
        for key, result in zip(unique_keys, results, strict=False):
            if isinstance(result, BaseException):
                logger.warning(
                    "storage write cleanup failed key=%s err=%s",
                    key,
                    result,
                )

    async def _delete_created(self, keys: Sequence[str]) -> None:
        cleanup = asyncio.create_task(self._delete_created_task(keys))
        try:
            await wait_for_started_task(
                cleanup,
                timeout_seconds=self.operation_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "storage cleanup exceeded %.3fs; continuing in background",
                self.operation_timeout_seconds,
            )
            self.track_background_task(cleanup)

    async def _finish_abandoned_write(
        self,
        started: asyncio.Future[object],
    ) -> None:
        raw_results = await asyncio.shield(started)
        if not isinstance(raw_results, Sequence):
            return
        created_keys, _first_error = _write_outcome(raw_results)
        await self._delete_created(created_keys)

    async def write_files(
        self,
        files: Sequence[tuple[str, bytes]],
    ) -> list[str]:
        writes = tuple(StorageWrite(key=key, data=data) for key, data in files)
        if not writes:
            return []

        def operation(write: StorageWrite) -> StorageWriteOperation:
            def put() -> bool:
                result = self.storage.put_bytes_result(
                    write.key,
                    write.data,
                    max_bytes=len(write.data),
                )
                return bool(result.created)

            return StorageWriteOperation(
                key=write.key,
                size_bytes=len(write.data),
                write=put,
            )

        return await self.write_operations(tuple(operation(write) for write in writes))

    async def write_operations(
        self,
        operations: Sequence[StorageWriteOperation],
    ) -> list[str]:
        pending = tuple(operations)
        if not pending:
            return []
        try:
            lease = await self.capacity.reserve(self._reservation_bytes(pending))
        except (StorageCapacityExceeded, StorageCapacityUnavailable) as exc:
            raise StorageDiskFullError(pending[0].key) from exc

        created_keys: list[str] = []
        async with maintained_capacity_lease(
            lease,
            ttl_seconds=self.lease_ttl_seconds,
        ) as guard:

            async def run_one(
                write_operation: StorageWriteOperation,
            ) -> tuple[str, bool]:
                created = await asyncio.to_thread(
                    write_operation.write,
                )
                return write_operation.key, bool(created)

            started = asyncio.ensure_future(
                asyncio.gather(
                    *(run_one(write_operation) for write_operation in pending),
                    return_exceptions=True,
                )
            )
            try:
                raw_results = await race_with_capacity_lease(
                    asyncio.shield(started),
                    guard,
                )
            except BaseException as operation_error:
                try:
                    raw_results = await wait_for_started_task(
                        started,
                        timeout_seconds=self.operation_timeout_seconds,
                    )
                except TimeoutError:
                    self.track_background_task(
                        asyncio.create_task(self._finish_abandoned_write(started))
                    )
                    raise operation_error
                created_keys, _first_error = _write_outcome(raw_results)
                await self._delete_created(created_keys)
                raise

            created_keys, first_error = _write_outcome(raw_results)
            if first_error is not None:
                await self._delete_created(created_keys)
                raise first_error
        return created_keys

    @asynccontextmanager
    async def reserve_bytes(
        self,
        bytes_required: int,
        *,
        key: str,
    ) -> AsyncIterator[object]:
        try:
            lease = await self.capacity.reserve(max(0, int(bytes_required)))
        except (StorageCapacityExceeded, StorageCapacityUnavailable) as exc:
            raise StorageDiskFullError(key) from exc
        async with maintained_capacity_lease(
            lease,
            ttl_seconds=self.lease_ttl_seconds,
        ) as guard:
            yield guard

    @asynccontextmanager
    async def cleanup_on_error(
        self,
        keys: list[str],
    ) -> AsyncIterator[None]:
        try:
            yield
        except BaseException:
            await self._delete_created(keys)
            raise

    async def delete_files(self, keys: Sequence[str]) -> None:
        await self._delete_created(keys)


__all__ = [
    "CapacityLeaseLost",
    "StorageWrite",
    "StorageWriteCoordinator",
    "StorageWriteOperation",
    "wait_for_started_task",
]
