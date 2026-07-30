"""Model-library job image queries and response item serialization."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from .runtime import ModelLibraryRuntimeAdapter

if TYPE_CHECKING:
    from lumen_core.models import WorkflowStep
    from lumen_core.schemas import ApparelModelLibraryJobItemOut, ImageOut


async def saved_image_id_set(
    db: AsyncSession,
    user_id: str,
    *,
    runtime: ModelLibraryRuntimeAdapter,
) -> dict[str, str]:
    """Return the saved library item id for each owned image id."""
    rows = (
        await db.execute(
            runtime.select(
                runtime.ModelLibraryItem.image_id,
                runtime.ModelLibraryItem.id,
            )
            .where(runtime.ModelLibraryItem.user_id == user_id)
            .order_by(runtime.ModelLibraryItem.created_at.asc())
        )
    ).all()
    out: dict[str, str] = {}
    for image_id, item_id in rows:
        if not image_id or not item_id:
            continue
        out.setdefault(str(image_id), str(item_id))
    return out


async def gather_job_image_outs(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
    runtime: ModelLibraryRuntimeAdapter,
) -> dict[str, ImageOut]:
    if not image_ids:
        return {}
    images = list(
        (
            await db.execute(
                runtime.select(runtime.Image).where(
                    runtime.Image.id.in_(image_ids),
                    runtime.Image.user_id == user_id,
                    runtime.Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return await runtime._image_out_map(db, images)


async def model_library_image_meta_by_id(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
    runtime: ModelLibraryRuntimeAdapter,
) -> dict[str, dict[str, Any]]:
    ids = runtime._dedupe_nonempty(image_ids)
    if not ids:
        return {}
    images = list(
        (
            await db.execute(
                runtime.select(runtime.Image).where(
                    runtime.Image.id.in_(ids),
                    runtime.Image.user_id == user_id,
                    runtime.Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    gen_ids = runtime._dedupe_nonempty(
        image.owner_generation_id or "" for image in images
    )
    generation_req: dict[str, dict[str, Any]] = {}
    if gen_ids:
        generations = list(
            (
                await db.execute(
                    runtime.select(runtime.Generation).where(
                        runtime.Generation.id.in_(gen_ids),
                        runtime.Generation.user_id == user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        generation_req = {
            generation.id: dict(generation.upstream_request or {})
            for generation in generations
            if isinstance(generation.upstream_request, dict)
        }
    out: dict[str, dict[str, Any]] = {}
    for image in images:
        meta: dict[str, Any] = {"mime": image.mime}
        stored = image.metadata_jsonb if isinstance(image.metadata_jsonb, dict) else {}
        parsed = runtime.parse_model_image_metadata(stored.get("model_library"))
        if parsed is not None:
            meta.update(
                {
                    "age_segment": parsed.age_segment,
                    "gender": parsed.gender,
                    "appearance_direction": parsed.appearance_direction,
                    "style_tags": list(parsed.style_tags or []),
                    "prompt_hint": parsed.prompt_hint,
                }
            )
        filename = runtime._clean_optional_text(
            stored.get("suggested_filename"),
            max_len=160,
        )
        if filename:
            meta["download_filename"] = filename
        for key in (
            "is_dual_race_bonus",
            "billing_free",
            "billing_label",
            "billing_exempt_reason",
        ):
            if key in stored:
                meta[key] = stored[key]
        req = generation_req.get(image.owner_generation_id or "", {})
        if req:
            for key in (
                "is_dual_race_bonus",
                "billing_free",
                "billing_label",
                "billing_exempt_reason",
            ):
                if key in req and key not in meta:
                    meta[key] = req[key]
            if not meta.get("age_segment"):
                meta["age_segment"] = runtime._clean_optional_text(
                    req.get("workflow_model_library_age_segment"),
                    max_len=32,
                )
            if not meta.get("gender"):
                meta["gender"] = runtime._clean_optional_text(
                    req.get("workflow_model_library_gender"),
                    max_len=16,
                )
            if not meta.get("appearance_direction"):
                meta["appearance_direction"] = runtime._clean_optional_text(
                    req.get("workflow_model_library_appearance_direction"),
                    max_len=80,
                )
            if not meta.get("style_tags"):
                meta["style_tags"] = runtime._clean_style_tags(
                    req.get("workflow_model_library_style_tags") or []
                )
        out[image.id] = meta
    return out


def job_item_out(
    *,
    image_id: str,
    image_out: ImageOut | None,
    saved_item_id: str | None,
    age_segment: str | None,
    gender: str | None,
    style_tags: list[str],
    appearance_direction: str | None,
    image_meta: dict[str, Any] | None,
    runtime: ModelLibraryRuntimeAdapter,
) -> ApparelModelLibraryJobItemOut:
    resolved = runtime.resolve_model_library_job_item(
        runtime.ModelLibraryJobItemValues(
            image_id=image_id,
            image_url=(
                image_out.url if image_out is not None else runtime._image_url(image_id)
            ),
            display_url=(image_out.display_url if image_out is not None else None),
            thumb_url=(image_out.thumb_url if image_out is not None else None),
            mime=(image_out.mime if image_out is not None else None),
            saved_item_id=saved_item_id,
            age_segment=age_segment,
            gender=gender,
            style_tags=style_tags,
            appearance_direction=appearance_direction,
            image_meta=image_meta or {},
            image_is_dual_race_bonus=bool(
                getattr(image_out, "is_dual_race_bonus", False)
                if image_out is not None
                else False
            ),
            image_billing_free=bool(
                getattr(image_out, "billing_free", False)
                if image_out is not None
                else False
            ),
            image_billing_label=(
                getattr(image_out, "billing_label", None)
                if image_out is not None
                else None
            ),
            image_billing_exempt_reason=(
                getattr(image_out, "billing_exempt_reason", None)
                if image_out is not None
                else None
            ),
        ),
    )
    return runtime.ApparelModelLibraryJobItemOut(
        image_id=resolved.image_id,
        image_url=resolved.image_url,
        display_url=resolved.display_url,
        thumb_url=resolved.thumb_url,
        saved_item_id=resolved.saved_item_id,
        style_tags=list(resolved.style_tags),
        appearance_direction=resolved.appearance_direction,
        gender=resolved.gender,
        download_filename=resolved.download_filename,
        is_dual_race_bonus=resolved.is_dual_race_bonus,
        billing_free=resolved.billing_free,
        billing_label=resolved.billing_label,
        billing_exempt_reason=resolved.billing_exempt_reason,
    )


def extract_bonus_ids(
    step: WorkflowStep | None,
    image_ids: Iterable[str],
    *,
    runtime: ModelLibraryRuntimeAdapter,
) -> list[str]:
    if step is None:
        return []
    output = step.output_json if isinstance(step.output_json, dict) else {}
    return runtime.extract_bonus_image_ids(output, image_ids)


async def workflow_produced_model_image_ids(
    db: AsyncSession,
    *,
    user_id: str,
    steps: list[WorkflowStep],
    runtime: ModelLibraryRuntimeAdapter,
) -> set[str]:
    """Return model images produced by a workflow, including bonus outputs."""
    produced = {
        iid
        for step in steps
        for iid in (step.image_ids or [])
        if isinstance(iid, str) and iid
    }
    for step in steps:
        produced.update(runtime._extract_bonus_ids(step, produced))
    all_task_ids = runtime._dedupe_nonempty(
        task_id for step in steps for task_id in (step.task_ids or [])
    )
    if not all_task_ids:
        return produced
    owned = (
        (
            await db.execute(
                runtime.select(runtime.Image.id).where(
                    runtime.Image.user_id == user_id,
                    runtime.Image.deleted_at.is_(None),
                    runtime.or_(
                        runtime.Image.owner_generation_id.in_(all_task_ids),
                        runtime.Image.owner_generation_id.in_(
                            runtime.select(runtime.Generation.id).where(
                                runtime.Generation.user_id == user_id,
                                runtime.Generation.upstream_request[
                                    "parent_generation_id"
                                ].astext.in_(all_task_ids),
                                runtime.Generation.upstream_request[
                                    "is_dual_race_bonus"
                                ]
                                .as_boolean()
                                .is_(True),
                            )
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    produced.update(iid for iid in owned if isinstance(iid, str) and iid)
    return produced
