"""Temporary adapter from the v1 runtime symbol groups to typed services."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import CompletionServices
from .runtime import CompletionPorts


@dataclass(frozen=True, slots=True)
class LegacyCompletionAdapter:
    ports: CompletionPorts
    services: CompletionServices

