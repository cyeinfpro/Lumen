"""Application-owned transaction boundary."""

from __future__ import annotations

from typing import Protocol


class WorkflowTransaction(Protocol):
    async def __aenter__(self) -> WorkflowTransaction: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None: ...

    async def commit(self) -> None: ...


class WorkflowTransactionFactory(Protocol):
    def __call__(self) -> WorkflowTransaction: ...


__all__ = ["WorkflowTransaction", "WorkflowTransactionFactory"]
