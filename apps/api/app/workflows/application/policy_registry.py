"""Explicit workflow policy registry."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from ..domain.models import WorkflowKind
from ..domain.policies import WorkflowPolicy
from .errors import WorkflowPolicyNotFoundError


class WorkflowPolicyRegistry:
    def __init__(self, policies: Iterable[WorkflowPolicy]) -> None:
        registered: dict[WorkflowKind, WorkflowPolicy] = {}
        for policy in policies:
            if policy.kind in registered:
                raise ValueError(
                    f"duplicate workflow policy registration: {policy.kind.value}"
                )
            registered[policy.kind] = policy
        self._policies = registered

    def require(self, kind: WorkflowKind) -> WorkflowPolicy:
        try:
            return self._policies[kind]
        except KeyError as exc:
            raise WorkflowPolicyNotFoundError(kind) from exc

    def __iter__(self) -> Iterator[WorkflowPolicy]:
        return iter(self._policies.values())


__all__ = ["WorkflowPolicyRegistry"]
