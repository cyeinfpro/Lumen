"""Showcase generation context and durable request payload assembly."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.models import ModelCandidate, User, WorkflowStep
from lumen_core.schemas import AccessoryPlanIn, ShowcaseImagesCreateIn

from ..workflow_domain.apparel_scene_planner import (
    scene_fingerprint as _scene_fingerprint,
)
from ..workflow_domain.showcase_shot_pool import ShotClass, ShotVariant
from .output_sync import sync_workflow_outputs as _sync_workflow_outputs  # noqa: F401
from .serialization import dedupe_nonempty as _dedupe_nonempty  # noqa: F401
from .serialization import http as _http  # noqa: F401
from .showcase_inputs import (
    showcase_reference_image_ids as _showcase_reference_image_ids,
)  # noqa: F401
from .showcase_inputs import showcase_target_image_count as _showcase_target_image_count  # noqa: F401
from .showcase_inputs import (
    validate_accessory_preview_image as _validate_accessory_preview_image,
)  # noqa: F401
from .showcase_orchestration import (
    prepare_showcase_preflight_impl as _prepare_showcase_preflight_impl,
)  # noqa: F401
from .showcase_shots import showcase_pick_shot_variants as _showcase_pick_shot_variants  # noqa: F401
from .workflow_runtime import get_owned_conversation as _get_owned_conversation  # noqa: F401
from .workflow_runtime import get_run as _get_run  # noqa: F401
from .workflow_runtime import (
    infer_age_segment_from_workflow as _infer_age_segment_from_workflow,
)  # noqa: F401
from .workflow_runtime import selected_candidate as _selected_candidate  # noqa: F401
from .workflow_runtime import step as _step  # noqa: F401


def _showcase_request_input_json(
    *,
    body: ShowcaseImagesCreateIn,
    request_id: str,
    shot_picks: list[tuple[ShotClass, ShotVariant]],
    age_segment: str,
    ref_ids: list[str],
    existing_image_ids: list[str],
    preflight_status: str,
    active_task_ids: list[str] | None = None,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scene_cards = list((preflight or {}).get("scene_cards") or [])
    planning = (preflight or {}).get("planning") or {}
    payload: dict[str, Any] = {
        "template": body.template,
        "shot_plan": [shot_class for shot_class, _ in shot_picks],
        "shot_variants": [
            {"shot_class": cls, "label": v["label"], "framing": v["framing"]}
            for cls, v in shot_picks
        ],
        "age_segment": age_segment,
        "aspect_ratio": body.aspect_ratio,
        "final_quality": body.final_quality,
        "output_count": body.output_count,
        "scene_environment": body.scene_environment,
        "scene_strategy": body.scene_strategy,
        "scene_variety": body.scene_variety,
        "scene_planner": body.scene_planner,
        "continuity_anchor": body.continuity_anchor,
        "allow_pet": body.allow_pet,
        "allow_background_people": body.allow_background_people,
        "generation_request_id": request_id,
        "preflight_status": preflight_status,
        "planner_version": "apparel-durable-outbox-v1",
        "active_task_ids": active_task_ids or [],
        "active_output_count": body.output_count,
        "baseline_image_count": len(_dedupe_nonempty(existing_image_ids)),
        "target_image_count": _showcase_target_image_count(
            existing_image_ids=existing_image_ids,
            output_count=body.output_count,
        ),
        "reference_image_ids": ref_ids,
    }
    if preflight:
        payload.update(
            {
                "garment_lock": preflight.get("garment_lock") or {},
                "scene_planning": planning,
                "scene_cards": scene_cards,
                "scene_fingerprints": planning.get("scene_fingerprints")
                or [
                    card.get("fingerprint") or _scene_fingerprint(card)
                    for card in scene_cards
                ],
                "per_image_prompts": preflight.get("per_image_prompts") or [],
                "prompt_reviews": preflight.get("prompt_reviews") or [],
                "preflight_timed_out": bool(preflight.get("preflight_timed_out")),
                "gpt55_reference_image_labels": preflight.get(
                    "gpt55_reference_image_labels"
                )
                or [],
                "gpt55_reference_image_skips": preflight.get(
                    "gpt55_reference_image_skips"
                )
                or [],
            }
        )
    else:
        payload.update(
            {
                "garment_lock": {},
                "scene_planning": {},
                "scene_cards": [],
                "scene_fingerprints": [],
                "per_image_prompts": [],
                "prompt_reviews": [],
                "preflight_timed_out": False,
            }
        )
    return payload


async def _showcase_generation_context(
    *,
    db: AsyncSession,
    user: User,
    workflow_run_id: str,
    body: ShowcaseImagesCreateIn,
) -> dict[str, Any]:
    run = await _get_run(
        db,
        user_id=user.id,
        run_id=workflow_run_id,
        lock=True,
    )
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status != "approved":
        raise _http(
            "product_not_approved",
            "approve product analysis first",
            409,
        )
    approval = await _step(db, run.id, "model_approval")
    if approval.status != "approved":
        raise _http(
            "model_not_approved",
            "approve a model candidate first",
            409,
        )
    candidate = await _selected_candidate(db, run.id)
    if not candidate.contact_sheet_image_id:
        raise _http(
            "missing_model_reference", "selected model has no reference image", 409
        )
    showcase = await _step(db, run.id, "showcase_generation")
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    age_segment = _infer_age_segment_from_workflow(run)
    seed_key = f"{run.id}:{body.template}:{body.output_count}:{showcase.task_ids and len(showcase.task_ids) or 0}"
    shot_picks = _showcase_pick_shot_variants(
        template=body.template,
        age_segment=age_segment,
        output_count=body.output_count,
        seed_key=seed_key,
    )

    accessory_plan = (approval.input_json or {}).get("accessory_plan")
    if not isinstance(accessory_plan, dict):
        accessory_plan = AccessoryPlanIn().model_dump()
    selected_accessory_image_id = (approval.input_json or {}).get(
        "selected_accessory_image_id"
    )
    selected_accessory_ref_id = (
        selected_accessory_image_id
        if isinstance(selected_accessory_image_id, str)
        else None
    )
    if selected_accessory_ref_id:
        selected_accessory_ref_id = await _validate_accessory_preview_image(
            db,
            user_id=user.id,
            run_id=run.id,
            approval_step=approval,
            image_id=selected_accessory_ref_id,
        )
    ref_ids = _showcase_reference_image_ids(
        product_image_ids=run.product_image_ids,
        model_image_id=candidate.contact_sheet_image_id,
        selected_accessory_image_id=selected_accessory_ref_id,
    )
    return {
        "run": run,
        "product_step": product_step,
        "candidate": candidate,
        "showcase": showcase,
        "conv": conv,
        "age_segment": age_segment,
        "shot_picks": shot_picks,
        "approval": approval,
        "accessory_plan": accessory_plan,
        "ref_ids": ref_ids,
    }


async def _prepare_durable_showcase_preflight(
    *,
    db: AsyncSession,
    context: dict[str, Any],
    body: ShowcaseImagesCreateIn,
) -> dict[str, Any]:
    """Build prompts locally so only durable generation work remains after commit."""
    product_step: WorkflowStep = context["product_step"]
    candidate: ModelCandidate = context["candidate"]
    preflight = await _prepare_showcase_preflight_impl(
        db=db,
        product_analysis=product_step.output_json or {},
        selected_candidate=candidate,
        accessory_plan=context["accessory_plan"],
        template=body.template,
        shot_picks=context["shot_picks"],
        age_segment=context["age_segment"],
        final_quality=body.final_quality,
        user_prompt=context["run"].user_prompt,
        aspect_ratio=body.aspect_ratio,
        scene_environment=body.scene_environment,
        scene_strategy=body.scene_strategy,
        scene_variety=body.scene_variety,
        scene_planner="rules_fallback",
        continuity_anchor=body.continuity_anchor,
        allow_pet=body.allow_pet,
        allow_background_people=body.allow_background_people,
    )
    planning = dict(preflight.get("planning") or {})
    planning["requested_planner"] = body.scene_planner
    if body.scene_planner != "rules_fallback":
        planning["fallback_reason"] = "durable_generation_outbox_dispatch"
    preflight["planning"] = planning
    preflight["preflight_timed_out"] = False
    return preflight


# Public workflow contracts.
prepare_durable_showcase_preflight = _prepare_durable_showcase_preflight
showcase_generation_context = _showcase_generation_context
showcase_request_input_json = _showcase_request_input_json
