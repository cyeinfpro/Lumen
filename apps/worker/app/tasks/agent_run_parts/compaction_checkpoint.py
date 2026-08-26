"""Validated durable Runtime checkpoint helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from lumen_core.model_entities import AgentRun

from ...agent_runtime_client import AgentRuntimeEvent


def checkpoint_provider_dispatched(dispatch: dict[str, Any]) -> None:
    dispatch["runtime_delivery"] = "provider_dispatched"
    dispatch["provider_dispatch_count"] = (
        int(dispatch.get("provider_dispatch_count") or 0) + 1
    )


def checkpoint_provider_response(
    dispatch: dict[str, Any],
    event: AgentRuntimeEvent,
) -> None:
    dispatch["runtime_delivery"] = "provider_response"
    statuses = [
        value
        for value in dispatch.get("provider_response_statuses", [])
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if isinstance(event.status, int) and not isinstance(event.status, bool):
        statuses.append(event.status)
    dispatch["provider_response_statuses"] = statuses[-16:]


def build_pi_compaction_checkpoint(
    run: AgentRun,
    event: AgentRuntimeEvent,
) -> dict[str, Any]:
    if (
        event.checkpoint_version not in {1, 2}
        or event.pi_runtime_version is None
        or event.summary is None
        or event.first_kept_message_id is None
        or event.tokens_before is None
        or event.provider_call_count is None
        or event.usage is None
    ):
        raise ValueError("Pi compaction checkpoint is incomplete")
    next_message_id = event.next_message_id or run.user_message_id
    phase = event.phase or "pre_prompt"
    if phase != "pre_prompt":
        raise ValueError("Pi compaction placement is unsupported")
    snapshot = (
        run.request_snapshot_jsonb
        if isinstance(run.request_snapshot_jsonb, dict)
        else {}
    )
    return {
        "schema_version": event.checkpoint_version,
        "pi_runtime_version": event.pi_runtime_version,
        "summary": event.summary,
        "first_kept_message_id": event.first_kept_message_id,
        "next_message_id": next_message_id,
        "tokens_before": event.tokens_before,
        "source_run_id": run.id,
        "source_execution_epoch": run.execution_epoch,
        "source_event_seq": event.seq,
        "reason": phase,
        "session_revision": event.session_revision,
        "placement_contract": (
            "runtime-pre-prompt-only-v1"
            if event.checkpoint_version == 1
            and snapshot.get("runtime_request_version") == 2
            else None
        ),
        "status": "ready",
        "compacted_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = [
    "build_pi_compaction_checkpoint",
    "checkpoint_provider_dispatched",
    "checkpoint_provider_response",
]
