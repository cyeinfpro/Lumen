"""Single-node Canvas execution submission."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Awaitable

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import (
    CanvasExecutionTask,
    CanvasNodeExecution,
    CanvasNodeSelection,
    CanvasRun,
)
from lumen_core.canvas import (
    canvas_execution_fingerprint,
    canvas_input_hash,
    canvas_node_definition_hash,
)
from lumen_core.canvas_schemas import (
    EXECUTABLE_NODE_TYPES,
    IMAGE_EXECUTABLE_NODE_TYPES,
)
from lumen_core.constants import MAX_MESSAGE_ATTACHMENTS
from lumen_core.models import User, VideoGeneration
from lumen_core.schemas import (
    ImageParamsIn,
    VideoCreateIn,
    VideoReferenceMediaIn,
)

from ..services.task_submission import (
    create_canvas_image_task,
    create_canvas_video_task,
    publish_canvas_image_task,
    publish_canvas_video_task,
)
from ..routes.videos import video_options
from .api_schemas import CanvasExecuteIn
from .core_adapter import stable_hash, validated_graph
from .document_service import get_owned_canvas
from .errors import canvas_http, idempotency_conflict
from .graph_resolution import ResolvedNode, find_node, resolve_node
from .run_event_service import append_run_event
from .version_service import create_version


_PROCESSOR_VERSION = "canvas-api-v1"
_POST_COMMIT_PUBLISH_TIMEOUT_S = 2.0
_ACTIVE_EXECUTION_STATUSES = (
    "pending",
    "ready",
    "queued",
    "running",
    "reconciling",
    "canceling",
)
logger = logging.getLogger(__name__)
_CANVAS_ATTACHMENT_ROLES = frozenset(
    {
        "reference",
        "subject",
        "product",
        "style",
        "edit_target",
        "background",
        "other",
    }
)


async def _await_post_commit_publish(
    label: str,
    awaitable: Awaitable[None],
    *,
    canvas_id: str,
    execution_id: str,
) -> None:
    """Bound best-effort publishing after the durable task commit."""

    try:
        await asyncio.wait_for(
            awaitable,
            timeout=_POST_COMMIT_PUBLISH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "canvas publish timeout label=%s canvas=%s execution=%s timeout_s=%.1f",
            label,
            canvas_id,
            execution_id,
            _POST_COMMIT_PUBLISH_TIMEOUT_S,
        )
    except Exception:
        logger.warning(
            "canvas publish failed label=%s canvas=%s execution=%s",
            label,
            canvas_id,
            execution_id,
            exc_info=True,
        )


def _image_params(config: dict[str, Any]) -> ImageParamsIn:
    try:
        quality = str(config.get("quality") or "").lower()
        size = str(config.get("size") or "").lower()
        resolution = quality if quality in {"1k", "2k", "4k"} else size
        render_quality = str(config.get("render_quality") or "").lower()
        if render_quality not in {"auto", "low", "medium", "high"}:
            render_quality = "medium" if quality == "standard" else "high"
        return ImageParamsIn.model_validate(
            {
                "aspect_ratio": config.get("aspect_ratio") or "1:1",
                "size_mode": config.get("size_mode") or "auto",
                "fixed_size": config.get("fixed_size"),
                "count": int(config.get("count") or 1),
                "quality": (resolution if resolution in {"1k", "2k", "4k"} else "1k"),
                "fast": config.get("fast"),
                "render_quality": render_quality,
                "output_format": config.get("output_format") or "webp",
                "output_compression": config.get("output_compression"),
                "background": config.get("background") or "auto",
                "moderation": config.get("moderation") or "low",
            }
        )
    except (TypeError, ValueError) as exc:
        raise canvas_http(
            "canvas_image_config_invalid",
            "Canvas image node configuration is invalid",
            422,
            reason=str(exc),
        ) from exc


def _require_single_image(
    resolved: ResolvedNode,
    *,
    handle: str,
    node_type: str,
) -> dict[str, Any]:
    values = resolved.images_by_handle.get(handle, [])
    if len(values) != 1:
        raise canvas_http(
            "canvas_input_cardinality_invalid",
            "Canvas image input requires exactly one asset",
            422,
            node_type=node_type,
            target_handle=handle,
            actual=len(values),
        )
    return values[0]


def _image_task_inputs(
    *,
    node_type: str,
    resolved: ResolvedNode,
) -> tuple[list[str], str | None]:
    attachment_ids: list[str]
    mask_image_id: str | None
    if node_type == "image_generate":
        references = resolved.images_by_handle.get("references", [])
        masks = resolved.images_by_handle.get("mask", [])
        if len(masks) > 1:
            raise canvas_http(
                "canvas_mask_invalid",
                "image generation accepts at most one mask",
                422,
            )
        if masks and len(references) != 1:
            raise canvas_http(
                "canvas_mask_invalid",
                "mask requires exactly one reference image",
                422,
            )
        attachment_ids = [item["image_id"] for item in references]
        mask_image_id = masks[0]["image_id"] if masks else None
    else:
        source = _require_single_image(
            resolved,
            handle="source",
            node_type=node_type,
        )
        if node_type == "image_edit":
            references = resolved.images_by_handle.get("references", [])
            attachment_ids = [
                source["image_id"],
                *(item["image_id"] for item in references),
            ]
            mask_image_id = None
        elif node_type == "image_inpaint":
            mask = _require_single_image(
                resolved,
                handle="mask",
                node_type=node_type,
            )
            attachment_ids = [source["image_id"]]
            mask_image_id = mask["image_id"]
        elif node_type == "image_upscale":
            attachment_ids = [source["image_id"]]
            mask_image_id = None
        else:
            raise canvas_http(
                "canvas_node_not_executable",
                "node type cannot be executed as an image task",
                422,
                node_type=node_type,
            )
    if len(attachment_ids) > MAX_MESSAGE_ATTACHMENTS:
        raise canvas_http(
            "canvas_input_cardinality_invalid",
            "Canvas image task exceeds the attachment limit",
            422,
            maximum=MAX_MESSAGE_ATTACHMENTS,
            actual=len(attachment_ids),
        )
    return attachment_ids, mask_image_id


def _video_reference_counts(resolved: ResolvedNode) -> dict[str, int]:
    return {
        "image": len(resolved.images_by_handle.get("reference_images", [])),
        "video": len(resolved.videos_by_handle.get("reference_videos", [])),
    }


def _video_option_supports_reference_media(
    option: Any,
    *,
    action: str,
    counts: dict[str, int],
) -> bool:
    if action != "reference":
        return True
    limits = getattr(option, "reference_media_limits", None)
    if not isinstance(limits, dict):
        return False
    return all(count <= int(limits.get(kind, 0) or 0) for kind, count in counts.items())


def _video_option_supports_duration(
    option: Any,
    *,
    action: str,
    resolution: str,
    duration_s: int,
    fallback_durations: list[int],
) -> bool:
    by_action_resolution = getattr(option, "durations_by_action_resolution", None)
    if isinstance(by_action_resolution, dict):
        action_resolutions = by_action_resolution.get(action)
        if isinstance(action_resolutions, dict):
            values = action_resolutions.get(resolution)
            if isinstance(values, list) and values:
                return duration_s in values
    by_action = getattr(option, "durations_by_action", None)
    if isinstance(by_action, dict):
        values = by_action.get(action)
        if isinstance(values, list) and values:
            return duration_s in values
    values = getattr(option, "durations_s", None)
    if isinstance(values, list) and values:
        return duration_s in values
    return duration_s in fallback_durations


async def _video_body(
    db: AsyncSession,
    *,
    user: User,
    resolved: ResolvedNode,
    idempotency_key: str,
) -> VideoCreateIn:
    config = resolved.node.get("config") or {}
    action = str(config.get("action") or config.get("mode") or "")
    resolution = str(config.get("resolution") or "720p")
    try:
        duration_s = int(config.get("duration_s") or config.get("duration") or 5)
    except (TypeError, ValueError) as exc:
        raise canvas_http(
            "canvas_video_config_invalid",
            "Canvas video node configuration is invalid",
            422,
            reason=str(exc),
        ) from exc
    model = str(config.get("model") or "")
    if not model:
        options = await video_options(user, db)
        reference_counts = _video_reference_counts(resolved)
        compatible = [
            option
            for option in options.models
            if action in option.actions
            and resolution in option.resolutions
            and _video_option_supports_duration(
                option,
                action=action,
                resolution=resolution,
                duration_s=duration_s,
                fallback_durations=options.durations_s,
            )
            and _video_option_supports_reference_media(
                option,
                action=action,
                counts=reference_counts,
            )
        ]
        if not compatible:
            raise canvas_http(
                "canvas_video_model_unavailable",
                "no video model supports the selected mode, resolution, duration, and reference media",
                422,
                action=action,
                resolution=resolution,
                duration_s=duration_s,
                reference_media=reference_counts,
            )
        model = compatible[0].model
    first_frames = resolved.images_by_handle.get("first_frame", [])
    reference_images = resolved.images_by_handle.get("reference_images", [])
    reference_videos = resolved.videos_by_handle.get("reference_videos", [])
    reference_media: list[VideoReferenceMediaIn] = []
    if action == "reference":
        reference_media.extend(
            VideoReferenceMediaIn(kind="image", image_id=item["image_id"])
            for item in reference_images
        )
        reference_media.extend(
            VideoReferenceMediaIn(kind="video", video_id=item["video_id"])
            for item in reference_videos
        )
    try:
        return VideoCreateIn.model_validate(
            {
                "action": action,
                "model": model,
                "prompt": resolved.prompt,
                "input_image_id": (
                    first_frames[0]["image_id"]
                    if action == "i2v" and len(first_frames) == 1
                    else None
                ),
                "reference_media": reference_media,
                "duration_s": duration_s,
                "resolution": resolution,
                "aspect_ratio": config.get("aspect_ratio") or "16:9",
                "generate_audio": bool(config.get("generate_audio", False)),
                "seed": config.get("seed"),
                "watermark": bool(config.get("watermark", False)),
                "idempotency_key": idempotency_key,
            }
        )
    except (TypeError, ValueError) as exc:
        raise canvas_http(
            "canvas_video_config_invalid",
            "Canvas video node configuration is invalid",
            422,
            reason=str(exc),
        ) from exc


async def _selection_fence(
    db: AsyncSession,
    *,
    canvas_id: str,
    node_id: str,
) -> int:
    row = (
        await db.execute(
            select(CanvasNodeSelection)
            .where(
                CanvasNodeSelection.canvas_id == canvas_id,
                CanvasNodeSelection.node_id == node_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = CanvasNodeSelection(
            canvas_id=canvas_id,
            node_id=node_id,
            execution_id=None,
            output_index=0,
            revision=0,
            locked=False,
        )
        db.add(row)
        await db.flush()
    base_revision = int(row.revision)
    return base_revision


async def _idempotent_run(
    db: AsyncSession,
    *,
    user_id: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> tuple[CanvasRun, CanvasNodeExecution] | None:
    run = (
        await db.execute(
            select(CanvasRun).where(
                CanvasRun.user_id == user_id,
                CanvasRun.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    if run.request_fingerprint != request_fingerprint:
        raise idempotency_conflict()
    execution = (
        await db.execute(
            select(CanvasNodeExecution)
            .where(CanvasNodeExecution.run_id == run.id)
            .order_by(CanvasNodeExecution.sequence.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if execution is None:
        raise canvas_http(
            "canvas_submission_incomplete",
            "idempotent Canvas run is missing its execution",
            409,
        )
    return run, execution


def _canvas_attachment_role(handle: str, item: dict[str, Any]) -> str:
    role = item.get("role")
    if handle == "source" and role in {None, "reference"}:
        return "edit_target"
    if role in _CANVAS_ATTACHMENT_ROLES:
        return str(role)
    return "edit_target" if handle == "source" else "reference"


def _execution_metadata(
    *,
    canvas_id: str,
    run_id: str,
    node_id: str,
    execution_id: str,
    resolved: ResolvedNode,
) -> dict[str, Any]:
    handle_priority = {
        "source": 0,
        "first_frame": 0,
        "references": 1,
        "reference_images": 1,
    }
    images = [
        {
            "image_id": item["image_id"],
            "role": _canvas_attachment_role(handle, item),
        }
        for handle, items in sorted(
            resolved.images_by_handle.items(),
            key=lambda pair: (handle_priority.get(pair[0], 2), pair[0]),
        )
        for item in items
        if handle != "mask"
    ]
    return {
        "source": "canvas",
        "action_source": "canvas_node_execute",
        "workflow_type": "infinite_canvas",
        "canvas_id": canvas_id,
        "canvas_run_id": run_id,
        "canvas_node_id": node_id,
        "canvas_execution_id": execution_id,
        "input_images": images,
        "attachment_roles": [dict(item) for item in images],
        "primary_input_image_id": images[0]["image_id"] if images else None,
    }


async def execute_node(
    db: AsyncSession,
    *,
    user: User,
    canvas_id: str,
    node_id: str,
    body: CanvasExecuteIn,
    header_idempotency_key: str | None,
    request: Request,
) -> tuple[CanvasRun, CanvasNodeExecution]:
    if header_idempotency_key != body.idempotency_key:
        raise canvas_http(
            "idempotency_key_mismatch",
            "Idempotency-Key must match idempotency_key",
            422,
        )
    request_fingerprint = stable_hash(
        {
            "canvas_id": canvas_id,
            "node_id": node_id,
            "document_revision": body.document_revision,
            "auto_select_on_success": body.auto_select_on_success,
        }
    )
    replay = await _idempotent_run(
        db,
        user_id=user.id,
        idempotency_key=body.idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    if replay is not None:
        return replay

    canvas = await get_owned_canvas(
        db,
        user_id=user.id,
        canvas_id=canvas_id,
        lock=True,
    )
    replay = await _idempotent_run(
        db,
        user_id=user.id,
        idempotency_key=body.idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    if replay is not None:
        return replay
    active = (
        await db.execute(
            select(CanvasRun, CanvasNodeExecution)
            .join(CanvasNodeExecution, CanvasNodeExecution.run_id == CanvasRun.id)
            .where(
                CanvasNodeExecution.canvas_id == canvas.id,
                CanvasNodeExecution.node_id == node_id,
                CanvasNodeExecution.status.in_(_ACTIVE_EXECUTION_STATUSES),
            )
            .order_by(CanvasNodeExecution.created_at.asc())
            .limit(1)
        )
    ).one_or_none()
    if active is not None:
        active_run, active_execution = active
        raise canvas_http(
            "canvas_execution_active",
            "another execution for this Canvas node is still active",
            409,
            run_id=active_run.id,
            execution_id=active_execution.id,
            status=active_execution.status,
        )
    if int(canvas.revision) != body.document_revision:
        raise canvas_http(
            "canvas_revision_conflict",
            "canvas revision changed before execution",
            409,
            document_revision=body.document_revision,
            current_revision=int(canvas.revision),
            updated_at=canvas.updated_at,
        )
    graph = validated_graph(canvas.graph_jsonb)
    requested_node = find_node(graph, node_id)
    node_type = str(requested_node.get("type") or "")
    if node_type not in EXECUTABLE_NODE_TYPES:
        raise canvas_http(
            "canvas_node_not_executable",
            "node type cannot be executed",
            422,
            node_type=node_type,
        )
    list(
        (
            await db.execute(
                select(CanvasNodeSelection)
                .where(CanvasNodeSelection.canvas_id == canvas.id)
                .with_for_update()
            )
        ).scalars()
    )
    resolved = await resolve_node(
        db,
        user=user,
        canvas_id=canvas.id,
        graph=graph,
        node_id=node_id,
    )
    selection_base_revision = await _selection_fence(
        db,
        canvas_id=canvas.id,
        node_id=node_id,
    )
    version = await create_version(
        db,
        canvas=canvas,
        user_id=user.id,
        kind="run",
        reuse_exact=True,
    )
    now = datetime.now(timezone.utc)
    run = CanvasRun(
        canvas_id=canvas.id,
        version_id=version.id,
        user_id=user.id,
        kind="single",
        status="queued",
        failure_policy="continue_independent",
        run_epoch=0,
        last_event_seq=0,
        target_node_ids=[node_id],
        idempotency_key=body.idempotency_key,
        request_fingerprint=request_fingerprint,
        budget_micro=0,
        reserved_micro=0,
        spent_micro=0,
        estimated_cost_micro=0,
        summary_jsonb={"document_revision": body.document_revision},
        started_at=now,
    )
    db.add(run)
    await db.flush()
    config = dict(resolved.node.get("config") or {})
    definition_hash = canvas_node_definition_hash(resolved.node)
    input_hash = canvas_input_hash(resolved.snapshot)
    execution_fingerprint = canvas_execution_fingerprint(
        definition_hash=definition_hash,
        input_hash=input_hash,
        node_schema_version=int(resolved.node.get("schema_version") or 1),
        effective_model=config.get("model"),
        effective_provider_capability=None,
        processor_version=_PROCESSOR_VERSION,
    )
    submission_key = f"cx:{stable_hash({'key': body.idempotency_key})}"
    execution = CanvasNodeExecution(
        canvas_id=canvas.id,
        run_id=run.id,
        user_id=user.id,
        node_id=node_id,
        node_type=node_type,
        node_schema_version=int(resolved.node.get("schema_version") or 1),
        sequence=0,
        attempt=0,
        attempt_epoch=0,
        status="queued",
        definition_hash=definition_hash,
        input_hash=input_hash,
        execution_fingerprint=execution_fingerprint,
        submission_idempotency_key=submission_key,
        request_fingerprint=request_fingerprint,
        config_snapshot_jsonb={
            **config,
            "_canvas": {
                "auto_select_on_success": body.auto_select_on_success,
                "selection_base_revision": selection_base_revision,
            },
        },
        input_snapshot_jsonb=resolved.snapshot,
        model_snapshot_jsonb={
            "model": config.get("model"),
            "node_type": node_type,
        },
        pricing_snapshot_jsonb={},
        processor_version=_PROCESSOR_VERSION,
        outputs_jsonb=[],
        selection_base_revision=selection_base_revision,
        started_at=now,
    )
    db.add(execution)
    await db.flush()
    await append_run_event(
        db,
        run=run,
        execution=execution,
        event_type="canvas.execution.queued",
        event_key=f"execution:{execution.id}:epoch:0:status:queued",
        payload={
            "execution_id": execution.id,
            "node_id": node_id,
            "status": "queued",
        },
    )
    metadata = _execution_metadata(
        canvas_id=canvas.id,
        run_id=run.id,
        node_id=node_id,
        execution_id=execution.id,
        resolved=resolved,
    )

    if node_type in IMAGE_EXECUTABLE_NODE_TYPES:
        attachment_ids, mask_image_id = _image_task_inputs(
            node_type=node_type,
            resolved=resolved,
        )
        submission = await create_canvas_image_task(
            db,
            user=user,
            canvas=canvas,
            prompt=resolved.prompt,
            attachment_ids=attachment_ids,
            mask_image_id=mask_image_id,
            image_params=_image_params(config),
            idempotency_key=submission_key,
            metadata=metadata,
        )
        if not submission.generation_ids:
            raise canvas_http(
                "canvas_task_not_created",
                "image generation task was not created",
                500,
            )
        for ordinal, generation_id in enumerate(submission.generation_ids):
            db.add(
                CanvasExecutionTask(
                    execution_id=execution.id,
                    ordinal=ordinal,
                    task_kind="generation",
                    generation_id=generation_id,
                    status="queued",
                    idempotency_key=f"{submission_key}:{ordinal}"[:96],
                    request_fingerprint=request_fingerprint,
                    billing_ref_type="generation",
                    billing_ref_id=generation_id,
                    output_jsonb={},
                )
            )
        await db.commit()
        await _await_post_commit_publish(
            "image",
            publish_canvas_image_task(
                db,
                user_id=user.id,
                submission=submission,
            ),
            canvas_id=canvas.id,
            execution_id=execution.id,
        )
        return run, execution

    if getattr(user, "account_mode", "wallet") != "wallet":
        raise canvas_http(
            "account_mode_forbidden",
            "video generation requires wallet mode",
            403,
        )
    video_key = f"cv:{execution.id}"[:96]
    video_body = await _video_body(
        db,
        user=user,
        resolved=resolved,
        idempotency_key=video_key,
    )
    video_submission = await create_canvas_video_task(
        db,
        body=video_body,
        user=user,
        request=request,
        metadata=metadata,
    )
    video_out = video_submission.generation
    actual = (
        await db.execute(
            select(VideoGeneration).where(VideoGeneration.id == video_out.id)
        )
    ).scalar_one()
    db.add(
        CanvasExecutionTask(
            execution_id=execution.id,
            ordinal=0,
            task_kind="video_generation",
            video_generation_id=actual.id,
            status="queued",
            idempotency_key=video_key,
            request_fingerprint=actual.request_fingerprint,
            billing_ref_type="video_generation",
            billing_ref_id=actual.id,
            output_jsonb={},
        )
    )
    await db.commit()
    await _await_post_commit_publish(
        "video",
        publish_canvas_video_task(submission=video_submission),
        canvas_id=canvas.id,
        execution_id=execution.id,
    )
    return run, execution
