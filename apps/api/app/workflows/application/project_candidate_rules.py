"""Pure state transitions for workflow model-candidate actions."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
import hashlib
import json
from typing import Any

from ..ports.project_candidates import (
    CandidateState,
    WorkflowRunState,
    WorkflowStepState,
)
from .errors import WorkflowRequestError
from .values import dedupe_nonempty


def _request_error(
    *,
    status_code: int,
    code: str,
    message: str,
) -> WorkflowRequestError:
    return WorkflowRequestError(
        status_code=status_code,
        code=code,
        message=message,
    )


def saved_library_item_ids(
    raw_saved_ids: object,
    new_item_id: object,
) -> list[str]:
    existing = (
        [value for value in raw_saved_ids if isinstance(value, str)]
        if isinstance(raw_saved_ids, list)
        else []
    )
    return dedupe_nonempty([*existing, new_item_id])


def accessory_preview_request_key(
    *,
    candidate_id: str,
    accessory_plan: dict[str, Any],
    style_prompt: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "accessory_plan": accessory_plan,
        "style_prompt": style_prompt.strip(),
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def revision_prompt(
    *,
    instruction: str,
    product_analysis: dict[str, Any],
    selected_candidate_id: str,
) -> str:
    must_preserve = product_analysis.get("must_preserve")
    preserve = (
        ", ".join(str(item) for item in must_preserve)
        if isinstance(must_preserve, list)
        else ""
    )
    return (
        "请根据用户要求返修这张服饰电商模特图。"
        "【商品 1:1 还原】衣服以白底产品图为准，不要改款、改色、改廓形、改领口袖型衣长、"
        "改图案/logo、改纽扣拉链口袋缝线。"
        "保持已确认模特的人脸、发型、身材比例和整体身份不变。"
        "需要逐项保留的商品细节："
        f"{preserve or '颜色、版型、领口、袖型、长度、logo/图案、口袋、纽扣、缝线'}。"
        f"返修要求：{instruction}，仅按此改动，不动商品和模特身份。"
        f"参考模特方案：{selected_candidate_id}。"
    )


def ensure_model_candidate_ready(candidate: CandidateState) -> None:
    if candidate.status != "ready" or not candidate.contact_sheet_image_id:
        raise _request_error(
            status_code=409,
            code="candidate_not_ready",
            message="model candidate is not ready to approve",
        )


def approve_model_candidate_state(
    *,
    candidates: Sequence[CandidateState],
    selected_candidate: CandidateState,
    approval_step: WorkflowStepState,
    showcase_step: WorkflowStepState,
    run: WorkflowRunState,
    user_id: str,
    now: datetime,
    adjustments: str,
    accessory_plan: dict[str, Any],
    selected_accessory_image_id: str | None,
) -> None:
    ensure_model_candidate_ready(selected_candidate)
    for candidate in candidates:
        if candidate.id == selected_candidate.id:
            candidate.status = "selected"
            candidate.selected_at = now
            brief = dict(candidate.model_brief_json or {})
            brief["adjustments"] = adjustments
            brief["accessory_plan"] = accessory_plan
            brief["selected_accessory_image_id"] = selected_accessory_image_id
            candidate.model_brief_json = brief
        elif candidate.status != "failed":
            candidate.status = "rejected"

    approval_step.status = "approved"
    approval_step.approved_at = now
    approval_step.approved_by = user_id
    approval_step.input_json = {
        "candidate_id": selected_candidate.id,
        "adjustments": adjustments,
        "accessory_plan": accessory_plan,
        "selected_accessory_image_id": selected_accessory_image_id,
    }
    approval_step.output_json = {
        "selected_candidate_id": selected_candidate.id,
        "contact_sheet_image_id": selected_candidate.contact_sheet_image_id,
        "selected_accessory_image_id": selected_accessory_image_id,
    }
    if showcase_step.status == "waiting_input":
        showcase_step.status = "needs_review"
    run.current_step = "model_approval"
    run.status = "needs_review"


def reopen_model_selection_state(
    *,
    candidates: Iterable[CandidateState],
    approval_step: WorkflowStepState,
    candidate_step: WorkflowStepState,
    showcase_step: WorkflowStepState,
    quality_step: WorkflowStepState,
    delivery_step: WorkflowStepState,
    run: WorkflowRunState,
    accessory_plan: dict[str, Any] | None,
    style_prompt: str,
) -> None:
    for candidate in candidates:
        if candidate.status in {"selected", "rejected"}:
            candidate.status = (
                "ready" if candidate.contact_sheet_image_id else "generating"
            )
            candidate.selected_at = None

    if candidate_step.status != "running":
        candidate_step.status = "needs_review"
    approval_step.status = "needs_review"
    approval_step.approved_at = None
    approval_step.approved_by = None
    approval_step.input_json = {
        **({"accessory_plan": accessory_plan} if accessory_plan else {}),
        **({"style_prompt": style_prompt} if style_prompt else {}),
    }
    approval_step.output_json = {}
    approval_step.task_ids = []
    approval_step.image_ids = []

    for step in (showcase_step, quality_step, delivery_step):
        step.status = "waiting_input"
        step.input_json = {}
        step.output_json = {}
        step.task_ids = []
        step.image_ids = []

    run.current_step = "model_candidates"
    run.status = "needs_review"


def apply_accessory_selection_state(
    *,
    approval_step: WorkflowStepState,
    run: WorkflowRunState,
    selected_accessory_image_id: str | None,
) -> None:
    approval_step.input_json = {
        **(approval_step.input_json or {}),
        "selected_accessory_image_id": selected_accessory_image_id,
    }
    approval_step.output_json = {
        **(approval_step.output_json or {}),
        "selected_accessory_image_id": selected_accessory_image_id,
    }
    run.current_step = "model_approval"
    if run.status not in {"running", "failed"}:
        run.status = "needs_review"


__all__ = [
    "accessory_preview_request_key",
    "apply_accessory_selection_state",
    "approve_model_candidate_state",
    "ensure_model_candidate_ready",
    "reopen_model_selection_state",
    "revision_prompt",
    "saved_library_item_ids",
]
