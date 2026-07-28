from __future__ import annotations

import asyncio
import ast
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest

from app.provider_runtime.upstream_services import ImageUpstreamRuntime
from app.tasks.completion_parts import default_runtime as completion
from app.tasks.completion_parts import (
    artifact_codec,
    citation_text,
    context,
    context_loading,
    history,
    stream,
    tool_images,
    tool_state,
)
from app.tasks.completion_parts.image_storage_runtime import (
    CompletionToolImageService,
)
from app.tasks.completion_parts.services import (
    CompletionToolService,
    CompletionUpstreamService,
)
from app.tasks.completion_parts.execution import (
    CompletionExecution,
    PreparationState,
    SettlementState,
    StreamingState,
    UsageState,
)
from app.tasks.completion_parts.contracts import (
    CompletionCommand,
    CompletionOutcome,
    CompletionPhase,
    CompletionResult,
    CompletionServices,
)


def _fake_image_upstream_runtime() -> ImageUpstreamRuntime:
    return ImageUpstreamRuntime(services=object())  # type: ignore[arg-type]


def test_completion_v2_contracts_are_typed_and_bounded() -> None:
    command = CompletionCommand.from_arq(
        {"redis": _fake_image_upstream_runtime(), "worker_id": "worker-1"},
        "completion-1",
    )
    result = CompletionResult(
        task_id=command.task_id,
        phase=CompletionPhase.COMPLETE,
        outcome=CompletionOutcome.SUCCEEDED,
        attempt=1,
    )

    assert command.worker_id == "worker-1"
    assert result.outcome is CompletionOutcome.SUCCEEDED
    assert len(CompletionServices.__dataclass_fields__) == 7
    for state_type in (
        CompletionExecution,
        PreparationState,
        StreamingState,
        UsageState,
        SettlementState,
    ):
        assert len(state_type.__dataclass_fields__) <= 15


def test_completion_public_runtime_has_no_dynamic_symbol_table() -> None:
    runtime_path = Path(completion.__file__).with_name("runtime.py")
    public_paths = (
        runtime_path.with_name("contracts.py"),
        runtime_path,
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in public_paths)

    for forbidden in (
        "RuntimeSlot",
        "typing import Any",
        "sqlalchemy",
        "SessionLocal",
        "fastapi",
    ):
        assert forbidden not in source


def test_completion_facade_preserves_tool_state_identity() -> None:
    identity_names = (
        "_CODE_INTERPRETER_TOOL_TYPE",
        "_CompletionToolTracker",
        "_FILE_SEARCH_TOOL_TYPE",
        "_IMAGE_GENERATION_TOOL_TYPE",
        "_ToolCallState",
        "_WEB_SEARCH_TOOL_TYPE",
        "_extract_tool_call_update",
        "_first_str",
        "_merge_tool_call_state",
        "_normalize_tool_status",
        "_normalize_tool_type",
        "_summarize_tool_error",
        "_tool_display_label",
        "_tool_status_rank",
    )

    for name in identity_names:
        assert getattr(completion, name) is getattr(tool_state, name)


def test_completion_facade_preserves_citation_text_identity() -> None:
    identity_names = (
        "_apply_url_citations",
        "_extract_completed_output_text",
        "_extract_url_citations",
        "_finalize_completion_text",
        "_markdown_link",
    )

    for name in identity_names:
        assert getattr(completion, name) is getattr(citation_text, name)


def test_completion_facade_preserves_history_identity_and_signatures() -> None:
    identity_names = (
        "_STICKY_TEXT_CHAR_LIMIT",
        "_SummaryBoundary",
        "_instructions_with_summary_guardrail",
        "_message_after_summary",
        "_message_created_at",
        "_role_eq",
        "_sticky_text_from_message",
        "_summary_age_seconds",
        "_summary_compressed_at",
        "_summary_covers_boundary",
        "_summary_created_at",
        "_truncate_sticky_text",
        "_with_summary_guardrail",
    )

    for name in identity_names:
        assert getattr(completion, name) is getattr(history, name)

    assert inspect.signature(completion._count_message_tokens) == inspect.signature(
        history._count_message_tokens
    )


def test_completion_token_count_facade_uses_late_bound_counter(
    monkeypatch,
) -> None:
    monkeypatch.setattr(completion, "count_tokens", lambda text: len(text))

    assert (
        completion._count_message_tokens(
            "user",
            {"text": "abcd"},
        )
        == history.MESSAGE_OVERHEAD_TOKENS + 4
    )


def test_completion_facade_preserves_context_packing_identity() -> None:
    identity_names = (
        "PackedContext",
        "_estimated_summary_source",
        "_fallback_pack",
        "_make_quality_probes",
        "_pack_with_existing_summary",
        "_packed_with_input",
    )

    for name in identity_names:
        assert getattr(completion, name) is getattr(context, name)


def test_completion_facade_preserves_new_leaf_symbol_identity() -> None:
    context_identity_names = (
        "_context_circuit_open",
        "_pick_current_user",
        "_pick_first_user",
    )
    stream_identity_names = (
        "_LeaseLost",
        "_TaskCancelled",
        "_ToolIdleTimeout",
        "_extract_reasoning_delta",
        "_extract_reasoning_text_from_item",
        "_extract_reasoning_text_from_response",
        "_next_completion_stream_event",
        "_raise_for_terminal_response_event",
    )
    tool_image_identity_names = (
        "_decode_upstream_image_b64",
        "_extract_image_events_from_response",
        "_tool_image_dedupe_key",
    )

    for name in context_identity_names:
        assert getattr(completion, name) is getattr(context_loading, name)
    for name in stream_identity_names:
        assert getattr(completion, name) is getattr(stream, name)
    for name in tool_image_identity_names:
        assert getattr(completion, name) is getattr(tool_images, name)

    assert (
        completion._decode_upstream_image_b64
        is artifact_codec.decode_upstream_image_b64
    )


def test_reasoning_item_extraction_preserves_chunk_order_and_precedence() -> None:
    item = {
        "summary_text": "top summary",
        "text": "top text",
        "summary": [
            "plain summary",
            {"summary_text": "fallback summary"},
            {"text": "preferred text", "summary_text": "ignored summary"},
            {"text": ""},
            42,
        ],
        "content": [
            {"text": "content text"},
            {"text": ""},
            "ignored content",
        ],
    }

    assert stream._extract_reasoning_text_from_item(item) == "\n".join(
        [
            "top summary",
            "top text",
            "plain summary",
            "fallback summary",
            "preferred text",
            "content text",
        ]
    )


def test_completion_facade_preserves_extracted_wrapper_signatures() -> None:
    assert tuple(inspect.signature(completion._pack_recent_history).parameters) == (
        "session",
        "conversation_id",
        "up_to_message_id",
        "system_prompt",
        "redis",
        "chat_model",
        "account_mode",
    )
    runtime = completion.build_completion_runtime(
        image_upstream_runtime=_fake_image_upstream_runtime(),
    )
    tools = cast(CompletionToolService, runtime.services.tool_executor)
    assert isinstance(
        tools.tool_image_service,
        CompletionToolImageService,
    )
    assert not hasattr(
        tools,
        "_store_and_publish_completion_tool_image",
    )
    assert tuple(
        inspect.signature(completion._iter_completion_stream_with_abort).parameters
    ) == (
        "stream",
        "cancel_requested",
        "lease_lost",
        "tool_tracker",
        "tool_idle_timeout_s",
    )


def test_completion_runtime_binds_injected_tool_image_storage() -> None:
    class StorageWrites:
        async def write_files(
            self,
            _files: list[tuple[str, bytes]],
        ) -> list[str]:
            return []

        @asynccontextmanager
        async def cleanup_on_error(self, _keys: list[str]):
            yield

    storage_writes = StorageWrites()
    runtime = completion.build_completion_runtime(
        image_upstream_runtime=_fake_image_upstream_runtime(),
        storage_writes=storage_writes,  # type: ignore[arg-type]
    )
    tools = cast(CompletionToolService, runtime.services.tool_executor)
    service = tools.tool_image_service

    assert service.storage.write_files.__self__ is storage_writes
    assert service.storage.cleanup_on_error.__self__ is storage_writes


def test_completion_runtime_binds_explicit_image_upstream_runtime() -> None:
    image_upstream_runtime = _fake_image_upstream_runtime()

    runtime = completion.build_completion_runtime(
        image_upstream_runtime=image_upstream_runtime,
    )
    upstream = cast(CompletionUpstreamService, runtime.services.upstream_client)
    bound_stream = upstream.stream_completion

    assert runtime.image_upstream_runtime is image_upstream_runtime
    assert bound_stream.func is completion.stream_completion  # type: ignore[attr-defined]
    assert bound_stream.keywords == {  # type: ignore[attr-defined]
        "runtime": image_upstream_runtime,
    }


@pytest.mark.asyncio
async def test_stream_facade_uses_late_bound_cancel_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, str]] = []

    async def cancelled(redis: object, task_id: str) -> bool:
        calls.append((redis, task_id))
        return True

    redis = object()
    cancel_requested = completion.asyncio.Event()
    stop_requested = completion.asyncio.Event()
    monkeypatch.setattr(completion, "_is_cancelled", cancelled)

    await completion._watch_completion_cancel(
        redis,
        "comp-late-bound",
        cancel_requested=cancel_requested,
        stop_requested=stop_requested,
        poll_interval_s=0.01,
    )

    assert calls == [(redis, "comp-late-bound")]
    assert cancel_requested.is_set()


@pytest.mark.asyncio
async def test_stream_interruption_on_lease_loss_closes_upstream() -> None:
    class BlockingStream:
        closed = False

        async def __anext__(self) -> dict[str, object]:
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    upstream = BlockingStream()
    lease_lost = asyncio.Event()
    lease_lost.set()

    with pytest.raises(stream.LeaseLost, match="lease lost during stream"):
        await stream.next_completion_stream_event(
            upstream,
            cancel_requested=asyncio.Event(),
            lease_lost=lease_lost,
        )

    assert upstream.closed is True


@pytest.mark.asyncio
async def test_active_tool_idle_timeout_closes_upstream() -> None:
    class BlockingStream:
        closed = False

        async def __anext__(self) -> dict[str, object]:
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    upstream = BlockingStream()

    with pytest.raises(stream.ToolIdleTimeout, match="tool call idle timeout"):
        await stream.next_completion_stream_event(
            upstream,
            cancel_requested=asyncio.Event(),
            lease_lost=asyncio.Event(),
            idle_timeout_s=0.001,
        )

    assert upstream.closed is True


def test_completion_leaf_modules_do_not_reverse_import_facade() -> None:
    for module in (context_loading, stream, tool_images):
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"completion", "app.tasks.completion"}
                assert not (node.module or "").endswith(".completion")
                assert all(name.name != "completion" for name in node.names)
            elif isinstance(node, ast.Import):
                assert all(name.name != "app.tasks.completion" for name in node.names)


def test_completion_facade_stays_strictly_below_3000_lines() -> None:
    source = Path(completion.__file__).read_text(encoding="utf-8")

    assert len(source.splitlines()) < 3000
