from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from PIL import Image as PILImage

from lumen_core.constants import (
    EV_GEN_PROGRESS,
    GenerationAction,
    GenerationErrorCode as EC,
    GenerationStage,
    GenerationStatus,
    ImageSource,
    MessageStatus,
)
from lumen_core.model_base import new_uuid7
from lumen_core.model_entities import Generation, Image, ImageVariant, Message

from ...artifact_commit import (
    ArtifactAdoption,
    ArtifactCommitNotAdopted,
    ArtifactCommitOutcomeUnknown,
    commit_error_or_default,
    commit_with_adoption_probe,
)
from ...provider_runtime.errors import UpstreamError
from ...upstream_parts import (
    GeneratedImageResult,
    GeneratedPayloadInput,
    cleanup_owned_generated_payload,
    materialize_generated_payload,
)
from .active_user_fence import lock_active_generation_user
from .diagnostics import (
    build_generation_diagnostics,
    image_effective_params_snapshot,
    request_event_provider_from_attempts,
    sanitize_generation_upstream_request,
)
from .errors import LeaseLost, TaskCancelled
from .event_delivery import stage_generation_success_event
from .image_artifact_contracts import sha256
from .lifecycle import raise_if_generation_interrupted
from .persistence import (
    BonusGenerationContext,
    compact_image_payload_meta,
    ensure_generation_conversation_alive,
    handle_dual_race_bonus_image,
    maybe_embed_model_image_metadata_bytes,
    model_image_metadata_from_request,
)
from .post_commit import enqueue_auto_title, run_workflow_tagging
from .queue import redis_text
from .retry_state import (
    RUNNING_GENERATION_STATUSES,
    anext_image_with_guards,
    await_with_lease_guard,
    consume_image_iter_close_result,
    ensure_generation_attempt_current,
    ensure_generation_updated,
    generation_attempt_update,
    generation_execution_epoch,
    generation_execution_identity,
)
from .run_state import GenerationRunState
from .services import RunGenerationDeps

# Keep the old private seam available to focused tests and extensions.
_run_post_commit_workflow_tagging = run_workflow_tagging


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GeneratedArtifact:
    image_id: str
    raw_image: bytes
    sha256: str
    orig_format: str
    orig_ext: str
    orig_mime: str
    width: int
    height: int
    actual_image_count: int
    blurhash: str | None
    display_bytes: bytes
    display_size: tuple[int, int]
    preview_bytes: bytes
    preview_size: tuple[int, int]
    thumb_bytes: bytes
    thumb_size: tuple[int, int]
    transparent_alpha_recovered: bool
    transparent_qc_payload: dict[str, Any] | None
    transparent_provider: str | None
    model_metadata: dict[str, Any]
    effective_params: dict[str, Any]
    image_metadata: dict[str, Any]
    key_orig: str
    key_display: str
    key_preview: str
    key_thumb: str
    generation_diagnostics: dict[str, Any] | None = None


def generation_artifact_keys(
    *,
    user_id: str,
    task_id: str,
    execution_epoch: int = 0,
    attempt: int | None = None,
    attempt_epoch: int | None = None,
    orig_ext: str,
) -> tuple[str, str, str, str]:
    epoch = max(0, int(execution_epoch))
    retry_attempt = int(attempt if attempt is not None else attempt_epoch or 0)
    if retry_attempt <= 0:
        raise ValueError("generation artifact attempt must be positive")
    prefix = f"u/{user_id}/g/{task_id}/executions/{epoch}/attempts/{retry_attempt}"
    return (
        f"{prefix}/orig.{orig_ext}",
        f"{prefix}/display2048.webp",
        f"{prefix}/preview1024.webp",
        f"{prefix}/thumb256.jpg",
    )


async def finalize_generation_success(
    state: GenerationRunState,
    g: RunGenerationDeps,
) -> None:
    await _validate_result_and_publish_finalizing(state, g)
    artifact = await _postprocess_generated_image(state, g)
    created_storage_keys = await _write_artifact_files(state, artifact, g)
    await _persist_generation_success(
        state,
        artifact,
        created_storage_keys,
        g,
    )
    state.task_outcome = "succeeded"
    await run_workflow_tagging(state, artifact.image_id, g)
    await _finalize_batch_extra_images(state, artifact.actual_image_count, g)
    await enqueue_auto_title(state)
    await _finalize_dual_race_bonus(state, g)


async def _validate_result_and_publish_finalizing(
    state: GenerationRunState,
    g: RunGenerationDeps,
) -> None:
    if state.b64_result is None:
        raise UpstreamError(
            "upstream returned no image (tool_choice downgrade?)",
            error_code=EC.NO_IMAGE_RETURNED.value,
            status_code=200,
        )
    await raise_if_generation_interrupted(
        state.redis,
        state.task_id,
        state.lease_lost,
        "cancelled after upstream result",
    )
    await _publish_finalizing_stage(
        state,
        g,
        GenerationStage.FINAL_RECEIVED.value,
    )
    await _publish_finalizing_stage(
        state,
        g,
        GenerationStage.PROCESSING.value,
    )


async def _publish_finalizing_stage(
    state: GenerationRunState,
    g: RunGenerationDeps,
    substage: str,
) -> None:
    await g.events.publish(
        state.redis,
        state.user_id,
        state.channel,
        EV_GEN_PROGRESS,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "trace_id": state.trace_id,
            "execution_epoch": generation_execution_epoch(state),
            "attempt": state.attempt,
            "stage": GenerationStage.FINALIZING.value,
            "substage": substage,
        },
    )


async def _postprocess_generated_image(
    state: GenerationRunState,
    g: RunGenerationDeps,
) -> GeneratedArtifact:
    started = time.monotonic()
    raw_image = _decode_upstream_result(state.b64_result, g)
    _raise_if_sha_echo(state, raw_image, g)
    transparent_requested = (
        state.image_request_options.get("background") == "transparent"
    )
    processed = await await_with_lease_guard(
        g.provider.postprocess(
            raw_image,
            prompt=state.prompt,
            transparent_requested=transparent_requested,
        ),
        state.lease_lost,
        redis=state.redis,
        task_id=state.task_id,
    )
    state.stage_timer.add_elapsed("normalize", started)
    return _build_artifact(state, processed, g)


def _decode_upstream_result(
    payload: GeneratedPayloadInput | None,
    g: RunGenerationDeps,
) -> bytes:
    if payload is None:
        raise UpstreamError(
            "upstream returned no image",
            error_code=EC.NO_IMAGE_RETURNED.value,
            status_code=200,
        )
    try:
        return materialize_generated_payload(payload)
    except (TypeError, ValueError) as exc:
        raise UpstreamError(
            f"bad image payload from upstream: {exc}",
            error_code=EC.BAD_RESPONSE.value,
            status_code=200,
        ) from exc
    finally:
        if not isinstance(payload, str):
            cleanup_owned_generated_payload(payload)


def _raise_if_sha_echo(
    state: GenerationRunState,
    raw_image: bytes,
    g: RunGenerationDeps,
) -> None:
    if state.action != GenerationAction.EDIT:
        return
    sha = sha256(raw_image)
    if any(sha == reference_sha for reference_sha, _ in state.references):
        raise UpstreamError(
            "upstream returned original image unchanged (sha echo)",
            error_code=EC.SHA_ECHO.value,
            status_code=200,
        )


def _build_artifact(
    state: GenerationRunState,
    processed: Any,
    g: RunGenerationDeps,
) -> GeneratedArtifact:
    image_id = new_uuid7()
    orig_ext = {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}[processed.orig_format]
    orig_mime = {
        "PNG": "image/png",
        "WEBP": "image/webp",
        "JPEG": "image/jpeg",
    }[processed.orig_format]
    model_metadata = model_image_metadata_from_request(
        image_id=image_id,
        mime=orig_mime,
        request=state.gen_upstream_request_snapshot,
        prompt=state.prompt,
    )
    raw_image, sha = _embed_model_metadata(
        state,
        processed.raw_image,
        processed.sha256,
        processed.orig_format,
        model_metadata,
        g,
    )
    effective_params = image_effective_params_snapshot(
        state.image_request_options,
        size=state.inpaint_size_override or state.resolved.size,
        width=processed.width,
        height=processed.height,
        mime=orig_mime,
    )
    image_metadata = {
        **model_metadata,
        "artifact_attempt_epoch": state.attempt,
        "artifact_execution_epoch": generation_execution_epoch(state),
        "artifact_execution_identity": generation_execution_identity(
            generation_execution_epoch(state),
            state.attempt,
        ),
    }
    key_orig, key_display, key_preview, key_thumb = generation_artifact_keys(
        user_id=state.user_id,
        task_id=state.task_id,
        execution_epoch=generation_execution_epoch(state),
        attempt=state.attempt,
        orig_ext=orig_ext,
    )
    return GeneratedArtifact(
        image_id=image_id,
        raw_image=raw_image,
        sha256=sha,
        orig_format=processed.orig_format,
        orig_ext=orig_ext,
        orig_mime=orig_mime,
        width=processed.width,
        height=processed.height,
        actual_image_count=1 + len(state.batch_extra_pairs),
        blurhash=processed.blurhash,
        display_bytes=processed.display.bytes,
        display_size=processed.display.size,
        preview_bytes=processed.preview.bytes,
        preview_size=processed.preview.size,
        thumb_bytes=processed.thumb.bytes,
        thumb_size=processed.thumb.size,
        transparent_alpha_recovered=processed.transparent_alpha_recovered,
        transparent_qc_payload=processed.transparent_qc_payload,
        transparent_provider=processed.transparent_provider,
        model_metadata=model_metadata,
        effective_params=effective_params,
        image_metadata=image_metadata,
        key_orig=key_orig,
        key_display=key_display,
        key_preview=key_preview,
        key_thumb=key_thumb,
    )


def _embed_model_metadata(
    state: GenerationRunState,
    raw_image: bytes,
    sha: str,
    orig_format: str,
    model_metadata: dict[str, Any],
    g: RunGenerationDeps,
) -> tuple[bytes, str]:
    if not model_metadata:
        return raw_image, sha
    try:
        with PILImage.open(io.BytesIO(raw_image)) as image:
            image.load()
            raw_image = maybe_embed_model_image_metadata_bytes(
                image=image,
                fmt=orig_format,
                raw_image=raw_image,
                metadata=model_metadata,
            )
        return raw_image, sha256(raw_image)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "model_library image metadata embed skipped task=%s err=%s",
            state.task_id,
            exc,
        )
        return raw_image, sha


async def _write_artifact_files(
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: RunGenerationDeps,
) -> list[str]:
    await raise_if_generation_interrupted(
        state.redis,
        state.task_id,
        state.lease_lost,
        "cancelled before storage write",
    )
    await _publish_finalizing_stage(state, g, GenerationStage.STORING.value)
    started = time.monotonic()
    created_keys = await await_with_lease_guard(
        g.artifacts.write_files(
            [
                (artifact.key_orig, artifact.raw_image),
                (artifact.key_display, artifact.display_bytes),
                (artifact.key_preview, artifact.preview_bytes),
                (artifact.key_thumb, artifact.thumb_bytes),
            ]
        ),
        state.lease_lost,
        redis=state.redis,
        task_id=state.task_id,
    )
    state.stage_timer.add_elapsed("upload", started)
    artifact.generation_diagnostics = _success_diagnostics(state, artifact, g)
    artifact.image_metadata["generation_diagnostics"] = artifact.generation_diagnostics
    if state.revised_prompt:
        artifact.image_metadata["revised_prompt"] = state.revised_prompt
    return created_keys


def _success_diagnostics(
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: RunGenerationDeps,
) -> dict[str, Any]:
    return build_generation_diagnostics(
        trace_id=state.trace_id,
        requested_params=state.requested_params_for_diag,
        effective_params=artifact.effective_params,
        revised_prompt=state.revised_prompt,
        provider=state.actual_upstream_provider
        or (state.upstream_provider_label if not state.is_dual_race else None),
        upstream_route=state.image_route,
        actual_route=state.actual_upstream_route,
        actual_source=state.actual_upstream_source,
        actual_endpoint=state.actual_upstream_endpoint,
        provider_attempts=state.provider_attempt_log,
        stage_timings_ms=state.stage_timer.snapshot(),
        route_diagnostics=state.route_diagnostics,
        upstream_duration_ms=state.upstream_duration_ms,
        duration_ms=int(max(0.0, time.monotonic() - state.task_start) * 1000),
        debug_id=state.task_id,
        expose_provider_diagnostics=g.queue.expose_provider_diagnostics,
    )


async def _persist_generation_success(
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    created_storage_keys: list[str],
    g: RunGenerationDeps,
) -> None:
    cleanup_allowed = True
    try:
        await raise_if_generation_interrupted(
            state.redis,
            state.task_id,
            state.lease_lost,
            "cancelled before generation persistence",
        )
        async with g.store.session() as session:
            if not await lock_active_generation_user(
                session,
                user_id=state.user_id,
            ):
                raise TaskCancelled("account deleted before generation persistence")
            await ensure_generation_attempt_current(
                session,
                state.task_id,
                state.attempt,
                execution_epoch=generation_execution_epoch(state),
            )
            state.conversation_id_for_title = (
                await ensure_generation_conversation_alive(
                    session,
                    message_id=state.message_id,
                    user_id=state.user_id,
                    lock=True,
                )
            )
            _add_image_rows(session, state, artifact, g)
            upstream_request = _success_upstream_request(state, artifact, g)
            state.parent_upstream_request_for_bonus = dict(upstream_request)
            await _mark_generation_succeeded(
                session,
                state,
                artifact,
                upstream_request,
                g,
            )
            await _attach_image_to_message(session, state, artifact, g)
            await _record_success_hooks(session, state, artifact.image_id, g)
            await raise_if_generation_interrupted(
                state.redis,
                state.task_id,
                state.lease_lost,
                "cancelled before billing settlement",
            )
            await g.billing.settle(
                session,
                state.generation,
                width=artifact.width,
                height=artifact.height,
                image_count=1,
            )
            # settle 之后不再检查中断。此处一旦抛异常，session 连同刚写入的
            # 钱包流水一起回滚，而上游图片已经产出并计过费；随后 failure
            # handler 会按 lease_lost 走 release 分支，等于平台替用户吸收这
            # 笔上游成本——纯转嫁要杜绝的。中断只允许发生在 settle 之前。
            success_delivery = _stage_success_event(session, state, artifact, g)
            commit_result = await commit_with_adoption_probe(
                session,
                probe=lambda: _probe_generation_success_adoption(
                    state,
                    artifact,
                    g,
                ),
                logger=logger,
                label=(
                    f"generation artifact task={state.task_id} "
                    f"epoch={generation_execution_epoch(state)} "
                    f"attempt={state.attempt}"
                ),
            )
            if commit_result.adopted:
                cleanup_allowed = False
            elif commit_result.outcome is ArtifactAdoption.NOT_ADOPTED:
                # 上游已产出图片并计费,只是本地 artifact 事务未被采纳;必须带
                # IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES 内的 error_code 抛出,让
                # runner 的 unknown 结算分支 settle 而不是 release(failure 决策表
                # 的 release 前提「非 unknown 码 = 适配层已证明上游未计费」对本地
                # 失败不成立,否则平台吸收已发生的上游成本)。
                commit_error = commit_error_or_default(
                    commit_result,
                    label=f"generation artifact task={state.task_id}",
                )
                raise ArtifactCommitNotAdopted(
                    f"generation artifact commit was not adopted task={state.task_id} "
                    f"epoch={generation_execution_epoch(state)} attempt={state.attempt}"
                    + (
                        f" cause={commit_error}"
                        if commit_error is not None
                        else ""
                    ),
                    error_code=EC.IMAGE_JOB_RESULT_UNKNOWN.value,
                    commit_error=commit_error,
                ) from commit_error
            else:
                cleanup_allowed = False
                unknown = ArtifactCommitOutcomeUnknown(
                    f"generation artifact commit outcome unknown task={state.task_id} "
                    f"epoch={generation_execution_epoch(state)} attempt={state.attempt}"
                )
                if commit_result.commit_error is not None:
                    raise unknown from commit_result.commit_error
                raise unknown
            await g.billing.flush_after_commit(session)
    except BaseException:
        if cleanup_allowed:
            await g.artifacts.delete_files(created_storage_keys)
        raise
    await g.events.deliver(state.redis, success_delivery)


async def _probe_generation_success_adoption(
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: RunGenerationDeps,
) -> ArtifactAdoption:
    async with g.store.session() as session:
        generation = await session.get(
            Generation,
            state.task_id,
            with_for_update=True,
        )
        image = await session.get(Image, artifact.image_id)
        if image is not None:
            exact_image = (
                image.owner_generation_id == state.task_id
                and image.user_id == state.user_id
                and image.storage_key == artifact.key_orig
                and image.sha256 == artifact.sha256
                and isinstance(image.metadata_jsonb, dict)
                and image.metadata_jsonb.get("artifact_attempt_epoch") == state.attempt
                and image.metadata_jsonb.get("artifact_execution_epoch")
                == generation_execution_epoch(state)
            )
            exact_generation = (
                generation is not None
                and generation.attempt == state.attempt
                and generation.execution_epoch == generation_execution_epoch(state)
                and generation.status == GenerationStatus.SUCCEEDED.value
            )
            return (
                ArtifactAdoption.ADOPTED
                if exact_image and exact_generation
                else ArtifactAdoption.UNKNOWN
            )
        if (
            generation is not None
            and generation.attempt == state.attempt
            and generation.execution_epoch == generation_execution_epoch(state)
            and generation.status == GenerationStatus.SUCCEEDED.value
        ):
            return ArtifactAdoption.UNKNOWN
        return ArtifactAdoption.NOT_ADOPTED


def _add_image_rows(
    session: Any,
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: RunGenerationDeps,
) -> None:
    parent_image_id = (
        state.primary_input_image_id if state.action == GenerationAction.EDIT else None
    )
    session.add(
        Image(
            id=artifact.image_id,
            user_id=state.user_id,
            owner_generation_id=state.task_id,
            source=ImageSource.GENERATED.value,
            parent_image_id=parent_image_id,
            storage_key=artifact.key_orig,
            mime=artifact.orig_mime,
            width=artifact.width,
            height=artifact.height,
            size_bytes=len(artifact.raw_image),
            sha256=artifact.sha256,
            blurhash=artifact.blurhash,
            visibility="private",
            metadata_jsonb=artifact.image_metadata,
        )
    )
    for kind, key, size in (
        ("display2048", artifact.key_display, artifact.display_size),
        ("preview1024", artifact.key_preview, artifact.preview_size),
        ("thumb256", artifact.key_thumb, artifact.thumb_size),
    ):
        session.add(
            ImageVariant(
                image_id=artifact.image_id,
                kind=kind,
                storage_key=key,
                width=size[0],
                height=size[1],
            )
        )


def _success_upstream_request(
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: RunGenerationDeps,
) -> dict[str, Any]:
    generation = state.generation
    upstream_request = (
        dict(state.gen_upstream_request_snapshot)
        if isinstance(state.gen_upstream_request_snapshot, dict)
        else dict(generation.upstream_request)
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    upstream_request.update(state.image_request_options)
    upstream_request.update(
        {
            "trace_id": state.trace_id,
            "size_actual": f"{artifact.width}x{artifact.height}",
            "mime": artifact.orig_mime,
            "upstream_route": state.image_route,
            "requested_params": state.requested_params_for_diag,
            "effective_params": artifact.effective_params,
            "image_count_requested": state.requested_image_count,
            "image_count_actual": artifact.actual_image_count,
            "generation_diagnostics": artifact.generation_diagnostics,
            "debug_id": state.task_id,
            "execution_epoch": generation_execution_epoch(state),
            "attempt": state.attempt,
        }
    )
    _apply_route_and_provider_fields(upstream_request, state, g)
    _apply_optional_success_fields(upstream_request, state, artifact)
    return sanitize_generation_upstream_request(
        upstream_request,
        expose_provider_diagnostics=g.queue.expose_provider_diagnostics,
    )


def _apply_route_and_provider_fields(
    upstream_request: dict[str, Any],
    state: GenerationRunState,
    g: RunGenerationDeps,
) -> None:
    if state.route_diagnostics:
        upstream_request["route_diagnostics"] = state.route_diagnostics[:12]
    if state.provider_attempt_log:
        upstream_request["provider_attempts"] = state.provider_attempt_log[:12]
    request_provider = (
        state.actual_upstream_provider
        or (state.upstream_provider_label if not state.is_dual_race else None)
        or request_event_provider_from_attempts(
            state.provider_attempt_log,
            redis_text=redis_text,
        )
    )
    if state.actual_upstream_provider:
        upstream_request["provider"] = state.actual_upstream_provider
        upstream_request["actual_provider"] = state.actual_upstream_provider
    elif state.upstream_provider_label and not state.is_dual_race:
        upstream_request["provider"] = state.upstream_provider_label
    else:
        upstream_request.pop("provider", None)
        upstream_request.pop("actual_provider", None)
    if request_provider:
        upstream_request["request_event_provider"] = request_provider
    else:
        upstream_request.pop("request_event_provider", None)


def _apply_optional_success_fields(
    upstream_request: dict[str, Any],
    state: GenerationRunState,
    artifact: GeneratedArtifact,
) -> None:
    optional_fields = {
        "upstream_duration_ms": state.upstream_duration_ms,
        "actual_route": state.actual_upstream_route,
        "actual_source": state.actual_upstream_source,
        "actual_endpoint": state.actual_upstream_endpoint,
        "transparent_qc": artifact.transparent_qc_payload,
        "transparent_pipeline_provider": artifact.transparent_provider,
        "revised_prompt": state.revised_prompt,
    }
    for key, value in optional_fields.items():
        if value is not None:
            upstream_request[key] = value
    if artifact.transparent_alpha_recovered:
        upstream_request["transparent_alpha_recovered"] = True
    upstream_request.update(state.image_job_meta)


async def _mark_generation_succeeded(
    session: Any,
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    upstream_request: dict[str, Any],
    g: RunGenerationDeps,
) -> None:
    result = await session.execute(
        generation_attempt_update(
            state.task_id,
            state.attempt,
            statuses=RUNNING_GENERATION_STATUSES,
            execution_epoch=generation_execution_epoch(state),
        ).values(
            status=GenerationStatus.SUCCEEDED.value,
            progress_stage=GenerationStage.FINALIZING,
            finished_at=datetime.now(timezone.utc),
            upstream_pixels=artifact.width * artifact.height,
            upstream_request=upstream_request,
            error_code=None,
            error_message=None,
        )
    )
    ensure_generation_updated(result, state.task_id, state.attempt)


async def _attach_image_to_message(
    session: Any,
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: RunGenerationDeps,
) -> None:
    row = await session.get(Message, state.message_id)
    if row is None or row.status == MessageStatus.CANCELED:
        return
    content = dict(row.content or {})
    images = list(content.get("images") or [])
    images.append(
        {
            "image_id": artifact.image_id,
            "from_generation_id": state.task_id,
            "width": artifact.width,
            "height": artifact.height,
            "mime": artifact.orig_mime,
            "url": g.artifacts.public_url(artifact.key_orig),
            "display_url": (f"/api/images/{artifact.image_id}/variants/display2048"),
            "preview_url": (f"/api/images/{artifact.image_id}/variants/preview1024"),
            "thumb_url": f"/api/images/{artifact.image_id}/variants/thumb256",
            "filename": artifact.model_metadata.get("suggested_filename"),
            **compact_image_payload_meta(artifact.image_metadata),
        }
    )
    content["images"] = images
    row.content = content
    row.status = MessageStatus.SUCCEEDED


async def _record_success_hooks(
    session: Any,
    state: GenerationRunState,
    image_id: str,
    g: RunGenerationDeps,
) -> None:
    hooks = (
        (
            "model_library_generate",
            g.workflows.record_model_library_generate_image,
        ),
        ("poster_workflow", g.workflows.record_poster_workflow_image),
        (
            "poster_style_library_generate",
            g.workflows.record_poster_style_library_generate_image,
        ),
    )
    for label, hook in hooks:
        try:
            await hook(
                session=session,
                user_id=state.user_id,
                generation=state.generation,
                image_id=image_id,
            )
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s post-success hook failed task=%s err=%s",
                label,
                state.task_id,
                exc,
            )


def _stage_success_event(
    session: Any,
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: RunGenerationDeps,
) -> Any:
    return stage_generation_success_event(
        session,
        state.user_id,
        state.channel,
        generation_id=state.task_id,
        message_id=state.message_id,
        image_id=artifact.image_id,
        actual_size=f"{artifact.width}x{artifact.height}",
        mime=artifact.orig_mime,
        image_url=g.artifacts.public_url(artifact.key_orig),
        filename=artifact.model_metadata.get("suggested_filename"),
        image_payload_meta=compact_image_payload_meta(artifact.image_metadata),
        diagnostics=artifact.generation_diagnostics,
    )


async def _finalize_batch_extra_images(
    state: GenerationRunState,
    actual_image_count: int,
    g: RunGenerationDeps,
) -> None:
    for batch_index, (extra_b64, extra_revised) in state.batch_extra_pairs:
        try:
            await handle_dual_race_bonus_image(
                replace(
                    _bonus_context(state, extra_b64, extra_revised),
                    upstream_provider=state.actual_upstream_provider,
                    upstream_actual_route=state.actual_upstream_route,
                    upstream_actual_source=state.actual_upstream_source,
                    upstream_actual_endpoint=state.actual_upstream_endpoint,
                    billing_meta={
                        "billing_free": False,
                        "billing_label": "billable",
                        "billing_policy": "batch_extra_settled_separately",
                    },
                    idempotency_suffix=f":n{batch_index}",
                    extra_upstream_fields={
                        "batch_parent_generation_id": state.task_id,
                        "batch_index": batch_index,
                        "batch_count": actual_image_count,
                    },
                    record_model_library_candidate=False,
                    settle_billing=True,
                    log_label="image2 n result",
                )
            )
        except (LeaseLost, TaskCancelled, asyncio.CancelledError):
            logger.info(
                "image2 n result finalize aborted by cancel/lease task=%s index=%s",
                state.task_id,
                batch_index,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "image2 n result finalize unexpected error task=%s index=%s err=%r",
                state.task_id,
                batch_index,
                exc,
            )


async def _finalize_dual_race_bonus(
    state: GenerationRunState,
    g: RunGenerationDeps,
) -> None:
    if state.image_iter is None:
        return
    bonus_pair = await _next_bonus_pair(state, g)
    if bonus_pair is None:
        return
    bonus_b64, bonus_revised = bonus_pair
    provider_event = state.progress_publisher.pop_provider_used_event()
    try:
        await handle_dual_race_bonus_image(
            replace(
                _bonus_context(state, bonus_b64, bonus_revised),
                upstream_provider=provider_event.get("provider"),
                upstream_actual_route=provider_event.get("route"),
                upstream_actual_source=provider_event.get("source"),
                upstream_actual_endpoint=provider_event.get("endpoint"),
                settle_billing=True,
            )
        )
    except (LeaseLost, TaskCancelled, asyncio.CancelledError):
        logger.info(
            "dual_race bonus finalize aborted by cancel/lease task=%s",
            state.task_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dual_race bonus finalize unexpected error task=%s err=%r",
            state.task_id,
            exc,
        )


async def _next_bonus_pair(
    state: GenerationRunState,
    g: RunGenerationDeps,
) -> GeneratedImageResult | None:
    try:
        return await anext_image_with_guards(
            state.image_iter,
            state.lease_lost,
            redis=state.redis,
            task_id=state.task_id,
        )
    except (LeaseLost, TaskCancelled, asyncio.CancelledError):
        logger.info(
            "dual_race bonus iter aborted by cancel/lease task=%s",
            state.task_id,
        )
        await consume_image_iter_close_result(
            state.image_iter,
            task_id=state.task_id,
        )
        state.image_iter = None
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dual_race bonus iter failed task=%s err=%r",
            state.task_id,
            exc,
        )
        return None


def _bonus_context(
    state: GenerationRunState,
    b64_result: GeneratedPayloadInput,
    revised_prompt: str | None,
) -> BonusGenerationContext:
    return BonusGenerationContext(
        services=state.services,
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        parent_task_id=state.task_id,
        execution_epoch=generation_execution_epoch(state),
        attempt=state.attempt,
        parent_idempotency_key=state.gen_idempotency_key,
        parent_upstream_request=(
            state.parent_upstream_request_for_bonus
            or state.gen_upstream_request_snapshot
        ),
        message_id=state.message_id,
        action=str(state.action),
        model=state.gen_model,
        prompt=state.prompt,
        size_requested=state.size_requested,
        aspect_ratio=state.aspect_ratio,
        input_image_ids=state.input_image_ids,
        primary_input_image_id=state.primary_input_image_id,
        references=state.references,
        image_request_options=state.image_request_options,
        b64_result=b64_result,
        revised_prompt=revised_prompt,
        upstream_provider=None,
        upstream_actual_route=None,
        upstream_actual_source=None,
        upstream_actual_endpoint=None,
        billing_meta=None,
        idempotency_suffix=":b",
        extra_upstream_fields=None,
        record_model_library_candidate=True,
        settle_billing=False,
        log_label="dual_race bonus",
    )
