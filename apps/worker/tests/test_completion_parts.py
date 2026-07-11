from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app import image_artifacts
from app.tasks import completion
from app.tasks.completion_parts import (
    citation_text,
    context,
    context_loading,
    history,
    stream,
    tool_images,
    tool_state,
)


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
        is image_artifacts._decode_upstream_image_b64
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
    assert tuple(
        inspect.signature(
            completion._store_and_publish_completion_tool_image
        ).parameters
    ) == (
        "redis",
        "user_id",
        "channel",
        "task_id",
        "message_id",
        "attempt",
        "attempt_epoch",
        "b64_image",
        "revised_prompt",
        "reserved_tool_image_micro",
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
                assert all(
                    name.name != "app.tasks.completion" for name in node.names
                )


def test_completion_facade_stays_strictly_below_3000_lines() -> None:
    source = Path(completion.__file__).read_text(encoding="utf-8")

    assert len(source.splitlines()) < 3000
