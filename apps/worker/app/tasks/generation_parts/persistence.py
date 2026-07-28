from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PIL import Image as PILImage
from sqlalchemy import select

from lumen_core.constants import (
    EV_GEN_ATTACHED,
    EV_GEN_SUCCEEDED,
    GenerationAction,
    GenerationStage,
    GenerationStatus,
    ImageSource,
)
from lumen_core.model_image_metadata import (
    build_model_image_metadata,
    model_image_filename,
    save_image_with_model_metadata,
)
from lumen_core.models import (
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    new_uuid7,
)

from ...upstream_parts import (
    GeneratedPayloadInput,
    cleanup_owned_generated_payload,
    materialize_generated_payload,
)
from .errors import TaskCancelled
from .event_delivery import stage_generation_event
from .image_artifact_contracts import sha256
from .services import RunGenerationDeps


logger = logging.getLogger(__name__)


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
    style_tags = clean_model_style_tags(
        request_value.get("workflow_model_library_style_tags") or []
    )
    payload = build_model_image_metadata(
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
        "suggested_filename": model_image_filename(
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
    save_image_with_model_metadata(
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
            select(Image)
            .where(
                Image.owner_generation_id == task_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
            .with_for_update()
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if getattr(row, "user_id", None) != user_id:
        logger.error(
            "short-circuit guard: image %s user mismatch expect=%s got=%s — ignoring",
            getattr(row, "id", "?"),
            user_id,
            getattr(row, "user_id", None),
        )
        return None
    if getattr(row, "source", None) != ImageSource.GENERATED.value:
        logger.error(
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
        logger.error(
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
        select(Conversation.id)
        .join(
            Message,
            Message.conversation_id == Conversation.id,
        )
        .where(
            Message.id == message_id,
            Message.deleted_at.is_(None),
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    )
    if lock:
        statement = statement.with_for_update(of=Conversation)
    conversation_id = (await session.execute(statement)).scalar_one_or_none()
    if conversation_id is None:
        raise TaskCancelled("conversation or message was deleted")
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
    services: RunGenerationDeps,
) -> None:
    for key, result in zip(keys, results, strict=False):
        if isinstance(result, BaseException):
            logger.warning(
                "storage cleanup failed key=%s err=%s",
                key,
                result,
            )


async def delete_storage_keys(keys: list[str], *, services: RunGenerationDeps) -> None:
    await services.artifacts.delete_files(keys)


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
    services: RunGenerationDeps,
) -> list[str]:
    return await services.artifacts.write_files(files)


@asynccontextmanager
async def cleanup_storage_on_error(
    keys: list[str],
    services: RunGenerationDeps,
) -> AsyncIterator[None]:
    try:
        yield
    except BaseException:
        cleanup = asyncio.ensure_future(services.artifacts.delete_files(keys))
        await _wait_for_storage_task(cleanup)
        raise


@dataclass(slots=True)
class BonusGenerationContext:
    services: RunGenerationDeps
    redis: Any
    user_id: str
    channel: str
    parent_task_id: str
    parent_idempotency_key: str
    parent_upstream_request: dict[str, Any] | None
    message_id: str
    action: str
    model: str
    prompt: str
    size_requested: str
    aspect_ratio: str
    input_image_ids: list[str]
    primary_input_image_id: str | None
    references: list[tuple[str, bytes]]
    image_request_options: dict[str, Any]
    b64_result: GeneratedPayloadInput
    revised_prompt: str | None
    upstream_provider: str | None
    upstream_actual_route: str | None
    upstream_actual_source: str | None
    upstream_actual_endpoint: str | None
    billing_meta: dict[str, Any] | None
    idempotency_suffix: str
    extra_upstream_fields: dict[str, Any] | None
    record_model_library_candidate: bool
    settle_billing: bool
    log_label: str


@dataclass(slots=True)
class BonusImageArtifact:
    bonus_generation_id: str
    image_id: str
    raw_image: bytes
    sha256: str
    orig_mime: str
    width: int
    height: int
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
    image_metadata: dict[str, Any]
    billing_meta: dict[str, Any]
    key_orig: str
    key_display: str
    key_preview: str
    key_thumb: str


async def handle_dual_race_bonus_image(
    context: BonusGenerationContext,
) -> bool:
    """Persist and publish a separately billed bonus generation."""
    artifact = await _prepare_bonus_artifact(context)
    if artifact is None:
        return False
    created_keys = await _write_bonus_files(context, artifact)
    if created_keys is None:
        return False
    deliveries = await _persist_bonus_generation(
        context,
        artifact,
        created_keys,
    )
    if deliveries is None:
        return False
    await context.services.events.deliver_many(context.redis, deliveries)
    logger.info(
        "%s image done: parent=%s bonus=%s",
        context.log_label,
        context.parent_task_id,
        artifact.bonus_generation_id,
    )
    return True


async def _prepare_bonus_artifact(
    context: BonusGenerationContext,
) -> BonusImageArtifact | None:
    if context.b64_result is None:
        return None
    raw_image = _decode_bonus_image(context)
    if raw_image is None or _bonus_sha_echoed(context, raw_image):
        return None
    processed = await _postprocess_bonus_image(context, raw_image)
    if processed is None:
        return None
    billing_meta = _bonus_billing_meta(context)
    if billing_meta is None:
        return None
    return _build_bonus_artifact(context, processed, billing_meta)


def _decode_bonus_image(
    context: BonusGenerationContext,
) -> bytes | None:
    try:
        return materialize_generated_payload(context.b64_result)
    except (TypeError, ValueError):
        logger.warning(
            "%s image payload decode failed parent=%s",
            context.log_label,
            context.parent_task_id,
        )
        return None
    finally:
        if not isinstance(context.b64_result, str):
            cleanup_owned_generated_payload(context.b64_result)


def _bonus_sha_echoed(
    context: BonusGenerationContext,
    raw_image: bytes,
) -> bool:
    # 只要这次请求带了参考图，就必须做 sha 回声检测，不能只对 EDIT 生效：
    # 回声图会被当成 bonus image 建行并**单独结算**，把上游原样退回的输入图
    # 计费给用户。判据用「有没有参考图」而不是「action 是不是 EDIT」，新增
    # 带参考图的 action 时自动继承这道防线。
    if not context.references:
        return False
    sha = sha256(raw_image)
    echoed = any(sha == reference_sha for reference_sha, _raw in context.references)
    if echoed:
        logger.info(
            "%s sha echoed reference parent=%s; skip",
            context.log_label,
            context.parent_task_id,
        )
    return echoed


async def _postprocess_bonus_image(
    context: BonusGenerationContext,
    raw_image: bytes,
) -> Any | None:
    try:
        return await context.services.provider.postprocess(
            raw_image,
            prompt=context.prompt,
            transparent_requested=(
                context.image_request_options.get("background") == "transparent"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s pillow decode failed parent=%s err=%r",
            context.log_label,
            context.parent_task_id,
            exc,
        )
        return None


def _bonus_billing_meta(
    context: BonusGenerationContext,
) -> dict[str, Any] | None:
    result = (
        dict(context.billing_meta)
        if context.billing_meta is not None
        else {
            "is_dual_race_bonus": True,
            "billing_free": False,
            "billing_label": "billable",
            "billing_policy": "dual_race_loser_settled_separately",
        }
    )
    if result.get("billing_free") is not True and not context.settle_billing:
        logger.warning(
            "%s missing settle_billing for billable image parent=%s",
            context.log_label,
            context.parent_task_id,
        )
        return None
    return result


def _build_bonus_artifact(
    context: BonusGenerationContext,
    processed: Any,
    billing_meta: dict[str, Any],
) -> BonusImageArtifact:
    bonus_generation_id = new_uuid7()
    image_id = new_uuid7()
    extension, mime = _bonus_format(processed.orig_format)
    model_metadata = model_image_metadata_from_request(
        image_id=image_id,
        mime=mime,
        request=context.parent_upstream_request,
        prompt=context.prompt,
    )
    raw_image, sha = _embed_bonus_metadata(
        context,
        processed.raw_image,
        processed.sha256,
        processed.orig_format,
        model_metadata,
    )
    return BonusImageArtifact(
        bonus_generation_id=bonus_generation_id,
        image_id=image_id,
        raw_image=raw_image,
        sha256=sha,
        orig_mime=mime,
        width=processed.width,
        height=processed.height,
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
        image_metadata={**model_metadata, **billing_meta},
        billing_meta=billing_meta,
        key_orig=(f"u/{context.user_id}/g/{bonus_generation_id}/orig.{extension}"),
        key_display=(f"u/{context.user_id}/g/{bonus_generation_id}/display2048.webp"),
        key_preview=(f"u/{context.user_id}/g/{bonus_generation_id}/preview1024.webp"),
        key_thumb=(f"u/{context.user_id}/g/{bonus_generation_id}/thumb256.jpg"),
    )


def _bonus_format(orig_format: str) -> tuple[str, str]:
    return (
        {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}[orig_format],
        {
            "PNG": "image/png",
            "WEBP": "image/webp",
            "JPEG": "image/jpeg",
        }[orig_format],
    )


def _embed_bonus_metadata(
    context: BonusGenerationContext,
    raw_image: bytes,
    sha: str,
    orig_format: str,
    model_metadata: dict[str, Any],
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
            "%s model metadata embed skipped parent=%s err=%s",
            context.log_label,
            context.parent_task_id,
            exc,
        )
        return raw_image, sha


async def _write_bonus_files(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> list[str] | None:
    try:
        return await context.services.artifacts.write_files(
            [
                (artifact.key_orig, artifact.raw_image),
                (artifact.key_display, artifact.display_bytes),
                (artifact.key_preview, artifact.preview_bytes),
                (artifact.key_thumb, artifact.thumb_bytes),
            ]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s storage write failed parent=%s err=%r",
            context.log_label,
            context.parent_task_id,
            exc,
        )
        return None


async def _persist_bonus_generation(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
    created_storage_keys: list[str],
) -> list[Any] | None:
    try:
        async with context.services.artifacts.cleanup_on_error(created_storage_keys):
            async with context.services.store.session() as session:
                upstream_request = _bonus_upstream_request(context, artifact)
                _add_bonus_rows(
                    session,
                    context,
                    artifact,
                    upstream_request,
                )
                await _attach_bonus_image_to_message(
                    session,
                    context,
                    artifact,
                )
                await _record_bonus_model_candidate(
                    session,
                    context,
                    artifact.image_id,
                )
                deliveries = _stage_bonus_events(
                    session,
                    context,
                    artifact,
                )
                await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s DB write failed parent=%s err=%r",
            context.log_label,
            context.parent_task_id,
            exc,
        )
        return None
    if context.settle_billing:
        await _settle_bonus_billing(context, artifact)
    return deliveries


async def _settle_bonus_billing(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> None:
    """bonus 图的结算单独成事务，且必须排在行落盘之后（审计 D-1）。

    以前 settle 和插入行共用一个事务：commit 之前的任何异常都会把刚写好的
    钱包流水一起回滚，而上游那张图早已产出、也早已计过费——平台等于替用户
    吸收了这笔上游成本，正是纯转嫁要杜绝的。审计给的另一个选项「异常时补偿
    性调用 release_generation」是退款，方向相反，不采纳。
    拆开之后行先落盘，结算即便失败也只留下一条「SUCCEEDED 但没有 settle
    流水」的 generation，可被对账捡起来重扣，不会静默变成免费图。
    """
    try:
        async with context.services.store.session() as session:
            bonus_row = await session.get(
                Generation,
                artifact.bonus_generation_id,
            )
            if bonus_row is None:
                raise LookupError(
                    f"bonus generation missing after commit: "
                    f"{artifact.bonus_generation_id}"
                )
            await context.services.billing.settle(
                session,
                bonus_row,
                width=artifact.width,
                height=artifact.height,
                image_count=1,
            )
            await session.commit()
            await context.services.billing.flush_after_commit(session)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "%s bonus settle failed parent=%s bonus=%s err=%r "
            "upstream cost already incurred, pending reconciliation",
            context.log_label,
            context.parent_task_id,
            artifact.bonus_generation_id,
            exc,
        )


def _bonus_upstream_request(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> dict[str, Any]:
    request = dict(context.parent_upstream_request or {})
    request.update(context.image_request_options)
    request.update(
        {
            "size_actual": f"{artifact.width}x{artifact.height}",
            "mime": artifact.orig_mime,
            **artifact.billing_meta,
            "parent_generation_id": context.parent_task_id,
        }
    )
    if context.extra_upstream_fields:
        request.update(context.extra_upstream_fields)
    _apply_bonus_provider_fields(request, context)
    _apply_bonus_optional_fields(request, context, artifact)
    return request


def _apply_bonus_provider_fields(
    request: dict[str, Any],
    context: BonusGenerationContext,
) -> None:
    if context.upstream_provider:
        request["provider"] = context.upstream_provider
        request["actual_provider"] = context.upstream_provider
        request["request_event_provider"] = context.upstream_provider
        return
    request.pop("provider", None)
    request.pop("actual_provider", None)
    request.pop("request_event_provider", None)


def _apply_bonus_optional_fields(
    request: dict[str, Any],
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> None:
    optional = {
        "actual_route": context.upstream_actual_route,
        "actual_source": context.upstream_actual_source,
        "actual_endpoint": context.upstream_actual_endpoint,
        "transparent_qc": artifact.transparent_qc_payload,
        "transparent_pipeline_provider": artifact.transparent_provider,
        "revised_prompt": context.revised_prompt,
    }
    for key, value in optional.items():
        if value is not None:
            request[key] = value
    if artifact.transparent_alpha_recovered:
        request["transparent_alpha_recovered"] = True


def _add_bonus_rows(
    session: Any,
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
    upstream_request: dict[str, Any],
) -> Any:
    now = datetime.now(timezone.utc)
    bonus_row = Generation(
        id=artifact.bonus_generation_id,
        message_id=context.message_id,
        user_id=context.user_id,
        action=context.action,
        model=context.model,
        prompt=context.prompt,
        size_requested=context.size_requested,
        aspect_ratio=context.aspect_ratio,
        input_image_ids=list(context.input_image_ids),
        primary_input_image_id=context.primary_input_image_id,
        upstream_request=upstream_request,
        status=GenerationStatus.SUCCEEDED.value,
        progress_stage=GenerationStage.FINALIZING.value,
        attempt=0,
        idempotency_key=_bonus_idempotency_key(context),
        started_at=now,
        finished_at=now,
        upstream_pixels=artifact.width * artifact.height,
    )
    session.add(bonus_row)
    session.add(
        Image(
            id=artifact.image_id,
            user_id=context.user_id,
            owner_generation_id=artifact.bonus_generation_id,
            source=ImageSource.GENERATED.value,
            parent_image_id=(
                context.primary_input_image_id
                if context.action == GenerationAction.EDIT.value
                else None
            ),
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
    _add_bonus_variants(session, context, artifact)
    return bonus_row


def _bonus_idempotency_key(context: BonusGenerationContext) -> str:
    suffix = context.idempotency_suffix or ":b"
    prefix_limit = max(1, 64 - len(suffix))
    return f"{context.parent_idempotency_key[:prefix_limit]}{suffix}"


def _add_bonus_variants(
    session: Any,
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> None:
    for kind, storage_key, size in (
        ("display2048", artifact.key_display, artifact.display_size),
        ("preview1024", artifact.key_preview, artifact.preview_size),
        ("thumb256", artifact.key_thumb, artifact.thumb_size),
    ):
        session.add(
            ImageVariant(
                image_id=artifact.image_id,
                kind=kind,
                storage_key=storage_key,
                width=size[0],
                height=size[1],
            )
        )


async def _attach_bonus_image_to_message(
    session: Any,
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> None:
    message = await session.get(Message, context.message_id)
    if message is None:
        return
    content = dict(message.content or {})
    images = list(content.get("images") or [])
    images.append(_bonus_image_payload(context, artifact))
    content["images"] = images
    message.content = content


def _bonus_image_payload(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> dict[str, Any]:
    return {
        "image_id": artifact.image_id,
        "from_generation_id": artifact.bonus_generation_id,
        "width": artifact.width,
        "height": artifact.height,
        "mime": artifact.orig_mime,
        "url": context.services.artifacts.public_url(artifact.key_orig),
        "display_url": (f"/api/images/{artifact.image_id}/variants/display2048"),
        "preview_url": (f"/api/images/{artifact.image_id}/variants/preview1024"),
        "thumb_url": f"/api/images/{artifact.image_id}/variants/thumb256",
        "filename": artifact.image_metadata.get("suggested_filename"),
        **artifact.billing_meta,
    }


async def _record_bonus_model_candidate(
    session: Any,
    context: BonusGenerationContext,
    image_id: str,
) -> None:
    if not context.record_model_library_candidate:
        return
    try:
        await context.services.workflows.record_model_library_candidate_image(
            session=session,
            user_id=context.user_id,
            parent_upstream_request=(context.parent_upstream_request or {}),
            bonus_image_id=image_id,
        )
    except (TimeoutError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "model_library candidate hook failed parent=%s err=%s",
            context.parent_task_id,
            exc,
        )


def _stage_bonus_events(
    session: Any,
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> list[Any]:
    attached = stage_generation_event(
        session,
        context.user_id,
        context.channel,
        EV_GEN_ATTACHED,
        {
            "message_id": context.message_id,
            "generation_id": artifact.bonus_generation_id,
            "parent_generation_id": context.parent_task_id,
            "action": context.action,
            "prompt": context.prompt,
            "size_requested": context.size_requested,
            "aspect_ratio": context.aspect_ratio,
            "input_image_ids": list(context.input_image_ids),
            "primary_input_image_id": context.primary_input_image_id,
            **artifact.billing_meta,
        },
    )
    succeeded = stage_generation_event(
        session,
        context.user_id,
        context.channel,
        EV_GEN_SUCCEEDED,
        {
            "generation_id": artifact.bonus_generation_id,
            "message_id": context.message_id,
            "images": [_bonus_success_image_payload(context, artifact)],
            "final_size": f"{artifact.width}x{artifact.height}",
            **artifact.billing_meta,
        },
    )
    return [attached, succeeded]


def _bonus_success_image_payload(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> dict[str, Any]:
    payload = _bonus_image_payload(context, artifact)
    payload.pop("width")
    payload.pop("height")
    payload["actual_size"] = f"{artifact.width}x{artifact.height}"
    return payload
