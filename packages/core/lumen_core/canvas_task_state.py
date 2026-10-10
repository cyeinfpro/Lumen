"""Safe presentation of durable task state; never submits or retries work."""

from __future__ import annotations

from typing import Any
from .task_outcome_knowledge import task_outcome_unknown


def canvas_task_recovery(source: Any | None, *, task_kind: str) -> dict[str, Any]:
    if source is None:
        return {
            "state": "unavailable",
            "can_query": True,
            "can_cancel": False,
            "can_generate_new": False,
            "automatic_resubmit": False,
        }
    status = getattr(source, "status", None)
    stage = getattr(source, "progress_stage", None)
    cancellation = getattr(source, "cancel_requested_at", None) is not None
    unknown = task_outcome_unknown(source, task_kind=task_kind)
    terminal = status in {"succeeded", "failed", "canceled", "expired"}
    if unknown:
        state = "submission_unknown"
    elif cancellation and not terminal:
        state = "cancel_requested"
    elif terminal:
        state = status
    elif stage in {"fetching", "downloading", "saving", "publishing", "finalizing"}:
        state = "saving_artifact"
    elif status in {"submitting", "submitted", "running"}:
        state = "running"
    elif status == "queued":
        state = "queued"
    else:
        state = "reconciling"
    return {
        "state": state,
        "can_query": not terminal or unknown,
        "can_cancel": (
            not terminal
            and not cancellation
            and status
            in {"queued", "running", "submitting", "submitted", "submit_unknown"}
            and task_kind in {"generation", "video_generation"}
        ),
        # A new candidate is a new intent, never recovery of an uncertain submit.
        "can_generate_new": terminal and not unknown,
        "automatic_resubmit": False,
    }
