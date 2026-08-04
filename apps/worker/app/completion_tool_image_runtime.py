"""Production construction for completion tool-image persistence."""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from PIL import Image as PILImage

from lumen_core import billing as billing_core
from lumen_core.constants import (
    EV_COMP_IMAGE,
    CompletionStatus,
    GenerationErrorCode as EC,
)
from lumen_core.model_base import new_uuid7
from lumen_core.model_entities import Completion, Image, ImageVariant, Message, OutboxEvent

from . import billing as worker_billing
from . import completion_billing, runtime_settings
from .db import SessionLocal
from .outbox.delivery import deliver_staged_outbox_events
from .outbox.staging import stage_outbox_event
from .sse_publish import publish_event
from .storage import storage
from .storage_writes import StorageWriteCoordinator
from .tasks.completion_parts import artifact_codec, artifact_storage
from .tasks.completion_parts.default_runtime_parts import (
    persistence_runtime,
    tool_runtime,
)
from .tasks.completion_parts.image_storage_runtime import (
    CompletionToolImageService,
)


logger = logging.getLogger(__name__)
_RUNNING_COMPLETION_STATUSES = (CompletionStatus.STREAMING.value,)
_CHAT_TOOL_IMAGE_BUDGET_SETTING = "chat.tool_image_generation_micro"


async def acquire_completion_xact_lock(
    session: Any,
    completion_id: str,
) -> None:
    await persistence_runtime.acquire_completion_xact_lock(
        session,
        completion_id,
        logger=logger,
    )


async def ensure_completion_tool_image_wallet_budget(
    *,
    user_id: str,
    task_id: str,
    reserved_micro: int = 0,
) -> int:
    return await tool_runtime.ensure_completion_tool_image_wallet_budget(
        user_id=user_id,
        task_id=task_id,
        reserved_micro=reserved_micro,
        runtime_settings=runtime_settings,
        session_factory=SessionLocal,
        completion_model=Completion,
        worker_billing=worker_billing,
        billing_core=billing_core,
        budget_setting=_CHAT_TOOL_IMAGE_BUDGET_SETTING,
    )


def _compute_blurhash(image: PILImage.Image) -> str | None:
    return tool_runtime.compute_blurhash(
        image,
        encoder=artifact_codec.compute_blurhash,
    )


def _image_format_and_meta(raw_image: bytes) -> tuple[Any, ...]:
    return tool_runtime.image_format_and_meta(
        raw_image,
        compute_blurhash=_compute_blurhash,
        make_display=artifact_codec.make_display,
        make_preview=artifact_codec.make_preview,
        make_thumb=artifact_codec.make_thumb,
        bad_response_error_code=EC.BAD_RESPONSE.value,
    )


def build_completion_tool_image_service(
    storage_writes: StorageWriteCoordinator | None = None,
) -> CompletionToolImageService:
    return tool_runtime.build_completion_tool_image_service(
        storage_writes=storage_writes,
        dependencies=tool_runtime.ToolImageServiceDependencies(
            default_write_files=artifact_storage.write_completion_image_files,
            default_cleanup_on_error=(
                artifact_storage.cleanup_completion_image_files_on_error
            ),
            default_delete_files=artifact_storage.delete_completion_image_files,
            reserve_budget=ensure_completion_tool_image_wallet_budget,
            format_and_meta=_image_format_and_meta,
            sha256=artifact_codec.sha256,
            session_factory=SessionLocal,
            new_id=new_uuid7,
            acquire_lock=acquire_completion_xact_lock,
            completion_model=Completion,
            running_statuses=_RUNNING_COMPLETION_STATUSES,
            superseded_error_type=persistence_runtime.CompletionEpochSuperseded,
            fallback_image_tokens=(
                completion_billing.fallback_completion_tool_image_tokens
            ),
            image_model=Image,
            image_variant_model=ImageVariant,
            message_model=Message,
            public_url=storage.public_url,
            stage_outbox_event=stage_outbox_event,
            deliver_outbox_events=partial(
                deliver_staged_outbox_events,
                session_factory=SessionLocal,
                event_publisher=publish_event,
                log=logger,
            ),
            outbox_model=OutboxEvent,
            image_event=EV_COMP_IMAGE,
            bad_response_error_code=EC.BAD_RESPONSE.value,
        ),
    )
