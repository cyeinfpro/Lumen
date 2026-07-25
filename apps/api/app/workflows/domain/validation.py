"""Policy validation values."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    field: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.issues

    @classmethod
    def valid(cls) -> ValidationResult:
        return cls()

    @classmethod
    def invalid(cls, *issues: ValidationIssue) -> ValidationResult:
        return cls(tuple(issues))


__all__ = ["ValidationIssue", "ValidationResult"]
