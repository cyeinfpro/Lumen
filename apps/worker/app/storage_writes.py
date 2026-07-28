from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

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


@dataclass(frozen=True)
class StorageWrite:
    key: str
    data: bytes


@dataclass(frozen=True)
class StorageWriteOperation:
    key: str
    size_bytes: int
    write: Callable[[], bool]


async def _wait_for_started_task(task: asyncio.Future[object]) -> object:
    while True:
        try:
            return await asyncio.shield(task)
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
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("storage write lease TTL must be positive")
        self.storage = storage
        self.capacity = capacity
        self.lease_ttl_seconds = lease_ttl_seconds

    @staticmethod
    def _reservation_bytes(
        operations: Sequence[StorageWriteOperation],
    ) -> int:
        # LocalStorage writes a temporary file before publishing the final path.
        return 2 * sum(max(0, operation.size_bytes) for operation in operations)

    async def _delete_created(self, keys: Sequence[str]) -> None:
        unique_keys = list(dict.fromkeys(keys))
        if not unique_keys:
            return
        cleanup = asyncio.ensure_future(
            asyncio.gather(
                *(asyncio.to_thread(self.storage.delete, key) for key in unique_keys),
                return_exceptions=True,
            )
        )
        results = await _wait_for_started_task(cleanup)
        for key, result in zip(unique_keys, results, strict=False):
            if isinstance(result, BaseException):
                logger.warning(
                    "storage write cleanup failed key=%s err=%s",
                    key,
                    result,
                )

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
            except BaseException:
                raw_results = await _wait_for_started_task(started)
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
]
