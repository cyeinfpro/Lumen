"""Shared Agent context preparation errors."""

from __future__ import annotations


class AgentContextError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Agent context could not be prepared")


__all__ = ["AgentContextError"]
