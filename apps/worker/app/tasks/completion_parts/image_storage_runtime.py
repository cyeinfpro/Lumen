from __future__ import annotations

import binascii
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, AsyncContextManager

from lumen_core.constants import ImageSource

from ...artifact_commit import (
    ArtifactAdoption,
    commit_error_or_default,
    commit_with_adoption_probe,
    rollback_artifact_transaction,
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
    publish: Callable[..., Awaitable[None]]
    image_event: str


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
        delivery_payload = _tool_image_delivery_payload(
            image_id=image_id,
            task_id=task_id,
            attempt_epoch=attempt_epoch,
            execution_epoch=execution_epoch,
            mime=orig_mime,
            key_orig=key_orig,
            revised_prompt=revised_prompt,
            public_url=self.repository.public_url,
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
            image = self.repository.image_model(
                id=image_id,
                user_id=user_id,
                owner_generation_id=None,
                source=ImageSource.GENERATED,
                parent_image_id=None,
                storage_key=key_orig,
                mime=orig_mime,
                width=width,
                height=height,
                size_bytes=len(raw_image),
                sha256=sha,
                blurhash=blurhash_str,
                visibility="private",
                metadata_jsonb={
                    "source": "completion_tool",
                    "completion_id": task_id,
                    "completion_attempt_epoch": attempt_epoch,
                    "completion_execution_epoch": execution_epoch,
                    **({"revised_prompt": revised_prompt} if revised_prompt else {}),
                },
            )
            session.add(image)
            session.add(
                self.repository.image_variant_model(
                    image_id=image_id,
                    kind="display2048",
                    storage_key=key_display,
                    width=display_size[0],
                    height=display_size[1],
                )
            )
            session.add(
                self.repository.image_variant_model(
                    image_id=image_id,
                    kind="preview1024",
                    storage_key=key_preview,
                    width=preview_size[0],
                    height=preview_size[1],
                )
            )
            session.add(
                self.repository.image_variant_model(
                    image_id=image_id,
                    kind="thumb256",
                    storage_key=key_thumb,
                    width=thumb_size[0],
                    height=thumb_size[1],
                )
            )

            message = await session.get(self.repository.message_model, message_id)
            if message is not None:
                content = dict(message.content or {})
                images_list = list(content.get("images") or [])
                images_list.append(
                    {
                        **delivery_payload,
                        "width": width,
                        "height": height,
                    }
                )
                content["images"] = images_list
                message.content = content

            await self.repository.record_usage(
                session=session,
                task_id=task_id,
                attempt_epoch=attempt_epoch,
                execution_epoch=execution_epoch,
                budget_micro=billing_budget_micro,
            )
            image_payload = {
                **delivery_payload,
                "actual_size": f"{width}x{height}",
            }
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
                if rolled_back:
                    await self.storage.delete_files(created_storage_keys)
                else:
                    logger.error(
                        "completion tool image cleanup deferred because rollback "
                        "was not confirmed task=%s epoch=%s attempt=%s image=%s",
                        task_id,
                        execution_epoch,
                        attempt_epoch,
                        image_id,
                    )
            raise

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
            )

        await self.events.publish(
            redis,
            user_id,
            channel,
            self.events.image_event,
            {
                "completion_id": task_id,
                "message_id": message_id,
                "attempt": attempt,
                "attempt_epoch": attempt_epoch,
                "execution_epoch": execution_epoch,
                "images": [image_payload],
            },
        )
        return image_payload, budget_reserved_micro


__all__ = [
    "CompletionToolImageBudget",
    "CompletionToolImageCodec",
    "CompletionToolImageEvents",
    "CompletionToolImageRepository",
    "CompletionToolImageService",
    "CompletionToolImageStorage",
]
