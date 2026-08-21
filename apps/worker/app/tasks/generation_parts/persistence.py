from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from lumen_core.constants import (
    EV_GEN_ATTACHED,
    EV_GEN_SUCCEEDED,
    GenerationAction,
    GenerationStage,
    GenerationStatus,
    ImageSource,
)
from lumen_core.models import (
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
)
from lumen_core.upstream_billing import GENERATION_TAKEOVER_CHECKPOINT_KEY

from ...artifact_commit import (
    ArtifactAdoption,
    ArtifactCommitOutcomeUnknown,
    commit_error_or_default,
    commit_with_adoption_probe,
)
from .active_user_fence import lock_active_generation_user
from .bonus_obligation import (
    BONUS_ARTIFACT_COMMITTED,
    BONUS_ARTIFACT_STATE_KEY,
    BONUS_BILLING_OBLIGATION_KEY,
    bonus_idempotency_key,
)
from .bonus_artifacts import (
    BonusGenerationContext,
    BonusImageArtifact,
    prepare_bonus_artifact,
)
from .errors import StaleGenerationAttempt, TaskCancelled
from .event_delivery import stage_generation_event
from .image_metadata import (
    clean_model_style_tags as clean_model_style_tags,
    maybe_embed_model_image_metadata_bytes as maybe_embed_model_image_metadata_bytes,
    model_image_metadata_from_request as model_image_metadata_from_request,
)
from .services import RunGenerationDeps


logger = logging.getLogger(__name__)


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


async def handle_dual_race_bonus_image(
    context: BonusGenerationContext,
) -> bool:
    """Persist and publish a separately billed bonus generation."""
    artifact = await prepare_bonus_artifact(context)
    if artifact is None:
        return False
    if context.bonus_generation_id is not None:
        adoption = await _probe_bonus_generation_adoption(context, artifact)
        if adoption is ArtifactAdoption.ADOPTED:
            return bool(
                not context.settle_billing
                or await _settle_bonus_billing(context, artifact)
            )
        if adoption is ArtifactAdoption.UNKNOWN:
            raise ArtifactCommitOutcomeUnknown(
                "bonus generation artifact conflicts with durable identity "
                f"parent={context.parent_task_id} "
                f"bonus={artifact.bonus_generation_id}"
            )
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
    if not await _deliver_bonus_events(context, deliveries):
        return False
    logger.info(
        "%s image done: parent=%s bonus=%s",
        context.log_label,
        context.parent_task_id,
        artifact.bonus_generation_id,
    )
    return True


async def _write_bonus_files(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> list[str] | None:
    try:
        async with context.services.store.session() as session:
            if not await _lock_active_bonus_user(
                session,
                context,
                boundary="storage",
            ):
                return None
            await _lock_current_bonus_parent(session, context)
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


async def _lock_active_bonus_user(
    session: Any,
    context: BonusGenerationContext,
    *,
    boundary: str,
) -> bool:
    if await lock_active_generation_user(session, user_id=context.user_id):
        return True
    logger.info(
        "%s blocked by inactive user parent=%s boundary=%s",
        context.log_label,
        context.parent_task_id,
        boundary,
    )
    return False


async def _lock_current_bonus_parent(
    session: Any,
    context: BonusGenerationContext,
) -> Generation:
    parent = await session.get(
        Generation,
        context.parent_task_id,
        with_for_update=True,
    )
    if parent is None:
        raise LookupError(f"parent generation missing: {context.parent_task_id}")
    if getattr(parent, "user_id", None) != context.user_id:
        raise StaleGenerationAttempt(
            f"bonus generation parent user mismatch task={context.parent_task_id}"
        )
    if (
        int(getattr(parent, "execution_epoch", 0) or 0) != context.execution_epoch
        or int(parent.attempt or 0) != context.attempt
    ):
        raise StaleGenerationAttempt(
            "bonus generation parent execution superseded "
            f"task={context.parent_task_id} "
            f"epoch={context.execution_epoch} attempt={context.attempt}"
        )
    return parent


async def _persist_bonus_generation(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
    created_storage_keys: list[str],
) -> list[Any] | None:
    cleanup_allowed = True
    inactive_user = False
    deliveries: list[Any] = []
    try:
        async with context.services.store.session() as session:
            if not await _lock_active_bonus_user(
                session,
                context,
                boundary="persistence",
            ):
                inactive_user = True
            else:
                await _lock_current_bonus_parent(session, context)
                upstream_request = _bonus_upstream_request(context, artifact)
                bonus_row = await _bonus_generation_for_artifact(
                    session,
                    context,
                    artifact,
                )
                _add_bonus_rows(
                    session,
                    context,
                    artifact,
                    upstream_request,
                    bonus_row=bonus_row,
                )
                await _attach_bonus_image_to_message(session, context, artifact)
                await _record_bonus_model_candidate(
                    session,
                    context,
                    artifact.image_id,
                )
                deliveries = _stage_bonus_events(session, context, artifact)
                commit_result = await commit_with_adoption_probe(
                    session,
                    probe=lambda: _probe_bonus_generation_adoption(
                        context,
                        artifact,
                    ),
                    logger=logger,
                    label=(
                        f"bonus generation artifact parent={context.parent_task_id} "
                        f"bonus={artifact.bonus_generation_id}"
                    ),
                )
                if commit_result.adopted:
                    cleanup_allowed = False
                elif commit_result.outcome is ArtifactAdoption.NOT_ADOPTED:
                    raise commit_error_or_default(
                        commit_result,
                        label=(
                            f"bonus generation artifact {artifact.bonus_generation_id}"
                        ),
                    )
                else:
                    cleanup_allowed = False
                    logger.error(
                        "%s DB commit outcome unknown parent=%s bonus=%s; "
                        "artifacts retained for reconciliation",
                        context.log_label,
                        context.parent_task_id,
                        artifact.bonus_generation_id,
                    )
                    return None
    except asyncio.CancelledError:
        if cleanup_allowed:
            await context.services.artifacts.delete_files(created_storage_keys)
        raise
    except Exception as exc:  # noqa: BLE001
        if cleanup_allowed:
            await context.services.artifacts.delete_files(created_storage_keys)
        logger.warning(
            "%s DB write failed parent=%s err=%r",
            context.log_label,
            context.parent_task_id,
            exc,
        )
        return None
    if inactive_user:
        await context.services.artifacts.delete_files(created_storage_keys)
        return None
    if context.settle_billing and not await _settle_bonus_billing(context, artifact):
        return None
    return deliveries


async def _deliver_bonus_events(
    context: BonusGenerationContext,
    deliveries: list[Any],
) -> bool:
    async with context.services.store.session() as session:
        if not await _lock_active_bonus_user(
            session,
            context,
            boundary="event_delivery",
        ):
            return False
    await context.services.events.deliver_many(context.redis, deliveries)
    return True


async def _probe_bonus_generation_adoption(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> ArtifactAdoption:
    async with context.services.store.session() as session:
        parent = await session.get(
            Generation,
            context.parent_task_id,
            with_for_update=True,
        )
        bonus = await session.get(Generation, artifact.bonus_generation_id)
        image = await session.get(Image, artifact.image_id)
        exact_bonus = _bonus_row_matches_context(bonus, context, artifact)
        exact_image = (
            image is not None
            and image.owner_generation_id == artifact.bonus_generation_id
            and image.user_id == context.user_id
            and image.storage_key == artifact.key_orig
            and image.sha256 == artifact.sha256
        )
        if exact_bonus and exact_image:
            return ArtifactAdoption.ADOPTED
        if image is not None:
            return ArtifactAdoption.UNKNOWN
        if context.require_precreated_generation and _pending_bonus_obligation_matches(
            bonus, context
        ):
            return ArtifactAdoption.NOT_ADOPTED
        if bonus is not None:
            return ArtifactAdoption.UNKNOWN
        if not _parent_matches_context(parent, context):
            return ArtifactAdoption.UNKNOWN
        return ArtifactAdoption.NOT_ADOPTED


def _parent_matches_context(
    parent: Any,
    context: BonusGenerationContext,
) -> bool:
    return bool(
        parent is not None
        and getattr(parent, "user_id", None) == context.user_id
        and int(getattr(parent, "execution_epoch", -1) or 0) == context.execution_epoch
        and int(getattr(parent, "attempt", -1) or 0) == context.attempt
    )


def _pending_bonus_obligation_matches(
    bonus: Any,
    context: BonusGenerationContext,
) -> bool:
    request = (
        bonus.upstream_request
        if isinstance(getattr(bonus, "upstream_request", None), dict)
        else {}
    )
    return bool(
        bonus is not None
        and getattr(bonus, "user_id", None) == context.user_id
        and getattr(bonus, "message_id", None) == context.message_id
        and request.get(BONUS_BILLING_OBLIGATION_KEY) is True
        and _bonus_request_matches_context(request, context)
    )


def _bonus_row_matches_context(
    bonus: Any,
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> bool:
    if (
        bonus is None
        or str(getattr(bonus, "id", "")) != artifact.bonus_generation_id
        or getattr(bonus, "status", None) != GenerationStatus.SUCCEEDED.value
        or getattr(bonus, "user_id", None) != context.user_id
        or getattr(bonus, "message_id", None) != context.message_id
    ):
        return False
    request = (
        bonus.upstream_request
        if isinstance(getattr(bonus, "upstream_request", None), dict)
        else {}
    )
    return _bonus_request_matches_context(request, context)


def _bonus_request_matches_context(
    request: dict[str, Any],
    context: BonusGenerationContext,
) -> bool:
    parent_id = request.get("parent_generation_id") or request.get(
        "batch_parent_generation_id"
    )
    expected_policy = (
        context.billing_meta.get("billing_policy")
        if isinstance(context.billing_meta, dict)
        else None
    )
    if expected_policy and request.get("billing_policy") != expected_policy:
        return False
    for key in ("batch_parent_generation_id", "batch_index", "batch_count"):
        expected = (context.extra_upstream_fields or {}).get(key)
        if expected is not None and request.get(key) != expected:
            return False
    return bool(
        parent_id == context.parent_task_id
        and _stored_int(
            request.get("parent_execution_epoch"),
            default=-1,
        )
        == context.execution_epoch
        and _stored_int(
            request.get("parent_attempt"),
            default=-1,
        )
        == _context_source_attempt(context)
    )


def _context_source_attempt(context: BonusGenerationContext) -> int:
    return max(1, int(context.source_attempt or context.attempt))


def _stored_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _settle_bonus_billing(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> bool:
    """bonus 图的结算单独成事务，且必须排在行落盘之后（审计 D-1）。

    以前 settle 和插入行共用一个事务：commit 之前的任何异常都会把刚写好的
    钱包流水一起回滚，而上游那张图早已产出、也早已计过费——平台等于替用户
    吸收了这笔上游成本，正是纯转嫁要杜绝的。审计给的另一个选项「异常时补偿
    性调用 release_generation」是退款，方向相反，不采纳。
    拆开之后行先落盘，结算即便失败也只留下一条「SUCCEEDED 但没有 settle
    流水」的 generation，可被对账捡起来重扣，不会静默变成免费图。
    异常时返回 False 而不是吞掉：调用方据此中止「已完成」流程并有机会
    重试（重试时 adoption 探针识别已落盘的行，settle 会再次尝试）；
    对账兜底仍然有效。
    """
    try:
        async with context.services.store.session() as session:
            if not await _lock_active_bonus_user(
                session,
                context,
                boundary="billing",
            ):
                return False
            bonus_row = await session.get(
                Generation,
                artifact.bonus_generation_id,
                with_for_update=True,
            )
            if bonus_row is None:
                raise LookupError(
                    f"bonus generation missing after commit: "
                    f"{artifact.bonus_generation_id}"
                )
            if getattr(bonus_row, "user_id", None) != context.user_id:
                raise StaleGenerationAttempt(
                    f"bonus generation user mismatch: {artifact.bonus_generation_id}"
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
        return False
    return True


def _bonus_upstream_request(
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> dict[str, Any]:
    request = dict(context.parent_upstream_request or {})
    request.pop(GENERATION_TAKEOVER_CHECKPOINT_KEY, None)
    request.update(context.image_request_options)
    request.update(
        {
            "size_actual": f"{artifact.width}x{artifact.height}",
            "mime": artifact.orig_mime,
            **artifact.billing_meta,
            BONUS_BILLING_OBLIGATION_KEY: True,
            BONUS_ARTIFACT_STATE_KEY: BONUS_ARTIFACT_COMMITTED,
            "parent_generation_id": context.parent_task_id,
            "parent_execution_epoch": context.execution_epoch,
            "parent_attempt": _context_source_attempt(context),
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
        "revised_prompt": context.revised_prompt,
    }
    for key, value in optional.items():
        if value is not None:
            request[key] = value
def _add_bonus_rows(
    session: Any,
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
    upstream_request: dict[str, Any],
    *,
    bonus_row: Any | None = None,
) -> Any:
    now = datetime.now(timezone.utc)
    if bonus_row is None:
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
    else:
        bonus_row.upstream_request = upstream_request
        bonus_row.finished_at = now
        bonus_row.upstream_pixels = artifact.width * artifact.height
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
    return bonus_idempotency_key(
        context.parent_idempotency_key,
        context.idempotency_suffix,
    )


async def _bonus_generation_for_artifact(
    session: Any,
    context: BonusGenerationContext,
    artifact: BonusImageArtifact,
) -> Any | None:
    if context.bonus_generation_id is None:
        return None
    bonus = await session.get(
        Generation,
        context.bonus_generation_id,
        with_for_update=True,
    )
    if bonus is None:
        if context.require_precreated_generation:
            raise LookupError(
                f"precreated bonus generation missing: {context.bonus_generation_id}"
            )
        return None
    request = (
        bonus.upstream_request
        if isinstance(getattr(bonus, "upstream_request", None), dict)
        else {}
    )
    if (
        str(bonus.id) != artifact.bonus_generation_id
        or getattr(bonus, "user_id", None) != context.user_id
        or request.get(BONUS_BILLING_OBLIGATION_KEY) is not True
        or not _bonus_request_matches_context(request, context)
    ):
        raise StaleGenerationAttempt(
            f"precreated bonus generation conflict: {context.bonus_generation_id}"
        )
    return bonus


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
