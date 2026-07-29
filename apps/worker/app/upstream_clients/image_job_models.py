"""Typed request outcomes for the image-job control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ImageJobCostKnowledge(StrEnum):
    NONE = "none"
    UNKNOWN = "unknown"
    INCURRED = "incurred"


class ImageJobResultState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class ImageJobRecoveryOutcome(StrEnum):
    POLL = "poll"
    DELIVER = "deliver"
    TERMINAL = "terminal"


class ImageJobCancelOutcome(StrEnum):
    CANCELLED_BEFORE_DISPATCH = "cancelled_before_dispatch"
    CANCEL_REQUESTED = "cancel_requested"
    ALREADY_TERMINAL = "already_terminal"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class ImageJobHandle:
    job_id: str
    upstream_api_key: str = field(repr=False, compare=False)
    status_code: int = 202


@dataclass(frozen=True)
class ImageJobStatus:
    payload: dict[str, Any]
    status_code: int


@dataclass(frozen=True)
class UploadedReference:
    url: str


@dataclass(frozen=True, slots=True)
class ImageJobExecutionHandle:
    job_id: str
    provider_id: str
    endpoint: str
    base_url: str
    idempotency_key: str
    result_state: ImageJobResultState = ImageJobResultState.PENDING
    cost_knowledge: ImageJobCostKnowledge = ImageJobCostKnowledge.UNKNOWN
    sidecar_status: str = "accepted"
    result_artifact: dict[str, Any] | None = None
    cancel_outcome: ImageJobCancelOutcome | None = None

    @property
    def recovery_outcome(self) -> ImageJobRecoveryOutcome:
        if (
            self.result_state == ImageJobResultState.SUCCEEDED
            and isinstance(self.result_artifact, dict)
            and isinstance(self.result_artifact.get("url"), str)
            and self.result_artifact["url"]
        ):
            return ImageJobRecoveryOutcome.DELIVER
        if self.result_state == ImageJobResultState.PENDING:
            return ImageJobRecoveryOutcome.POLL
        return ImageJobRecoveryOutcome.TERMINAL

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "provider_id": self.provider_id,
            "endpoint": self.endpoint,
            "base_url": self.base_url,
            "idempotency_key": self.idempotency_key,
            "dispatch_state": "accepted",
            "result_state": self.result_state.value,
            "cost_knowledge": self.cost_knowledge.value,
            "sidecar_status": self.sidecar_status,
            "recovery_outcome": self.recovery_outcome.value,
        }
        if self.result_artifact is not None:
            payload["result_artifact"] = dict(self.result_artifact)
        if self.cancel_outcome is not None:
            payload["cancel_outcome"] = self.cancel_outcome.value
        return payload

    @classmethod
    def from_mapping(cls, value: Any) -> ImageJobExecutionHandle | None:
        if not isinstance(value, dict):
            return None
        required = ("job_id", "provider_id", "endpoint", "base_url", "idempotency_key")
        normalized: dict[str, str] = {}
        for key in required:
            item = value.get(key)
            if not isinstance(item, str) or not item.strip():
                return None
            normalized[key] = item.strip()
        try:
            result_state = ImageJobResultState(
                str(value.get("result_state") or ImageJobResultState.PENDING.value)
            )
            cost_knowledge = ImageJobCostKnowledge(
                str(value.get("cost_knowledge") or ImageJobCostKnowledge.UNKNOWN.value)
            )
        except ValueError:
            return None
        cancel_outcome = None
        raw_cancel_outcome = value.get("cancel_outcome")
        if isinstance(raw_cancel_outcome, str) and raw_cancel_outcome:
            try:
                cancel_outcome = ImageJobCancelOutcome(raw_cancel_outcome)
            except ValueError:
                return None
        artifact = value.get("result_artifact")
        return cls(
            **normalized,
            result_state=result_state,
            cost_knowledge=cost_knowledge,
            sidecar_status=str(value.get("sidecar_status") or "accepted"),
            result_artifact=dict(artifact) if isinstance(artifact, dict) else None,
            cancel_outcome=cancel_outcome,
        )


@dataclass(frozen=True, slots=True)
class ImageJobCancelResult:
    job_id: str
    outcome: ImageJobCancelOutcome
    status: str
    status_code: int | None
    outcome_uncertain: bool


__all__ = [
    "ImageJobCancelOutcome",
    "ImageJobCancelResult",
    "ImageJobCostKnowledge",
    "ImageJobExecutionHandle",
    "ImageJobHandle",
    "ImageJobRecoveryOutcome",
    "ImageJobResultState",
    "ImageJobStatus",
    "UploadedReference",
]
