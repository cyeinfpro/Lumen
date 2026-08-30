"""Provider attempt ownership and health reporting helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..provider_runtime.contracts import ResolvedProvider


def is_text_provider_failure(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPError):
        return True
    if getattr(exc, "status_code", None) is not None:
        return True
    error_code = getattr(exc, "error_code", None)
    if isinstance(error_code, str) and error_code.strip():
        return True
    if isinstance(exc, RuntimeError):
        message = str(exc).lower()
        return (
            message.startswith("ssh proxy ") or "unsupported proxy protocol" in message
        )
    return False


@dataclass
class TextProviderAttempt:
    _pool: Any = field(repr=False)
    provider: ResolvedProvider
    _reported: bool = field(default=False, init=False, repr=False)

    def report_success(self) -> None:
        if self._reported:
            return
        self._pool.report_success(self.provider.name)
        self._reported = True

    def report_failure(self) -> None:
        if self._reported:
            return
        self._pool.report_failure(
            self.provider.name,
            selected_circuit_state=self.provider.text_circuit_state,
            half_open_probe_token=self.provider.half_open_probe_token,
        )
        self._reported = True

    def report_exception(self, exc: BaseException) -> bool:
        if not is_text_provider_failure(exc):
            return False
        self.report_failure()
        return True

    def release(self) -> None:
        if self._reported:
            return
        self._pool.release_text_attempt(self.provider)
        self._reported = True


@dataclass
class AgentProviderAttempt:
    _pool: Any = field(repr=False)
    provider: ResolvedProvider
    model: str
    _reported: bool = field(default=False, init=False, repr=False)

    def report_success(self) -> None:
        if self._reported:
            return
        self._pool.report_agent_success(self.provider, self.model)
        self._reported = True

    def report_failure(self) -> None:
        if self._reported:
            return
        self._pool.report_agent_failure(self.provider, self.model)
        self._reported = True

    def release(self) -> None:
        if self._reported:
            return
        self._pool.release_agent_attempt(self.provider, self.model)
        self._reported = True


class UntrackedProviderAttempt:
    """Compatibility attempt for lightweight pools used by late-bound tests."""

    def report_success(self) -> None:
        return None

    def report_failure(self) -> None:
        return None

    def report_exception(self, exc: BaseException) -> bool:
        return is_text_provider_failure(exc)

    def release(self) -> None:
        return None


@contextmanager
def text_provider_attempt(
    pool: Any,
    provider: Any,
) -> Iterator[TextProviderAttempt | UntrackedProviderAttempt]:
    """Track a real provider-pool attempt while preserving simple test doubles."""
    attempt_factory = getattr(pool, "text_attempt", None)
    if not callable(attempt_factory):
        attempt = UntrackedProviderAttempt()
        try:
            yield attempt
        finally:
            attempt.release()
        return
    with attempt_factory(provider) as attempt:
        yield attempt


@contextmanager
def agent_provider_attempt(
    pool: Any,
    provider: ResolvedProvider,
    model: str,
) -> Iterator[AgentProviderAttempt | UntrackedProviderAttempt]:
    attempt_factory = getattr(pool, "agent_attempt", None)
    if not callable(attempt_factory):
        attempt = UntrackedProviderAttempt()
        try:
            yield attempt
        finally:
            attempt.release()
        return
    with attempt_factory(provider, model) as attempt:
        yield attempt


__all__ = [
    "AgentProviderAttempt",
    "TextProviderAttempt",
    "UntrackedProviderAttempt",
    "agent_provider_attempt",
    "is_text_provider_failure",
    "text_provider_attempt",
]
