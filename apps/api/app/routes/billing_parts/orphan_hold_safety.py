"""Fail-closed evidence checks for administrative orphan-hold release."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.agent_dispatch import provider_dispatch_evidence_count
from lumen_core.model_entities.billing_operations import WalletTransaction
from lumen_core.model_entities.agents import AgentRun
from lumen_core.model_entities.tasks import Completion, Generation, VideoGeneration
from lumen_core.upstream_billing import (
    UpstreamCostKnowledge,
    has_proven_undelivered_dispatch,
)

_HOLD_TASKS = MappingProxyType(
    {
        "generation": (
            Generation,
            frozenset({"succeeded", "failed", "canceled"}),
        ),
        "completion": (
            Completion,
            frozenset({"succeeded", "failed", "canceled"}),
        ),
        "video_generation": (
            VideoGeneration,
            frozenset({"succeeded", "failed", "canceled", "expired"}),
        ),
        "agent_run": (
            AgentRun,
            frozenset({"succeeded", "partial", "failed", "cancelled"}),
        ),
    }
)


def _billing_task_id(ref_id: str) -> str:
    task_id, marker, retry_count = ref_id.rpartition(":retry:")
    if marker and task_id and retry_count.isdigit() and int(retry_count) > 0:
        return task_id
    return ref_id


def _current_task_billing_ref(task: Any, *, ref_type: str) -> str:
    if ref_type == "generation":
        return billing_core.generation_billing_ref_id(task)
    if ref_type == "completion":
        return billing_core.completion_billing_ref_id(task)
    return str(task.id)


async def ensure_hold_task_is_terminal(
    db: AsyncSession,
    hold: WalletTransaction,
    *,
    http: Any,
) -> str:
    ref_type = str(hold.ref_type)
    task_config = _HOLD_TASKS.get(ref_type)
    if task_config is None:
        raise http(
            "HOLD_RELEASE_NOT_PROVEN_SAFE",
            f"hold reference type {ref_type} has no upstream cost evidence contract",
            409,
        )
    model, terminal_statuses = task_config
    task = (
        await db.execute(
            select(model)
            .where(
                model.id == _billing_task_id(str(hold.ref_id)),
                model.user_id == hold.user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if task is None:
        raise http(
            "HOLD_RELEASE_NOT_PROVEN_SAFE",
            "hold task is missing, so upstream cost cannot be proven absent",
            409,
        )
    status = str(task.status)
    if status not in terminal_statuses:
        raise http(
            "HOLD_TASK_ACTIVE",
            f"hold task is still active with status {status}",
            409,
        )
    if status == "succeeded":
        raise http(
            "HOLD_RELEASE_NOT_PROVEN_SAFE",
            "succeeded task may have incurred upstream cost",
            409,
        )
    current_ref = _current_task_billing_ref(task, ref_type=ref_type)
    if current_ref != str(hold.ref_id):
        raise http(
            "HOLD_RELEASE_EVIDENCE_MISMATCH",
            "hold belongs to a different billing retry than the persisted task evidence",
            409,
        )
    proof = _hold_release_proof(task, ref_type=ref_type)
    if proof is None:
        raise http(
            "HOLD_RELEASE_NOT_PROVEN_SAFE",
            "persisted evidence does not prove upstream cost was absent",
            409,
        )
    return proof


def _hold_release_proof(task: Any, *, ref_type: str) -> str | None:
    proven_absent = UpstreamCostKnowledge.PROVEN_ABSENT.value
    payloads: list[Mapping[str, Any]] = []
    attributes = (
        ("diagnostics",)
        if ref_type == "video_generation"
        else ("dispatch_jsonb", "billing_jsonb")
        if ref_type == "agent_run"
        else ("upstream_request",)
    )
    for attribute in attributes:
        value = getattr(task, attribute, None)
        if isinstance(value, Mapping):
            payloads.append(value)
            for nested_key in ("generation_diagnostics", "billing_evidence"):
                nested = value.get(nested_key)
                if isinstance(nested, Mapping):
                    payloads.append(nested)
    if any(
        str(payload.get("upstream_cost_knowledge") or "") == proven_absent
        for payload in payloads
    ):
        return "upstream_cost_knowledge:proven_absent"
    if ref_type in {"generation", "completion"} and has_proven_undelivered_dispatch(
        task
    ):
        return "upstream_dispatch:proven_undelivered"
    if ref_type == "video_generation" and _video_delivery_proven_absent(task):
        return "submit_delivery_state:proven_absent"
    if ref_type == "agent_run":
        dispatch = (
            task.dispatch_jsonb
            if isinstance(getattr(task, "dispatch_jsonb", None), Mapping)
            else {}
        )
        if provider_dispatch_evidence_count(dict(dispatch)) > 0:
            return None
        if dispatch.get("runtime_delivery") in {
            "claimed",
            "context_ready",
            "proven_absent",
        }:
            return f"runtime_delivery:{dispatch['runtime_delivery']}"
    return None


def _video_delivery_proven_absent(task: Any) -> bool:
    if getattr(task, "provider_task_id", None):
        return False
    diagnostics = (
        task.diagnostics
        if isinstance(getattr(task, "diagnostics", None), Mapping)
        else {}
    )
    if isinstance(diagnostics.get("submit_receipt"), Mapping):
        return False
    states: list[str] = []
    aggregate = diagnostics.get("submit_delivery_state")
    if aggregate in {"proven_absent", "unknown", "confirmed"}:
        states.append(str(aggregate))
    history = diagnostics.get("submit_delivery_history")
    if isinstance(history, list):
        for item in history:
            state = item.get("state") if isinstance(item, Mapping) else None
            if state in {"proven_absent", "unknown", "confirmed"}:
                states.append(str(state))
    if not states:
        return False
    precedence = {"proven_absent": 0, "unknown": 1, "confirmed": 2}
    return max(states, key=precedence.__getitem__) == "proven_absent"
