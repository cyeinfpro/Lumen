"""Infrastructure-neutral apparel workflow rules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
import re
from typing import Any

from ..domain.apparel_library import normalize_age_segment
from ..ports.apparel_workflow import (
    ApparelWorkflowRunState,
    ApparelWorkflowStepState,
    CandidateImageState,
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


def _clean_items(
    values: Iterable[object],
    *,
    max_items: int,
    max_len: int,
) -> list[str]:
    return [value[:max_len] for value in dedupe_nonempty(values)[:max_items]]


def infer_age_segment_from_text(text: str) -> str:
    if "幼儿" in text:
        return "toddler"
    if any(word in text for word in ("儿童", "童装", "小朋友", "孩子")):
        return "child"
    if "青少年" in text:
        return "teen"
    if "青年" in text:
        return "young_adult"
    if "中年" in text or "中老年" in text:
        return "middle_aged"
    if "老年" in text:
        return "senior"
    if "熟龄" in text or "成年" in text:
        return "adult"
    return "user_favorites"


def metadata_model_profile_from_prompt(text: str) -> dict[str, Any]:
    gender = None
    if "女性" in text or "女" in text:
        gender = "female"
    elif "男性" in text or "男" in text:
        gender = "male"
    appearance = None
    for keyword, value in (
        ("欧美", "european"),
        ("亚洲", "asian"),
        ("拉美", "latin"),
        ("中东", "middle_eastern"),
        ("非洲", "african"),
    ):
        if keyword in text:
            appearance = value
            break
    return {
        "age_segment": normalize_age_segment(infer_age_segment_from_text(text)),
        "gender": gender,
        "appearance_direction": appearance,
    }


def infer_age_segment_from_workflow(run: ApparelWorkflowRunState) -> str:
    profile = (run.metadata_jsonb or {}).get("model_profile")
    if isinstance(profile, dict):
        age = normalize_age_segment(profile.get("age_segment"))
        if age != "user_favorites":
            return age
    return infer_age_segment_from_text(run.user_prompt or "")


def primary_candidate_image_id(candidate: CandidateImageState) -> str | None:
    if candidate.contact_sheet_image_id:
        return candidate.contact_sheet_image_id
    raw_ids = (candidate.model_brief_json or {}).get("candidate_image_ids")
    if isinstance(raw_ids, list):
        for image_id in raw_ids:
            if isinstance(image_id, str) and image_id:
                return image_id
    return None


def normalize_accessory_plan(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    strength = str(value.get("strength") or "subtle")
    if strength not in {"subtle", "medium", "strong"}:
        strength = "subtle"
    items = value.get("items")
    return {
        "enabled": bool(value.get("enabled", True)),
        "items": _clean_items(
            items if isinstance(items, list) else (),
            max_items=12,
            max_len=80,
        ),
        "strength": strength,
    }


def accessory_plan_from_product_analysis(
    product_analysis: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw_items = (product_analysis or {}).get("styling_recommendations")
    if isinstance(raw_items, str):
        normalized = raw_items.strip()
        items: Iterable[object] = (
            ()
            if not normalized or normalized.lower() == "unknown"
            else re.split(r"[、,，;\n]+", normalized)
        )
    elif isinstance(raw_items, list):
        items = raw_items
    else:
        items = ()
    return {
        "enabled": True,
        "items": _clean_items(items, max_items=3, max_len=80),
        "strength": "subtle",
    }


def resolve_accessory_plan(
    *,
    requested: object,
    model_settings_output: Mapping[str, Any] | None,
    model_settings_input: Mapping[str, Any] | None,
    product_analysis: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return (
        normalize_accessory_plan(requested)
        or normalize_accessory_plan((model_settings_output or {}).get("accessory_plan"))
        or normalize_accessory_plan((model_settings_input or {}).get("accessory_plan"))
        or accessory_plan_from_product_analysis(product_analysis)
    )


def resolve_style_prompt(
    *,
    requested: str,
    model_settings_output: Mapping[str, Any] | None,
    model_settings_input: Mapping[str, Any] | None,
    fallback: str,
) -> str:
    return (
        requested.strip()
        or str((model_settings_output or {}).get("style_prompt") or "").strip()
        or str((model_settings_input or {}).get("style_prompt") or "").strip()
        or fallback
    )


def merge_product_corrections(
    product_output: Mapping[str, Any] | None,
    corrections: Mapping[str, Any] | None,
    *,
    confirmed_at: datetime,
) -> dict[str, Any]:
    result = dict(product_output or {})
    raw_corrections = dict(corrections) if isinstance(corrections, Mapping) else {}
    for key, value in raw_corrections.items():
        if value is not None:
            result[key] = value
    result["user_corrections"] = raw_corrections
    result["confirmed_at"] = confirmed_at.isoformat()
    return result


def ensure_product_analysis_ready(product_step: ApparelWorkflowStepState) -> None:
    if product_step.status not in {"needs_review", "approved"}:
        raise _request_error(
            status_code=409,
            code="step_not_ready",
            message="product analysis is not ready to approve",
        )


def approve_product_analysis_state(
    *,
    run: ApparelWorkflowRunState,
    product_step: ApparelWorkflowStepState,
    model_settings_step: ApparelWorkflowStepState,
    corrections: Mapping[str, Any] | None,
    user_id: str,
    confirmed_at: datetime,
    approved_at: datetime,
) -> None:
    ensure_product_analysis_ready(product_step)
    product_step.output_json = merge_product_corrections(
        product_step.output_json,
        corrections,
        confirmed_at=confirmed_at,
    )
    product_step.status = "approved"
    product_step.approved_at = approved_at
    product_step.approved_by = user_id
    if model_settings_step.status == "waiting_input":
        model_settings_step.status = "needs_review"
        model_settings_step.input_json = {
            "style_prompt": run.user_prompt,
            "avoid": ["过度网红感", "夸张姿势", "强烈妆容"],
        }
    run.current_step = "model_settings"
    run.status = "needs_review"


__all__ = [
    "accessory_plan_from_product_analysis",
    "approve_product_analysis_state",
    "ensure_product_analysis_ready",
    "infer_age_segment_from_text",
    "infer_age_segment_from_workflow",
    "merge_product_corrections",
    "metadata_model_profile_from_prompt",
    "normalize_accessory_plan",
    "primary_candidate_image_id",
    "resolve_accessory_plan",
    "resolve_style_prompt",
]
