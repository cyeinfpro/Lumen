from __future__ import annotations

import json

from app.routes import events


def test_compaction_bridge_channels_reuse_valid_conversation_channels() -> None:
    mapped = events._compaction_bridge_channels(
        ["user:user-1", "conv:conv-1", "task:task-1", "conv:conv-2"]
    )

    assert mapped == {
        "lumen:events:conversation:conv-1": "conv:conv-1",
        "lumen:events:conversation:conv-2": "conv:conv-2",
    }


def test_compaction_payload_formats_as_non_persistent_sse_event() -> None:
    raw = json.dumps(
        {
            "kind": "context.compaction",
            "conversation_id": "conv-1",
            "phase": "completed",
            "ok": True,
        }
    )

    out = events._format_compaction_sse(raw, expected_conv_id="conv-1")

    assert out is not None
    assert out["event"] == "context.compaction"
    assert "id" not in out
    assert json.loads(out["data"]) == {
        "kind": "context.compaction",
        "conversation_id": "conv-1",
        "phase": "completed",
        "ok": True,
    }


def test_compaction_payload_rejects_wrong_kind_and_conversation() -> None:
    assert (
        events._format_compaction_sse(
            json.dumps({"kind": "other", "conversation_id": "conv-1"}),
            expected_conv_id="conv-1",
        )
        is None
    )
    assert (
        events._format_compaction_sse(
            json.dumps({"kind": "context.compaction", "conversation_id": "conv-2"}),
            expected_conv_id="conv-1",
        )
        is None
    )
