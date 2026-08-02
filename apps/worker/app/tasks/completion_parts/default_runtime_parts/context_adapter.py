"""Late-bound context adapter backed by explicit dependency providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from lumen_core.byok_retention import (
    BYOK_DEFAULT_DELETE_ENABLED,
    ByokRetentionPolicy,
    applies_to_account_mode,
    cutoffs,
)
from lumen_core.constants import DEFAULT_CHAT_INSTRUCTIONS
from lumen_core.model_entities import (
    Completion,
    Conversation,
    Message,
)

from .. import context_loading, history
from ..context import PackedContext
from ..context_enrichment import (
    inject_user_memory_context,
    record_completion_context_metadata,
)
from ..history import SummaryBoundary
from . import context_runtime


@dataclass(frozen=True, slots=True)
class ContextAdapterDependencies:
    runtime_settings: Callable[[], Any]
    session_factory: Callable[[], Any]
    count_tokens: Callable[[], Callable[[str], int]]
    estimate_system_prompt_tokens: Callable[[], Callable[[str], int]]
    estimate_text_tokens: Callable[[], Callable[[str], int]]
    get_input_budget: Callable[[], Callable[..., int]]
    input_token_budget: Callable[[], int]
    context_summary: Callable[[], Any]
    memory_extraction: Callable[[], Any]
    storage_get_bytes: Callable[..., Any]
    logger: Any
    pick_first_user: Callable[..., Any]
    pick_current_user: Callable[..., Any]
    context_circuit_open: Callable[..., Any]
    compression_enabled_default: int
    compression_trigger_percent_default: int
    summary_target_tokens_default: int
    summary_min_recent_messages_default: int
    summary_min_interval_seconds_default: int
    attachment_to_data_url: Callable[[], Callable[..., Any]]
    message_to_input_item: Callable[[], Callable[..., Any]]
    count_message_tokens: Callable[[], Callable[..., int]]
    estimate_system_prompt_tokens_once: Callable[[], Callable[..., int]]
    message_retention_filter: Callable[[], Callable[..., Any]]
    resolve_summary_model: Callable[[], Callable[..., Any]]
    resolve_int_setting: Callable[[], Callable[..., Any]]
    ensure_context_summary: Callable[[], Callable[..., Any]]
    build_input_from_packed_context: Callable[[], Callable[..., Any]]
    load_rows_desc: Callable[[], Callable[..., Any]]
    load_rows_desc_after_summary: Callable[[], Callable[..., Any]]
    pick_first_user_from_summary: Callable[[], Callable[..., Any]]
    pick_current_user_with_lookup: Callable[[], Callable[..., Any]]
    context_loading_hooks: Callable[[], Callable[..., Any]]
    pack_recent_history: Callable[[], Callable[..., Any]]
    resolve_byok_retention_policy: Callable[[], Callable[..., Any]]
    get_message: Callable[[], Callable[..., Any]]


class ContextAdapter:
    def __init__(self, dependencies: ContextAdapterDependencies) -> None:
        self._deps = dependencies

    async def resolve_byok_retention_policy(self) -> ByokRetentionPolicy:
        return await context_runtime.resolve_byok_retention_policy(
            runtime_settings=self._deps.runtime_settings(),
            default_delete_enabled=BYOK_DEFAULT_DELETE_ENABLED,
        )

    async def message_retention_filter_for_account(self, account_mode: str | None):
        return await context_runtime.message_retention_filter_for_account(
            account_mode,
            applies_to_account_mode=applies_to_account_mode,
            resolve_policy=self._deps.resolve_byok_retention_policy(),
            retention_cutoffs=cutoffs,
            message_model=Message,
        )

    def count_message_tokens(
        self,
        role: str,
        content: dict[str, Any] | None,
    ) -> int:
        return context_runtime.count_message_tokens(
            role,
            content,
            token_counter=self._deps.count_tokens(),
            history_module=history,
        )

    def estimate_system_prompt_tokens_once(self, system_prompt: str | None) -> int:
        return context_runtime.estimate_system_prompt_tokens_once(
            system_prompt,
            default_instructions=DEFAULT_CHAT_INSTRUCTIONS,
            estimate_system_prompt_tokens=self._deps.estimate_system_prompt_tokens(),
            estimate_text_tokens=self._deps.estimate_text_tokens(),
        )

    async def attachment_to_data_url(
        self,
        session: Any,
        image_id: str,
    ) -> str | None:
        return await context_runtime.attachment_to_data_url(
            session,
            image_id,
            storage_get_bytes=self._deps.storage_get_bytes,
            logger=self._deps.logger,
        )

    async def message_to_input_item(
        self,
        session: Any,
        message: Message,
    ) -> dict[str, Any] | None:
        return await context_runtime.message_to_input_item(
            session,
            message,
            attachment_loader=self._deps.attachment_to_data_url(),
        )

    async def build_input_from_packed_context(
        self,
        session: Any,
        packed: PackedContext,
    ) -> list[dict[str, Any]]:
        return await context_runtime.build_input_from_packed_context(
            session,
            packed,
            message_converter=self._deps.message_to_input_item(),
        )

    async def load_rows_desc(
        self,
        session: Any,
        *,
        conversation_id: str,
        target: Message,
        budget_tokens: int | None,
        system_prompt: str | None,
        retention_filter: Any | None = None,
    ) -> tuple[list[Message], int, bool]:
        return await context_runtime.load_rows_desc(
            session,
            conversation_id=conversation_id,
            target=target,
            budget_tokens=budget_tokens,
            system_prompt=system_prompt,
            retention_filter=retention_filter,
            count_message_tokens=self._deps.count_message_tokens(),
            estimate_system_prompt_tokens=(
                self._deps.estimate_system_prompt_tokens_once()
            ),
        )

    async def load_rows_desc_after_summary(
        self,
        session: Any,
        *,
        conversation_id: str,
        target: Message,
        summary: dict[str, Any],
        retention_filter: Any | None = None,
    ) -> list[Message]:
        return await context_runtime.load_rows_desc_after_summary(
            session,
            conversation_id=conversation_id,
            target=target,
            summary=summary,
            retention_filter=retention_filter,
        )

    async def get_message(
        self,
        session: Any,
        message_id: str | None,
    ) -> Message | None:
        return await context_runtime.get_message(
            session,
            message_id,
            logger=self._deps.logger,
        )

    async def pick_first_user_from_summary(
        self,
        session: Any,
        summary: dict[str, Any],
    ) -> Message | None:
        return await context_runtime.pick_first_user_from_summary(
            session,
            summary,
            get_message=self._deps.get_message(),
        )

    async def pick_current_user_with_lookup(
        self,
        session: Any,
        rows_desc: list[Message],
        target: Message,
        summary: dict[str, Any] | None = None,
    ) -> Message | None:
        return await context_runtime.pick_current_user_with_lookup(
            session,
            rows_desc,
            target,
            summary,
            get_message=self._deps.get_message(),
        )

    async def resolve_summary_model(self) -> str:
        return await context_runtime.resolve_summary_model(
            runtime_settings=self._deps.runtime_settings(),
            logger=self._deps.logger,
        )

    async def resolve_int_setting(self, spec_key: str, default: int) -> int:
        return await context_runtime.resolve_int_setting(
            spec_key,
            default,
            runtime_settings=self._deps.runtime_settings(),
            logger=self._deps.logger,
        )

    async def ensure_context_summary(
        self,
        session: Any,
        conversation: Conversation,
        boundary: SummaryBoundary,
        *,
        target_tokens: int,
        model: str,
        redis: Any | None,
    ) -> dict[str, Any] | None:
        return await context_runtime.ensure_context_summary(
            session,
            conversation,
            boundary,
            target_tokens=target_tokens,
            model=model,
            redis=redis,
            service=self._deps.context_summary(),
            logger=self._deps.logger,
        )

    def context_loading_hooks(self) -> context_loading.ContextLoadingHooks:
        return context_runtime.build_context_loading_hooks(
            count_message_tokens=self._deps.count_message_tokens(),
            count_tokens=self._deps.count_tokens(),
            estimate_system_prompt_tokens=(
                self._deps.estimate_system_prompt_tokens_once()
            ),
            get_input_budget=self._deps.get_input_budget(),
            message_retention_filter_for_account=self._deps.message_retention_filter(),
            resolve_summary_model=self._deps.resolve_summary_model(),
            resolve_int_setting=self._deps.resolve_int_setting(),
            ensure_context_summary=self._deps.ensure_context_summary(),
            build_input_from_packed_context=(
                self._deps.build_input_from_packed_context()
            ),
            load_rows_desc=self._deps.load_rows_desc(),
            load_rows_desc_after_summary=self._deps.load_rows_desc_after_summary(),
            pick_first_user_from_summary=self._deps.pick_first_user_from_summary(),
            pick_current_user_with_lookup=(self._deps.pick_current_user_with_lookup()),
            pick_first_user=self._deps.pick_first_user,
            pick_current_user=self._deps.pick_current_user,
            context_circuit_open=self._deps.context_circuit_open,
            input_token_budget=self._deps.input_token_budget(),
            compression_enabled_default=self._deps.compression_enabled_default,
            compression_trigger_percent_default=(
                self._deps.compression_trigger_percent_default
            ),
            summary_target_tokens_default=self._deps.summary_target_tokens_default,
            summary_min_recent_messages_default=(
                self._deps.summary_min_recent_messages_default
            ),
            summary_min_interval_seconds_default=(
                self._deps.summary_min_interval_seconds_default
            ),
            logger=self._deps.logger,
        )

    async def pack_recent_history(
        self,
        session: Any,
        *,
        conversation_id: str,
        up_to_message_id: str,
        system_prompt: str | None,
        redis: Any | None = None,
        chat_model: str | None = None,
        account_mode: str | None = None,
    ) -> PackedContext:
        return await context_runtime.pack_recent_history(
            session,
            conversation_id=conversation_id,
            up_to_message_id=up_to_message_id,
            system_prompt=system_prompt,
            redis=redis,
            chat_model=chat_model,
            account_mode=account_mode,
            hooks=self._deps.context_loading_hooks()(),
        )

    async def build_input_from_history(
        self,
        session: Any,
        *,
        conversation_id: str,
        up_to_message_id: str,
        system_prompt: str | None,
    ) -> list[dict[str, Any]]:
        return await context_runtime.build_input_from_history(
            session,
            conversation_id=conversation_id,
            up_to_message_id=up_to_message_id,
            system_prompt=system_prompt,
            pack_recent_history=self._deps.pack_recent_history(),
        )

    async def inject_user_memory_context(
        self,
        _session: Any,
        *,
        input_list: list[dict[str, Any]],
        user_id: str,
        conversation_id: str | None,
        parent_user_message_id: str | None,
        redis: Any | None = None,
    ) -> dict[str, Any]:
        async with self._deps.session_factory() as memory_session:
            return await context_runtime.inject_memory_context(
                memory_session,
                input_list=input_list,
                user_id=user_id,
                conversation_id=conversation_id,
                parent_user_message_id=parent_user_message_id,
                redis=redis,
                injector=inject_user_memory_context,
                memory_extraction=self._deps.memory_extraction(),
                message_model=Message,
            )

    async def record_completion_context_metadata(
        self,
        session: Any,
        *,
        task_id: str,
        attempt_epoch: int,
        packed: PackedContext,
    ) -> None:
        await context_runtime.record_context_metadata(
            session,
            task_id=task_id,
            attempt_epoch=attempt_epoch,
            packed=packed,
            recorder=record_completion_context_metadata,
            completion_model=Completion,
        )
