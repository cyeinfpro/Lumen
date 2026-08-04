from __future__ import annotations

import binascii
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, AsyncContextManager

from ...artifact_commit import (
    ArtifactAdoption,
    commit_error_or_default,
    commit_with_adoption_probe,
    rollback_artifact_transaction,
)
from ...outbox.contracts import PendingOutboxDelivery
from .image_storage_persistence import (
    COMPLETION_IMAGE_EVENT_METADATA_KEY,
    COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY,
    COMPLETION_IMAGE_EVENT_PUBLISHED_KEY,
    CompletionToolImageKeys,
    CompletionToolImageMetadata,
    CompletionToolImageVariantSizes,
    CompletionToolImageWrite,
    PreparedCompletionToolImage,
    stage_completion_tool_image,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompletionToolImageBudget:
    reserve: Callable[..., Awaitable[int]]


@dataclass(frozen=True, slots=True)
class CompletionToolImageCodec:
    decode: Callable[[str], bytes]
    format_and_meta: Callable[[bytes], tuple[Any, ...]]
    sha256: Callable[[bytes], str]
    upstream_error_type: Any
    bad_response_error_code: str


@dataclass(frozen=True, slots=True)
class CompletionToolImageRepository:
    session_factory: Callable[[], Any]
    new_id: Callable[[], str]
    acquire_task_lock: Callable[[Any, str], Awaitable[None]]
    completion_model: Any
    superseded_error_type: type[BaseException]
    record_usage: Callable[..., Awaitable[None]]
    image_model: Any
    image_variant_model: Any
    message_model: Any
    public_url: Callable[[str], str]


@dataclass(frozen=True, slots=True)
class CompletionToolImageStorage:
    write_files: Callable[
        [list[tuple[str, bytes]]],
        Awaitable[list[str]],
    ]
    cleanup_on_error: Callable[[list[str]], AsyncContextManager[None]]
    delete_files: Callable[[list[str]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CompletionToolImageEvents:
    image_event: str
    stage: Callable[..., PendingOutboxDelivery] | None = None
    deliver: (
        Callable[[Any, list[PendingOutboxDelivery]], Awaitable[None]] | None
    ) = None
    outbox_model: Any = None
    publish: Callable[..., Awaitable[None]] | None = None


@dataclass(slots=True)
class CompletionToolImageEventContext:
    channel: str
    attempt: int
    delivery: PendingOutboxDelivery | None = None


def _tool_image_delivery_payload(
    *,
    image_id: str,
    task_id: str,
    attempt_epoch: int,
    execution_epoch: int,
    mime: str,
    key_orig: str,
    revised_prompt: str | None,
    public_url: Callable[[str], str],
) -> dict[str, Any]:
    return {
        "image_id": image_id,
        "from_completion_id": task_id,
        "completion_execution_epoch": execution_epoch,
        "completion_attempt_epoch": attempt_epoch,
        "mime": mime,
        "url": public_url(key_orig),
        "display_url": f"/api/images/{image_id}/variants/display2048",
        "preview_url": f"/api/images/{image_id}/variants/preview1024",
        "thumb_url": f"/api/images/{image_id}/variants/thumb256",
        **({"revised_prompt": revised_prompt} if revised_prompt else {}),
    }


@dataclass(frozen=True, slots=True)
class CompletionToolImageService:
    budget: CompletionToolImageBudget
    codec: CompletionToolImageCodec
    repository: CompletionToolImageRepository
    storage: CompletionToolImageStorage
    events: CompletionToolImageEvents

    async def store_tool_image(
        self,
        *,
        session: Any,
        task_id: str,
        attempt_epoch: int,
        execution_epoch: int,
        user_id: str,
        message_id: str,
        raw_image: bytes,
        revised_prompt: str | None,
        billing_budget_micro: int,
        event_context: CompletionToolImageEventContext | None = None,
        cleanup_created_files_on_failure: bool = True,
    ) -> dict[str, Any]:
        (
            orig_ext,
            orig_mime,
            width,
            height,
            blurhash_str,
            display_bytes,
            display_size,
            preview_bytes,
            preview_size,
            thumb_bytes,
            thumb_size,
        ) = self.codec.format_and_meta(raw_image)
        image_id = self.repository.new_id()
        sha = self.codec.sha256(raw_image)
        key_prefix = (
            f"u/{user_id}/completion-tools/{task_id}/"
            f"executions/{execution_epoch}/attempts/{attempt_epoch}/{image_id}"
        )
        key_orig = f"{key_prefix}/orig.{orig_ext}"
        key_display = f"{key_prefix}/display2048.webp"
        key_preview = f"{key_prefix}/preview1024.webp"
        key_thumb = f"{key_prefix}/thumb256.jpg"
        prepared = PreparedCompletionToolImage(
            image_id=image_id,
            metadata=CompletionToolImageMetadata(
                extension=orig_ext,
                mime=orig_mime,
                width=width,
                height=height,
                size_bytes=len(raw_image),
                sha256=sha,
                blurhash=blurhash_str,
            ),
            keys=CompletionToolImageKeys(
                original=key_orig,
                display=key_display,
                preview=key_preview,
                thumbnail=key_thumb,
            ),
            variant_sizes=CompletionToolImageVariantSizes(
                display=display_size,
                preview=preview_size,
                thumbnail=thumb_size,
            ),
            delivery_payload=_tool_image_delivery_payload(
                image_id=image_id,
                task_id=task_id,
                attempt_epoch=attempt_epoch,
                execution_epoch=execution_epoch,
                mime=orig_mime,
                key_orig=key_orig,
                revised_prompt=revised_prompt,
                public_url=self.repository.public_url,
            ),
        )
        created_storage_keys = await self.storage.write_files(
            [
                (key_orig, raw_image),
                (key_display, display_bytes),
                (key_preview, preview_bytes),
                (key_thumb, thumb_bytes),
            ]
        )
        cleanup_allowed = True
        non_adoption_confirmed = False
        try:
            image_payload = await stage_completion_tool_image(
                self,
                session,
                prepared=prepared,
                write=CompletionToolImageWrite(
                    task_id=task_id,
                    attempt_epoch=attempt_epoch,
                    execution_epoch=execution_epoch,
                    user_id=user_id,
                    message_id=message_id,
                    revised_prompt=revised_prompt,
                    billing_budget_micro=billing_budget_micro,
                    event_context=event_context,
                ),
            )
            commit_result = await commit_with_adoption_probe(
                session,
                probe=lambda: self._probe_tool_image_adoption(
                    task_id=task_id,
                    attempt_epoch=attempt_epoch,
                    execution_epoch=execution_epoch,
                    image_id=image_id,
                    key_orig=key_orig,
                    sha=sha,
                ),
                logger=logger,
                label=(
                    f"completion tool image task={task_id} "
                    f"epoch={execution_epoch} attempt={attempt_epoch} image={image_id}"
                ),
            )
            if commit_result.adopted:
                cleanup_allowed = False
            elif commit_result.outcome is ArtifactAdoption.NOT_ADOPTED:
                non_adoption_confirmed = True
                raise commit_error_or_default(
                    commit_result,
                    label=f"completion tool image {image_id}",
                )
            else:
                cleanup_allowed = False
                unknown = self.repository.superseded_error_type(
                    f"completion tool image commit outcome unknown task={task_id} "
                    f"execution_epoch={execution_epoch} "
                    f"attempt_epoch={attempt_epoch} image={image_id}"
                )
                if commit_result.commit_error is not None:
                    raise unknown from commit_result.commit_error
                raise unknown
            return image_payload
        except BaseException:
            if cleanup_allowed:
                rolled_back = non_adoption_confirmed or (
                    await rollback_artifact_transaction(
                        session,
                        logger=logger,
                        label=(
                            f"completion tool image task={task_id} "
                            f"epoch={execution_epoch} attempt={attempt_epoch}"
                        ),
                    )
                )
                if rolled_back and cleanup_created_files_on_failure:
                    await self.storage.delete_files(created_storage_keys)
                elif not rolled_back:
                    logger.error(
                        "completion tool image cleanup deferred because rollback "
                        "was not confirmed task=%s epoch=%s attempt=%s image=%s",
                        task_id,
                        execution_epoch,
                        attempt_epoch,
                        image_id,
                    )
            raise

    async def deliver_tool_image_event(
        self,
        *,
        redis: Any,
        event_context: CompletionToolImageEventContext,
        image_payload: dict[str, Any],
        task_id: str,
    ) -> None:
        if event_context.delivery is None:
            return
        try:
            if self.events.deliver is None:
                raise RuntimeError(
                    "completion image durable event delivery is not configured"
                )
            await self.events.deliver(redis, [event_context.delivery])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "completion image outbox fast path failed task=%s image=%s err=%s",
                task_id,
                image_payload.get("image_id"),
                exc,
            )

    async def _probe_tool_image_adoption(
        self,
        *,
        task_id: str,
        attempt_epoch: int,
        execution_epoch: int,
        image_id: str,
        key_orig: str,
        sha: str,
    ) -> ArtifactAdoption:
        async with self.repository.session_factory() as session:
            await self.repository.acquire_task_lock(session, task_id)
            completion = await session.get(
                self.repository.completion_model,
                task_id,
                with_for_update=True,
            )
            image = await session.get(self.repository.image_model, image_id)
            if image is not None:
                metadata = (
                    image.metadata_jsonb
                    if isinstance(image.metadata_jsonb, dict)
                    else {}
                )
                exact = (
                    completion is not None
                    and completion.attempt == attempt_epoch
                    and int(getattr(completion, "execution_epoch", 0) or 0)
                    == execution_epoch
                    and image.user_id == getattr(completion, "user_id", None)
                    and image.storage_key == key_orig
                    and image.sha256 == sha
                    and metadata.get("completion_id") == task_id
                    and metadata.get("completion_attempt_epoch") == attempt_epoch
                    and metadata.get("completion_execution_epoch") == execution_epoch
                )
                event_id = metadata.get(COMPLETION_IMAGE_EVENT_METADATA_KEY)
                if exact and isinstance(event_id, str) and event_id:
                    exact = (
                        await session.get(self.events.outbox_model, event_id)
                    ) is not None
                return ArtifactAdoption.ADOPTED if exact else ArtifactAdoption.UNKNOWN
            return ArtifactAdoption.NOT_ADOPTED

    async def store_and_publish_tool_image(
        self,
        *,
        redis: Any,
        user_id: str,
        channel: str,
        task_id: str,
        message_id: str,
        attempt: int,
        attempt_epoch: int,
        execution_epoch: int,
        b64_image: str,
        revised_prompt: str | None,
        reserved_tool_image_micro: int = 0,
    ) -> tuple[dict[str, Any] | None, int]:
        budget_reserved_micro = await self.budget.reserve(
            user_id=user_id,
            task_id=task_id,
            reserved_micro=reserved_tool_image_micro,
        )
        event_context = CompletionToolImageEventContext(
            channel=channel,
            attempt=attempt,
        )
        try:
            raw_image = self.codec.decode(b64_image)
        except binascii.Error as exc:
            raise self.codec.upstream_error_type(
                f"bad base64 from image_generation tool: {exc}",
                error_code=self.codec.bad_response_error_code,
                status_code=200,
            ) from exc
        async with self.repository.session_factory() as session:
            image_payload = await self.store_tool_image(
                session=session,
                task_id=task_id,
                attempt_epoch=attempt_epoch,
                execution_epoch=execution_epoch,
                user_id=user_id,
                message_id=message_id,
                raw_image=raw_image,
                revised_prompt=revised_prompt,
                billing_budget_micro=budget_reserved_micro,
                event_context=event_context,
            )

        await self.deliver_tool_image_event(
            redis=redis,
            event_context=event_context,
            image_payload=image_payload,
            task_id=task_id,
        )
        return image_payload, budget_reserved_micro


__all__ = [
    "CompletionToolImageBudget",
    "CompletionToolImageCodec",
    "CompletionToolImageEventContext",
    "CompletionToolImageEvents",
    "CompletionToolImageRepository",
    "CompletionToolImageService",
    "CompletionToolImageStorage",
    "COMPLETION_IMAGE_EVENT_METADATA_KEY",
    "COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY",
    "COMPLETION_IMAGE_EVENT_PUBLISHED_KEY",
]
