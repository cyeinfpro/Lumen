from __future__ import annotations

import binascii
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, AsyncContextManager

from lumen_core.constants import ImageSource


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


@dataclass(frozen=True, slots=True)
class CompletionToolImageEvents:
    publish: Callable[..., Awaitable[None]]
    image_event: str


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
        key_prefix = f"u/{user_id}/completion-tools/{task_id}/{image_id}"
        key_orig = f"{key_prefix}/orig.{orig_ext}"
        key_display = f"{key_prefix}/display2048.webp"
        key_preview = f"{key_prefix}/preview1024.webp"
        key_thumb = f"{key_prefix}/thumb256.jpg"

        created_storage_keys = await self.storage.write_files(
            [
                (key_orig, raw_image),
                (key_display, display_bytes),
                (key_preview, preview_bytes),
                (key_thumb, thumb_bytes),
            ]
        )
        async with self.storage.cleanup_on_error(created_storage_keys):
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
                    **(
                        {"revised_prompt": revised_prompt}
                        if revised_prompt
                        else {}
                    ),
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
                        "image_id": image_id,
                        "from_completion_id": task_id,
                        "width": width,
                        "height": height,
                        "mime": orig_mime,
                        "url": self.repository.public_url(key_orig),
                        "display_url": (
                            f"/api/images/{image_id}/variants/display2048"
                        ),
                        "preview_url": (
                            f"/api/images/{image_id}/variants/preview1024"
                        ),
                        "thumb_url": f"/api/images/{image_id}/variants/thumb256",
                        **(
                            {"revised_prompt": revised_prompt}
                            if revised_prompt
                            else {}
                        ),
                    }
                )
                content["images"] = images_list
                message.content = content

            await self.repository.record_usage(
                session=session,
                task_id=task_id,
                attempt_epoch=attempt_epoch,
                budget_micro=billing_budget_micro,
            )
            image_payload = {
                "image_id": image_id,
                "from_completion_id": task_id,
                "actual_size": f"{width}x{height}",
                "mime": orig_mime,
                "url": self.repository.public_url(key_orig),
                "display_url": f"/api/images/{image_id}/variants/display2048",
                "preview_url": f"/api/images/{image_id}/variants/preview1024",
                "thumb_url": f"/api/images/{image_id}/variants/thumb256",
                **(
                    {"revised_prompt": revised_prompt}
                    if revised_prompt
                    else {}
                ),
            }
            await session.commit()
            return image_payload

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
