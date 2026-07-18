from __future__ import annotations

import asyncio
import binascii
import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from PIL import Image as PILImage

from ._facade import GenerationFacade

_g = GenerationFacade()
bind_generation_facade = _g.bind


def clean_model_style_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        output.append(tag[:32])
        if len(output) >= 12:
            break
    return output


def model_image_metadata_from_request(
    *,
    image_id: str,
    mime: str,
    request: dict[str, Any] | None,
    prompt: str | None = None,
) -> dict[str, Any]:
    request_value = request if isinstance(request, dict) else {}
    if request_value.get("workflow_action") != "model_library_generate":
        return {}
    age_segment = request_value.get("workflow_model_library_age_segment")
    gender = request_value.get("workflow_model_library_gender")
    appearance_direction = request_value.get(
        "workflow_model_library_appearance_direction"
    )
    style_tags = _g._clean_model_style_tags(
        request_value.get("workflow_model_library_style_tags") or []
    )
    payload = _g.build_model_image_metadata(
        age_segment=age_segment if isinstance(age_segment, str) else None,
        gender=gender if isinstance(gender, str) else None,
        appearance_direction=(
            appearance_direction if isinstance(appearance_direction, str) else None
        ),
        style_tags=style_tags,
        source="model_library_generate",
        prompt_hint=prompt,
    )
    if not payload:
        return {}
    extension = "png"
    if isinstance(mime, str) and mime.startswith("image/"):
        extension = "jpg" if mime == "image/jpeg" else mime.removeprefix("image/")
    return {
        "model_library": payload,
        "suggested_filename": _g.model_image_filename(
            image_id=image_id,
            ext=extension,
            age_segment=payload.get("age_segment"),
            gender=payload.get("gender"),
            appearance_direction=payload.get("appearance_direction"),
            style_tags=style_tags,
        ),
    }


def compact_image_payload_meta(metadata: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key in (
        "is_dual_race_bonus",
        "billing_free",
        "billing_label",
        "billing_exempt_reason",
    ):
        value = metadata.get(key)
        if value is not None and value is not False:
            output[key] = value
    return output


def maybe_embed_model_image_metadata_bytes(
    *,
    image: PILImage.Image,
    fmt: str,
    raw_image: bytes,
    metadata: dict[str, Any],
) -> bytes:
    payload = metadata.get("model_library") if isinstance(metadata, dict) else None
    if fmt.upper() != "PNG" or not isinstance(payload, dict) or not payload:
        return raw_image
    output = io.BytesIO()
    _g.save_image_with_model_metadata(
        image,
        output,
        fmt="PNG",
        metadata=payload,
    )
    return output.getvalue()


async def find_existing_generated_image(
    session: Any,
    *,
    task_id: str,
    user_id: str,
) -> Any | None:
    row = (
        await session.execute(
            _g.select(_g.Image)
            .where(
                _g.Image.owner_generation_id == task_id,
                _g.Image.user_id == user_id,
                _g.Image.deleted_at.is_(None),
            )
            .with_for_update()
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if getattr(row, "user_id", None) != user_id:
        _g.logger.error(
            "short-circuit guard: image %s user mismatch expect=%s got=%s — ignoring",
            getattr(row, "id", "?"),
            user_id,
            getattr(row, "user_id", None),
        )
        return None
    if getattr(row, "source", None) != _g.ImageSource.GENERATED.value:
        _g.logger.error(
            "short-circuit guard: image %s source mismatch got=%s — ignoring",
            getattr(row, "id", "?"),
            getattr(row, "source", None),
        )
        return None
    try:
        width = int(getattr(row, "width", 0) or 0)
        height = int(getattr(row, "height", 0) or 0)
    except (TypeError, ValueError):
        width = height = 0
    if width <= 0 or height <= 0:
        _g.logger.error(
            "short-circuit guard: image %s invalid dimensions %sx%s — ignoring",
            getattr(row, "id", "?"),
            getattr(row, "width", None),
            getattr(row, "height", None),
        )
        return None
    return row


async def ensure_generation_conversation_alive(
    session: Any,
    *,
    message_id: str,
    user_id: str,
    lock: bool = False,
) -> str:
    statement = (
        _g.select(_g.Conversation.id)
        .join(
            _g.Message,
            _g.Message.conversation_id == _g.Conversation.id,
        )
        .where(
            _g.Message.id == message_id,
            _g.Message.deleted_at.is_(None),
            _g.Conversation.user_id == user_id,
            _g.Conversation.deleted_at.is_(None),
        )
    )
    if lock:
        statement = statement.with_for_update(of=_g.Conversation)
    conversation_id = (await session.execute(statement)).scalar_one_or_none()
    if conversation_id is None:
        raise _g._TaskCancelled("conversation or message was deleted")
    return str(conversation_id)


async def _wait_for_storage_task(task: asyncio.Future[Any]) -> Any:
    """Wait for an already-started storage task despite caller cancellation."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


def _log_storage_cleanup_results(
    keys: list[str],
    results: list[bool | BaseException],
) -> None:
    for key, result in zip(keys, results, strict=False):
        if isinstance(result, BaseException):
            _g.logger.warning(
                "storage cleanup failed key=%s err=%s",
                key,
                result,
            )


async def delete_storage_keys(keys: list[str]) -> None:
    unique_keys = list(dict.fromkeys(keys))
    if not unique_keys:
        return
    cleanup: asyncio.Future[list[bool | BaseException]] = asyncio.ensure_future(
        asyncio.gather(
            *(asyncio.to_thread(_g.storage.delete, key) for key in unique_keys),
            return_exceptions=True,
        )
    )
    try:
        results = await asyncio.shield(cleanup)
    except BaseException:
        results = await _wait_for_storage_task(cleanup)
        _log_storage_cleanup_results(unique_keys, results)
        raise
    _log_storage_cleanup_results(unique_keys, results)


def _storage_write_outcome(
    results: list[tuple[str, bool] | BaseException],
) -> tuple[list[str], BaseException | None]:
    created_keys: list[str] = []
    first_exc: BaseException | None = None
    for result in results:
        if isinstance(result, BaseException):
            first_exc = first_exc or result
            continue
        key, created = result
        if created:
            created_keys.append(key)
    return created_keys, first_exc


async def write_generation_files(
    files: list[tuple[str, bytes]],
) -> list[str]:
    async def put_one(key: str, data: bytes) -> tuple[str, bool]:
        result = await asyncio.to_thread(
            _g.storage.put_bytes_result,
            key,
            data,
        )
        return key, bool(result.created)

    writes: asyncio.Future[list[tuple[str, bool] | BaseException]] = (
        asyncio.ensure_future(
            asyncio.gather(
                *(put_one(key, data) for key, data in files),
                return_exceptions=True,
            )
        )
    )
    try:
        results = await asyncio.shield(writes)
    except BaseException:
        results = await _wait_for_storage_task(writes)
        created_keys, _first_exc = _storage_write_outcome(results)
        cleanup = asyncio.ensure_future(_g._delete_storage_keys(created_keys))
        await _wait_for_storage_task(cleanup)
        raise

    created_keys, first_exc = _storage_write_outcome(results)
    if first_exc is not None:
        await _g._delete_storage_keys(created_keys)
        raise first_exc
    return created_keys


@asynccontextmanager
async def cleanup_storage_on_error(
    keys: list[str],
) -> AsyncIterator[None]:
    try:
        yield
    except BaseException:
        cleanup = asyncio.ensure_future(_g._delete_storage_keys(keys))
        await _wait_for_storage_task(cleanup)
        raise


async def handle_dual_race_bonus_image(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    parent_task_id: str,
    parent_idempotency_key: str,
    parent_upstream_request: dict[str, Any] | None,
    message_id: str,
    action: str,
    model: str,
    prompt: str,
    size_requested: str,
    aspect_ratio: str,
    input_image_ids: list[str],
    primary_input_image_id: str | None,
    references: list[tuple[str, bytes]],
    image_request_options: dict[str, Any],
    b64_result: str,
    revised_prompt: str | None,
    upstream_provider: str | None = None,
    upstream_actual_route: str | None = None,
    upstream_actual_source: str | None = None,
    upstream_actual_endpoint: str | None = None,
    billing_meta: dict[str, Any] | None = None,
    idempotency_suffix: str = ":b",
    extra_upstream_fields: dict[str, Any] | None = None,
    record_model_library_candidate: bool = True,
    settle_billing: bool = False,
    log_label: str = "dual_race bonus",
) -> bool:
    """Persist and publish a separately billed bonus generation."""
    if not b64_result:
        return False

    try:
        raw_image = _g._decode_upstream_image_b64(b64_result)
    except binascii.Error:
        _g.logger.warning(
            "%s base64 decode failed parent=%s",
            log_label,
            parent_task_id,
        )
        return False
    sha = _g._sha256(raw_image)

    if action == _g.GenerationAction.EDIT.value:
        if any(sha == ref_sha for ref_sha, _raw in references):
            _g.logger.info(
                "%s sha echoed reference parent=%s; skip",
                log_label,
                parent_task_id,
            )
            return False

    transparent_requested = image_request_options.get("background") == "transparent"
    try:
        processed_image = await _g._postprocess_raw_generated_image(
            raw_image,
            prompt=prompt,
            transparent_requested=transparent_requested,
        )
    except Exception as exc:  # noqa: BLE001
        _g.logger.warning(
            "%s pillow decode failed parent=%s err=%r",
            log_label,
            parent_task_id,
            exc,
        )
        return False
    raw_image = processed_image.raw_image
    sha = processed_image.sha256
    orig_format = processed_image.orig_format
    width = processed_image.width
    height = processed_image.height
    blurhash_str = processed_image.blurhash
    display_bytes = processed_image.display.bytes
    display_size = processed_image.display.size
    preview_bytes = processed_image.preview.bytes
    preview_size = processed_image.preview.size
    thumb_bytes = processed_image.thumb.bytes
    thumb_size = processed_image.thumb.size
    transparent_alpha_recovered = processed_image.transparent_alpha_recovered
    transparent_qc_payload = processed_image.transparent_qc_payload
    transparent_provider = processed_image.transparent_provider

    bonus_generation_id = _g.new_uuid7()
    image_id = _g.new_uuid7()
    orig_ext_by_format = {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}
    orig_mime_by_format = {
        "PNG": "image/png",
        "WEBP": "image/webp",
        "JPEG": "image/jpeg",
    }
    orig_extension = orig_ext_by_format[orig_format]
    orig_mime = orig_mime_by_format[orig_format]
    model_metadata = _g._model_image_metadata_from_request(
        image_id=image_id,
        mime=orig_mime,
        request=parent_upstream_request,
        prompt=prompt,
    )
    result_billing_meta: dict[str, Any] = (
        dict(billing_meta)
        if billing_meta is not None
        else {
            "is_dual_race_bonus": True,
            "billing_free": False,
            "billing_label": "billable",
            "billing_policy": "dual_race_loser_settled_separately",
        }
    )
    if result_billing_meta.get("billing_free") is not True and not settle_billing:
        _g.logger.warning(
            "%s missing settle_billing for billable image parent=%s",
            log_label,
            parent_task_id,
        )
        return False
    image_metadata: dict[str, Any] = {
        **model_metadata,
        **result_billing_meta,
    }
    if model_metadata:
        try:
            with PILImage.open(io.BytesIO(raw_image)) as image:
                image.load()
                raw_image = _g._maybe_embed_model_image_metadata_bytes(
                    image=image,
                    fmt=orig_format,
                    raw_image=raw_image,
                    metadata=model_metadata,
                )
            sha = _g._sha256(raw_image)
        except Exception as exc:  # noqa: BLE001
            _g.logger.info(
                "%s model metadata embed skipped parent=%s err=%s",
                log_label,
                parent_task_id,
                exc,
            )
    key_orig = f"u/{user_id}/g/{bonus_generation_id}/orig.{orig_extension}"
    key_display = f"u/{user_id}/g/{bonus_generation_id}/display2048.webp"
    key_preview = f"u/{user_id}/g/{bonus_generation_id}/preview1024.webp"
    key_thumb = f"u/{user_id}/g/{bonus_generation_id}/thumb256.jpg"

    try:
        created_storage_keys = await _g._write_generation_files(
            [
                (key_orig, raw_image),
                (key_display, display_bytes),
                (key_preview, preview_bytes),
                (key_thumb, thumb_bytes),
            ]
        )
    except Exception as exc:  # noqa: BLE001
        _g.logger.warning(
            "%s storage write failed parent=%s err=%r",
            log_label,
            parent_task_id,
            exc,
        )
        return False

    try:
        async with _g._cleanup_storage_on_error(created_storage_keys):
            async with _g.SessionLocal() as session:
                suffix = idempotency_suffix or ":b"
                bonus_idempotency_key = (
                    f"{parent_idempotency_key[: max(1, 64 - len(suffix))]}{suffix}"
                )
                bonus_upstream_request: dict[str, Any] = dict(
                    parent_upstream_request or {}
                )
                bonus_upstream_request.update(image_request_options)
                bonus_upstream_request["size_actual"] = f"{width}x{height}"
                bonus_upstream_request["mime"] = orig_mime
                bonus_upstream_request.update(result_billing_meta)
                bonus_upstream_request["parent_generation_id"] = parent_task_id
                if extra_upstream_fields:
                    bonus_upstream_request.update(extra_upstream_fields)
                if upstream_provider:
                    bonus_upstream_request["provider"] = upstream_provider
                    bonus_upstream_request["actual_provider"] = upstream_provider
                    bonus_upstream_request["request_event_provider"] = upstream_provider
                else:
                    bonus_upstream_request.pop("provider", None)
                    bonus_upstream_request.pop("actual_provider", None)
                    bonus_upstream_request.pop("request_event_provider", None)
                if upstream_actual_route:
                    bonus_upstream_request["actual_route"] = upstream_actual_route
                if upstream_actual_source:
                    bonus_upstream_request["actual_source"] = upstream_actual_source
                if upstream_actual_endpoint:
                    bonus_upstream_request["actual_endpoint"] = upstream_actual_endpoint
                if transparent_alpha_recovered:
                    bonus_upstream_request["transparent_alpha_recovered"] = True
                if transparent_qc_payload is not None:
                    bonus_upstream_request["transparent_qc"] = transparent_qc_payload
                if transparent_provider is not None:
                    bonus_upstream_request["transparent_pipeline_provider"] = (
                        transparent_provider
                    )
                if revised_prompt:
                    bonus_upstream_request["revised_prompt"] = revised_prompt

                now = datetime.now(timezone.utc)
                bonus_row = _g.Generation(
                    id=bonus_generation_id,
                    message_id=message_id,
                    user_id=user_id,
                    action=action,
                    model=model,
                    prompt=prompt,
                    size_requested=size_requested,
                    aspect_ratio=aspect_ratio,
                    input_image_ids=list(input_image_ids),
                    primary_input_image_id=primary_input_image_id,
                    upstream_request=bonus_upstream_request,
                    status=_g.GenerationStatus.SUCCEEDED.value,
                    progress_stage=_g.GenerationStage.FINALIZING.value,
                    attempt=0,
                    idempotency_key=bonus_idempotency_key,
                    started_at=now,
                    finished_at=now,
                    upstream_pixels=width * height,
                )
                session.add(bonus_row)

                image_row = _g.Image(
                    id=image_id,
                    user_id=user_id,
                    owner_generation_id=bonus_generation_id,
                    source=_g.ImageSource.GENERATED.value,
                    parent_image_id=(
                        primary_input_image_id
                        if action == _g.GenerationAction.EDIT.value
                        else None
                    ),
                    storage_key=key_orig,
                    mime=orig_mime,
                    width=width,
                    height=height,
                    size_bytes=len(raw_image),
                    sha256=sha,
                    blurhash=blurhash_str,
                    visibility="private",
                    metadata_jsonb=image_metadata,
                )
                session.add(image_row)
                session.add(
                    _g.ImageVariant(
                        image_id=image_id,
                        kind="display2048",
                        storage_key=key_display,
                        width=display_size[0],
                        height=display_size[1],
                    )
                )
                session.add(
                    _g.ImageVariant(
                        image_id=image_id,
                        kind="preview1024",
                        storage_key=key_preview,
                        width=preview_size[0],
                        height=preview_size[1],
                    )
                )
                session.add(
                    _g.ImageVariant(
                        image_id=image_id,
                        kind="thumb256",
                        storage_key=key_thumb,
                        width=thumb_size[0],
                        height=thumb_size[1],
                    )
                )

                message = await session.get(_g.Message, message_id)
                if message is not None:
                    content = dict(message.content or {})
                    images_list = list(content.get("images") or [])
                    images_list.append(
                        {
                            "image_id": image_id,
                            "from_generation_id": bonus_generation_id,
                            "width": width,
                            "height": height,
                            "mime": orig_mime,
                            "url": _g.storage.public_url(key_orig),
                            "display_url": (
                                f"/api/images/{image_id}/variants/display2048"
                            ),
                            "preview_url": (
                                f"/api/images/{image_id}/variants/preview1024"
                            ),
                            "thumb_url": (f"/api/images/{image_id}/variants/thumb256"),
                            "filename": image_metadata.get("suggested_filename"),
                            **result_billing_meta,
                        }
                    )
                    content["images"] = images_list
                    message.content = content

                if record_model_library_candidate:
                    try:
                        await _g._maybe_record_model_library_candidate_image(
                            session=session,
                            user_id=user_id,
                            parent_upstream_request=(parent_upstream_request or {}),
                            bonus_image_id=image_id,
                        )
                    except (TimeoutError, asyncio.CancelledError):
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _g.logger.warning(
                            "model_library candidate hook failed parent=%s err=%s",
                            parent_task_id,
                            exc,
                        )

                if settle_billing:
                    await _g.worker_billing.settle_generation(
                        session,
                        bonus_row,
                        width=width,
                        height=height,
                        image_count=1,
                    )
                attached_delivery = _g._stage_generation_event(
                    session,
                    user_id,
                    channel,
                    _g.EV_GEN_ATTACHED,
                    {
                        "message_id": message_id,
                        "generation_id": bonus_generation_id,
                        "parent_generation_id": parent_task_id,
                        "action": action,
                        "prompt": prompt,
                        "size_requested": size_requested,
                        "aspect_ratio": aspect_ratio,
                        "input_image_ids": list(input_image_ids),
                        "primary_input_image_id": primary_input_image_id,
                        **result_billing_meta,
                    },
                )
                success_delivery = _g._stage_generation_event(
                    session,
                    user_id,
                    channel,
                    _g.EV_GEN_SUCCEEDED,
                    {
                        "generation_id": bonus_generation_id,
                        "message_id": message_id,
                        "images": [
                            {
                                "image_id": image_id,
                                "from_generation_id": bonus_generation_id,
                                "actual_size": f"{width}x{height}",
                                "mime": orig_mime,
                                "url": _g.storage.public_url(key_orig),
                                "display_url": (
                                    f"/api/images/{image_id}/variants/display2048"
                                ),
                                "preview_url": (
                                    f"/api/images/{image_id}/variants/preview1024"
                                ),
                                "thumb_url": (
                                    f"/api/images/{image_id}/variants/thumb256"
                                ),
                                "filename": image_metadata.get("suggested_filename"),
                                **result_billing_meta,
                            }
                        ],
                        "final_size": f"{width}x{height}",
                        **result_billing_meta,
                    },
                )
                await session.commit()
                if settle_billing:
                    await _g.worker_billing.flush_balance_cache_refreshes(session)
    except Exception as exc:  # noqa: BLE001
        _g.logger.warning(
            "%s DB write failed parent=%s err=%r",
            log_label,
            parent_task_id,
            exc,
        )
        return False

    await _g._deliver_generation_events(
        redis,
        [attached_delivery, success_delivery],
    )

    _g.logger.info(
        "%s image done: parent=%s bonus=%s",
        log_label,
        parent_task_id,
        bonus_generation_id,
    )
    return True
