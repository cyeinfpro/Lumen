"""Durable billing obligations for successful dual-race bonus lanes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select

from lumen_core import billing as billing_core
from lumen_core.constants import GenerationStage, GenerationStatus
from lumen_core.model_base import new_uuid7
from lumen_core.model_entities.tasks import Generation
from lumen_core.upstream_billing import (
    has_proven_no_cost_dispatch,
    has_proven_undelivered_dispatch,
    has_upstream_dispatch_receipt,
    has_upstream_response_receipt,
)

from ...artifact_commit import (
    ArtifactAdoption,
    ArtifactCommitOutcomeUnknown,
    commit_error_or_default,
    commit_with_adoption_probe,
)
from .active_user_fence import lock_active_generation_user
from .errors import StaleGenerationAttempt, TaskCancelled
from .execution_boundary import dual_race_bonus_execution_from_request


logger = logging.getLogger(__name__)

BONUS_BILLING_OBLIGATION_KEY = "bonus_billing_obligation"
BONUS_BILLING_WIDTH_KEY = "bonus_billing_width"
BONUS_BILLING_HEIGHT_KEY = "bonus_billing_height"
BONUS_ARTIFACT_STATE_KEY = "bonus_artifact_state"
BONUS_ARTIFACT_PENDING = "pending"
BONUS_ARTIFACT_COMMITTED = "committed"
BONUS_BILLING_EVIDENCE_KEY = "dual_race_bonus_execution"
BILLING_ADMISSION_BILLABLE_KEY = "billing_admission_billable"
BILLING_ADMISSION_SOURCE_KEY = "billing_admission_source"
BILLING_ADMISSION_REF_ID_KEY = "billing_admission_ref_id"
BILLING_ADMISSION_FREE_SOURCE = "no_billable_admission_evidence"


class _ActiveUserLocker(Protocol):
    async def __call__(self, session: Any, *, user_id: str) -> bool: ...


class _HeldAmountLookup(Protocol):
    async def __call__(
        self,
        session: Any,
        user_id: str,
        ref_type: str,
        ref_id: str,
    ) -> int: ...


def _execution_epoch(state: Any) -> int:
    return max(
        0,
        int(getattr(getattr(state, "generation", None), "execution_epoch", 0) or 0),
    )


def _stored_int(value: Any, *, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def bonus_idempotency_key(parent_idempotency_key: str, suffix: str = ":b") -> str:
    normalized_suffix = suffix or ":b"
    prefix_limit = max(1, 64 - len(normalized_suffix))
    return f"{parent_idempotency_key[:prefix_limit]}{normalized_suffix}"


def dual_race_bonus_idempotency_suffix(
    execution_epoch: int,
    attempt: int,
) -> str:
    return f":b:e{max(0, int(execution_epoch))}:a{max(1, int(attempt))}"


def _parent_request(state: Any) -> dict[str, Any]:
    if isinstance(getattr(state, "parent_upstream_request_for_bonus", None), dict):
        return dict(state.parent_upstream_request_for_bonus)
    if isinstance(getattr(state, "gen_upstream_request_snapshot", None), dict):
        return dict(state.gen_upstream_request_snapshot)
    return {}


def _request_has_pricing_admission(request: dict[str, Any]) -> bool:
    return bool(
        isinstance(request.get("billing_pricing_snapshot"), dict)
        or request.get("billing_rate_multiplier_x10000") is not None
    )


def _request_has_billable_receipt(task: Any) -> bool:
    return bool(
        has_upstream_response_receipt(task)
        or (
            has_upstream_dispatch_receipt(task)
            and not has_proven_undelivered_dispatch(task)
            and not has_proven_no_cost_dispatch(task)
        )
    )


async def _held_amount_for_parent(
    session: Any,
    user_id: str,
    ref_type: str,
    ref_id: str,
) -> int:
    return await billing_core._held_amount_for_ref(  # noqa: SLF001
        session,
        user_id,
        ref_type,
        ref_id,
    )


def _store_billing_admission(
    state: Any,
    parent: Any,
    *,
    billable: bool,
    source: str,
    billing_ref_id: str,
    request: dict[str, Any],
) -> None:
    request[BILLING_ADMISSION_BILLABLE_KEY] = bool(billable)
    request[BILLING_ADMISSION_SOURCE_KEY] = str(source)
    request[BILLING_ADMISSION_REF_ID_KEY] = str(billing_ref_id)
    request["billing_free"] = not billable
    request["billing_label"] = "billable" if billable else "free"
    if billable:
        request.pop("billing_exempt_reason", None)
    parent.upstream_request = request
    state.billing_admission_billable = bool(billable)
    state.billing_admission_source = str(source)
    state.gen_upstream_request_snapshot = dict(request)


def _stored_admission_matches_ref(
    request: dict[str, Any],
    parent: Any,
    *,
    billing_ref_id: str,
) -> bool:
    stored_ref_id = request.get(BILLING_ADMISSION_REF_ID_KEY)
    if stored_ref_id is None:
        return billing_ref_id == str(getattr(parent, "id", ""))
    return str(stored_ref_id) == billing_ref_id


async def capture_parent_billing_admission(
    session: Any,
    parent: Any,
    state: Any,
    *,
    held_amount_for_ref: _HeldAmountLookup = _held_amount_for_parent,
) -> bool:
    """Freeze the parent's billable admission before bonus costs are created."""

    parent_request = getattr(parent, "upstream_request", None)
    request = (
        dict(parent_request)
        if isinstance(parent_request, dict)
        else _parent_request(state)
    )
    billing_ref_id = billing_core.generation_billing_ref_id(parent)
    stored = request.get(BILLING_ADMISSION_BILLABLE_KEY)
    if isinstance(stored, bool) and _stored_admission_matches_ref(
        request,
        parent,
        billing_ref_id=billing_ref_id,
    ):
        source = str(request.get(BILLING_ADMISSION_SOURCE_KEY) or "persisted_admission")
        _store_billing_admission(
            state,
            parent,
            billable=stored,
            source=source,
            billing_ref_id=billing_ref_id,
            request=request,
        )
        return stored

    if _request_has_pricing_admission(request):
        billable, source = True, "pricing_snapshot"
    elif _request_has_billable_receipt(parent):
        billable, source = True, "billable_upstream_receipt"
    else:
        held = await held_amount_for_ref(
            session,
            str(getattr(parent, "user_id", getattr(state, "user_id", ""))),
            "generation",
            billing_ref_id,
        )
        billable = int(held or 0) > 0
        source = "wallet_hold" if billable else BILLING_ADMISSION_FREE_SOURCE

    _store_billing_admission(
        state,
        parent,
        billable=billable,
        source=source,
        billing_ref_id=billing_ref_id,
        request=request,
    )
    return billable


def billing_admission_billable(state: Any) -> bool | None:
    stored = getattr(state, "billing_admission_billable", None)
    if isinstance(stored, bool):
        return stored
    request = _parent_request(state)
    stored = request.get(BILLING_ADMISSION_BILLABLE_KEY)
    return stored if isinstance(stored, bool) else None


def billing_obligation_metadata(
    state: Any,
    *,
    policy: str,
    is_dual_race_bonus: bool = False,
) -> dict[str, Any]:
    billable = billing_admission_billable(state)
    if billable is None:
        request = _parent_request(state)
        if request.get("billing_free") is True:
            billable = False
        else:
            # Compatibility for callers outside the runner admission path.
            billable = True
    metadata: dict[str, Any] = {
        "billing_free": not billable,
        "billing_label": "billable" if billable else "free",
        "billing_policy": policy,
    }
    if is_dual_race_bonus:
        metadata["is_dual_race_bonus"] = True
    if not billable:
        metadata["billing_exempt_reason"] = "parent_billing_admission_free"
    return metadata


def apply_billing_admission_to_request(
    request: dict[str, Any],
    state: Any,
) -> None:
    billable = billing_admission_billable(state)
    if billable is None:
        return
    request[BILLING_ADMISSION_BILLABLE_KEY] = billable
    request["billing_free"] = not billable
    request["billing_label"] = "billable" if billable else "free"
    if billable:
        request.pop("billing_exempt_reason", None)
    source = getattr(state, "billing_admission_source", None)
    if isinstance(source, str) and source:
        request[BILLING_ADMISSION_SOURCE_KEY] = source
    admission_ref_id = _parent_request(state).get(BILLING_ADMISSION_REF_ID_KEY)
    if isinstance(admission_ref_id, str) and admission_ref_id:
        request[BILLING_ADMISSION_REF_ID_KEY] = admission_ref_id


def _resolved_dimensions(state: Any, event: dict[str, Any]) -> tuple[int, int]:
    raw = event.get("size")
    if not isinstance(raw, str) or "x" not in raw:
        raw = str(getattr(getattr(state, "resolved", None), "size", "") or "")
    width_raw, separator, height_raw = raw.lower().partition("x")
    if not separator:
        raise ValueError("dual-race bonus obligation requires a resolved size")
    try:
        width = int(width_raw)
        height = int(height_raw)
    except ValueError as exc:
        raise ValueError("dual-race bonus obligation size is invalid") from exc
    if width <= 0 or height <= 0:
        raise ValueError("dual-race bonus obligation dimensions must be positive")
    return width, height


def _obligation_request(
    state: Any,
    event: dict[str, Any],
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    request = _parent_request(state)
    request.update(getattr(state, "image_request_options", {}) or {})
    request.update(
        {
            **billing_obligation_metadata(
                state,
                policy="dual_race_loser_settled_separately",
                is_dual_race_bonus=True,
            ),
            BONUS_BILLING_OBLIGATION_KEY: True,
            BONUS_BILLING_WIDTH_KEY: width,
            BONUS_BILLING_HEIGHT_KEY: height,
            BONUS_ARTIFACT_STATE_KEY: BONUS_ARTIFACT_PENDING,
            "parent_generation_id": str(state.task_id),
            "parent_execution_epoch": _execution_epoch(state),
            "parent_attempt": int(state.attempt),
            "dual_race_bonus_lane": str(event.get("lane") or ""),
            "dual_race_name": str(event.get("race_name") or ""),
            "dual_race_bonus_artifact_ready": event.get("artifact_ready") is not False,
            "dual_race_bonus_obligation_reason": str(
                event.get("obligation_reason") or "loser_completed"
            ),
        }
    )
    execution = event.get("execution")
    if isinstance(execution, dict):
        request[BONUS_BILLING_EVIDENCE_KEY] = dict(execution)
    return request


def _obligation_matches(
    generation: Any,
    state: Any,
    *,
    idempotency_key: str,
) -> bool:
    request = (
        generation.upstream_request
        if isinstance(getattr(generation, "upstream_request", None), dict)
        else {}
    )
    return bool(
        getattr(generation, "user_id", None) == state.user_id
        and getattr(generation, "idempotency_key", None) == idempotency_key
        and request.get(BONUS_BILLING_OBLIGATION_KEY) is True
        and request.get("parent_generation_id") == str(state.task_id)
        and _stored_int(request.get("parent_execution_epoch"))
        == _execution_epoch(state)
        and _stored_int(request.get("parent_attempt")) == int(state.attempt)
    )


async def _find_obligation(
    session: Any,
    state: Any,
    *,
    idempotency_key: str,
    lock: bool,
) -> Any | None:
    statement = select(Generation).where(
        Generation.user_id == state.user_id,
        Generation.idempotency_key == idempotency_key,
    )
    if lock:
        statement = statement.with_for_update()
    return (await session.execute(statement)).scalar_one_or_none()


async def record_dual_race_bonus_obligation(
    state: Any,
    event: dict[str, Any],
    *,
    lock_active_user: _ActiveUserLocker = lock_active_generation_user,
) -> str:
    width, height = _resolved_dimensions(state, event)
    parent_idempotency_key = str(state.gen_idempotency_key or "")
    if not parent_idempotency_key:
        raise ValueError("dual-race bonus obligation requires an idempotency key")
    key = bonus_idempotency_key(
        parent_idempotency_key,
        dual_race_bonus_idempotency_suffix(
            _execution_epoch(state),
            int(state.attempt),
        ),
    )
    obligation_id: str | None = None
    async with state.services.store.session() as session:
        if not await lock_active_user(session, user_id=state.user_id):
            raise TaskCancelled("account deleted before bonus billing obligation")
        parent = await session.get(
            Generation,
            state.task_id,
            with_for_update=True,
        )
        if parent is None or getattr(parent, "user_id", None) != state.user_id:
            raise StaleGenerationAttempt(
                f"dual-race bonus parent superseded task={state.task_id}"
            )
        existing = await _find_obligation(
            session,
            state,
            idempotency_key=key,
            lock=True,
        )
        if existing is not None:
            if not _obligation_matches(existing, state, idempotency_key=key):
                raise StaleGenerationAttempt(
                    f"dual-race bonus obligation conflict task={state.task_id}"
                )
            existing_request = (
                existing.upstream_request
                if isinstance(getattr(existing, "upstream_request", None), dict)
                else {}
            )
            existing_billable = existing_request.get("billing_free") is not True
            state.billing_admission_billable = existing_billable
            state.billing_admission_source = "existing_bonus_obligation"
            obligation_id = str(existing.id)
        else:
            await capture_parent_billing_admission(
                session,
                parent,
                state,
            )
            now = datetime.now(timezone.utc)
            bonus = Generation(
                id=new_uuid7(),
                message_id=state.message_id,
                user_id=state.user_id,
                action=str(state.action),
                model=state.gen_model,
                prompt=state.prompt,
                size_requested=state.size_requested or f"{width}x{height}",
                aspect_ratio=state.aspect_ratio,
                input_image_ids=list(state.input_image_ids),
                primary_input_image_id=state.primary_input_image_id,
                upstream_request=_obligation_request(
                    state,
                    event,
                    width=width,
                    height=height,
                ),
                status=GenerationStatus.SUCCEEDED.value,
                progress_stage=GenerationStage.FINALIZING.value,
                attempt=0,
                idempotency_key=key,
                started_at=now,
                finished_at=now,
                upstream_pixels=width * height,
            )
            session.add(bonus)
            adopted_id: str | None = None

            async def _probe() -> ArtifactAdoption:
                nonlocal adopted_id
                async with state.services.store.session() as probe_session:
                    adopted = await _find_obligation(
                        probe_session,
                        state,
                        idempotency_key=key,
                        lock=False,
                    )
                    if adopted is None:
                        return ArtifactAdoption.NOT_ADOPTED
                    if not _obligation_matches(
                        adopted,
                        state,
                        idempotency_key=key,
                    ):
                        return ArtifactAdoption.UNKNOWN
                    adopted_id = str(adopted.id)
                    return ArtifactAdoption.ADOPTED

            commit_result = await commit_with_adoption_probe(
                session,
                probe=_probe,
                logger=logger,
                label=f"dual-race bonus obligation parent={state.task_id}",
            )
            if commit_result.adopted:
                obligation_id = adopted_id or str(bonus.id)
            elif commit_result.outcome is ArtifactAdoption.NOT_ADOPTED:
                raise commit_error_or_default(
                    commit_result,
                    label=f"dual-race bonus obligation parent={state.task_id}",
                )
            else:
                raise ArtifactCommitOutcomeUnknown(
                    "dual-race bonus obligation commit outcome unknown "
                    f"parent={state.task_id}"
                )
    if obligation_id is None:
        raise ArtifactCommitOutcomeUnknown(
            f"dual-race bonus obligation missing after commit parent={state.task_id}"
        )
    state.dual_race_bonus_obligation_id = obligation_id
    return obligation_id


async def ensure_dual_race_bonus_obligation(state: Any) -> str | None:
    if not bool(getattr(state, "is_dual_race", False)):
        return None
    existing_id = getattr(state, "dual_race_bonus_obligation_id", None)
    if existing_id is not None:
        return str(existing_id)
    execution = dual_race_bonus_execution_from_request(
        getattr(state, "gen_upstream_request_snapshot", None),
        winner_endpoint=getattr(state, "actual_upstream_endpoint", None),
    )
    if execution is None:
        return None
    artifact_ready = execution.recovery_outcome.value == "deliver"
    return await record_dual_race_bonus_obligation(
        state,
        {
            "type": "dual_race_bonus_ready",
            "lane": f"image_jobs:{execution.endpoint}",
            "race_name": "image_jobs dual_race recovery",
            "size": getattr(getattr(state, "resolved", None), "size", None),
            "artifact_ready": artifact_ready,
            "obligation_reason": (
                "recovered_loser_result"
                if artifact_ready
                else "recovered_loser_cancel_cost"
            ),
            "execution": execution.to_dict(),
        },
    )


__all__ = [
    "BONUS_ARTIFACT_COMMITTED",
    "BONUS_ARTIFACT_PENDING",
    "BONUS_ARTIFACT_STATE_KEY",
    "BONUS_BILLING_HEIGHT_KEY",
    "BONUS_BILLING_OBLIGATION_KEY",
    "BONUS_BILLING_WIDTH_KEY",
    "BONUS_BILLING_EVIDENCE_KEY",
    "BILLING_ADMISSION_BILLABLE_KEY",
    "BILLING_ADMISSION_FREE_SOURCE",
    "BILLING_ADMISSION_REF_ID_KEY",
    "BILLING_ADMISSION_SOURCE_KEY",
    "apply_billing_admission_to_request",
    "billing_admission_billable",
    "billing_obligation_metadata",
    "bonus_idempotency_key",
    "capture_parent_billing_admission",
    "dual_race_bonus_idempotency_suffix",
    "ensure_dual_race_bonus_obligation",
    "record_dual_race_bonus_obligation",
]
