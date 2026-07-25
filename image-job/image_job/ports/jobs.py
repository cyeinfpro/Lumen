"""Job persistence port."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any, Protocol


class JobRepository(Protocol):
    async def initialize(self) -> None: ...

    async def one(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> sqlite3.Row | None: ...

    async def all(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[sqlite3.Row]: ...

    async def execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> int: ...

    async def readiness_probe(self) -> bool: ...
