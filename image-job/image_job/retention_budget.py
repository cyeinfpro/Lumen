"""Shared entry and wall-clock budget for retention filesystem work."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class TraversalBudget:
    remaining_entries: int
    deadline: float
    monotonic: Callable[[], float]
    exhausted: bool = False
    durability_failed: bool = False

    def available(self) -> bool:
        if self.remaining_entries <= 0 or self.monotonic() >= self.deadline:
            self.exhausted = True
            return False
        return True

    def consume(self) -> bool:
        if not self.available():
            return False
        self.remaining_entries -= 1
        return True


def new_traversal_budget(
    max_entries: int,
    *,
    time_budget_s: float,
    monotonic: Callable[[], float],
) -> TraversalBudget:
    started = monotonic()
    return TraversalBudget(
        remaining_entries=max_entries,
        deadline=started + time_budget_s,
        monotonic=monotonic,
    )
