"""Chat tool configuration and generated-image storage adapters."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

from PIL import Image as PILImage

from ....provider_runtime.errors import UpstreamError
from .. import tool_images
from ..image_storage_runtime import (
    CompletionToolImageBudget,
    CompletionToolImageCodec,
    CompletionToolImageEvents,
    CompletionToolImageRepository,
    CompletionToolImageService,
    CompletionToolImageStorage,
)


class CompletionToolInsufficientBalance(UpstreamError):
    """Wallet balance fell below the image tool budget before publishing output."""


@dataclass(frozen=True, slots=True)
class ToolImageServiceDependencies:
    default_write_files: Callable[..., Any]
    default_cleanup_on_error: Callable[..., Any]
    default_delete_files: Callable[..., Any]
    reserve_budget: Callable[..., Any]
    format_and_meta: Callable[..., Any]
    sha256: Callable[[bytes], str]
    session_factory: Callable[..., Any]
    new_id: Callable[[], str]
    acquire_lock: Callable[..., Any]
    completion_model: Any
    running_statuses: tuple[str, ...]
    superseded_error_type: type[BaseException]
    fallback_image_tokens: Callable[..., Any]
    image_model: Any
    image_variant_model: Any
    message_model: Any
    public_url: Callable[..., str]
    stage_outbox_event: Callable[..., Any]
    deliver_outbox_events: Callable[..., Any]
    outbox_model: Any
    image_event: str
    bad_response_error_code: str


async def chat_tools_from_content(
    content: dict[str, Any] | None,
    *,
    runtime_settings: Any,
    logger: Any,
    content_str_list: Callable[..., list[str]],
    split_csv_ids: Callable[[str | None], list[str]],
    vector_store_setting: str,
    web_search_type: str,
    file_search_type: str,
    code_interpreter_type: str,
    image_generation_type: str,
    image_size: str,
) -> list[dict[str, Any]]:
    content = content or {}
    tools: list[dict[str, Any]] = []
    if content.get("web_search") is True:
        tools.append({"type": web_search_type})
    if content.get("file_search") is True:
        vector_store_ids = content_str_list(content, "vector_store_ids")
        if not vector_store_ids:
            try:
                vector_store_ids = split_csv_ids(
                    await runtime_settings.resolve(vector_store_setting)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "file_search vector store setting resolve failed: %s", exc
                )
                vector_store_ids = []
        if not vector_store_ids:
            raise UpstreamError(
                "file_search requested but no vector_store_ids are configured",
                error_code="FILE_SEARCH_NOT_CONFIGURED",
                status_code=400,
            )
        tools.append({"type": file_search_type, "vector_store_ids": vector_store_ids})
    if content.get("code_interpreter") is True:
        tools.append({"type": code_interpreter_type, "container": {"type": "auto"}})
    if content.get("image_generation") is True:
        tools.append(
            {
                "type": image_generation_type,
                "model": "gpt-image-2",
                "size": image_size,
                "quality": "medium",
                "output_format": "png",
                "background": "auto",
            }
        )
    return tools


def configure_chat_tools(body: dict[str, Any], tools: list[dict[str, Any]]) -> None:
    if not tools:
        return
    body["tools"] = tools
    body["tool_choice"] = "auto"
    body["parallel_tool_calls"] = False


def tool_limited_completion_body(
    body: dict[str, Any],
    *,
    fallback_text: str,
) -> dict[str, Any]:
    fallback = dict(body)
    fallback.pop("tools", None)
    fallback["tool_choice"] = "none"
    fallback["parallel_tool_calls"] = False
    input_items = list(body.get("input") or [])
    input_items.append(
        {
            "role": "user",
            "content": [{"type": "input_text", "text": fallback_text}],
        }
    )
    fallback["input"] = input_items
    return fallback


def compute_blurhash(
    image: PILImage.Image,
    *,
    encoder: Callable[..., str | None],
) -> str | None:
    return tool_images._compute_blurhash(image, compute_blurhash=encoder)


def image_format_and_meta(
    raw_image: bytes,
    *,
    compute_blurhash: Callable[..., str | None],
    make_display: Callable[..., Any],
    make_preview: Callable[..., Any],
    make_thumb: Callable[..., Any],
    bad_response_error_code: str,
) -> tuple[Any, ...]:
    return tool_images._image_format_and_meta(
        raw_image,
        hooks=tool_images.ToolImageFormatHooks(
            compute_blurhash=compute_blurhash,
            make_display=make_display,
            make_preview=make_preview,
            make_thumb=make_thumb,
            upstream_error_type=UpstreamError,
            bad_response_error_code=bad_response_error_code,
        ),
    )


def build_completion_tool_image_service(
    *,
    storage_writes: Any | None,
    dependencies: ToolImageServiceDependencies,
) -> CompletionToolImageService:
    write_files = (
        dependencies.default_write_files
        if storage_writes is None
        else storage_writes.write_files
    )
    cleanup_on_error = (
        dependencies.default_cleanup_on_error
        if storage_writes is None
        else storage_writes.cleanup_on_error
    )
    delete_files = (
        dependencies.default_delete_files
        if storage_writes is None
        else storage_writes.delete_files
    )
    usage_hooks = tool_images.ToolImageUsageHooks(
        acquire_lock=dependencies.acquire_lock,
        completion_model=dependencies.completion_model,
        running_statuses=dependencies.running_statuses,
        superseded_error_type=dependencies.superseded_error_type,
        fallback_image_tokens=dependencies.fallback_image_tokens,
    )
    return CompletionToolImageService(
        budget=CompletionToolImageBudget(reserve=dependencies.reserve_budget),
        codec=CompletionToolImageCodec(
            decode=tool_images.decode_upstream_image_b64,
            format_and_meta=dependencies.format_and_meta,
            sha256=dependencies.sha256,
            upstream_error_type=UpstreamError,
            bad_response_error_code=dependencies.bad_response_error_code,
        ),
        repository=CompletionToolImageRepository(
            session_factory=dependencies.session_factory,
            new_id=dependencies.new_id,
            acquire_task_lock=dependencies.acquire_lock,
            completion_model=dependencies.completion_model,
            superseded_error_type=dependencies.superseded_error_type,
            record_usage=partial(
                tool_images._record_completion_tool_image_usage,
                hooks=usage_hooks,
            ),
            image_model=dependencies.image_model,
            image_variant_model=dependencies.image_variant_model,
            message_model=dependencies.message_model,
            public_url=dependencies.public_url,
        ),
        storage=CompletionToolImageStorage(
            write_files=write_files,
            cleanup_on_error=cleanup_on_error,
            delete_files=delete_files,
        ),
        events=CompletionToolImageEvents(
            stage=dependencies.stage_outbox_event,
            deliver=dependencies.deliver_outbox_events,
            outbox_model=dependencies.outbox_model,
            image_event=dependencies.image_event,
        ),
    )


async def ensure_completion_tool_image_wallet_budget(
    *,
    user_id: str,
    task_id: str,
    reserved_micro: int,
    runtime_settings: Any,
    session_factory: Callable[..., Any],
    completion_model: Any,
    worker_billing: Any,
    billing_core: Any,
    budget_setting: str,
) -> int:
    return await tool_images._ensure_completion_tool_image_wallet_budget(
        user_id=user_id,
        task_id=task_id,
        reserved_micro=reserved_micro,
        hooks=tool_images.ToolImageBudgetHooks(
            runtime_settings=runtime_settings,
            session_factory=session_factory,
            completion_model=completion_model,
            worker_billing=worker_billing,
            billing_core=billing_core,
            insufficient_balance_error_type=CompletionToolInsufficientBalance,
            budget_setting=budget_setting,
        ),
    )
