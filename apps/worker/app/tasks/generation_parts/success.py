from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .runtime import GenerationRunState


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


async def finalize_generation_success(state: GenerationRunState, g: Any) -> None:
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
    await _finalize_batch_extra_images(state, artifact.actual_image_count, g)
    await _enqueue_auto_title(state)
    await _finalize_dual_race_bonus(state, g)


async def _validate_result_and_publish_finalizing(
    state: GenerationRunState,
    g: Any,
) -> None:
    if not state.b64_result:
        raise g.UpstreamError(
            "upstream returned no image (tool_choice downgrade?)",
            error_code=g.EC.NO_IMAGE_RETURNED.value,
            status_code=200,
        )
    await g._raise_if_generation_interrupted(
        state.redis,
        state.task_id,
        state.lease_lost,
        "cancelled after upstream result",
    )
    await _publish_finalizing_stage(
        state,
        g,
        g.GenerationStage.FINAL_RECEIVED.value,
    )
    await _publish_finalizing_stage(
        state,
        g,
        g.GenerationStage.PROCESSING.value,
    )


async def _publish_finalizing_stage(
    state: GenerationRunState,
    g: Any,
    substage: str,
) -> None:
    await g.publish_event(
        state.redis,
        state.user_id,
        state.channel,
        g.EV_GEN_PROGRESS,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "trace_id": state.trace_id,
            "stage": g.GenerationStage.FINALIZING.value,
            "substage": substage,
        },
    )


async def _postprocess_generated_image(
    state: GenerationRunState,
    g: Any,
) -> GeneratedArtifact:
    started = g.time.monotonic()
    raw_image = _decode_upstream_result(state.b64_result, g)
    _raise_if_sha_echo(state, raw_image, g)
    transparent_requested = (
        state.image_request_options.get("background") == "transparent"
    )
    processed = await g._await_with_lease_guard(
        g._postprocess_raw_generated_image(
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


def _decode_upstream_result(b64_result: str | None, g: Any) -> bytes:
    try:
        return g._decode_upstream_image_b64(b64_result or "")
    except g.binascii.Error as exc:
        raise g.UpstreamError(
            f"bad base64 from upstream: {exc}",
            error_code=g.EC.BAD_RESPONSE.value,
            status_code=200,
        ) from exc


def _raise_if_sha_echo(
    state: GenerationRunState,
    raw_image: bytes,
    g: Any,
) -> None:
    if state.action != g.GenerationAction.EDIT:
        return
    sha = g._sha256(raw_image)
    if any(sha == reference_sha for reference_sha, _ in state.references):
        raise g.UpstreamError(
            "upstream returned original image unchanged (sha echo)",
            error_code=g.EC.SHA_ECHO.value,
            status_code=200,
        )


def _build_artifact(
    state: GenerationRunState,
    processed: Any,
    g: Any,
) -> GeneratedArtifact:
    image_id = g.new_uuid7()
    orig_ext = {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}[processed.orig_format]
    orig_mime = {
        "PNG": "image/png",
        "WEBP": "image/webp",
        "JPEG": "image/jpeg",
    }[processed.orig_format]
    model_metadata = g._model_image_metadata_from_request(
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
    effective_params = g._image_effective_params_snapshot(
        state.image_request_options,
        size=state.inpaint_size_override or state.resolved.size,
        width=processed.width,
        height=processed.height,
        mime=orig_mime,
    )
    image_metadata = dict(model_metadata)
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
        key_orig=f"u/{state.user_id}/g/{state.task_id}/orig.{orig_ext}",
        key_display=f"u/{state.user_id}/g/{state.task_id}/display2048.webp",
        key_preview=f"u/{state.user_id}/g/{state.task_id}/preview1024.webp",
        key_thumb=f"u/{state.user_id}/g/{state.task_id}/thumb256.jpg",
    )


def _embed_model_metadata(
    state: GenerationRunState,
    raw_image: bytes,
    sha: str,
    orig_format: str,
    model_metadata: dict[str, Any],
    g: Any,
) -> tuple[bytes, str]:
    if not model_metadata:
        return raw_image, sha
    try:
        with g.PILImage.open(g.io.BytesIO(raw_image)) as image:
            image.load()
            raw_image = g._maybe_embed_model_image_metadata_bytes(
                image=image,
                fmt=orig_format,
                raw_image=raw_image,
                metadata=model_metadata,
            )
        return raw_image, g._sha256(raw_image)
    except Exception as exc:  # noqa: BLE001
        g.logger.info(
            "model_library image metadata embed skipped task=%s err=%s",
            state.task_id,
            exc,
        )
        return raw_image, sha


async def _write_artifact_files(
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: Any,
) -> list[str]:
    await g._raise_if_generation_interrupted(
        state.redis,
        state.task_id,
        state.lease_lost,
        "cancelled before storage write",
    )
    await _publish_finalizing_stage(state, g, g.GenerationStage.STORING.value)
    started = g.time.monotonic()
    created_keys = await g._await_with_lease_guard(
        g._write_generation_files(
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
    g: Any,
) -> dict[str, Any]:
    return g._build_generation_diagnostics(
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
        duration_ms=int(max(0.0, g.time.monotonic() - state.task_start) * 1000),
        debug_id=state.task_id,
        expose_provider_diagnostics=g.settings.expose_provider_diagnostics,
    )


async def _persist_generation_success(
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    created_storage_keys: list[str],
    g: Any,
) -> None:
    async with g._cleanup_storage_on_error(created_storage_keys):
        await g._raise_if_generation_interrupted(
            state.redis,
            state.task_id,
            state.lease_lost,
            "cancelled before generation persistence",
        )
        async with g.SessionLocal() as session:
            await g._ensure_generation_attempt_current(
                session,
                state.task_id,
                state.attempt,
            )
            state.conversation_id_for_title = (
                await g._ensure_generation_conversation_alive(
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
            await g._raise_if_generation_interrupted(
                state.redis,
                state.task_id,
                state.lease_lost,
                "cancelled before billing settlement",
            )
            await g.worker_billing.settle_generation(
                session,
                state.generation,
                width=artifact.width,
                height=artifact.height,
                image_count=1,
            )
            await g._raise_if_generation_interrupted(
                state.redis,
                state.task_id,
                state.lease_lost,
                "cancelled before success commit",
            )
            success_delivery = _stage_success_event(session, state, artifact, g)
            await session.commit()
            await g.worker_billing.flush_balance_cache_refreshes(session)
    await g._deliver_generation_event(state.redis, success_delivery)


def _add_image_rows(
    session: Any,
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: Any,
) -> None:
    parent_image_id = (
        state.primary_input_image_id
        if state.action == g.GenerationAction.EDIT
        else None
    )
    session.add(
        g.Image(
            id=artifact.image_id,
            user_id=state.user_id,
            owner_generation_id=state.task_id,
            source=g.ImageSource.GENERATED.value,
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
            g.ImageVariant(
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
    g: Any,
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
        }
    )
    _apply_route_and_provider_fields(upstream_request, state, g)
    _apply_optional_success_fields(upstream_request, state, artifact)
    return g._sanitize_generation_upstream_request(
        upstream_request,
        expose_provider_diagnostics=g.settings.expose_provider_diagnostics,
    )


def _apply_route_and_provider_fields(
    upstream_request: dict[str, Any],
    state: GenerationRunState,
    g: Any,
) -> None:
    if state.route_diagnostics:
        upstream_request["route_diagnostics"] = state.route_diagnostics[:12]
    if state.provider_attempt_log:
        upstream_request["provider_attempts"] = state.provider_attempt_log[:12]
    request_provider = (
        state.actual_upstream_provider
        or (state.upstream_provider_label if not state.is_dual_race else None)
        or g._request_event_provider_from_attempts(state.provider_attempt_log)
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
    g: Any,
) -> None:
    result = await session.execute(
        g._generation_attempt_update(
            state.task_id,
            state.attempt,
            statuses=g._RUNNING_GENERATION_STATUSES,
        ).values(
            status=g.GenerationStatus.SUCCEEDED.value,
            progress_stage=g.GenerationStage.FINALIZING,
            finished_at=datetime.now(timezone.utc),
            upstream_pixels=artifact.width * artifact.height,
            upstream_request=upstream_request,
            error_code=None,
            error_message=None,
        )
    )
    g._ensure_generation_updated(result, state.task_id, state.attempt)


async def _attach_image_to_message(
    session: Any,
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: Any,
) -> None:
    row = await session.get(g.Message, state.message_id)
    if row is None or row.status == g.MessageStatus.CANCELED:
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
            "url": g.storage.public_url(artifact.key_orig),
            "display_url": (f"/api/images/{artifact.image_id}/variants/display2048"),
            "preview_url": (f"/api/images/{artifact.image_id}/variants/preview1024"),
            "thumb_url": f"/api/images/{artifact.image_id}/variants/thumb256",
            "filename": artifact.model_metadata.get("suggested_filename"),
            **g._compact_image_payload_meta(artifact.image_metadata),
        }
    )
    content["images"] = images
    row.content = content
    row.status = g.MessageStatus.SUCCEEDED


async def _record_success_hooks(
    session: Any,
    state: GenerationRunState,
    image_id: str,
    g: Any,
) -> None:
    hooks = (
        (
            "model_library_generate",
            g._maybe_record_model_library_generate_image,
        ),
        ("poster_workflow", g._maybe_record_poster_workflow_image),
        (
            "poster_style_library_generate",
            g._maybe_record_poster_style_library_generate_image,
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
            g.logger.warning(
                "%s post-success hook failed task=%s err=%s",
                label,
                state.task_id,
                exc,
            )


def _stage_success_event(
    session: Any,
    state: GenerationRunState,
    artifact: GeneratedArtifact,
    g: Any,
) -> Any:
    return g._stage_generation_success_event(
        session,
        state.user_id,
        state.channel,
        generation_id=state.task_id,
        message_id=state.message_id,
        image_id=artifact.image_id,
        actual_size=f"{artifact.width}x{artifact.height}",
        mime=artifact.orig_mime,
        image_url=g.storage.public_url(artifact.key_orig),
        filename=artifact.model_metadata.get("suggested_filename"),
        image_payload_meta=g._compact_image_payload_meta(artifact.image_metadata),
        diagnostics=artifact.generation_diagnostics,
    )


async def _finalize_batch_extra_images(
    state: GenerationRunState,
    actual_image_count: int,
    g: Any,
) -> None:
    for batch_index, (extra_b64, extra_revised) in state.batch_extra_pairs:
        try:
            await g._handle_dual_race_bonus_image(
                **_bonus_common_kwargs(state),
                b64_result=extra_b64,
                revised_prompt=extra_revised,
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
        except (g._LeaseLost, g._TaskCancelled, asyncio.CancelledError):
            g.logger.info(
                "image2 n result finalize aborted by cancel/lease task=%s index=%s",
                state.task_id,
                batch_index,
            )
        except Exception as exc:  # noqa: BLE001
            g.logger.warning(
                "image2 n result finalize unexpected error task=%s index=%s err=%r",
                state.task_id,
                batch_index,
                exc,
            )


async def _enqueue_auto_title(state: GenerationRunState) -> None:
    if not state.conversation_id_for_title:
        return
    from ..auto_title import maybe_enqueue_auto_title

    await maybe_enqueue_auto_title(
        state.redis,
        state.conversation_id_for_title,
    )


async def _finalize_dual_race_bonus(
    state: GenerationRunState,
    g: Any,
) -> None:
    if state.image_iter is None:
        return
    bonus_pair = await _next_bonus_pair(state, g)
    if bonus_pair is None:
        return
    bonus_b64, bonus_revised = bonus_pair
    provider_event = state.progress_publisher.pop_provider_used_event()
    try:
        await g._handle_dual_race_bonus_image(
            **_bonus_common_kwargs(state),
            b64_result=bonus_b64,
            revised_prompt=bonus_revised,
            upstream_provider=provider_event.get("provider"),
            upstream_actual_route=provider_event.get("route"),
            upstream_actual_source=provider_event.get("source"),
            upstream_actual_endpoint=provider_event.get("endpoint"),
            settle_billing=True,
        )
    except (g._LeaseLost, g._TaskCancelled, asyncio.CancelledError):
        g.logger.info(
            "dual_race bonus finalize aborted by cancel/lease task=%s",
            state.task_id,
        )
    except Exception as exc:  # noqa: BLE001
        g.logger.warning(
            "dual_race bonus finalize unexpected error task=%s err=%r",
            state.task_id,
            exc,
        )


async def _next_bonus_pair(
    state: GenerationRunState,
    g: Any,
) -> tuple[str, str | None] | None:
    try:
        return await g._anext_image_with_guards(
            state.image_iter,
            state.lease_lost,
            redis=state.redis,
            task_id=state.task_id,
        )
    except (g._LeaseLost, g._TaskCancelled, asyncio.CancelledError):
        g.logger.info(
            "dual_race bonus iter aborted by cancel/lease task=%s",
            state.task_id,
        )
        await g._consume_image_iter_close_result(
            state.image_iter,
            task_id=state.task_id,
        )
        state.image_iter = None
        return None
    except Exception as exc:  # noqa: BLE001
        g.logger.warning(
            "dual_race bonus iter failed task=%s err=%r",
            state.task_id,
            exc,
        )
        return None


def _bonus_common_kwargs(state: GenerationRunState) -> dict[str, Any]:
    return {
        "redis": state.redis,
        "user_id": state.user_id,
        "channel": state.channel,
        "parent_task_id": state.task_id,
        "parent_idempotency_key": state.gen_idempotency_key,
        "parent_upstream_request": (
            state.parent_upstream_request_for_bonus
            or state.gen_upstream_request_snapshot
        ),
        "message_id": state.message_id,
        "action": str(state.action),
        "model": state.gen_model,
        "prompt": state.prompt,
        "size_requested": state.size_requested,
        "aspect_ratio": state.aspect_ratio,
        "input_image_ids": state.input_image_ids,
        "primary_input_image_id": state.primary_input_image_id,
        "references": state.references,
        "image_request_options": state.image_request_options,
    }
