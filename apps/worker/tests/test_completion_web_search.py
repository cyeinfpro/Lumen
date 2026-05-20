from __future__ import annotations

import os
import tempfile
from typing import Any

os.environ.setdefault(
    "STORAGE_ROOT", f"{tempfile.gettempdir()}/lumen-worker-test-storage"
)

import pytest

from app.tasks import completion


def test_configure_web_search_tool_adds_responses_tool_fields() -> None:
    body = {
        "model": "gpt-5.5",
        "input": [],
        "instructions": "you are helpful",
        "stream": True,
        "store": True,
    }

    completion._configure_chat_tools(body, [{"type": "web_search"}])

    assert body["tools"] == [{"type": "web_search"}]
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is False


@pytest.mark.asyncio
async def test_file_search_without_vector_store_raises_config_error(
    monkeypatch: Any,
) -> None:
    async def empty_setting(_key: str) -> str:
        return ""

    monkeypatch.setattr(completion.runtime_settings, "resolve", empty_setting)

    with pytest.raises(completion.UpstreamError) as exc_info:
        await completion._chat_tools_from_content({"file_search": True})

    assert exc_info.value.error_code == "FILE_SEARCH_NOT_CONFIGURED"


def test_extract_reasoning_delta_accepts_multiple_event_shapes() -> None:
    assert (
        completion._extract_reasoning_delta(
            {"type": "response.reasoning_summary_text.delta", "delta": "a"}
        )
        == "a"
    )
    assert (
        completion._extract_reasoning_delta(
            {"type": "response.reasoning_text.delta", "delta": "b"}
        )
        == "b"
    )
    assert (
        completion._extract_reasoning_delta(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "summary": [{"text": "c"}],
                },
            }
        )
        == "c"
    )


def test_extract_image_events_from_completed_response() -> None:
    response = {
        "output": [
            {"type": "message", "content": []},
            {"type": "image_generation_call", "result": "abc"},
        ]
    }

    events = completion._extract_image_events_from_response(response)

    assert events == [
        {
            "type": "response.output_item.done",
            "item": {"type": "image_generation_call", "result": "abc"},
        }
    ]


def test_reasoning_effort_normalizes_minimal_to_none_for_upstream() -> None:
    assert (
        completion._normalize_reasoning_effort_for_upstream("minimal")
        == "none"
    )
    assert (
        completion._normalize_reasoning_effort_for_upstream("none")
        == "none"
    )
    assert (
        completion._normalize_reasoning_effort_for_upstream("high")
        == "high"
    )


def test_completion_upstream_metadata_records_provider_for_request_events() -> None:
    upstream_request = {"web_search": True, "service_tier": "priority"}
    merged = completion._merge_completion_upstream_metadata(
        upstream_request,
        provider_event={
            "provider": "pool-a",
            "route": "responses",
            "endpoint": "responses",
            "source": "text",
        },
        fast_mode=False,
    )

    assert merged["provider"] == "pool-a"
    assert merged["actual_provider"] == "pool-a"
    assert merged["request_event_provider"] == "pool-a"
    assert merged["upstream_route"] == "responses"
    assert merged["actual_route"] == "responses"
    assert merged["actual_endpoint"] == "responses"
    assert merged["actual_source"] == "text"
    assert "service_tier" not in merged


def test_completion_upstream_metadata_preserves_priority_when_fast() -> None:
    merged = completion._merge_completion_upstream_metadata(
        {},
        provider_event=None,
        fast_mode=True,
    )

    assert merged["upstream_route"] == "responses"
    assert merged["actual_endpoint"] == "responses"
    assert merged["service_tier"] == "priority"


def test_finalize_completion_text_turns_url_annotations_into_markdown_links() -> None:
    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "OpenAI released a new feature.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "start_index": 0,
                                "end_index": 6,
                                "url": "https://openai.com/index/example",
                                "title": "OpenAI",
                            }
                        ],
                    }
                ],
            }
        ]
    }

    text = completion._finalize_completion_text("", response)

    assert text == "[OpenAI](https://openai.com/index/example) released a new feature."


def test_finalize_completion_text_appends_sources_when_annotation_has_no_span() -> None:
    response = {
        "output_text": "The answer used web search.",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "The answer used web search.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/source",
                                "title": "Example Source",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    text = completion._finalize_completion_text("", response)

    assert "The answer used web search." in text
    assert "来源" in text
    assert "[Example Source](https://example.com/source)" in text


def test_tool_tracker_normalizes_tool_specific_events_and_dedupes() -> None:
    tracker = completion._CompletionToolTracker()

    first = tracker.update(
        {
            "type": "response.web_search_call.searching",
            "id": "ws-1",
            "query": "latest OpenAI docs",
        }
    )
    duplicate = tracker.update(
        {
            "type": "response.web_search_call.searching",
            "id": "ws-1",
            "query": "latest OpenAI docs",
        }
    )

    assert first == {
        "id": "ws-1",
        "type": "web_search",
        "status": "running",
        "label": "联网搜索",
        "title": "latest OpenAI docs",
    }
    assert duplicate is None
    assert tracker.content() == [first]


def test_tool_tracker_backfills_from_completed_response_output() -> None:
    tracker = completion._CompletionToolTracker()
    response = {
        "output": [
            {"type": "message", "content": []},
            {"id": "file-1", "type": "file_search_call", "status": "completed"},
        ]
    }

    published = tracker.update_from_response(response)

    assert published == [
        {
            "id": "file-1",
            "type": "file_search",
            "status": "succeeded",
            "label": "检索文件",
        }
    ]
    assert tracker.content() == published


def test_tool_tracker_keeps_failed_terminal_state() -> None:
    tracker = completion._CompletionToolTracker()

    failed = tracker.update(
        {
            "type": "response.output_item.done",
            "item": {
                "id": "code-1",
                "type": "code_interpreter_call",
                "status": "failed",
                "error": {"code": "tool_error", "message": "runtime crashed"},
            },
        }
    )
    later_done = tracker.update(
        {
            "type": "response.output_item.done",
            "item": {
                "id": "code-1",
                "type": "code_interpreter_call",
                "status": "completed",
            },
        }
    )

    assert failed == {
        "id": "code-1",
        "type": "code_interpreter",
        "status": "failed",
        "label": "运行代码",
        "error": "tool_error: runtime crashed",
    }
    assert later_done is None
    assert tracker.content() == [failed]


def test_tool_tracker_finalizes_active_calls_on_cancel() -> None:
    tracker = completion._CompletionToolTracker()
    tracker.update(
        {
            "type": "response.web_search_call.searching",
            "id": "ws-1",
            "query": "latest docs",
        }
    )

    published = tracker.finalize_active(completion.ToolStatus.CANCELLED.value)

    assert published == [
        {
            "id": "ws-1",
            "type": "web_search",
            "status": "cancelled",
            "label": "联网搜索",
            "title": "latest docs",
        }
    ]
    assert tracker.content() == published
    assert tracker.finalize_active(completion.ToolStatus.FAILED.value) == []


def test_tool_tracker_unknown_status_is_observable_not_running() -> None:
    tracker = completion._CompletionToolTracker()

    payload = tracker.update(
        {
            "type": "response.output_item.added",
            "item": {
                "id": "tool-1",
                "type": "file_search_call",
                "status": "provider_specific_wait",
            },
        }
    )

    assert payload == {
        "id": "tool-1",
        "type": "file_search",
        "status": "unknown",
        "label": "检索文件",
    }
