from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "STORAGE_ROOT", f"{tempfile.gettempdir()}/lumen-worker-test-storage"
)

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
