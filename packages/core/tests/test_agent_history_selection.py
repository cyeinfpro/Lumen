from __future__ import annotations

from dataclasses import dataclass

from lumen_core.agent_history_selection import (
    select_agent_history_tail,
    semantic_agent_message,
)


@dataclass(frozen=True)
class Row:
    id: str
    role: str
    content: dict[str, object]
    status: str | None = None


def test_nonsemantic_rows_do_not_consume_tail_budget() -> None:
    stale = [
        Row(f"failed-{index}", "assistant", {"text": ""}, "failed")
        for index in range(3_000)
    ]
    meaningful = [
        Row("user-latest", "user", {"text": "latest request"}),
        Row("assistant-latest", "assistant", {"text": "latest answer"}),
    ]
    rows = [*stale, *meaningful]
    selected = select_agent_history_tail(
        rows,
        item_id=lambda item: item.id,
        role=lambda item: item.role,
        semantic=lambda item: semantic_agent_message(
            role=item.role,
            content=item.content,
            status=item.status,
        ),
        token_estimate=lambda _item: 1,
        max_entries=2,
    )
    assert [item.id for item in selected.items] == [
        "user-latest",
        "assistant-latest",
    ]
    assert selected.truncated is False


def test_newest_complete_units_are_retained_in_original_order() -> None:
    rows = [
        Row(f"user-{index}", "user", {"text": f"request {index}"})
        if position % 2 == 0
        else Row(
            f"assistant-{index}",
            "assistant",
            {
                "text": f"answer {index}",
                "tool_calls": [{"id": f"tool-{index}"}],
            },
        )
        for index in range(1_100)
        for position in range(2)
    ]
    selected = select_agent_history_tail(
        rows,
        item_id=lambda item: item.id,
        role=lambda item: item.role,
        semantic=lambda item: True,
        token_estimate=lambda _item: 1,
        max_entries=2_048,
    )
    assert len(selected.items) == 2_048
    assert selected.items[0].id == "user-76"
    assert selected.items[-1].id == "assistant-1099"
    assert selected.removed_entries == 152
    for index in range(0, len(selected.items), 2):
        assert selected.items[index].role == "user"
        assert selected.items[index + 1].role == "assistant"


def test_oversized_newest_unit_never_falls_back_to_older_history() -> None:
    rows = [
        Row("user-old", "user", {"text": "old"}),
        Row("assistant-old", "assistant", {"text": "old answer"}),
        Row("user-new", "user", {"text": "new"}),
        Row("assistant-new-1", "assistant", {"text": "one"}),
        Row("assistant-new-2", "assistant", {"text": "two"}),
        Row("assistant-new-3", "assistant", {"text": "three"}),
    ]
    selected = select_agent_history_tail(
        rows,
        item_id=lambda item: item.id,
        role=lambda item: item.role,
        semantic=lambda item: True,
        token_estimate=lambda _item: 1,
        max_entries=3,
    )
    assert selected.items == ()
    assert selected.truncated is True
    assert selected.removed_entries == len(rows)


def test_token_budget_never_splits_a_tool_bearing_logical_turn() -> None:
    rows = [
        Row("user-old", "user", {"text": "old"}),
        Row("assistant-old", "assistant", {"text": "old answer"}),
        Row("user-new", "user", {"text": "new"}),
        Row(
            "assistant-new",
            "assistant",
            {"text": "new answer", "tool_calls": [{"id": "tool-new"}]},
        ),
    ]
    selected = select_agent_history_tail(
        rows,
        item_id=lambda item: item.id,
        role=lambda item: item.role,
        semantic=lambda item: True,
        token_estimate=lambda _item: 10,
        max_entries=10,
        max_tokens=20,
    )
    assert [item.id for item in selected.items] == ["user-new", "assistant-new"]
